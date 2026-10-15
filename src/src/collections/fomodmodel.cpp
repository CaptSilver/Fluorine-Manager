#include "fomodmodel.h"

#include <QRegularExpression>
#include <QStringConverter>
#include <QXmlStreamReader>

#include <iterator>
#include <map>

namespace {
// ModuleConfig.xml is read into this small tree first. Tag and attribute names
// are lower-cased and stripped of any namespace prefix on the way in, so the
// builders below never see the mixed case installers are written in.
struct Node {
  QString tag;
  std::map<QString, QString> attrs;
  QString text; // text before the first child, as the XML text of a leaf like
                // <flag>
  std::vector<Node> children;

  bool hasAttr(const QString &name) const { return attrs.count(name) > 0; }

  QString attr(const QString &name) const {
    const auto it = attrs.find(name);
    return it == attrs.end() ? QString() : it->second;
  }

  const Node *first(const QString &name) const {
    for (const Node &child : children) {
      if (child.tag == name)
        return &child;
    }
    return nullptr;
  }

  std::vector<const Node *> kids(const QString &name) const {
    std::vector<const Node *> result;
    for (const Node &child : children) {
      if (child.tag == name)
        result.push_back(&child);
    }
    return result;
  }

  // The children named path.back(), below the chain of single containers
  // path[:-1]; empty when a container is absent.
  std::vector<const Node *>
  nested(std::initializer_list<const char *> path) const {
    const Node *current = this;
    for (auto it = path.begin(); std::next(it) != path.end(); ++it) {
      current = current->first(QString::fromLatin1(*it));
      if (!current)
        return {};
    }
    return current->kids(QString::fromLatin1(*(path.end() - 1)));
  }
};

QString localLower(QStringView qualified) {
  const qsizetype colon = qualified.lastIndexOf(u':');
  return (colon < 0 ? qualified : qualified.mid(colon + 1))
      .toString()
      .toLower();
}

bool readTree(const QString &text, Node &root, QString &error) {
  QXmlStreamReader xml(text);
  // Installers use prefixes without declaring them; localLower drops the
  // prefix.
  xml.setNamespaceProcessing(false);

  std::vector<Node *> open;
  bool haveRoot = false;
  while (!xml.atEnd()) {
    switch (xml.readNext()) {
    case QXmlStreamReader::StartElement: {
      Node *node = nullptr;
      if (open.empty()) {
        if (haveRoot)
          break;
        node = &root;
        haveRoot = true;
      } else {
        open.back()->children.emplace_back();
        node = &open.back()->children.back();
      }
      node->tag = localLower(xml.qualifiedName());
      for (const QXmlStreamAttribute &attribute : xml.attributes())
        node->attrs[localLower(attribute.qualifiedName())] =
            attribute.value().toString();
      open.push_back(node);
      break;
    }
    case QXmlStreamReader::EndElement:
      if (!open.empty())
        open.pop_back();
      break;
    case QXmlStreamReader::Characters:
      if (!open.empty() && open.back()->children.empty())
        open.back()->text += xml.text().toString();
      break;
    default:
      break;
    }
  }

  if (xml.hasError()) {
    error = QStringLiteral("cannot parse ModuleConfig.xml: %1 (line %2)")
                .arg(xml.errorString())
                .arg(xml.lineNumber());
    return false;
  }
  if (!haveRoot) {
    error =
        QStringLiteral("cannot parse ModuleConfig.xml: it has no root element");
    return false;
  }
  return true;
}

bool parsePriority(const Node &element, int &priority, QString &error) {
  const QString text = element.attr(QStringLiteral("priority")).trimmed();
  priority = 0;
  if (text.isEmpty())
    return true;
  bool ok = false;
  priority = text.toInt(&ok);
  if (!ok) {
    error = QStringLiteral("bad FOMOD priority '%1'").arg(text);
    return false;
  }
  return true;
}

bool parseFiles(const Node *filesElement, std::vector<FomodFile> &out,
                QString &error) {
  if (!filesElement)
    return true;
  for (const Node &child : filesElement->children) {
    const bool isFolder = child.tag == QLatin1String("folder");
    if (!isFolder && child.tag != QLatin1String("file"))
      continue;
    FomodFile file;
    file.source = child.attr(QStringLiteral("source"));
    file.hasDestination = child.hasAttr(QStringLiteral("destination"));
    file.destination = child.attr(QStringLiteral("destination"));
    file.isFolder = isFolder;
    if (!parsePriority(child, file.priority, error))
      return false;
    out.push_back(file);
  }
  return true;
}

FomodCondition parseCondition(const Node &dependencies) {
  FomodCondition condition;
  condition.kind =
      dependencies.attr(QStringLiteral("operator"))
                  .compare(QLatin1String("or"), Qt::CaseInsensitive) == 0
          ? FomodCondition::Kind::Or
          : FomodCondition::Kind::And;

  for (const Node &child : dependencies.children) {
    if (child.tag == QLatin1String("flagdependency")) {
      FomodCondition leaf;
      leaf.kind = FomodCondition::Kind::Flag;
      leaf.name = child.attr(QStringLiteral("flag"));
      leaf.value = child.attr(QStringLiteral("value"));
      condition.children.push_back(leaf);
    } else if (child.tag == QLatin1String("dependencies") ||
               child.tag == QLatin1String("dependency")) {
      condition.children.push_back(parseCondition(child));
    } else if (child.tag == QLatin1String("filedependency")) {
      FomodCondition leaf;
      leaf.kind = FomodCondition::Kind::File;
      leaf.name = child.attr(QStringLiteral("file"));
      leaf.value = child.attr(QStringLiteral("state"));
      condition.children.push_back(leaf);
    } else if (child.tag == QLatin1String("gamedependency")) {
      FomodCondition leaf;
      leaf.kind = FomodCondition::Kind::Game;
      leaf.value = child.attr(QStringLiteral("version"));
      condition.children.push_back(leaf);
    } else if (child.tag == QLatin1String("fommdependency")) {
      FomodCondition leaf;
      leaf.kind = FomodCondition::Kind::Fomm;
      leaf.value = child.attr(QStringLiteral("version"));
      condition.children.push_back(leaf);
    }
  }
  return condition;
}

QString typeDescriptorOf(const Node &plugin) {
  const Node *descriptor = plugin.first(QStringLiteral("typedescriptor"));
  if (!descriptor)
    return {};
  if (const Node *type = descriptor->first(QStringLiteral("type")))
    return type->attr(QStringLiteral("name"));
  if (const Node *dependencyType =
          descriptor->first(QStringLiteral("dependencytype"))) {
    if (const Node *defaultType =
            dependencyType->first(QStringLiteral("defaulttype")))
      return defaultType->attr(QStringLiteral("name"));
  }
  return {};
}

bool parsePlugin(const Node &element, int documentIndex, FomodPlugin &plugin,
                 QString &error) {
  plugin.name = element.attr(QStringLiteral("name"));
  plugin.documentIndex = documentIndex;
  plugin.typeDescriptor = typeDescriptorOf(element);
  if (!parseFiles(element.first(QStringLiteral("files")), plugin.files, error))
    return false;
  if (const Node *flags = element.first(QStringLiteral("conditionflags"))) {
    for (const Node *flag : flags->kids(QStringLiteral("flag"))) {
      // The text is kept as written: replay compares flag values exactly.
      plugin.flags.push_back({flag->attr(QStringLiteral("name")), flag->text});
    }
  }
  return true;
}

// Bytes that are neither UTF-8 nor carry a BOM: Windows-1252 when Qt can decode
// it, else Latin-1 (which agrees except in 0x80-0x9F).
QString decodeLegacy(const QByteArray &bytes) {
  QStringDecoder cp1252("windows-1252");
  if (cp1252.isValid()) {
    const QString text = cp1252(bytes);
    if (!cp1252.hasError())
      return text;
  }
  return QString::fromLatin1(bytes);
}
} // namespace

QString decodeFomodXml(const QByteArray &bytes) {
  QString text;
  const bool utf16le = bytes.startsWith(QByteArrayView("\xff\xfe", 2));
  const bool utf16be = bytes.startsWith(QByteArrayView("\xfe\xff", 2));
  if (utf16le || utf16be) {
    QStringDecoder decoder(utf16le ? QStringConverter::Utf16LE
                                   : QStringConverter::Utf16BE);
    text = decoder(QByteArrayView(bytes).sliced(2));
  } else if (bytes.startsWith(QByteArrayView("\xef\xbb\xbf", 3))) {
    text = QString::fromUtf8(bytes.constData() + 3, bytes.size() - 3);
  } else {
    QStringDecoder utf8(QStringConverter::Utf8,
                        QStringConverter::Flag::Stateless);
    text = utf8(bytes);
    if (utf8.hasError())
      text = decodeLegacy(bytes);
  }

  while (text.startsWith(QChar(0xFEFF)))
    text.remove(0, 1);

  // Installers declare an encoding that often disagrees with the bytes; the
  // text is decoded already, so the declaration can only get in the parser's
  // way.
  static const QRegularExpression declaration(
      QStringLiteral(R"(^\s*<\?xml[^>]*\?>)"));
  text.remove(declaration);
  return text;
}

std::optional<FomodModel> FomodModel::parse(const QByteArray &xml,
                                            QString &error) {
  Node root;
  if (!readTree(decodeFomodXml(xml), root, error))
    return std::nullopt;

  FomodModel model;
  if (!parseFiles(root.first(QStringLiteral("requiredinstallfiles")),
                  model.requiredInstallFiles, error))
    return std::nullopt;

  for (const Node *stepElement : root.nested({"installsteps", "installstep"})) {
    FomodStep step;
    step.name = stepElement->attr(QStringLiteral("name"));
    for (const Node *groupElement :
         stepElement->nested({"optionalfilegroups", "group"})) {
      FomodGroup group;
      group.name = groupElement->attr(QStringLiteral("name"));
      group.type = groupElement->attr(QStringLiteral("type"));
      int index = 0;
      for (const Node *pluginElement :
           groupElement->nested({"plugins", "plugin"})) {
        FomodPlugin plugin;
        if (!parsePlugin(*pluginElement, index++, plugin, error))
          return std::nullopt;
        group.plugins.push_back(std::move(plugin));
      }
      step.groups.push_back(std::move(group));
    }
    model.steps.push_back(std::move(step));
  }

  for (const Node *patternElement :
       root.nested({"conditionalfileinstalls", "patterns", "pattern"})) {
    FomodPattern pattern;
    if (const Node *dependencies =
            patternElement->first(QStringLiteral("dependencies")))
      pattern.condition = parseCondition(*dependencies);
    if (!parseFiles(patternElement->first(QStringLiteral("files")),
                    pattern.files, error))
      return std::nullopt;
    model.conditionalFileInstalls.push_back(std::move(pattern));
  }

  return model;
}

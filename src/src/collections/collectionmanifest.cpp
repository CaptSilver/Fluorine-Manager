#include "collectionmanifest.h"

#include <QJsonArray>
#include <QJsonDocument>

namespace
{
int intValue(const QJsonValue& value)
{
  return static_cast<int>(value.toInteger(0));
}

bool sectionHasContent(const QJsonValue& value)
{
  switch (value.type()) {
  case QJsonValue::Array:
    return !value.toArray().isEmpty();
  case QJsonValue::Object:
    return !value.toObject().isEmpty();
  case QJsonValue::String:
    return !value.toString().isEmpty();
  case QJsonValue::Bool:
    return value.toBool();
  case QJsonValue::Double:
    return true;
  default:
    return false;
  }
}

// Same refusals as the command-line installer this was ported from: a source
// that is not Nexus, or choices that are not FOMOD choices.
QString unsupportedReason(const CollectionMod& mod)
{
  if (mod.source.type != QStringLiteral("nexus")) {
    return QStringLiteral("source type '%1' is not nexus").arg(mod.source.type);
  }
  if (mod.source.modId <= 0 || mod.source.fileId <= 0) {
    return QStringLiteral("source has no modId or fileId");
  }
  if (!mod.choicesType.isEmpty() && mod.choicesType != QStringLiteral("fomod")) {
    return QStringLiteral("installer choices of type '%1' are not replayed here")
        .arg(mod.choicesType);
  }
  return {};
}

CollectionMod parseMod(const QJsonObject& entry)
{
  CollectionMod mod;
  mod.name     = entry.value(QStringLiteral("name")).toString();
  mod.version  = entry.value(QStringLiteral("version")).toString();
  mod.optional = entry.value(QStringLiteral("optional")).toBool(false);
  mod.phase    = intValue(entry.value(QStringLiteral("phase")));

  const QJsonObject source   = entry.value(QStringLiteral("source")).toObject();
  mod.source.type            = source.value(QStringLiteral("type")).toString();
  mod.source.modId           = intValue(source.value(QStringLiteral("modId")));
  mod.source.fileId          = intValue(source.value(QStringLiteral("fileId")));
  mod.source.md5             = source.value(QStringLiteral("md5")).toString();
  mod.source.fileSize        = source.value(QStringLiteral("fileSize")).toInteger(0);
  mod.source.logicalFilename = source.value(QStringLiteral("logicalFilename")).toString();

  const QJsonValue choices = entry.value(QStringLiteral("choices"));
  if (choices.isObject()) {
    if (!choices.toObject().isEmpty()) {
      mod.choices     = choices.toObject();
      mod.choicesType = mod.choices.value(QStringLiteral("type")).toString();
      if (mod.choicesType.isEmpty()) {
        mod.choicesType = QStringLiteral("fomod");
      }
    }
  } else if (!choices.isUndefined() && !choices.isNull()) {
    mod.choicesType = QStringLiteral("invalid");
  }

  mod.unsupported = unsupportedReason(mod);
  return mod;
}
}  // namespace

std::optional<CollectionManifest> CollectionManifest::parse(const QByteArray& json,
                                                            QString& error)
{
  QJsonParseError parseError;
  const QJsonDocument doc = QJsonDocument::fromJson(json, &parseError);
  if (parseError.error != QJsonParseError::NoError) {
    error = QStringLiteral("collection.json is not valid JSON: %1")
                .arg(parseError.errorString());
    return std::nullopt;
  }
  if (!doc.isObject()) {
    error = QStringLiteral("collection.json is not a JSON object");
    return std::nullopt;
  }

  const QJsonObject root = doc.object();
  if (!root.value(QStringLiteral("info")).isObject()) {
    error = QStringLiteral("collection.json has no info section");
    return std::nullopt;
  }
  if (!root.value(QStringLiteral("mods")).isArray()) {
    error = QStringLiteral("collection.json has no mods array");
    return std::nullopt;
  }

  CollectionManifest manifest;

  const QJsonObject info   = root.value(QStringLiteral("info")).toObject();
  manifest.info.name       = info.value(QStringLiteral("name")).toString();
  manifest.info.domainName = info.value(QStringLiteral("domainName")).toString();
  manifest.info.author     = info.value(QStringLiteral("author")).toString();
  for (const QJsonValue& version : info.value(QStringLiteral("gameVersions")).toArray()) {
    if (version.isString()) {
      manifest.info.gameVersions << version.toString();
    }
  }

  manifest.recommendNewProfile = root.value(QStringLiteral("collectionConfig"))
                                     .toObject()
                                     .value(QStringLiteral("recommendNewProfile"))
                                     .toBool(false);

  const QJsonArray mods = root.value(QStringLiteral("mods")).toArray();
  manifest.mods.reserve(static_cast<size_t>(mods.size()));
  for (int i = 0; i < mods.size(); ++i) {
    if (!mods.at(i).isObject()) {
      error = QStringLiteral("mods[%1] is not an object").arg(i);
      return std::nullopt;
    }
    manifest.mods.push_back(parseMod(mods.at(i).toObject()));
  }

  for (const char* section : {"modRules", "loadOrder", "externalResources", "tools"}) {
    if (sectionHasContent(root.value(QLatin1String(section)))) {
      manifest.unhandledSections << QString::fromLatin1(section);
    }
  }

  return manifest;
}

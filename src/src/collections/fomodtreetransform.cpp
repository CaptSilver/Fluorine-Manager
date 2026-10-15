#include "fomodtreetransform.h"

#include <QStringList>

#include <algorithm>
#include <tuple>

using namespace MOBase;

namespace {
QStringList splitSlashes(const QString &path) {
  return path.split(QLatin1Char('/'), Qt::SkipEmptyParts);
}

// Finds `parts` below `dir`. An archive written on Windows can hold "Car/" and
// "car/" as two sibling entries, and IFileTree::find() only looks at the first
// one that matches, so every child that matches is tried instead, exact
// spelling first.
std::shared_ptr<const FileTreeEntry> resolve(const IFileTree &dir,
                                             const QStringList &parts,
                                             qsizetype index,
                                             FileTreeEntry::FileTypes wanted) {
  const QString &part = parts[index];
  const bool last = index + 1 == parts.size();
  for (const bool exactPass : {true, false}) {
    for (const auto &child : dir) {
      const bool exact = child->name() == part;
      if (exact != exactPass || child->compare(part) != 0) {
        continue;
      }
      if (last) {
        if (wanted.testFlag(child->fileType())) {
          return child;
        }
      } else if (child->isDir()) {
        const auto subtree = child->astree();
        if (auto found = resolve(*subtree, parts, index + 1, wanted)) {
          return found;
        }
      }
    }
  }
  return nullptr;
}
} // namespace

namespace FomodTreeTransform {
std::optional<QString>
findFomodParent(const std::shared_ptr<const IFileTree> &tree) {
  bool found = false;
  QStringList
      bestKey; // path of the winning ModuleConfig.xml, component by component
  QStringList bestParent;

  tree->walk(
      [&](const QString &dir, std::shared_ptr<const FileTreeEntry> entry) {
        if (entry->isFile() &&
            entry->compare(QStringLiteral("ModuleConfig.xml")) == 0) {
          QStringList parts = splitSlashes(dir);
          if (!parts.isEmpty() &&
              parts.last().compare(QStringLiteral("fomod"),
                                   Qt::CaseInsensitive) == 0) {
            QStringList key = parts;
            key.append(entry->name());
            if (!found ||
                std::lexicographical_compare(key.begin(), key.end(),
                                             bestKey.begin(), bestKey.end())) {
              found = true;
              bestKey = key;
              parts.removeLast();
              bestParent = parts;
            }
          }
        }
        return IFileTree::WalkReturn::CONTINUE;
      },
      QStringLiteral("/"));

  if (!found) {
    return std::nullopt;
  }
  return bestParent.join(QLatin1Char('/'));
}

std::shared_ptr<const FileTreeEntry>
moduleConfigEntry(const std::shared_ptr<const IFileTree> &tree,
                  const QString &fomodParent) {
  QStringList parts = splitSlashes(fomodParent);
  parts << QStringLiteral("fomod") << QStringLiteral("ModuleConfig.xml");
  return resolve(*tree, parts, 0, FileTreeEntry::FILE);
}

bool apply(std::shared_ptr<IFileTree> &tree, const QString &fomodParent,
           const std::vector<FileRule> &rules, QString &error) {
  // Ascending, so the winning rule is applied last and replaces what the others
  // placed.
  std::vector<const FileRule *> ordered;
  ordered.reserve(rules.size());
  for (const FileRule &rule : rules) {
    ordered.push_back(&rule);
  }
  std::stable_sort(ordered.begin(), ordered.end(),
                   [](const FileRule *a, const FileRule *b) {
                     return std::tie(a->priority, a->sequence) <
                            std::tie(b->priority, b->sequence);
                   });

  const std::shared_ptr<IFileTree> result = tree->createOrphanTree();
  if (!result) {
    error = QStringLiteral("cannot create a tree for the installed files");
    return false;
  }
  const QStringList base = splitSlashes(fomodParent);

  for (const FileRule *rule : ordered) {
    QStringList parts = base;
    parts += splitSlashes(rule->source);
    if (parts.size() == base.size()) {
      error = QStringLiteral("FOMOD entry with an empty source");
      return false;
    }

    const auto source = resolve(*tree, parts, 0,
                                rule->isFolder ? FileTreeEntry::DIRECTORY
                                               : FileTreeEntry::FILE);
    if (!source) {
      error = QStringLiteral("FOMOD %1 '%2' is not in the archive")
                  .arg(rule->isFolder ? QStringLiteral("folder")
                                      : QStringLiteral("file"),
                       rule->source);
      return false;
    }

    // MERGE: a file replaces a file, a directory merges into a directory. An
    // earlier rule's files are only replaced where this rule has a file at the
    // same path.
    if (rule->isFolder) {
      const QString into = rule->destination.isEmpty()
                               ? QString()
                               : rule->destination + QLatin1Char('/');
      const auto directory = source->astree();
      for (const auto &child : *directory) {
        if (!result->copy(child, into, IFileTree::InsertPolicy::MERGE)) {
          error = QStringLiteral("FOMOD cannot place '%1' from folder '%2'")
                      .arg(child->name(), rule->source);
          return false;
        }
      }
    } else if (!result->copy(source, rule->destination,
                             IFileTree::InsertPolicy::MERGE)) {
      error = QStringLiteral("FOMOD cannot place '%1' at '%2'")
                  .arg(rule->source, rule->destination);
      return false;
    }
  }

  tree = result;
  return true;
}
} // namespace FomodTreeTransform

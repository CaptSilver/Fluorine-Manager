#pragma once

#include <uibase/ifiletree.h>

#include <QString>
#include <QStringList>

#include <algorithm>
#include <map>
#include <memory>
#include <utility>
#include <vector>

// A file that remembers the path it was created from. clone() keeps that
// origin, the way ArchiveFileEntry::clone() keeps its archive index, so after a
// copy a test can tell which source entry ended up at a destination.
class MemoryFileEntry : public virtual MOBase::FileTreeEntry {
public:
  MemoryFileEntry(std::shared_ptr<const MOBase::IFileTree> parent, QString name,
                  QString origin)
      : MOBase::FileTreeEntry(parent, name), m_Origin(std::move(origin)) {}

  std::shared_ptr<MOBase::FileTreeEntry> clone() const override {
    return std::make_shared<MemoryFileEntry>(nullptr, name(), m_Origin);
  }

  const QString m_Origin;
};

// An IFileTree over a list of paths, built the way ArchiveFileTreeImpl is:
// directories are grouped by their exact spelling, so "Car/" and "car/" stay
// two sibling entries as they do in a real archive. Building it with
// addDirectory() would merge them (the base class compares names
// case-insensitively) and hide exactly the case under test.
class MemoryFileTree : public MOBase::IFileTree {
public:
  using Item = std::pair<QStringList, bool>; // path components, isDirectory

  // "a/b.txt" is a file, "a/c/" is a directory.
  static std::shared_ptr<MemoryFileTree> make(const QStringList &paths) {
    std::vector<Item> items;
    for (const QString &path : paths) {
      const bool isDir = path.endsWith(QLatin1Char('/'));
      const QStringList parts =
          path.split(QLatin1Char('/'), Qt::SkipEmptyParts);
      if (!parts.isEmpty()) {
        items.emplace_back(parts, isDir);
      }
    }
    return std::shared_ptr<MemoryFileTree>(
        new MemoryFileTree(nullptr, QString(), QString(), std::move(items)));
  }

protected:
  MemoryFileTree(std::shared_ptr<const IFileTree> parent, QString name,
                 QString prefix, std::vector<Item> items)
      : FileTreeEntry(parent, name), IFileTree(), m_Prefix(std::move(prefix)),
        m_Items(std::move(items)) {}

  std::shared_ptr<IFileTree>
  makeDirectory(std::shared_ptr<const IFileTree> parent,
                QString name) const override {
    return std::shared_ptr<MemoryFileTree>(
        new MemoryFileTree(parent, name, QString(), {}));
  }

  bool doPopulate(
      std::shared_ptr<const IFileTree> parent,
      std::vector<std::shared_ptr<FileTreeEntry>> &entries) const override {
    // Same-name entries must be adjacent; spellings that differ only by case
    // stay separate groups, ordered by exact spelling.
    std::sort(m_Items.begin(), m_Items.end(), [](const Item &a, const Item &b) {
      const int folded = a.first[0].compare(b.first[0], Qt::CaseInsensitive);
      return folded != 0 ? folded < 0 : a.first[0] < b.first[0];
    });

    bool inGroup = false;
    QString groupName;
    std::vector<Item> groupItems;
    const auto flushGroup = [&] {
      if (!inGroup) {
        return;
      }
      entries.push_back(std::shared_ptr<MemoryFileTree>(new MemoryFileTree(
          parent, groupName, m_Prefix + groupName + QLatin1Char('/'),
          std::move(groupItems))));
      groupItems.clear();
      inGroup = false;
    };

    for (const Item &item : m_Items) {
      const QString &head = item.first.first();
      if (inGroup && head != groupName) {
        flushGroup();
      }
      if (item.first.size() == 1 && !item.second) {
        entries.push_back(
            std::make_shared<MemoryFileEntry>(parent, head, m_Prefix + head));
        continue;
      }
      inGroup = true;
      groupName = head;
      if (item.first.size() > 1) {
        groupItems.emplace_back(item.first.mid(1), item.second);
      }
    }
    flushGroup();
    return false; // IFileTree sorts the entries
  }

  std::shared_ptr<IFileTree> doClone() const override {
    return std::shared_ptr<MemoryFileTree>(
        new MemoryFileTree(nullptr, name(), m_Prefix, m_Items));
  }

private:
  QString m_Prefix; // full path of this directory, with a trailing '/'; empty
                    // at the root
  mutable std::vector<Item> m_Items;
};

// Sorted '/'-separated paths of every file below `tree`.
inline QStringList
treeLeaves(const std::shared_ptr<const MOBase::IFileTree> &tree) {
  QStringList leaves;
  tree->walk(
      [&](const QString &dir,
          std::shared_ptr<const MOBase::FileTreeEntry> entry) {
        if (entry->isFile()) {
          leaves << dir + entry->name();
        }
        return MOBase::IFileTree::WalkReturn::CONTINUE;
      },
      QStringLiteral("/"));
  leaves.sort();
  return leaves;
}

// For every file below `tree`: its path -> the path its MemoryFileEntry was
// created from (empty for files that are not MemoryFileEntry).
inline std::map<QString, QString>
treeOrigins(const std::shared_ptr<const MOBase::IFileTree> &tree) {
  std::map<QString, QString> origins;
  tree->walk(
      [&](const QString &dir,
          std::shared_ptr<const MOBase::FileTreeEntry> entry) {
        if (entry->isFile()) {
          const auto *file = dynamic_cast<const MemoryFileEntry *>(entry.get());
          origins[dir + entry->name()] = file ? file->m_Origin : QString();
        }
        return MOBase::IFileTree::WalkReturn::CONTINUE;
      },
      QStringLiteral("/"));
  return origins;
}

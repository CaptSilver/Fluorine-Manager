#include "collectionstate.h"

#include <QHash>

#include <set>

std::vector<ModlistEntry> parseModlist(const QByteArray& contents)
{
  std::vector<ModlistEntry> entries;
  std::set<QString> seen;

  for (const QByteArray& rawLine : contents.split('\n')) {
    const QByteArray line = rawLine.endsWith('\r') ? rawLine.chopped(1) : rawLine;
    if (line.isEmpty() || line.at(0) == '#') {
      continue;
    }

    ModlistEntry entry;
    QByteArray name;
    switch (line.at(0)) {
    case '-':
      entry.enabled = false;
      name          = line.mid(1);
      break;
    case '+':
      name = line.mid(1);
      break;
    case '*':
      entry.foreign = true;
      name          = line.mid(1);
      break;
    default:
      name = line;
      break;
    }

    entry.name = QString::fromUtf8(name.trimmed());
    if (entry.name.isEmpty() || !seen.insert(entry.name).second) {
      continue;
    }
    entries.push_back(entry);
  }

  return entries;
}

void applyModlist(CollectionState& state, const std::vector<ModlistEntry>& entries)
{
  QHash<QString, bool> enabledByName;
  for (const ModlistEntry& entry : entries) {
    if (!entry.foreign) {
      enabledByName.insert(entry.name.toLower(), entry.enabled);
    }
  }

  for (InstalledMod& mod : state.mods) {
    const auto found = enabledByName.constFind(mod.folder.toLower());
    if (found == enabledByName.constEnd()) {
      mod.listedEnabled.reset();
    } else {
      mod.listedEnabled = *found;
    }
  }
}

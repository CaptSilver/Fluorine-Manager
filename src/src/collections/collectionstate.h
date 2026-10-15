#pragma once

#include "collectionmetatag.h"

#include <QByteArray>
#include <QString>
#include <QStringList>
#include <QtGlobal>

#include <optional>
#include <set>
#include <utility>
#include <vector>

// What is installed and downloaded right now, as plain data, so the planner
// needs no organizer. It is captured once before a run starts: "not listed"
// means "not in the profile before this run".

struct InstalledMod
{
  QString folder;
  std::vector<CollectionOwner> owners;
  std::set<std::pair<int, int>> installedFiles;
  // State in the target profile's modlist.txt; nullopt = not listed. There is no
  // priority field: placement reads priorities from the live profile, where foreign
  // mods and unlisted mods also take part in the numbering.
  std::optional<bool> listedEnabled;
};

struct DownloadedFile
{
  int modId  = 0;
  int fileId = 0;
  QString path;
  qint64 size = 0;
};

struct CollectionState
{
  std::vector<InstalledMod> mods;
  std::vector<DownloadedFile> downloads;
  QStringList profiles;
  QString currentProfile;
  QString gameVersion;
};

struct ModlistEntry
{
  QString name;
  bool enabled = true;
  bool foreign = false;  // '*' lines: game DLC and other mods Fluorine does not manage
};

// Reads modlist.txt the way Profile::refreshModStatus does: '#' comments,
// '-name' disabled, '+name' enabled, '*name' foreign, a bare name enabled.
// Entries come back in file order, where the top line is the highest priority.
// A name that appears twice keeps its first line.
std::vector<ModlistEntry> parseModlist(const QByteArray& contents);

// Sets listedEnabled on every mod the list names (names match case-insensitively) and
// clears it on the rest. Foreign entries are skipped.
void applyModlist(CollectionState& state, const std::vector<ModlistEntry>& entries);

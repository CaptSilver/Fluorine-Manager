#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"
#include "collectionmetatag.h"
#include "collectionstate.h"

#include <QString>
#include <QStringList>

#include <set>
#include <vector>

struct PlanChoices {
  enum class Mode { TopUp, FullSync };
  std::set<int> optionalFileIds;
  QString targetProfile;
  bool newProfile = false;
  Mode mode = Mode::TopUp;
  std::set<int> replaceFileIds; // from Retry
};

struct PlannedMod {
  enum class Action { Install, Replace, AlreadyInstalled, Adopt, Refused };
  const CollectionMod *mod = nullptr;
  QString folder;
  Action action = Action::Install;
  QString reason;
  bool needsDownload = false;
  QString downloadPath;
  bool addOwner = false; // tag this slug on an existing folder (adopt, or owned
                         // by another slug with the same file)
};

struct CollectionPlan {
  std::vector<PlannedMod> mods;
  std::vector<QString> droppedFolders;
  std::vector<QString> toDisable;
  bool firstInstall = true;
  QStringList warnings;
  qint64 bytesToDownload = 0;
  int apiRequests = 0;
  int archivesOnDisk = 0;
};

// Refusal reasons other code compares against: the report seeds its rows from
// them.
namespace PlanReason {
inline const QString OptionalNotSelected =
    QStringLiteral("optional, not selected");
inline const QString FolderExists =
    QStringLiteral("folder exists and is not from this collection");
inline const QString InvalidFolderName =
    QStringLiteral("name is not a valid folder name");
inline const QString FolderNameCollides =
    QStringLiteral("folder name collides with another mod");
} // namespace PlanReason

// Precedence for an existing folder, first match wins: owner entry for this
// slug; owner entry for another slug; Adopt (untagged, installedFiles has
// (modId, *), folded name equal); any folder holding the exact (modId, fileId)
// (user install, AlreadyInstalled, never tagged); untagged folder at the
// planned name (Refused unless in replaceFileIds).
CollectionPlan planCollection(const CollectionLink &link,
                              const CollectionManifest &manifest,
                              const CollectionState &state,
                              const PlanChoices &choices);

// Lower-cases, collapses whitespace, and drops the characters and trailing dots
// that folder-name cleaning removes, so two spellings of one folder name
// compare equal.
QString foldCollectionName(const QString &name);

#pragma once

#include "collectionmetatag.h"
#include "collectionstate.h"

#include <QString>

class ModInfoRegular;
class OrganizerCore;

// Reads the installed mods, the ready downloads, the profile list and the modlist.txt of
// `targetProfile` into a plain struct, so the planner never touches live objects.
// An empty or unknown targetProfile leaves listedEnabled/priority unset on every mod,
// which is what a profile that is about to be created looks like.
// Call it before anything is installed: "not listed" then means "not in the profile
// before this run".
CollectionState captureCollectionState(OrganizerCore& core, const QString& targetProfile);

// Adds `owner` to the mod's owners tag and records (modId, fileId) in its installedFiles,
// through the live ModInfo so its own saveMeta cannot write stale settings over the file
// afterwards. Returns false when the values are not on disk after the save.
bool addCollectionOwner(ModInfoRegular& mod, const CollectionOwner& owner, int modId,
                        int fileId);

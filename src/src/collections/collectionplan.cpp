#include "collectionplan.h"

#include <uibase/filesystemutilities.h>

#include <QRegularExpression>

#include <algorithm>
#include <map>

QString foldCollectionName(const QString &name) {
  static const QRegularExpression forbidden(QStringLiteral(R"([<>:"/\\|?*])"));

  QString folded = name;
  folded.remove(forbidden);
  folded = folded.simplified();
  while (folded.endsWith('.') || folded.endsWith(' '))
    folded.chop(1);
  return folded.toCaseFolded();
}

namespace {
using FilePair = std::pair<int, int>;

// Where a manifest entry already lives, if anywhere.
enum class Kind {
  None,
  OwnedSame,    // this collection's tag, same file
  OwnedChanged, // this collection's tag, same mod, another file
  OtherSame,    // another collection's tag, same file
  OtherChanged, // another collection's tag, same mod, another file
  UserExact,    // untagged folder holding exactly this file
  AtName,       // untagged folder sitting on the planned name
  TaggedAtName // tagged folder sitting on the planned name, for a different mod
};

struct Existing {
  Kind kind = Kind::None;
  const InstalledMod *folder = nullptr;
  QString otherSlug; // OtherSame, OtherChanged, TaggedAtName
};

// A folder can back one manifest entry only.
using Claimed = std::set<const InstalledMod *>;

bool sameName(const QString &a, const QString &b) {
  return a.compare(b, Qt::CaseInsensitive) == 0;
}

const CollectionOwner *ownerOf(const InstalledMod &mod, const QString &slug) {
  for (const CollectionOwner &owner : mod.owners) {
    if (owner.slug == slug)
      return &owner;
  }
  return nullptr;
}

bool holdsFile(const InstalledMod &mod, const FilePair &file) {
  return mod.installedFiles.count(file) > 0;
}

void refuse(PlannedMod &planned, const QString &reason) {
  planned.action = PlannedMod::Action::Refused;
  planned.reason = reason;
  planned.addOwner = false;
}

const InstalledMod *findOwnedSame(const CollectionState &state,
                                  const QString &slug, const CollectionMod &mod,
                                  const Claimed &claimed) {
  for (const InstalledMod &installed : state.mods) {
    const CollectionOwner *owner = ownerOf(installed, slug);
    if (owner && owner->modId == mod.source.modId &&
        owner->fileId == mod.source.fileId && !claimed.count(&installed))
      return &installed;
  }
  return nullptr;
}

// The candidate whose folder is named like the manifest entry, else the first
// one.
const InstalledMod *
pickByName(const std::vector<const InstalledMod *> &candidates,
           const QString &plannedName) {
  for (const InstalledMod *candidate : candidates) {
    if (!plannedName.isEmpty() && sameName(candidate->folder, plannedName))
      return candidate;
  }
  return candidates.empty() ? nullptr : candidates.front();
}

// Looks for the folder a manifest entry already lives in, first match wins.
Existing findExisting(const CollectionLink &link, const CollectionState &state,
                      const CollectionMod &mod, const QString &plannedName,
                      const Claimed &claimed) {
  const int modId = mod.source.modId;
  const int fileId = mod.source.fileId;

  // Tagged by this collection for the same mod but another file: a changed file
  // in a newer revision. The folder keeps its name even if the mod was renamed.
  std::vector<const InstalledMod *> changed;
  for (const InstalledMod &installed : state.mods) {
    const CollectionOwner *owner = ownerOf(installed, link.slug);
    if (owner && owner->modId == modId && !claimed.count(&installed))
      changed.push_back(&installed);
  }
  if (const InstalledMod *folder = pickByName(changed, plannedName))
    return {Kind::OwnedChanged, folder, {}};

  // Tagged by another collection. The same file is shared; another file is not
  // ours to overwrite. The exact file wins when both exist.
  for (int pass = 0; pass < 2; ++pass) {
    for (const InstalledMod &installed : state.mods) {
      if (claimed.count(&installed))
        continue;
      for (const CollectionOwner &owner : installed.owners) {
        if (owner.slug == link.slug || owner.modId != modId)
          continue;
        const bool same = owner.fileId == fileId;
        if (same == (pass == 0))
          return {same ? Kind::OtherSame : Kind::OtherChanged, &installed,
                  owner.slug};
      }
    }
  }

  // A folder the user installed by hand that holds exactly this file.
  for (const InstalledMod &installed : state.mods) {
    if (!claimed.count(&installed) && holdsFile(installed, {modId, fileId}))
      return {Kind::UserExact, &installed, {}};
  }

  // Something else already sits on the folder name the install would create.
  if (!plannedName.isEmpty()) {
    for (const InstalledMod &installed : state.mods) {
      if (claimed.count(&installed) || !sameName(installed.folder, plannedName))
        continue;
      if (installed.owners.empty())
        return {Kind::AtName, &installed, {}};
      return {Kind::TaggedAtName, &installed, installed.owners.front().slug};
    }
  }
  return {};
}

const DownloadedFile *findDownload(const CollectionState &state,
                                   const CollectionMod &mod) {
  for (const DownloadedFile &file : state.downloads) {
    const bool sizeMatches =
        mod.source.fileSize == 0 || file.size == mod.source.fileSize;
    if (file.modId == mod.source.modId && file.fileId == mod.source.fileId &&
        sizeMatches)
      return &file;
  }
  return nullptr;
}
} // namespace

CollectionPlan planCollection(const CollectionLink &link,
                              const CollectionManifest &manifest,
                              const CollectionState &state,
                              const PlanChoices &choices) {
  CollectionPlan plan;
  const std::size_t count = manifest.mods.size();
  plan.mods.resize(count);

  std::vector<QString> plannedNames(count);
  std::vector<Existing> existing(count);
  Claimed claimed;

  for (std::size_t i = 0; i < count; ++i) {
    QString name = manifest.mods[i].name;
    if (MOBase::fixDirectoryName(name))
      plannedNames[i] = name;
  }

  // Exact tag matches go first so a changed-file match for one entry cannot
  // take the folder another entry owns outright.
  for (std::size_t i = 0; i < count; ++i) {
    if (const InstalledMod *folder =
            findOwnedSame(state, link.slug, manifest.mods[i], claimed)) {
      existing[i] = {Kind::OwnedSame, folder, {}};
      claimed.insert(folder);
    }
  }
  for (std::size_t i = 0; i < count; ++i) {
    if (existing[i].kind != Kind::None)
      continue;
    existing[i] =
        findExisting(link, state, manifest.mods[i], plannedNames[i], claimed);
    if (existing[i].folder)
      claimed.insert(existing[i].folder);
  }

  for (std::size_t i = 0; i < count; ++i) {
    const CollectionMod &mod = manifest.mods[i];
    const Existing &found = existing[i];
    PlannedMod &planned = plan.mods[i];

    planned.mod = &mod;
    planned.folder = found.folder ? found.folder->folder : plannedNames[i];

    if (!mod.unsupported.isEmpty()) {
      refuse(planned, mod.unsupported);
      continue;
    }
    if (mod.optional && !choices.optionalFileIds.count(mod.source.fileId)) {
      refuse(planned, PlanReason::OptionalNotSelected);
      continue;
    }

    switch (found.kind) {
    case Kind::OwnedSame:
    case Kind::UserExact:
      planned.action = PlannedMod::Action::AlreadyInstalled;
      break;
    case Kind::OtherSame:
      planned.action = PlannedMod::Action::AlreadyInstalled;
      planned.addOwner = true;
      break;
    case Kind::OwnedChanged:
      planned.action = PlannedMod::Action::Replace;
      break;
    case Kind::OtherChanged:
      refuse(planned,
             QStringLiteral("installed by collection %1").arg(found.otherSlug));
      break;
    case Kind::TaggedAtName:
      refuse(planned, QStringLiteral("folder is installed by collection %1")
                          .arg(found.otherSlug));
      break;
    case Kind::AtName:
      if (choices.replaceFileIds.count(mod.source.fileId))
        planned.action = PlannedMod::Action::Replace;
      else
        refuse(planned, PlanReason::FolderExists);
      break;
    case Kind::None:
      if (plannedNames[i].isEmpty())
        refuse(planned, PlanReason::InvalidFolderName);
      else
        planned.action = PlannedMod::Action::Install;
      break;
    }
  }

  // Two entries that would create the same folder would overwrite each other,
  // and the installer would stop to ask for a new name, so neither is
  // installed.
  std::map<QString, std::vector<std::size_t>> byFolder;
  for (std::size_t i = 0; i < count; ++i) {
    const PlannedMod &planned = plan.mods[i];
    // A refused entry still owns the folder it matched, so an install that
    // lands there would hit an existing directory.
    const bool claimsFolder = planned.action != PlannedMod::Action::Refused ||
                              existing[i].folder != nullptr;
    if (claimsFolder && !planned.folder.isEmpty())
      byFolder[planned.folder.toCaseFolded()].push_back(i);
  }
  for (const auto &entry : byFolder) {
    if (entry.second.size() < 2)
      continue;
    for (std::size_t i : entry.second) {
      if (plan.mods[i].action == PlannedMod::Action::Install)
        refuse(plan.mods[i], PlanReason::FolderNameCollides);
    }
  }

  // Tally what the run has to fetch.
  for (PlannedMod &planned : plan.mods) {
    if (planned.action != PlannedMod::Action::Install &&
        planned.action != PlannedMod::Action::Replace)
      continue;
    if (const DownloadedFile *file = findDownload(state, *planned.mod)) {
      planned.downloadPath = file->path;
      ++plan.archivesOnDisk;
    } else {
      planned.needsDownload = true;
      plan.bytesToDownload += planned.mod->source.fileSize;
      // File info, download link, and the mod description that DownloadManager
      // fetches after the transfer: a download is not "ready" until that last
      // answer arrives.
      plan.apiRequests += 3;
    }
  }

  if (!manifest.info.gameVersions.isEmpty() && !state.gameVersion.isEmpty() &&
      !manifest.info.gameVersions.contains(state.gameVersion)) {
    plan.warnings.push_back(
        QStringLiteral("Game version %1 is not one this collection lists (%2).")
            .arg(state.gameVersion, manifest.info.gameVersions.join(", ")));
  }
  if (manifest.hasUnhandled()) {
    plan.warnings.push_back(
        QStringLiteral("This collection has %1, which Fluorine does not apply.")
            .arg(manifest.unhandledSections.join(", ")));
  }

  return plan;
}

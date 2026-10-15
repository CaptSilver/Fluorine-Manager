#include "collectionlive.h"

#include "collectionlink.h"

#include "downloadmanager.h"
#include "modinfo.h"
#include "modinforegular.h"
#include "organizercore.h"
#include "profile.h"
#include "settings.h"

#include <iplugingame.h>
#include <log.h>

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QMetaType>
#include <QVariant>

#include <algorithm>
#include <climits>
#include <optional>

using namespace MOBase;

namespace
{
QString matchProfileName(const QStringList& profiles, const QString& wanted)
{
  for (const QString& name : profiles) {
    if (name.compare(wanted, Qt::CaseInsensitive) == 0) {
      return name;
    }
  }
  return {};
}

void captureMods(OrganizerCore& core, CollectionState& state)
{
  const unsigned int count = ModInfo::getNumMods();
  for (unsigned int i = 0; i < count; ++i) {
    const ModInfo::Ptr info = ModInfo::getByIndex(i);

    // ModInfoSeparator and ModInfoBackup derive from ModInfoRegular and report
    // isRegular(), so they have to be excluded by name.
    if (!info->isRegular() || info->isSeparator() || info->isBackup() ||
        info->isOverwrite() || info->isForeign()) {
      continue;
    }

    InstalledMod mod;
    mod.folder = info->name();
    mod.owners = CollectionMetaTag::decodeOwners(
        CollectionMetaTag::storedString(info->pluginSetting(
            CollectionMetaTag::PluginName, CollectionMetaTag::OwnersKey, QVariant())));
    mod.installedFiles = info->installedFiles();
    state.mods.push_back(std::move(mod));
  }

  // A folder the stopgap or an earlier run left in the mods directory may not be a
  // ModInfo yet (the list has not been refreshed since). Its tag still counts, or the
  // planner would install the same file a second time. Untagged unknown folders stay
  // out: they are reported as "folder exists".
  const QString modsDir = core.settings().paths().mods();
  const QDir dir(modsDir);
  for (const QString& folder : dir.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
    if (ModInfo::getIndex(folder) != UINT_MAX) {
      continue;
    }

    const QString meta = dir.filePath(folder) + "/meta.ini";
    auto owners        = CollectionMetaTag::readOwners(meta);
    if (owners.empty()) {
      continue;
    }

    state.mods.push_back({folder, std::move(owners),
                          CollectionMetaTag::readInstalledFiles(meta), std::nullopt});
  }
}

void captureDownloads(OrganizerCore& core, CollectionState& state)
{
  DownloadManager* downloads = core.downloadManager();
  const IPluginGame* game    = core.managedGame();
  if (downloads == nullptr) {
    return;
  }

  const int total = downloads->numTotalDownloads();
  for (int i = 0; i < total; ++i) {
    const auto rowState = downloads->getState(i);
    if (rowState != DownloadManager::STATE_READY &&
        rowState != DownloadManager::STATE_INSTALLED &&
        rowState != DownloadManager::STATE_UNINSTALLED) {
      continue;
    }

    const auto* info = downloads->getFileInfo(i);
    if (info == nullptr || info->modID <= 0 || info->fileID <= 0) {
      continue;
    }

    // Mod ids are per game: a Skyrim file with the same ids is not ours.
    if (game != nullptr && !info->gameName.isEmpty() &&
        !collectionDomainMatches(info->gameName, game->gameShortName(),
                                 game->gameNexusName(), game->validShortNames())) {
      continue;
    }

    const QString path = downloads->getFilePath(i);
    const QFileInfo file(path);
    if (!file.isFile()) {
      continue;
    }

    // getFileSize() is the size announced for the transfer, not what is on disk.
    state.downloads.push_back({info->modID, info->fileID, path, file.size()});
  }
}

void captureModlist(OrganizerCore& core, CollectionState& state,
                    const QString& targetProfile)
{
  const QString profile = matchProfileName(state.profiles, targetProfile);
  if (profile.isEmpty()) {
    return;
  }

  // The current profile writes its list on a delay; flush it so the file matches memory.
  const auto current = core.currentProfile();
  if (current != nullptr && current->name() == profile) {
    current->writeModlistNow(true);
  }

  QFile file(core.settings().paths().profiles() + "/" + profile + "/modlist.txt");
  if (!file.open(QIODevice::ReadOnly)) {
    log::warn("collection: cannot read {}: {}", file.fileName(), file.errorString());
    return;
  }
  applyModlist(state, parseModlist(file.readAll()));
}
}  // namespace

CollectionState captureCollectionState(OrganizerCore& core, const QString& targetProfile)
{
  CollectionState state;

  state.profiles = core.profileNames();
  if (const auto current = core.currentProfile()) {
    state.currentProfile = current->name();
  }
  if (const IPluginGame* game = core.managedGame()) {
    state.gameVersion = game->gameVersion();
  }

  captureMods(core, state);
  captureDownloads(core, state);
  captureModlist(core, state, targetProfile);
  return state;
}

bool addCollectionOwner(ModInfoRegular& mod, const CollectionOwner& owner, int modId,
                        int fileId)
{
  const auto owners = CollectionMetaTag::withOwner(
      CollectionMetaTag::decodeOwners(
          CollectionMetaTag::storedString(mod.pluginSetting(
              CollectionMetaTag::PluginName, CollectionMetaTag::OwnersKey, QVariant()))),
      owner);

  // addInstalledFile only marks the mod dirty; setPluginSetting saves, so the order
  // matters: with the calls swapped the pair would wait for the next unrelated save.
  mod.addInstalledFile(modId, fileId);
  mod.setPluginSetting(CollectionMetaTag::PluginName, CollectionMetaTag::OwnersKey,
                       CollectionMetaTag::encodeOwners(owners));

  // saveMeta() reports a failed write only through its log, so read the file back.
  const auto onDisk = CollectionMetaTag::readOwners(mod.absolutePath() + "/meta.ini");
  return std::find(onDisk.begin(), onDisk.end(), owner) != onDisk.end();
}

#include "collections/collectionplan.h"

#include <gtest/gtest.h>

#include <QFile>
#include <QRegularExpression>
#include <QString>

#include <ostream>

[[maybe_unused]] static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << text.toStdString() << '"';
}

namespace {
using Action = PlannedMod::Action;

CollectionLink coreLink(int revision = 2) {
  CollectionLink link;
  link.domain = "cyberpunk2077";
  link.slug = "core";
  link.revision = revision;
  return link;
}

CollectionMod modOf(const QString &name, int modId, int fileId,
                    qint64 size = 1000, bool optional = false) {
  CollectionMod mod;
  mod.name = name;
  mod.version = "1.0";
  mod.optional = optional;
  mod.source.type = "nexus";
  mod.source.modId = modId;
  mod.source.fileId = fileId;
  mod.source.fileSize = size;
  return mod;
}

CollectionManifest manifestOf(std::vector<CollectionMod> mods) {
  CollectionManifest manifest;
  manifest.info.name = "Core";
  manifest.info.domainName = "cyberpunk2077";
  manifest.mods = std::move(mods);
  return manifest;
}

CollectionOwner tagFor(const QString &slug, int modId, int fileId) {
  return CollectionOwner{slug, 1, modId, fileId};
}

InstalledMod folderOf(const QString &folder,
                      std::set<std::pair<int, int>> files = {},
                      std::vector<CollectionOwner> owners = {}) {
  InstalledMod mod;
  mod.folder = folder;
  mod.installedFiles = std::move(files);
  mod.owners = std::move(owners);
  mod.listedEnabled = true;
  return mod;
}

CollectionState stateWith(std::vector<InstalledMod> mods) {
  CollectionState state;
  state.mods = std::move(mods);
  return state;
}
} // namespace

TEST(Plan, FreshInstallInstallsEverything) {
  const CollectionManifest manifest =
      manifestOf({modOf("Alpha", 1, 10, 100), modOf("Beta", 2, 20, 200)});
  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  for (const PlannedMod &planned : plan.mods) {
    EXPECT_EQ(planned.action, Action::Install);
    EXPECT_TRUE(planned.needsDownload);
    EXPECT_FALSE(planned.addOwner);
  }
  EXPECT_EQ(plan.mods[0].mod, &manifest.mods[0]);
  EXPECT_EQ(plan.mods[0].folder, "Alpha");
  EXPECT_EQ(plan.mods[1].folder, "Beta");
  EXPECT_EQ(plan.bytesToDownload, 300);
  EXPECT_EQ(plan.apiRequests, 6);
  EXPECT_EQ(plan.archivesOnDisk, 0);
}

TEST(Plan, EverythingDownloadedNeedsNoDownload) {
  const CollectionManifest manifest =
      manifestOf({modOf("Alpha", 1, 10, 100), modOf("Beta", 2, 20, 200)});
  CollectionState state;
  state.downloads = {DownloadedFile{1, 10, "/dl/alpha.7z", 100},
                     DownloadedFile{2, 20, "/dl/beta.7z", 200}};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  EXPECT_EQ(plan.mods[0].action, Action::Install);
  EXPECT_FALSE(plan.mods[0].needsDownload);
  EXPECT_EQ(plan.mods[0].downloadPath, "/dl/alpha.7z");
  EXPECT_FALSE(plan.mods[1].needsDownload);
  EXPECT_EQ(plan.bytesToDownload, 0);
  EXPECT_EQ(plan.apiRequests, 0);
  EXPECT_EQ(plan.archivesOnDisk, 2);
}

TEST(Plan, SizeMismatchNeedsDownload) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10, 100)});
  CollectionState state;
  state.downloads = {DownloadedFile{1, 10, "/dl/alpha.7z", 99}};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_TRUE(plan.mods[0].needsDownload);
  EXPECT_TRUE(plan.mods[0].downloadPath.isEmpty());
  EXPECT_EQ(plan.bytesToDownload, 100);
  EXPECT_EQ(plan.archivesOnDisk, 0);
}

TEST(Plan, OptionalUntickedRefused) {
  const CollectionManifest manifest = manifestOf(
      {modOf("Required", 1, 10, 100), modOf("Extra", 2, 20, 200, true)});
  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  EXPECT_EQ(plan.mods[0].action, Action::Install);
  EXPECT_EQ(plan.mods[1].action, Action::Refused);
  EXPECT_EQ(plan.mods[1].reason, PlanReason::OptionalNotSelected);
  EXPECT_FALSE(plan.mods[1].needsDownload);
  EXPECT_EQ(plan.bytesToDownload, 100);
}

TEST(Plan, OptionalTickedInstalled) {
  const CollectionManifest manifest = manifestOf(
      {modOf("Required", 1, 10, 100), modOf("Extra", 2, 20, 200, true)});
  PlanChoices choices;
  choices.optionalFileIds = {20};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, choices);

  ASSERT_EQ(plan.mods.size(), 2u);
  EXPECT_EQ(plan.mods[1].action, Action::Install);
  EXPECT_EQ(plan.bytesToDownload, 300);
}

TEST(Plan, SameFolderNameRefusesBoth) {
  const CollectionManifest manifest =
      manifestOf({modOf("Same Name", 1, 10), modOf("Same  Name", 2, 20),
                  modOf("Other", 3, 30)});
  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 3u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_EQ(plan.mods[0].reason, PlanReason::FolderNameCollides);
  EXPECT_EQ(plan.mods[1].action, Action::Refused);
  EXPECT_EQ(plan.mods[1].reason, PlanReason::FolderNameCollides);
  EXPECT_EQ(plan.mods[2].action, Action::Install);
  EXPECT_EQ(plan.apiRequests, 3);
}

TEST(Plan, InvalidFolderNameRefused) {
  const CollectionManifest manifest =
      manifestOf({modOf("...", 1, 10), modOf("???", 2, 20)});
  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  for (const PlannedMod &planned : plan.mods) {
    EXPECT_EQ(planned.action, Action::Refused);
    EXPECT_EQ(planned.reason, PlanReason::InvalidFolderName);
    EXPECT_TRUE(planned.folder.isEmpty());
  }
}

TEST(Plan, DosDeviceNameRefused) {
  const CollectionManifest manifest = manifestOf(
      {modOf("CON", 1, 10), modOf("NUL", 2, 20), modOf("COM1.", 3, 30)});
  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 3u);
  for (const PlannedMod &planned : plan.mods) {
    EXPECT_EQ(planned.action, Action::Refused);
    EXPECT_EQ(planned.reason, PlanReason::InvalidFolderName);
  }
}

TEST(Plan, UntaggedFolderExistsRefused) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state = stateWith({folderOf("cool mod", {{999, 1}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_EQ(plan.mods[0].reason, PlanReason::FolderExists);
  EXPECT_EQ(plan.mods[0].folder, "cool mod");
  EXPECT_FALSE(plan.mods[0].needsDownload);
}

TEST(Plan, UntaggedFolderInReplaceFileIdsReplaced) {
  const CollectionManifest manifest =
      manifestOf({modOf("Cool Mod", 10, 100, 50)});
  const CollectionState state = stateWith({folderOf("Cool Mod", {{999, 1}})});
  PlanChoices choices;
  choices.replaceFileIds = {100};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Replace);
  EXPECT_EQ(plan.mods[0].folder, "Cool Mod");
  EXPECT_TRUE(plan.mods[0].needsDownload);
  EXPECT_EQ(plan.bytesToDownload, 50);
}

TEST(Plan, TaggedFolderAtPlannedNameForOtherModIsRefusedNotReplaceable) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state =
      stateWith({folderOf("Cool Mod", {{55, 1}}, {tagFor("extras", 55, 1)})});
  PlanChoices choices;
  choices.replaceFileIds = {100};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_TRUE(plan.mods[0].reason.contains("extras"));
}

TEST(Plan, TaggedSameFileAlreadyInstalled) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state =
      stateWith({folderOf("Cool Mod", {{10, 100}}, {tagFor("core", 10, 100)})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::AlreadyInstalled);
  EXPECT_EQ(plan.mods[0].folder, "Cool Mod");
  EXPECT_FALSE(plan.mods[0].needsDownload);
  EXPECT_FALSE(plan.mods[0].addOwner);
  EXPECT_EQ(plan.apiRequests, 0);
}

TEST(Plan, TaggedOtherFileReplaced) {
  const CollectionManifest manifest = manifestOf({modOf("New Name", 10, 100)});
  const CollectionState state =
      stateWith({folderOf("Old Name", {{10, 90}}, {tagFor("core", 10, 90)})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Replace);
  EXPECT_EQ(plan.mods[0].folder, "Old Name");
  EXPECT_TRUE(plan.mods[0].needsDownload);
  EXPECT_FALSE(plan.mods[0].addOwner);
}

TEST(Plan, OwnedByOtherSlugDifferentFileRefused) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state =
      stateWith({folderOf("Cool Mod", {{10, 90}}, {tagFor("extras", 10, 90)})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_TRUE(plan.mods[0].reason.contains("extras"));
  EXPECT_EQ(plan.mods[0].folder, "Cool Mod");
  EXPECT_FALSE(plan.mods[0].needsDownload);
}

TEST(Plan, OwnedByOtherSlugSameFileAlreadyInstalledAndAddsOwner) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state = stateWith(
      {folderOf("Cool Mod", {{10, 100}}, {tagFor("extras", 10, 100)})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::AlreadyInstalled);
  EXPECT_TRUE(plan.mods[0].addOwner);
  EXPECT_EQ(plan.mods[0].folder, "Cool Mod");
  EXPECT_FALSE(plan.mods[0].needsDownload);
}

TEST(Plan, UserInstallWithExactFileAlreadyInstalledNeverTagged) {
  const CollectionManifest manifest = manifestOf({modOf("Cool Mod", 10, 100)});
  const CollectionState state =
      stateWith({folderOf("My Own Name", {{10, 100}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::AlreadyInstalled);
  EXPECT_EQ(plan.mods[0].folder, "My Own Name");
  EXPECT_FALSE(plan.mods[0].addOwner);
  EXPECT_FALSE(plan.mods[0].needsDownload);
}

TEST(Plan, UnsupportedManifestModRefused) {
  CollectionMod bundled = modOf("Bundled", 1, 10);
  bundled.unsupported = "source type 'bundle' is not nexus";
  const CollectionManifest manifest = manifestOf({bundled});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_EQ(plan.mods[0].reason, "source type 'bundle' is not nexus");
  EXPECT_FALSE(plan.mods[0].needsDownload);
  EXPECT_EQ(plan.apiRequests, 0);
}

TEST(Plan, GameVersionWarning) {
  CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  manifest.info.gameVersions = {"2.3.1.0"};

  CollectionState older;
  older.gameVersion = "1.6.0.0";
  const CollectionPlan warned =
      planCollection(coreLink(), manifest, older, PlanChoices{});
  ASSERT_EQ(warned.warnings.size(), 1);
  EXPECT_TRUE(warned.warnings[0].contains("1.6.0.0"));
  EXPECT_TRUE(warned.warnings[0].contains("2.3.1.0"));

  CollectionState matching;
  matching.gameVersion = "2.3.1.0";
  EXPECT_TRUE(planCollection(coreLink(), manifest, matching, PlanChoices{})
                  .warnings.isEmpty());

  EXPECT_TRUE(
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{})
          .warnings.isEmpty());
}

TEST(Plan, UnhandledSectionsWarning) {
  CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  manifest.unhandledSections = {"modRules", "loadOrder"};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{});

  ASSERT_EQ(plan.warnings.size(), 1);
  EXPECT_TRUE(plan.warnings[0].contains("modRules"));
  EXPECT_TRUE(plan.warnings[0].contains("loadOrder"));
}

// Each download costs three requests: file info, download link, and the mod
// description DownloadManager fetches once the transfer ends
// (downloadmanager.cpp:3174, :2206, :2403).
TEST(Plan, RequestCountIsThreePerDownload) {
  const CollectionManifest manifest =
      manifestOf({modOf("Alpha", 1, 10, 100), modOf("Beta", 2, 20, 200),
                  modOf("Gamma", 3, 30, 400)});
  CollectionState state;
  state.downloads = {DownloadedFile{2, 20, "/dl/beta.7z", 200}};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  EXPECT_EQ(plan.apiRequests, 6);
}

TEST(Plan, BytesToDownloadCountsOnlyNeededFiles) {
  const CollectionManifest manifest =
      manifestOf({modOf("Alpha", 1, 10, 100), modOf("Beta", 2, 20, 200),
                  modOf("Gamma", 3, 30, 400)});
  CollectionState state;
  state.downloads = {DownloadedFile{2, 20, "/dl/beta.7z", 200}};

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  EXPECT_EQ(plan.bytesToDownload, 500);
  EXPECT_EQ(plan.archivesOnDisk, 1);
}

TEST(Plan, InstallOntoFolderClaimedByRefusedEntryIsRefused) {
  const CollectionManifest manifest = manifestOf(
      {modOf("Alpha", 1, 10, 100, true), modOf("Beta", 2, 20, 200)});
  const CollectionState state = stateWith({folderOf("Beta", {{1, 10}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_EQ(plan.mods[1].action, Action::Refused);
  EXPECT_EQ(plan.mods[1].reason, PlanReason::FolderNameCollides);
  EXPECT_EQ(plan.bytesToDownload, 0);
  EXPECT_EQ(plan.apiRequests, 0);
}

namespace {
QByteArray readFixture(const char *name) {
  QFile file(QString(FLUORINE_TEST_SOURCE_DIR) +
             "/src/tests/fixtures/collections/" + name);
  EXPECT_TRUE(file.open(QIODevice::ReadOnly)) << file.fileName().toStdString();
  return file.readAll();
}

// Folder name as collection_install.py's sanitize_name makes it.
QString scriptFolderName(const QString &name) {
  static const QRegularExpression forbidden(R"([/\\:*?"<>|])");
  QString folder = name;
  folder.remove(forbidden);
  while (folder.endsWith('.') || folder.endsWith(' '))
    folder.chop(1);
  return folder;
}
} // namespace

TEST(Plan, AdoptsUntaggedFolderWithModIdAndFoldedName) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10, 100)});
  const CollectionState state = stateWith({folderOf("Alpha", {{1, 10}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Adopt);
  EXPECT_TRUE(plan.mods[0].addOwner);
  EXPECT_EQ(plan.mods[0].folder, "Alpha");
  EXPECT_FALSE(plan.mods[0].needsDownload);
  EXPECT_FALSE(plan.firstInstall);
  EXPECT_EQ(plan.apiRequests, 0);
}

TEST(Plan, AdoptionFoldsWhitespaceAndCase) {
  // "Eron   Male V Preset" is a real name from the Core manifest: the script
  // keeps the three spaces, fixDirectoryName would collapse them.
  const CollectionManifest manifest = manifestOf(
      {modOf("Eron   Male V Preset", 1, 10), modOf("Mixed Case Mod", 2, 20)});
  const CollectionState state =
      stateWith({folderOf("Eron   Male V Preset", {{1, 10}}),
                 folderOf("mixed case  MOD", {{2, 20}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 2u);
  EXPECT_EQ(plan.mods[0].action, Action::Adopt);
  EXPECT_EQ(plan.mods[0].folder, "Eron   Male V Preset");
  EXPECT_EQ(plan.mods[1].action, Action::Adopt);
  EXPECT_EQ(plan.mods[1].folder, "mixed case  MOD");
}

TEST(Plan, AdoptionWithOtherFileIdIsReplace) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10, 100)});
  const CollectionState state = stateWith({folderOf("Alpha", {{1, 9}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Replace);
  EXPECT_EQ(plan.mods[0].folder, "Alpha");
  EXPECT_FALSE(plan.mods[0].addOwner);
  EXPECT_TRUE(plan.mods[0].needsDownload);
  EXPECT_FALSE(plan.firstInstall);
  EXPECT_TRUE(plan.droppedFolders.empty());
}

TEST(Plan, AdoptionRefusedWhenNameDoesNotFoldMatch) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10, 100)});

  // Renamed by the user but still holding the exact file: a user install, never
  // tagged.
  const CollectionPlan exact = planCollection(
      coreLink(), manifest, stateWith({folderOf("Renamed By User", {{1, 10}})}),
      PlanChoices{});
  ASSERT_EQ(exact.mods.size(), 1u);
  EXPECT_EQ(exact.mods[0].action, Action::AlreadyInstalled);
  EXPECT_FALSE(exact.mods[0].addOwner);
  EXPECT_EQ(exact.mods[0].folder, "Renamed By User");

  // Renamed and holding an older file: nothing ties it to this entry, so it
  // installs fresh.
  const CollectionPlan older = planCollection(
      coreLink(), manifest, stateWith({folderOf("Renamed By User", {{1, 9}})}),
      PlanChoices{});
  ASSERT_EQ(older.mods.size(), 1u);
  EXPECT_EQ(older.mods[0].action, Action::Install);
  EXPECT_EQ(older.mods[0].folder, "Alpha");
}

TEST(Plan, AdoptionToleratesNamesFixDirectoryNameRejects) {
  const CollectionManifest manifest = manifestOf({modOf("CON", 7, 70)});
  const CollectionState state = stateWith({folderOf("CON", {{7, 70}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Adopt);
  EXPECT_EQ(plan.mods[0].folder, "CON");
}

TEST(Plan, AdoptsStopgapFoldersOverEachRealManifest) {
  for (const char *fixture :
       {"rcuccp-r189.json", "srpv39-r129.json", "vfy7w1-r112.json"}) {
    SCOPED_TRACE(fixture);
    QString error;
    const std::optional<CollectionManifest> manifest =
        CollectionManifest::parse(readFixture(fixture), error);
    ASSERT_TRUE(manifest.has_value()) << error.toStdString();

    CollectionState state;
    for (const CollectionMod &mod : manifest->mods) {
      InstalledMod folder;
      folder.folder = scriptFolderName(mod.name);
      folder.installedFiles = {{mod.source.modId, mod.source.fileId}};
      folder.listedEnabled = true;
      state.mods.push_back(folder);
    }

    const CollectionPlan plan =
        planCollection(coreLink(), *manifest, state, PlanChoices{});

    ASSERT_EQ(plan.mods.size(), manifest->mods.size());
    for (std::size_t i = 0; i < plan.mods.size(); ++i) {
      const std::string name = manifest->mods[i].name.toStdString();
      EXPECT_EQ(plan.mods[i].action, Action::Adopt) << name;
      EXPECT_TRUE(plan.mods[i].addOwner) << name;
      EXPECT_EQ(plan.mods[i].folder, scriptFolderName(manifest->mods[i].name))
          << name;
      EXPECT_FALSE(plan.mods[i].needsDownload) << name;
    }
    EXPECT_FALSE(plan.firstInstall);
    EXPECT_TRUE(plan.droppedFolders.empty());
    EXPECT_TRUE(plan.toDisable.empty());
    EXPECT_EQ(plan.bytesToDownload, 0);
    EXPECT_EQ(plan.apiRequests, 0);
  }
}

TEST(Plan, FirstInstallFalseWhenAnyFolderOwnedOrAdoptable) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});

  EXPECT_TRUE(
      planCollection(coreLink(), manifest, CollectionState{}, PlanChoices{})
          .firstInstall);

  // A folder the user made that has nothing to do with the collection.
  EXPECT_TRUE(planCollection(coreLink(), manifest,
                             stateWith({folderOf("Mine", {{77, 1}})}),
                             PlanChoices{})
                  .firstInstall);

  // Only another collection's mods are present.
  EXPECT_TRUE(planCollection(coreLink(), manifest,
                             stateWith({folderOf("Theirs", {{5, 50}},
                                                 {tagFor("extras", 5, 50)})}),
                             PlanChoices{})
                  .firstInstall);

  // Owned by this collection, even though the new revision no longer lists it.
  EXPECT_FALSE(planCollection(coreLink(), manifest,
                              stateWith({folderOf("Gone", {{5, 50}},
                                                  {tagFor("core", 5, 50)})}),
                              PlanChoices{})
                   .firstInstall);

  // Adoptable.
  EXPECT_FALSE(planCollection(coreLink(), manifest,
                              stateWith({folderOf("Alpha", {{1, 10}})}),
                              PlanChoices{})
                   .firstInstall);
}

TEST(Plan, TopUpListsDroppedButDisablesNothing) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  const CollectionState state =
      stateWith({folderOf("Alpha", {{1, 10}}, {tagFor("core", 1, 10)}),
                 folderOf("Gone", {{5, 50}}, {tagFor("core", 5, 50)}),
                 folderOf("Mine", {{77, 1}})});

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, PlanChoices{});

  EXPECT_EQ(plan.droppedFolders, std::vector<QString>{"Gone"});
  EXPECT_TRUE(plan.toDisable.empty());
  EXPECT_FALSE(plan.firstInstall);
}

TEST(Plan, FullSyncDisablesDroppedWithNoOtherOwner) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  const CollectionState state =
      stateWith({folderOf("Alpha", {{1, 10}}, {tagFor("core", 1, 10)}),
                 folderOf("Gone", {{5, 50}}, {tagFor("core", 5, 50)})});
  PlanChoices choices;
  choices.mode = PlanChoices::Mode::FullSync;

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  EXPECT_EQ(plan.droppedFolders, std::vector<QString>{"Gone"});
  EXPECT_EQ(plan.toDisable, std::vector<QString>{"Gone"});
}

TEST(Plan, SharedOwnerKeepsDroppedFolderEnabled) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  const CollectionState state =
      stateWith({folderOf("Alpha", {{1, 10}}, {tagFor("core", 1, 10)}),
                 folderOf("Shared", {{5, 50}},
                          {tagFor("core", 5, 50), tagFor("extras", 5, 50)})});
  PlanChoices choices;
  choices.mode = PlanChoices::Mode::FullSync;

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  EXPECT_EQ(plan.droppedFolders, std::vector<QString>{"Shared"});
  EXPECT_TRUE(plan.toDisable.empty());
}

TEST(Plan, ChangedFileInNewRevisionIsReplace) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10, 100)});
  const CollectionState state =
      stateWith({folderOf("Alpha", {{1, 9}}, {tagFor("core", 1, 9)})});
  PlanChoices choices;
  choices.mode = PlanChoices::Mode::FullSync;

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Replace);
  EXPECT_EQ(plan.mods[0].folder, "Alpha");
  EXPECT_TRUE(plan.droppedFolders.empty());
  EXPECT_TRUE(plan.toDisable.empty());
}

TEST(Plan, UserAddedModNeverDroppedOrDisabled) {
  const CollectionManifest manifest = manifestOf({modOf("Alpha", 1, 10)});
  const CollectionState state =
      stateWith({folderOf("Alpha", {{1, 10}}, {tagFor("core", 1, 10)}),
                 folderOf("Mine", {{77, 1}}),
                 folderOf("Theirs", {{5, 50}}, {tagFor("extras", 5, 50)})});
  PlanChoices choices;
  choices.mode = PlanChoices::Mode::FullSync;

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  EXPECT_TRUE(plan.droppedFolders.empty());
  EXPECT_TRUE(plan.toDisable.empty());
}

TEST(Plan, OptionalUntickedOwnedFolderIsNotDropped) {
  const CollectionManifest manifest =
      manifestOf({modOf("Extra", 2, 20, 100, true)});
  const CollectionState state =
      stateWith({folderOf("Extra", {{2, 20}}, {tagFor("core", 2, 20)})});
  PlanChoices choices;
  choices.mode = PlanChoices::Mode::FullSync;

  const CollectionPlan plan =
      planCollection(coreLink(), manifest, state, choices);

  ASSERT_EQ(plan.mods.size(), 1u);
  EXPECT_EQ(plan.mods[0].action, Action::Refused);
  EXPECT_TRUE(plan.droppedFolders.empty());
  EXPECT_TRUE(plan.toDisable.empty());
}

// A top-up over an older revision of a real collection: every mod of the r189
// manifest is installed and tagged, one of them with an older file id, and the
// older revision also had a mod that r189 no longer lists.
TEST(Plan,
     TopUpOverOlderRevisionOfRealFixtureReplacesChangedFileAndDropsRemovedMod) {
  QString error;
  const std::optional<CollectionManifest> manifest =
      CollectionManifest::parse(readFixture("rcuccp-r189.json"), error);
  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  ASSERT_GT(manifest->mods.size(), 10u);

  const std::size_t changed = 5;
  CollectionState state;
  for (std::size_t i = 0; i < manifest->mods.size(); ++i) {
    const CollectionModSource &source = manifest->mods[i].source;
    const int fileId = i == changed ? source.fileId - 1 : source.fileId;

    InstalledMod folder;
    folder.folder = scriptFolderName(manifest->mods[i].name);
    folder.installedFiles = {{source.modId, fileId}};
    folder.owners = {tagFor("core", source.modId, fileId)};
    folder.listedEnabled = true;
    state.mods.push_back(folder);
  }
  state.mods.push_back(folderOf("Dropped In Newer Revision", {{999999, 1}},
                                {tagFor("core", 999999, 1)}));

  const CollectionPlan plan =
      planCollection(coreLink(189), *manifest, state, PlanChoices{});

  ASSERT_EQ(plan.mods.size(), manifest->mods.size());
  for (std::size_t i = 0; i < plan.mods.size(); ++i) {
    const std::string name = manifest->mods[i].name.toStdString();
    EXPECT_EQ(plan.mods[i].action,
              i == changed ? Action::Replace : Action::AlreadyInstalled)
        << name;
    EXPECT_EQ(plan.mods[i].folder, scriptFolderName(manifest->mods[i].name))
        << name;
    EXPECT_EQ(plan.mods[i].needsDownload, i == changed) << name;
  }
  EXPECT_FALSE(plan.firstInstall);
  EXPECT_EQ(plan.droppedFolders,
            std::vector<QString>{"Dropped In Newer Revision"});
  EXPECT_TRUE(plan.toDisable.empty()) << "a top-up never disables";
  EXPECT_EQ(plan.bytesToDownload, manifest->mods[changed].source.fileSize);
  EXPECT_EQ(plan.apiRequests, 3);

  PlanChoices fullSync;
  fullSync.mode = PlanChoices::Mode::FullSync;
  const CollectionPlan synced =
      planCollection(coreLink(189), *manifest, state, fullSync);
  EXPECT_EQ(synced.toDisable,
            std::vector<QString>{"Dropped In Newer Revision"});
}

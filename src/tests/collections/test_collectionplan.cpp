#include "collections/collectionplan.h"

#include <gtest/gtest.h>

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

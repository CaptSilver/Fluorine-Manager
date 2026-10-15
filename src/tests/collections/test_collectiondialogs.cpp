#include "collections/collectionresultsdialog.h"
#include "collections/collectionreviewdialog.h"

#include <QComboBox>
#include <QLabel>
#include <QListWidget>
#include <QPushButton>
#include <QRadioButton>
#include <QTableWidget>

#include <gtest/gtest.h>

#include <set>

namespace {

CollectionLink makeLink() {
  CollectionLink link;
  link.domain = QStringLiteral("cyberpunk2077");
  link.slug = QStringLiteral("test-pack");
  link.revision = 2;
  return link;
}

CollectionMod makeMod(const QString &name, int modId, int fileId,
                      bool optional = false) {
  CollectionMod mod;
  mod.name = name;
  mod.version = QStringLiteral("1.0");
  mod.optional = optional;
  mod.source.type = QStringLiteral("nexus");
  mod.source.modId = modId;
  mod.source.fileId = fileId;
  mod.source.md5 = QStringLiteral("0123456789abcdef0123456789abcdef");
  mod.source.fileSize = 1000;
  return mod;
}

CollectionManifest makeManifest(const QString &name, bool recommendNewProfile,
                                std::vector<CollectionMod> mods) {
  CollectionManifest manifest;
  manifest.info.name = name;
  manifest.info.domainName = QStringLiteral("cyberpunk2077");
  manifest.info.gameVersions = {QStringLiteral("2.3.1.0")};
  manifest.recommendNewProfile = recommendNewProfile;
  manifest.mods = std::move(mods);
  return manifest;
}

CollectionState makeState(QStringList profiles = {QStringLiteral("Default")},
                          const QString &current = QStringLiteral("Default")) {
  CollectionState state;
  state.profiles = std::move(profiles);
  state.currentProfile = current;
  state.gameVersion = QStringLiteral("2.3.1.0");
  return state;
}

CollectionOwner owner(const QString &slug, int revision, int modId,
                      int fileId) {
  CollectionOwner result;
  result.slug = slug;
  result.revision = revision;
  result.modId = modId;
  result.fileId = fileId;
  return result;
}

InstalledMod installed(const QString &folder,
                       std::vector<CollectionOwner> owners,
                       std::set<std::pair<int, int>> files) {
  InstalledMod mod;
  mod.folder = folder;
  mod.owners = std::move(owners);
  mod.installedFiles = std::move(files);
  mod.listedEnabled = true;
  return mod;
}

const PlannedMod *plannedForFile(const CollectionPlan &plan, int fileId) {
  for (const PlannedMod &planned : plan.mods) {
    if (planned.mod != nullptr && planned.mod->source.fileId == fileId) {
      return &planned;
    }
  }
  return nullptr;
}

QStringList itemTexts(const QListWidget *list) {
  QStringList texts;
  for (int i = 0; i < list->count(); ++i) {
    texts << list->item(i)->text();
  }
  return texts;
}

} // namespace

// ---- review dialog: mode group ----

TEST(ReviewDialog, ModeGroupHiddenOnFirstInstall) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10),
                    makeMod(QStringLiteral("Beta"), 2, 20)});

  CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 1000);

  EXPECT_TRUE(dialog.currentPlan().firstInstall);
  EXPECT_FALSE(dialog.modeGroupVisible());
}

TEST(ReviewDialog, ModeGroupShownWhenStopgapFoldersAdoptable) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10),
                    makeMod(QStringLiteral("Beta"), 2, 20)});
  // A folder the earlier script installed: no owner tag, the mod's file
  // recorded, and the folder named like the manifest entry.
  CollectionState state = makeState();
  state.mods.push_back(installed(QStringLiteral("Alpha"), {}, {{1, 10}}));

  CollectionReviewDialog dialog(makeLink(), manifest, state, 1000);

  EXPECT_FALSE(dialog.currentPlan().firstInstall);
  EXPECT_TRUE(dialog.modeGroupVisible());
}

// ---- review dialog: profile defaults ----

TEST(ReviewDialog, NewProfileDefaultWhenRecommendedAndNoProfileOfThatName) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("NCR Core"), true,
                   {makeMod(QStringLiteral("Alpha"), 1, 10)});

  CollectionReviewDialog dialog(makeLink(), manifest,
                                makeState({QStringLiteral("Default")}), 1000);

  EXPECT_TRUE(dialog.newProfileSelected());
  EXPECT_EQ(dialog.selectedProfile().toStdString(), "NCR Core");
  EXPECT_TRUE(dialog.choices().newProfile);
  EXPECT_EQ(dialog.choices().targetProfile.toStdString(), "NCR Core");
}

TEST(ReviewDialog, ExistingProfileDefaultWhenNameCollides) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("NCR Core"), true,
                   {makeMod(QStringLiteral("Alpha"), 1, 10)});

  CollectionReviewDialog dialog(
      makeLink(), manifest,
      makeState({QStringLiteral("Default"), QStringLiteral("NCR Core")}), 1000);

  EXPECT_FALSE(dialog.newProfileSelected());
  EXPECT_EQ(dialog.selectedProfile().toStdString(), "NCR Core");
  const auto *radio = dialog.findChild<QRadioButton *>("newProfileRadio");
  ASSERT_NE(radio, nullptr);
  EXPECT_FALSE(radio->isEnabled())
      << "a second profile of that name is not offered";
}

TEST(ReviewDialog, CurrentProfileDefaultWhenNotRecommended) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("NCR Extras"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10)});

  CollectionReviewDialog dialog(
      makeLink(), manifest,
      makeState({QStringLiteral("Default"), QStringLiteral("Other")},
                QStringLiteral("Other")),
      1000);

  EXPECT_FALSE(dialog.newProfileSelected());
  EXPECT_EQ(dialog.selectedProfile().toStdString(), "Other");
  const auto *combo = dialog.findChild<QComboBox *>("profileCombo");
  ASSERT_NE(combo, nullptr);
  EXPECT_EQ(combo->count(), 2);
}

// ---- review dialog: content ----

TEST(ReviewDialog, OptionalModsStartUnticked) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10),
                    makeMod(QStringLiteral("Opt One"), 3, 30, true),
                    makeMod(QStringLiteral("Opt Two"), 4, 40, true)});

  CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 1000);

  const auto *list = dialog.findChild<QListWidget *>("optionalList");
  ASSERT_NE(list, nullptr);
  ASSERT_EQ(list->count(), 2);
  EXPECT_EQ(list->item(0)->checkState(), Qt::Unchecked);
  EXPECT_EQ(list->item(1)->checkState(), Qt::Unchecked);
  EXPECT_TRUE(dialog.choices().optionalFileIds.empty());

  const PlannedMod *optional = plannedForFile(dialog.currentPlan(), 30);
  ASSERT_NE(optional, nullptr);
  EXPECT_EQ(optional->action, PlannedMod::Action::Refused);
}

TEST(ReviewDialog, TickingOptionalReplans) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10),
                    makeMod(QStringLiteral("Opt One"), 3, 30, true)});

  CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 1000);
  const int requestsBefore = dialog.currentPlan().apiRequests;

  auto *list = dialog.findChild<QListWidget *>("optionalList");
  ASSERT_NE(list, nullptr);
  list->item(0)->setCheckState(Qt::Checked);

  EXPECT_EQ(dialog.choices().optionalFileIds, (std::set<int>{30}));
  const PlannedMod *optional = plannedForFile(dialog.currentPlan(), 30);
  ASSERT_NE(optional, nullptr);
  EXPECT_EQ(optional->action, PlannedMod::Action::Install);
  EXPECT_EQ(dialog.currentPlan().apiRequests, requestsBefore + 3);
}

TEST(ReviewDialog, FullSyncListsFoldersKeptEnabledByOtherOwner) {
  // Revision 1 of this collection installed Alpha, Gone and Shared. Revision 2
  // only lists Alpha. Shared is also owned by another collection.
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10)});
  CollectionState state = makeState();
  state.mods.push_back(installed(QStringLiteral("Alpha"),
                                 {owner(QStringLiteral("test-pack"), 1, 1, 10)},
                                 {{1, 10}}));
  state.mods.push_back(installed(QStringLiteral("Gone"),
                                 {owner(QStringLiteral("test-pack"), 1, 5, 50)},
                                 {{5, 50}}));
  state.mods.push_back(
      installed(QStringLiteral("Shared"),
                {owner(QStringLiteral("test-pack"), 1, 6, 60),
                 owner(QStringLiteral("other-pack"), 4, 6, 60)},
                {{6, 60}}));

  CollectionReviewDialog dialog(makeLink(), manifest, state, 1000);

  const auto *list = dialog.findChild<QListWidget *>("droppedList");
  auto *fullSync = dialog.findChild<QRadioButton *>("fullSyncRadio");
  ASSERT_NE(list, nullptr);
  ASSERT_NE(fullSync, nullptr);
  EXPECT_TRUE(dialog.modeGroupVisible());

  // Top up leaves both alone.
  EXPECT_EQ(list->count(), 2);
  for (const QString &text : itemTexts(list)) {
    EXPECT_TRUE(text.contains(QStringLiteral("left as it is")))
        << text.toStdString();
  }

  fullSync->setChecked(true);

  EXPECT_EQ(dialog.choices().mode, PlanChoices::Mode::FullSync);
  const QList<QListWidgetItem *> gone =
      list->findItems(QStringLiteral("Gone"), Qt::MatchStartsWith);
  const QList<QListWidgetItem *> shared =
      list->findItems(QStringLiteral("Shared"), Qt::MatchStartsWith);
  ASSERT_EQ(gone.size(), 1);
  ASSERT_EQ(shared.size(), 1);
  EXPECT_TRUE(gone[0]->text().contains(QStringLiteral("will be disabled")))
      << gone[0]->text().toStdString();
  EXPECT_TRUE(shared[0]->text().contains(QStringLiteral("stays enabled")))
      << shared[0]->text().toStdString();
}

TEST(ReviewDialog, BudgetLineShowsNeededAndAvailable) {
  const CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10),
                    makeMod(QStringLiteral("Beta"), 2, 20)});

  {
    CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 1000);
    EXPECT_EQ(dialog.currentPlan().apiRequests, 6);
    EXPECT_TRUE(
        dialog.budgetText().contains(QStringLiteral("6 requests needed")))
        << dialog.budgetText().toStdString();
    EXPECT_TRUE(dialog.budgetText().contains(QStringLiteral("1000 available")));
    EXPECT_FALSE(dialog.budgetText().contains(QStringLiteral("Not enough")));

    const auto *label = dialog.findChild<QLabel *>("budgetLabel");
    ASSERT_NE(label, nullptr);
    EXPECT_EQ(label->text().toStdString(), dialog.budgetText().toStdString());
  }
  {
    CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 2);
    EXPECT_TRUE(dialog.budgetText().contains(QStringLiteral("Not enough")))
        << dialog.budgetText().toStdString();
  }
}

TEST(ReviewDialog, UnhandledSectionsWarned) {
  CollectionManifest manifest =
      makeManifest(QStringLiteral("Test Pack"), false,
                   {makeMod(QStringLiteral("Alpha"), 1, 10)});
  manifest.unhandledSections = {QStringLiteral("tools")};

  CollectionReviewDialog dialog(makeLink(), manifest, makeState(), 1000);

  const auto *label = dialog.findChild<QLabel *>("warningsLabel");
  ASSERT_NE(label, nullptr);
  ASSERT_FALSE(dialog.currentPlan().warnings.isEmpty());
  EXPECT_FALSE(label->isHidden());
  // The dialog shows what the plan says, verbatim, and the plan names the
  // section.
  for (const QString &warning : dialog.currentPlan().warnings) {
    EXPECT_TRUE(label->text().contains(warning)) << warning.toStdString();
  }
  EXPECT_TRUE(label->text().contains(QStringLiteral("tools")))
      << label->text().toStdString();
}

// ---- results dialog ----

namespace {

CollectionReportRow reportRow(const QString &name, int fileId,
                              CollectionReportRow::Outcome outcome,
                              bool retryable = false,
                              bool folderExists = false) {
  CollectionReportRow row;
  row.name = name;
  row.folder = name;
  row.modId = fileId / 10;
  row.fileId = fileId;
  row.outcome = outcome;
  row.retryable = retryable;
  row.folderExists = folderExists;
  if (outcome == CollectionReportRow::Outcome::Failed) {
    row.reason = QStringLiteral("it failed");
  }
  return row;
}

CollectionReport makeReport() {
  using Outcome = CollectionReportRow::Outcome;
  CollectionReport report;
  report.collectionName = QStringLiteral("Test Pack");
  report.revision = 2;
  report.placed = true;
  report.rows.push_back(
      reportRow(QStringLiteral("Alpha"), 10, Outcome::Installed));
  report.rows.push_back(
      reportRow(QStringLiteral("Beta"), 20, Outcome::Failed, true));
  report.rows.push_back(
      reportRow(QStringLiteral("Gamma"), 30, Outcome::Failed, true, true));
  report.rows.push_back(
      reportRow(QStringLiteral("Delta"), 40, Outcome::Failed, false));
  return report;
}

bool hasCheckbox(const QTableWidget *table, int row, int column) {
  const QTableWidgetItem *item = table->item(row, column);
  return item != nullptr && item->data(Qt::CheckStateRole).isValid() &&
         (item->flags() & Qt::ItemIsUserCheckable);
}

} // namespace

TEST(ResultsDialog, FailedRowsOfferRetry) {
  CollectionResultsDialog dialog(makeReport());

  const auto *table = dialog.findChild<QTableWidget *>("resultsTable");
  ASSERT_NE(table, nullptr);
  ASSERT_EQ(table->rowCount(), 4);

  EXPECT_FALSE(hasCheckbox(table, 0, CollectionResultsDialog::RetryColumn))
      << "an installed mod has nothing to retry";
  EXPECT_TRUE(hasCheckbox(table, 1, CollectionResultsDialog::RetryColumn));
  EXPECT_EQ(table->item(1, CollectionResultsDialog::RetryColumn)->checkState(),
            Qt::Checked)
      << "a failed row that can be retried starts ticked";
  EXPECT_FALSE(hasCheckbox(table, 3, CollectionResultsDialog::RetryColumn))
      << "a failure retrying cannot fix offers no box";

  const auto *retry = dialog.findChild<QPushButton *>("retryButton");
  ASSERT_NE(retry, nullptr);
  EXPECT_TRUE(retry->isEnabled());
  EXPECT_FALSE(dialog.retryRequested());
}

TEST(ResultsDialog, NoRetryButtonWhenNothingCanBeRetried) {
  using Outcome = CollectionReportRow::Outcome;
  CollectionReport report;
  report.collectionName = QStringLiteral("Test Pack");
  report.placed = true;
  report.rows.push_back(
      reportRow(QStringLiteral("Alpha"), 10, Outcome::Installed));
  report.rows.push_back(
      reportRow(QStringLiteral("Delta"), 40, Outcome::Failed, false));

  CollectionResultsDialog dialog(report);

  EXPECT_EQ(dialog.findChild<QPushButton *>("retryButton"), nullptr);
}

TEST(ResultsDialog, FolderExistsRowsOfferReplace) {
  CollectionResultsDialog dialog(makeReport());

  const auto *table = dialog.findChild<QTableWidget *>("resultsTable");
  ASSERT_NE(table, nullptr);

  EXPECT_TRUE(hasCheckbox(table, 2, CollectionResultsDialog::ReplaceColumn));
  EXPECT_EQ(
      table->item(2, CollectionResultsDialog::ReplaceColumn)->checkState(),
      Qt::Unchecked)
      << "replacing a folder is never ticked for the user";
  EXPECT_FALSE(hasCheckbox(table, 1, CollectionResultsDialog::ReplaceColumn));
  EXPECT_FALSE(hasCheckbox(table, 0, CollectionResultsDialog::ReplaceColumn));
}

TEST(ResultsDialog, FolderExistsRowWithoutRetryOffersReplaceOnly) {
  // A row the planner refused because its folder exists: retrying it unchanged
  // would be refused again, so only Replace is offered, and ticking it is
  // enough to retry.
  using Outcome = CollectionReportRow::Outcome;
  CollectionReport report;
  report.collectionName = QStringLiteral("Test Pack");
  report.placed = true;
  report.rows.push_back(
      reportRow(QStringLiteral("Gamma"), 30, Outcome::Failed, false, true));

  CollectionResultsDialog dialog(report);

  const auto *table = dialog.findChild<QTableWidget *>("resultsTable");
  ASSERT_NE(table, nullptr);
  EXPECT_FALSE(hasCheckbox(table, 0, CollectionResultsDialog::RetryColumn));
  EXPECT_TRUE(hasCheckbox(table, 0, CollectionResultsDialog::ReplaceColumn));

  auto *retry = dialog.findChild<QPushButton *>("retryButton");
  ASSERT_NE(retry, nullptr);
  EXPECT_FALSE(retry->isEnabled()) << "nothing is ticked yet";

  dialog.setRowChecked(0, false, true);
  EXPECT_TRUE(retry->isEnabled());
  EXPECT_EQ(dialog.replaceFileIds(), (std::set<int>{30}));
  EXPECT_TRUE(dialog.retryFileIds().empty())
      << "the session adds the Replace ids to the retry set";
}

TEST(ResultsDialog, RetryAndReplaceIdsFollowTicks) {
  CollectionResultsDialog dialog(makeReport());

  dialog.setRowChecked(1, true, false);
  dialog.setRowChecked(2, true, true);
  EXPECT_EQ(dialog.retryFileIds(), (std::set<int>{20, 30}));
  EXPECT_EQ(dialog.replaceFileIds(), (std::set<int>{30}));

  dialog.setRowChecked(1, false, false);
  EXPECT_EQ(dialog.retryFileIds(), (std::set<int>{30}));

  // Ticking Replace on a row also ticks its Retry box: replacing without
  // retrying would do nothing.
  dialog.setRowChecked(2, false, false);
  EXPECT_TRUE(dialog.retryFileIds().empty());
  const auto *table = dialog.findChild<QTableWidget *>("resultsTable");
  ASSERT_NE(table, nullptr);
  table->item(2, CollectionResultsDialog::ReplaceColumn)
      ->setCheckState(Qt::Checked);
  EXPECT_EQ(dialog.retryFileIds(), (std::set<int>{30}));
  EXPECT_EQ(dialog.replaceFileIds(), (std::set<int>{30}));

  auto *retry = dialog.findChild<QPushButton *>("retryButton");
  ASSERT_NE(retry, nullptr);
  EXPECT_FALSE(dialog.retryRequested());
  retry->click();
  EXPECT_TRUE(dialog.retryRequested());
}

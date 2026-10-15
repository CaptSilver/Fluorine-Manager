#include "collections/collectionplacementmoves.h"
#include "collections/collectionreport.h"

#include <gtest/gtest.h>

#include <QString>

#include <algorithm>
#include <optional>
#include <ostream>

[[maybe_unused]] static void PrintTo(const QString& text, std::ostream* os)
{
  *os << '"' << text.toStdString() << '"';
}

namespace
{
using Action  = PlannedMod::Action;
using Outcome = CollectionReportRow::Outcome;
using Layout  = std::vector<QString>;

CollectionPlan planWith(bool firstInstall, const std::vector<Action>& actions)
{
  CollectionPlan plan;
  plan.firstInstall = firstInstall;
  for (Action action : actions) {
    PlannedMod planned;
    planned.action = action;
    plan.mods.push_back(planned);
  }
  return plan;
}

// Profile::setModPriority (profile.cpp:724-759) applied to a priority per mod.
void referenceSetModPriority(std::vector<int>& priorityOfMod, int mod, int newPriority)
{
  newPriority = std::clamp(newPriority, 0, static_cast<int>(priorityOfMod.size()) - 1);
  const int oldPriority = priorityOfMod[mod];
  if (newPriority == oldPriority)
    return;
  for (int& priority : priorityOfMod) {
    if (newPriority < oldPriority && priority >= newPriority && priority < oldPriority)
      priority += 1;
    else if (newPriority > oldPriority && priority <= newPriority &&
             priority > oldPriority)
      priority -= 1;
  }
  priorityOfMod[mod] = newPriority;
}

InstalledMod listed(const QString& folder, std::optional<bool> enabled)
{
  InstalledMod mod;
  mod.folder        = folder;
  mod.listedEnabled = enabled;
  return mod;
}

CollectionState stateListing(const std::vector<QString>& enabledFolders)
{
  CollectionState state;
  for (const QString& folder : enabledFolders)
    state.mods.push_back(listed(folder, true));
  return state;
}
}  // namespace

TEST(Placement, FirstInstallPutsManifestOrderOnTopLastEntryHighest)
{
  const CollectionPlan plan =
      planWith(true, {Action::Install, Action::Install, Action::Refused, Action::Install});
  const Layout present = {"M1", "M2", "", "M3"};
  // New folders land above the listed ones, in no useful order.
  const Layout layout = {"Base1", "Base2", "M3", "M1", "M2"};

  const std::vector<PriorityMove> moves = placementMoves(plan, CollectionState{}, present, layout);

  EXPECT_EQ(simulateMoves(layout, moves),
            (Layout{"Base1", "Base2", "M1", "M2", "M3"}));
}

TEST(Placement, FirstInstallMovesUserInstallIntoBlock)
{
  const CollectionPlan plan =
      planWith(true, {Action::Install, Action::AlreadyInstalled, Action::Install});
  const Layout present = {"M1", "Mine", "M2"};
  const Layout layout  = {"Mine", "Base", "M1", "M2"};

  const std::vector<PriorityMove> moves =
      placementMoves(plan, CollectionState{}, present, layout);

  EXPECT_EQ(simulateMoves(layout, moves), (Layout{"Base", "M1", "Mine", "M2"}));
}

TEST(Placement, TopUpNewModLandsAboveNearestPrecedingPresentNeighbour)
{
  const CollectionPlan plan = planWith(
      false, {Action::AlreadyInstalled, Action::AlreadyInstalled, Action::Install,
              Action::AlreadyInstalled});
  const Layout present = {"A", "B", "N", "C"};
  const Layout layout  = {"Base", "A", "B", "C", "Patch", "N"};
  const CollectionState state = stateListing({"A", "B", "C"});

  const std::vector<PriorityMove> moves = placementMoves(plan, state, present, layout);

  EXPECT_EQ(simulateMoves(layout, moves),
            (Layout{"Base", "A", "B", "N", "C", "Patch"}));

  // B was refused and is not in the profile: A is the nearest preceding present mod.
  const Layout withoutB = {"A", "", "N", "C"};
  const Layout layoutNoB = {"Base", "A", "C", "Patch", "N"};
  EXPECT_EQ(simulateMoves(layoutNoB, placementMoves(plan, state, withoutB, layoutNoB)),
            (Layout{"Base", "A", "N", "C", "Patch"}));
}

TEST(Placement, TopUpWithNoPrecedingNeighbourGoesToTopOfBlock)
{
  const CollectionPlan plan = planWith(
      false, {Action::Install, Action::AlreadyInstalled, Action::AlreadyInstalled});
  const Layout present = {"N", "B", "C"};
  const Layout layout  = {"Base", "B", "C", "Patch", "N"};
  const CollectionState state = stateListing({"B", "C"});

  const std::vector<PriorityMove> moves = placementMoves(plan, state, present, layout);

  EXPECT_EQ(simulateMoves(layout, moves), (Layout{"Base", "B", "C", "N", "Patch"}));

  // With no existing block at all the new mods stack on top in manifest order.
  const CollectionPlan fresh = planWith(false, {Action::Install, Action::Install});
  const Layout freshPresent  = {"N1", "N2"};
  const Layout freshLayout   = {"Base", "Other", "N2", "N1"};
  EXPECT_EQ(simulateMoves(freshLayout, placementMoves(fresh, CollectionState{}, freshPresent, freshLayout)),
            (Layout{"Base", "Other", "N1", "N2"}));
}

TEST(Placement, TopUpLeavesExistingBlockAndUserPatchesUnmoved)
{
  const CollectionPlan plan = planWith(
      false, {Action::AlreadyInstalled, Action::Install, Action::AlreadyInstalled});
  const Layout present = {"A", "N", "C"};
  const Layout layout  = {"Base", "A", "C", "Patch1", "Patch2", "N"};
  const CollectionState state = stateListing({"A", "C"});

  const std::vector<PriorityMove> moves = placementMoves(plan, state, present, layout);

  ASSERT_EQ(moves.size(), 1u);
  EXPECT_EQ(moves[0].folder, "N");
  const Layout result = simulateMoves(layout, moves);
  EXPECT_EQ(result, (Layout{"Base", "A", "N", "C", "Patch1", "Patch2"}));

  // Everything but N keeps its relative order.
  Layout others = result;
  others.erase(std::find(others.begin(), others.end(), QString("N")));
  EXPECT_EQ(others, (Layout{"Base", "A", "C", "Patch1", "Patch2"}));
}

TEST(Placement, ReplayedPlacementOverPlacedBlockMovesNothing)
{
  const CollectionPlan topUp = planWith(
      false, {Action::AlreadyInstalled, Action::Install, Action::AlreadyInstalled});
  const Layout topUpPresent = {"A", "N", "C"};
  const Layout topUpLayout  = {"Base", "A", "C", "Patch", "N"};
  const CollectionState topUpState = stateListing({"A", "C"});
  const Layout topUpPlaced = simulateMoves(
      topUpLayout, placementMoves(topUp, topUpState, topUpPresent, topUpLayout));
  EXPECT_TRUE(placementMoves(topUp, topUpState, topUpPresent, topUpPlaced).empty());

  const CollectionPlan first = planWith(true, {Action::Install, Action::Install});
  const Layout firstPresent  = {"M1", "M2"};
  const Layout firstLayout   = {"Base", "M2", "M1"};
  const Layout firstPlaced = simulateMoves(
      firstLayout, placementMoves(first, CollectionState{}, firstPresent, firstLayout));
  EXPECT_EQ(firstPlaced, (Layout{"Base", "M1", "M2"}));
  EXPECT_TRUE(
      placementMoves(first, CollectionState{}, firstPresent, firstPlaced).empty());
}

TEST(Placement, TopUpIntoProfileThatListsNoneOfTheCollectionPlacesManifestOrder)
{
  const CollectionPlan plan =
      planWith(false, {Action::AlreadyInstalled, Action::Adopt, Action::AlreadyInstalled,
                       Action::Adopt});
  const Layout present = {"M1", "M2", "M3", "M4"};
  // The folders sit among other mods, out of manifest order, and none is listed.
  const Layout layout = {"M3", "Other1", "M1", "M4", "Other2", "M2"};

  const std::vector<PriorityMove> moves =
      placementMoves(plan, CollectionState{}, present, layout);

  EXPECT_EQ(simulateMoves(layout, moves),
            (Layout{"Other1", "Other2", "M1", "M2", "M3", "M4"}));
}

TEST(Placement, TopUpMovesUnlistedPresentModButNotListedOne)
{
  const CollectionPlan plan = planWith(
      false, {Action::AlreadyInstalled, Action::AlreadyInstalled, Action::AlreadyInstalled});
  const Layout present = {"A", "B", "C"};
  const Layout layout  = {"B", "Patch", "A", "C"};
  CollectionState state;
  state.mods = {listed("A", true), listed("B", std::nullopt)};  // C is not in state at all

  const std::vector<PriorityMove> moves = placementMoves(plan, state, present, layout);

  // A stays where it is; B and C are movers and land above A in manifest order.
  EXPECT_EQ(simulateMoves(layout, moves), (Layout{"Patch", "A", "B", "C"}));
  for (const PriorityMove& move : moves)
    EXPECT_NE(move.folder, "A");
}

TEST(Report, ReplaceThatDidNotRunIsPlacedAsAlreadyInstalled)
{
  const CollectionPlan plan = planWith(
      false, {Action::Replace, Action::Replace, Action::Install, Action::Adopt});
  CollectionReport report;
  for (Outcome outcome : {Outcome::Replaced, Outcome::Failed, Outcome::Installed,
                          Outcome::Adopted}) {
    CollectionReportRow row;
    row.outcome = outcome;
    report.rows.push_back(row);
  }

  const CollectionPlan placed = planAsRun(plan, report);

  ASSERT_EQ(placed.mods.size(), 4u);
  EXPECT_EQ(placed.mods[0].action, Action::Replace);
  EXPECT_EQ(placed.mods[1].action, Action::AlreadyInstalled);
  EXPECT_EQ(placed.mods[2].action, Action::Install);
  EXPECT_EQ(placed.mods[3].action, Action::Adopt);
  EXPECT_EQ(plan.mods[1].action, Action::Replace);  // the input is untouched
}

TEST(Placement, SimulateMatchesSetModPriorityShifting)
{
  const Layout start = {"A", "B", "C", "D", "E"};
  const std::vector<PriorityMove> moves = {{"E", 0}, {"A", 3}, {"C", 99},
                                           {"B", 2}, {"D", -4}, {"D", 2}};

  std::vector<int> priority = {0, 1, 2, 3, 4};  // priority of A..E
  for (const PriorityMove& move : moves)
    referenceSetModPriority(priority, move.folder.at(0).unicode() - u'A',
                            move.newPriority);
  Layout expected(start.size());
  for (std::size_t mod = 0; mod < start.size(); ++mod)
    expected[priority[mod]] = start[mod];

  EXPECT_EQ(simulateMoves(start, moves), expected);
}

TEST(Placement, EnableSetKeepsListedDisabledMod)
{
  CollectionState state;
  state.mods = {listed("Off", false), listed("On", true)};
  const CollectionPlan plan =
      planWith(false, {Action::AlreadyInstalled, Action::AlreadyInstalled});

  const QStringList enable = placementEnableSet(plan, state, {"Off", "On"});

  // Both are listed, so both keep their + or -; nothing is switched.
  EXPECT_TRUE(enable.isEmpty());
}

TEST(Placement, EnableSetIncludesInstalledAndUnlisted)
{
  CollectionState state;
  state.mods = {listed("Rep", false), listed("Unlisted", std::nullopt),
                listed("Gone", true)};
  const CollectionPlan plan = planWith(
      false, {Action::Install, Action::Replace, Action::Adopt, Action::Refused});

  const QStringList enable =
      placementEnableSet(plan, state, {"New", "Rep", "Unlisted", ""});

  EXPECT_EQ(enable, (QStringList{"New", "Rep", "Unlisted"}));
}

TEST(Report, SeedMarksRefusedRowsWithReasons)
{
  CollectionManifest manifest;
  manifest.info.name = "Core";
  for (int i = 1; i <= 6; ++i) {
    CollectionMod mod;
    mod.name            = QString("Mod %1").arg(i);
    mod.source.modId    = i;
    mod.source.fileId   = i * 10;
    manifest.mods.push_back(mod);
  }
  CollectionLink link;
  link.slug     = "core";
  link.revision = 7;

  CollectionPlan plan;
  plan.droppedFolders = {"Gone"};
  plan.toDisable      = {"Gone"};
  const std::vector<std::pair<Action, QString>> actions = {
      {Action::Install, {}},
      {Action::AlreadyInstalled, {}},
      {Action::Adopt, {}},
      {Action::Refused, PlanReason::OptionalNotSelected},
      {Action::Refused, PlanReason::FolderExists},
      {Action::Refused, QString("installed by collection extras")}};
  for (std::size_t i = 0; i < actions.size(); ++i) {
    PlannedMod planned;
    planned.mod    = &manifest.mods[i];
    planned.folder = QString("Folder %1").arg(i + 1);
    planned.action = actions[i].first;
    planned.reason = actions[i].second;
    plan.mods.push_back(planned);
  }

  const CollectionReport report = seedReport(manifest, link, plan);

  EXPECT_EQ(report.collectionName, "Core");
  EXPECT_EQ(report.revision, 7);
  EXPECT_EQ(report.droppedFolders, std::vector<QString>{"Gone"});
  EXPECT_EQ(report.disabledFolders, std::vector<QString>{"Gone"});
  EXPECT_FALSE(report.cancelled);
  EXPECT_FALSE(report.placed);
  ASSERT_EQ(report.rows.size(), 6u);

  EXPECT_EQ(report.rows[0].name, "Mod 1");
  EXPECT_EQ(report.rows[0].folder, "Folder 1");
  EXPECT_EQ(report.rows[0].modId, 1);
  EXPECT_EQ(report.rows[0].fileId, 10);
  EXPECT_EQ(report.rows[0].outcome, Outcome::Skipped);
  EXPECT_FALSE(report.rows[0].retryable);
  EXPECT_EQ(report.rows[1].outcome, Outcome::AlreadyInstalled);
  EXPECT_EQ(report.rows[2].outcome, Outcome::Adopted);
  EXPECT_EQ(report.rows[3].outcome, Outcome::Skipped);
  EXPECT_EQ(report.rows[3].reason, PlanReason::OptionalNotSelected);
  EXPECT_FALSE(report.rows[3].folderExists);
  EXPECT_EQ(report.rows[4].outcome, Outcome::Failed);
  EXPECT_EQ(report.rows[4].reason, PlanReason::FolderExists);
  EXPECT_TRUE(report.rows[4].folderExists);
  EXPECT_FALSE(report.rows[4].retryable);
  EXPECT_EQ(report.rows[5].outcome, Outcome::Failed);
  EXPECT_EQ(report.rows[5].reason, "installed by collection extras");
  EXPECT_FALSE(report.rows[5].folderExists);
}

TEST(Report, RetryAndReplaceIdsFromTickedRows)
{
  CollectionReport report;
  auto addRow = [&](int fileId, Outcome outcome, bool retryable, bool folderExists) {
    CollectionReportRow row;
    row.fileId       = fileId;
    row.outcome      = outcome;
    row.retryable    = retryable;
    row.folderExists = folderExists;
    report.rows.push_back(row);
  };
  addRow(100, Outcome::Failed, true, false);
  addRow(200, Outcome::Failed, false, true);
  addRow(300, Outcome::Installed, false, false);
  addRow(400, Outcome::Failed, true, false);

  EXPECT_EQ(retryFileIds(report, {0, 1, 2, 9}), (std::set<int>{100}));
  EXPECT_EQ(replaceFileIds(report, {0, 1, 2, 9}), (std::set<int>{200}));
  EXPECT_EQ(retryFileIds(report, {0, 3}), (std::set<int>{100, 400}));
  EXPECT_TRUE(replaceFileIds(report, {}).empty());
}

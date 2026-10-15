#include "collectionreport.h"

namespace
{
using Action  = PlannedMod::Action;
using Outcome = CollectionReportRow::Outcome;

CollectionReportRow rowFor(const PlannedMod& planned)
{
  CollectionReportRow row;
  row.name   = planned.mod->name;
  row.folder = planned.folder;
  row.modId  = planned.mod->source.modId;
  row.fileId = planned.mod->source.fileId;

  switch (planned.action) {
  case Action::AlreadyInstalled:
    row.outcome = Outcome::AlreadyInstalled;
    break;
  case Action::Adopt:
    row.outcome = Outcome::Adopted;
    break;
  case Action::Install:
  case Action::Replace:
    row.outcome = Outcome::Skipped;
    row.reason  = QStringLiteral("not processed");
    break;
  case Action::Refused:
    row.reason = planned.reason;
    if (planned.reason == PlanReason::OptionalNotSelected) {
      row.outcome = Outcome::Skipped;
    } else {
      row.outcome      = Outcome::Failed;
      row.folderExists = planned.reason == PlanReason::FolderExists;
    }
    break;
  }
  return row;
}

std::set<int> fileIdsOfTicked(const CollectionReport& report, const std::set<int>& ticked,
                              bool CollectionReportRow::*flag)
{
  std::set<int> ids;
  for (int index : ticked) {
    if (index < 0 || index >= static_cast<int>(report.rows.size()))
      continue;
    const CollectionReportRow& row = report.rows[index];
    if (row.*flag)
      ids.insert(row.fileId);
  }
  return ids;
}
}  // namespace

CollectionReport seedReport(const CollectionManifest& manifest, const CollectionLink& link,
                            const CollectionPlan& plan)
{
  CollectionReport report;
  report.collectionName = manifest.info.name;
  report.revision       = link.revision;
  report.droppedFolders = plan.droppedFolders;
  report.disabledFolders = plan.toDisable;
  for (const PlannedMod& planned : plan.mods)
    report.rows.push_back(rowFor(planned));
  return report;
}

CollectionPlan planAsRun(const CollectionPlan& plan, const CollectionReport& report)
{
  CollectionPlan asRun = plan;
  for (std::size_t i = 0; i < asRun.mods.size() && i < report.rows.size(); ++i) {
    if (asRun.mods[i].action == Action::Replace &&
        report.rows[i].outcome != Outcome::Replaced)
      asRun.mods[i].action = Action::AlreadyInstalled;
  }
  return asRun;
}

std::set<int> retryFileIds(const CollectionReport& report, const std::set<int>& tickedRows)
{
  return fileIdsOfTicked(report, tickedRows, &CollectionReportRow::retryable);
}

std::set<int> replaceFileIds(const CollectionReport& report, const std::set<int>& tickedRows)
{
  return fileIdsOfTicked(report, tickedRows, &CollectionReportRow::folderExists);
}

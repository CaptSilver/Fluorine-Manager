#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"
#include "collectionplan.h"

#include <QString>
#include <QStringList>

#include <set>
#include <vector>

struct CollectionReportRow
{
  enum class Outcome
  {
    Installed,
    Replaced,
    Adopted,
    AlreadyInstalled,
    Skipped,
    Failed
  };
  QString name;
  QString folder;
  int modId = 0;
  int fileId = 0;
  Outcome outcome = Outcome::Skipped;
  QString reason;
  QStringList notes;  // "needs a look": FOMOD warnings, layout notes
  bool retryable = false;
  bool folderExists = false;  // offers Replace
};

struct CollectionReport
{
  QString collectionName;
  int revision = 0;
  std::vector<CollectionReportRow> rows;
  std::vector<QString> droppedFolders;
  bool cancelled = false;
  bool placed = false;
  QString sessionError;
};

// One row per plan entry, in manifest order. Refused entries carry their reason;
// entries the run still has to install start as Skipped and the runner overwrites
// them as each mod settles, so whatever a cancel leaves behind reads as skipped.
CollectionReport seedReport(const CollectionManifest& manifest, const CollectionLink& link,
                            const CollectionPlan& plan);

// The plan as the run left it, for placement: a Replace whose row did not end as
// Replaced never touched the existing folder, so it counts as already installed
// and is not moved or switched on.
CollectionPlan planAsRun(const CollectionPlan& plan, const CollectionReport& report);

// File ids of the ticked rows (indices into report.rows; out-of-range ones are ignored)
// that can be retried, i.e. whose row is marked retryable.
std::set<int> retryFileIds(const CollectionReport& report, const std::set<int>& tickedRows);

// File ids of the ticked rows whose existing folder may be overwritten.
std::set<int> replaceFileIds(const CollectionReport& report, const std::set<int>& tickedRows);

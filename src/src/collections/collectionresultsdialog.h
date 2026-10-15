#pragma once

#include "collectionreport.h"

#include <QDialog>

#include <set>

class QPushButton;
class QTableWidget;
class QTableWidgetItem;

// The report at the end of a run: one row per mod in report order, with Retry
// and Replace checkboxes where a row offers them. A row offers Retry when it
// can be retried as it is (row.retryable) and Replace when its folder already
// exists (row.folderExists). Retry is ticked for failed rows that offer it;
// Replace (delete the existing folder and install over it) is never ticked for
// the user. Ticking Replace also ticks Retry where the row has one, and a
// Replace tick alone is enough to ask for a retry: the session adds the Replace
// ids to the retry set.
class CollectionResultsDialog : public QDialog {
  Q_OBJECT

public:
  enum Column {
    NameColumn,
    ResultColumn,
    DetailsColumn,
    RetryColumn,
    ReplaceColumn,
    ColumnCount
  };

  CollectionResultsDialog(const CollectionReport &report,
                          QWidget *parent = nullptr);

  // True when the dialog was closed with the Retry button.
  bool retryRequested() const;

  // File ids of the ticked rows, through retryFileIds()/replaceFileIds() of the
  // report.
  std::set<int> retryFileIds() const;
  std::set<int> replaceFileIds() const;

  // Sets both boxes of a report row. A box the row does not offer is left
  // alone.
  void setRowChecked(int row, bool retry, bool replace);

private:
  std::set<int> tickedRows(int column) const;
  void rowChanged(QTableWidgetItem *item);
  void refreshRetryButton();

  CollectionReport m_report;
  QTableWidget *m_table = nullptr;
  QPushButton *m_retryButton = nullptr;
  bool m_retryRequested = false;
  bool m_updating = false;
};

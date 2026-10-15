#include "collectionresultsdialog.h"

#include <QFont>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QPushButton>
#include <QTableWidget>
#include <QVBoxLayout>

namespace {

using Outcome = CollectionReportRow::Outcome;

QString outcomeText(Outcome outcome) {
  switch (outcome) {
  case Outcome::Installed:
    return QObject::tr("Installed");
  case Outcome::Replaced:
    return QObject::tr("Replaced");
  case Outcome::Adopted:
    return QObject::tr("Adopted");
  case Outcome::AlreadyInstalled:
    return QObject::tr("Already installed");
  case Outcome::Skipped:
    return QObject::tr("Skipped");
  case Outcome::Failed:
    return QObject::tr("Failed");
  }
  return {};
}

QTableWidgetItem *textItem(const QString &text) {
  auto *item = new QTableWidgetItem(text);
  item->setFlags(Qt::ItemIsEnabled);
  return item;
}

// A cell with a checkbox, or an empty cell with no box when the row does not
// offer it.
QTableWidgetItem *checkItem(bool offered, bool ticked) {
  auto *item = new QTableWidgetItem;
  if (offered) {
    item->setFlags(Qt::ItemIsUserCheckable | Qt::ItemIsEnabled);
    item->setCheckState(ticked ? Qt::Checked : Qt::Unchecked);
  } else {
    item->setFlags(Qt::NoItemFlags);
  }
  return item;
}

bool hasCheckbox(const QTableWidgetItem *item) {
  return item != nullptr && item->data(Qt::CheckStateRole).isValid();
}

} // namespace

CollectionResultsDialog::CollectionResultsDialog(const CollectionReport &report,
                                                 QWidget *parent)
    : QDialog(parent), m_report(report) {
  setWindowTitle(tr("Collection results"));
  resize(900, 600);

  auto *root = new QVBoxLayout(this);

  auto *title = new QLabel(
      tr("%1, revision %2").arg(report.collectionName).arg(report.revision),
      this);
  QFont titleFont = title->font();
  titleFont.setBold(true);
  title->setFont(titleFont);
  root->addWidget(title);

  QStringList notices;
  if (report.cancelled) {
    notices << tr(
        "The run was cancelled. Mod order and enabled state were not changed. "
        "Run the link again to finish.");
  }
  if (!report.sessionError.isEmpty()) {
    notices << report.sessionError;
  }
  if (!report.cancelled && !report.placed && report.sessionError.isEmpty()) {
    notices << tr("Mod order and enabled state were not changed.");
  }
  if (!notices.isEmpty()) {
    auto *banner = new QLabel(notices.join(QLatin1Char('\n')), this);
    banner->setObjectName(QStringLiteral("bannerLabel"));
    banner->setWordWrap(true);
    root->addWidget(banner);
  }

  int counts[6] = {0, 0, 0, 0, 0, 0};
  for (const CollectionReportRow &row : report.rows) {
    ++counts[static_cast<int>(row.outcome)];
  }
  auto *summary =
      new QLabel(tr("Installed %1, replaced %2, adopted %3, already installed "
                    "%4, skipped %5, failed %6.")
                     .arg(counts[static_cast<int>(Outcome::Installed)])
                     .arg(counts[static_cast<int>(Outcome::Replaced)])
                     .arg(counts[static_cast<int>(Outcome::Adopted)])
                     .arg(counts[static_cast<int>(Outcome::AlreadyInstalled)])
                     .arg(counts[static_cast<int>(Outcome::Skipped)])
                     .arg(counts[static_cast<int>(Outcome::Failed)]),
                 this);
  summary->setObjectName(QStringLiteral("summaryLabel"));
  summary->setWordWrap(true);
  root->addWidget(summary);

  m_table =
      new QTableWidget(static_cast<int>(report.rows.size()), ColumnCount, this);
  m_table->setObjectName(QStringLiteral("resultsTable"));
  m_table->setHorizontalHeaderLabels(
      {tr("Mod"), tr("Result"), tr("Details"), tr("Retry"), tr("Replace")});
  m_table->setSelectionMode(QAbstractItemView::NoSelection);
  m_table->setEditTriggers(QAbstractItemView::NoEditTriggers);
  m_table->verticalHeader()->setVisible(false);
  m_table->horizontalHeader()->setSectionResizeMode(DetailsColumn,
                                                    QHeaderView::Stretch);
  m_table->horizontalHeader()->setSectionResizeMode(
      NameColumn, QHeaderView::ResizeToContents);

  bool anythingToRetry = false;
  for (int r = 0; r < static_cast<int>(report.rows.size()); ++r) {
    const CollectionReportRow &row = report.rows[static_cast<std::size_t>(r)];

    auto *name = textItem(row.name.isEmpty() ? row.folder : row.name);
    name->setToolTip(tr("Folder: %1\nMod %2, file %3")
                         .arg(row.folder)
                         .arg(row.modId)
                         .arg(row.fileId));
    auto *result = textItem(outcomeText(row.outcome));
    if (row.outcome == Outcome::Failed) {
      QFont bold = result->font();
      bold.setBold(true);
      result->setFont(bold);
    }

    QStringList details;
    if (!row.reason.isEmpty()) {
      details << row.reason;
    }
    if (!row.notes.isEmpty()) {
      details
          << tr("Needs a look: %1").arg(row.notes.join(QStringLiteral("; ")));
    }
    auto *detail = textItem(details.join(QStringLiteral(" ")));
    detail->setToolTip(details.join(QLatin1Char('\n')));

    // A "folder exists" refusal is not retryable as it stands (the same install
    // would be refused again), so Retry is only offered when the row can
    // succeed unchanged.
    const bool offersRetry = row.retryable;
    const bool offersReplace = row.folderExists;
    anythingToRetry = anythingToRetry || offersRetry || offersReplace;

    m_table->setItem(r, NameColumn, name);
    m_table->setItem(r, ResultColumn, result);
    m_table->setItem(r, DetailsColumn, detail);
    m_table->setItem(
        r, RetryColumn,
        checkItem(offersRetry, offersRetry && row.outcome == Outcome::Failed));
    m_table->setItem(r, ReplaceColumn, checkItem(offersReplace, false));
  }
  root->addWidget(m_table, 1);

  if (!report.droppedFolders.empty()) {
    QStringList folders;
    for (const QString &folder : report.droppedFolders) {
      folders << folder;
    }
    auto *dropped = new QLabel(
        tr("Left as they were, because this revision no longer lists them: %1")
            .arg(folders.join(QStringLiteral(", "))),
        this);
    dropped->setObjectName(QStringLiteral("droppedLabel"));
    dropped->setWordWrap(true);
    root->addWidget(dropped);
  }

  auto *buttons = new QHBoxLayout;
  buttons->addStretch(1);
  if (anythingToRetry) {
    m_retryButton = new QPushButton(tr("Retry ticked"), this);
    m_retryButton->setObjectName(QStringLiteral("retryButton"));
    connect(m_retryButton, &QPushButton::clicked, this, [this] {
      m_retryRequested = true;
      accept();
    });
    buttons->addWidget(m_retryButton);
  }
  auto *closeButton = new QPushButton(tr("Close"), this);
  closeButton->setObjectName(QStringLiteral("closeButton"));
  connect(closeButton, &QPushButton::clicked, this, &QDialog::reject);
  buttons->addWidget(closeButton);
  root->addLayout(buttons);

  connect(m_table, &QTableWidget::itemChanged, this,
          &CollectionResultsDialog::rowChanged);
  refreshRetryButton();
}

bool CollectionResultsDialog::retryRequested() const {
  return m_retryRequested;
}

std::set<int> CollectionResultsDialog::retryFileIds() const {
  // Qualified: inside the class the unqualified name is this member function.
  return ::retryFileIds(m_report, tickedRows(RetryColumn));
}

std::set<int> CollectionResultsDialog::replaceFileIds() const {
  return ::replaceFileIds(m_report, tickedRows(ReplaceColumn));
}

void CollectionResultsDialog::setRowChecked(int row, bool retry, bool replace) {
  if (row < 0 || row >= m_table->rowCount()) {
    return;
  }
  m_updating = true;
  if (QTableWidgetItem *item = m_table->item(row, RetryColumn);
      hasCheckbox(item)) {
    item->setCheckState(retry ? Qt::Checked : Qt::Unchecked);
  }
  if (QTableWidgetItem *item = m_table->item(row, ReplaceColumn);
      hasCheckbox(item)) {
    item->setCheckState(replace ? Qt::Checked : Qt::Unchecked);
  }
  m_updating = false;
  refreshRetryButton();
}

std::set<int> CollectionResultsDialog::tickedRows(int column) const {
  std::set<int> rows;
  for (int r = 0; r < m_table->rowCount(); ++r) {
    const QTableWidgetItem *item = m_table->item(r, column);
    if (hasCheckbox(item) && item->checkState() == Qt::Checked) {
      rows.insert(r);
    }
  }
  return rows;
}

void CollectionResultsDialog::rowChanged(QTableWidgetItem *item) {
  if (m_updating) {
    return;
  }
  m_updating = true;
  QTableWidgetItem *retry = m_table->item(item->row(), RetryColumn);
  QTableWidgetItem *replace = m_table->item(item->row(), ReplaceColumn);
  if (item->column() == ReplaceColumn && item->checkState() == Qt::Checked &&
      hasCheckbox(retry)) {
    retry->setCheckState(Qt::Checked);
  }
  if (item->column() == RetryColumn && item->checkState() == Qt::Unchecked &&
      hasCheckbox(replace)) {
    replace->setCheckState(Qt::Unchecked);
  }
  m_updating = false;
  refreshRetryButton();
}

void CollectionResultsDialog::refreshRetryButton() {
  if (m_retryButton != nullptr) {
    m_retryButton->setEnabled(!tickedRows(RetryColumn).empty() ||
                              !tickedRows(ReplaceColumn).empty());
  }
}

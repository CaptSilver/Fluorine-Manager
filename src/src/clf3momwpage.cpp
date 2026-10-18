#include "clf3momwpage.h"
#include "clf3processenvironment.h"

#include <QComboBox>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSplitter>
#include <QTreeWidget>
#include <QVBoxLayout>

namespace
{
QString humanize(QString name)
{
  return name.replace('_', ' ');
}
}  // namespace

Clf3MomwPage::Clf3MomwPage(QWidget* parent) : QWidget(parent)
{
  setObjectName("clf3MomwPage");
  buildUi();

  connect(&m_engine, &Clf3EngineManager::ready, this, [this](const QString& path) {
    m_enginePath = path;
    reviewSelected();
  });
  connect(&m_engine, &Clf3EngineManager::statusChanged, this,
          [this](const QString& message) { m_status->setText(message); });
  connect(&m_engine, &Clf3EngineManager::failed, this, [this](const QString& message) {
    setBusy(false, tr("Could not prepare CLF3: %1").arg(message));
  });

  connect(&m_process, &QProcess::readyReadStandardOutput, this,
          [this] { m_output += m_process.readAllStandardOutput(); });
  connect(&m_process, &QProcess::readyReadStandardError, this, [this] {
    m_errors += m_process.readAllStandardError();
    if (m_errors.size() > 65536)
      m_errors = m_errors.right(65536);
  });
  connect(&m_process, &QProcess::errorOccurred, this,
          [this](QProcess::ProcessError error) {
            if (error == QProcess::FailedToStart)
              setBusy(false, tr("CLF3 could not start: %1").arg(m_process.errorString()));
          });
  connect(&m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
          &Clf3MomwPage::finished);
}

Clf3MomwPage::~Clf3MomwPage()
{
  m_engine.cancel();
  if (m_process.state() != QProcess::NotRunning)
    m_process.kill();
}

void Clf3MomwPage::buildUi()
{
  auto* layout = new QVBoxLayout(this);

  auto* heading = new QLabel(tr("Install an OpenMW modlist"));
  QFont headingFont = heading->font();
  headingFont.setPointSize(headingFont.pointSize() + 5);
  headingFont.setBold(true);
  heading->setFont(headingFont);
  layout->addWidget(heading);

  auto* intro = new QLabel(
      tr("Modlists published by modding-openmw.com for OpenMW. Review shows the mods, "
         "load order and tools a list needs, using only public site data."));
  intro->setWordWrap(true);
  intro->setProperty("secondary", true);
  layout->addWidget(intro);

  auto* row = new QHBoxLayout;
  row->addWidget(new QLabel(tr("List:")));
  m_lists = new QComboBox;
  m_lists->setMinimumWidth(280);
  m_lists->setEnabled(false);
  row->addWidget(m_lists);
  m_review = new QPushButton(tr("Review"));
  m_review->setEnabled(false);
  row->addWidget(m_review);
  m_refresh = new QPushButton(tr("Refresh lists"));
  row->addWidget(m_refresh);
  row->addStretch(1);
  layout->addLayout(row);

  m_status = new QLabel;
  m_status->setWordWrap(true);
  m_status->setProperty("secondary", true);
  layout->addWidget(m_status);

  m_summary = new QLabel;
  m_summary->setTextFormat(Qt::PlainText);
  m_summary->setWordWrap(true);
  layout->addWidget(m_summary);

  auto* split = new QSplitter(Qt::Horizontal);
  m_entries   = new QTreeWidget;
  m_entries->setRootIsDecorated(false);
  m_entries->setUniformRowHeights(true);
  m_entries->setAlternatingRowColors(true);
  m_entries->setHeaderLabels({tr("#"), tr("Fluorine mod"), tr("Folder")});
  m_entries->header()->setStretchLastSection(true);
  m_entries->setColumnWidth(0, 56);
  m_entries->setColumnWidth(1, 380);
  split->addWidget(m_entries);
  m_notes = new QPlainTextEdit;
  m_notes->setReadOnly(true);
  m_notes->setPlaceholderText(tr("Tools, warnings and blockers appear here."));
  split->addWidget(m_notes);
  split->setStretchFactor(0, 3);
  split->setStretchFactor(1, 2);
  layout->addWidget(split, 1);

  auto* footer = new QHBoxLayout;
  auto* install = new QPushButton(tr("Install"));
  install->setEnabled(false);
  install->setToolTip(tr("Installing OpenMW lists is not available yet."));
  footer->addWidget(install);
  auto* note = new QLabel(tr("Installing is not available yet. This tab is a preview."));
  note->setProperty("secondary", true);
  footer->addWidget(note, 1);
  layout->addLayout(footer);

  connect(m_review, &QPushButton::clicked, this, &Clf3MomwPage::reviewSelected);
  connect(m_lists, &QComboBox::activated, this, [this] { reviewSelected(); });
  connect(m_refresh, &QPushButton::clicked, this, [this] {
    m_listsLoaded = false;
    clearPlan();
    ensureEngine();
  });
}

void Clf3MomwPage::showEvent(QShowEvent* event)
{
  QWidget::showEvent(event);
  if (!m_started) {
    m_started = true;
    ensureEngine();
  }
}

void Clf3MomwPage::ensureEngine()
{
  if (m_process.state() != QProcess::NotRunning)
    return;
  const QString override = qEnvironmentVariable("FLUORINE_CLF3_PATH");
  if (!override.isEmpty())
    m_enginePath = QFileInfo(override).absoluteFilePath();
  if (!m_enginePath.isEmpty()) {
    reviewSelected();
    return;
  }
  setBusy(true, tr("Preparing CLF3…"));
  m_engine.prepare();
}

void Clf3MomwPage::reviewSelected()
{
  if (m_enginePath.isEmpty()) {
    ensureEngine();
    return;
  }
  if (!m_listsLoaded) {
    run(Mode::Lists, {"momw", "lists"});
    return;
  }
  const QString slug = m_lists->currentData().toString();
  if (slug.isEmpty())
    return;
  run(Mode::Plan, {"momw", "plan", slug, "--json"});
}

void Clf3MomwPage::run(Mode mode, const QStringList& arguments)
{
  if (m_process.state() != QProcess::NotRunning)
    return;
  m_mode = mode;
  m_output.clear();
  m_errors.clear();
  if (mode == Mode::Plan)
    clearPlan();
  setBusy(true, mode == Mode::Lists ? tr("Loading the list of OpenMW modlists…")
                                    : tr("Reviewing %1…").arg(m_lists->currentText()));
  m_process.setProcessEnvironment(clf3EngineEnvironment());
  m_process.start(m_enginePath, arguments);
}

void Clf3MomwPage::setBusy(bool busy, const QString& message)
{
  m_review->setEnabled(!busy && m_lists->count() > 0);
  m_lists->setEnabled(!busy && m_lists->count() > 0);
  m_refresh->setEnabled(!busy);
  if (!message.isEmpty() || !busy)
    m_status->setText(message);
}

void Clf3MomwPage::finished(int exitCode, QProcess::ExitStatus status)
{
  m_output += m_process.readAllStandardOutput();
  const Mode mode = m_mode;
  m_mode          = Mode::None;
  if (mode == Mode::None)
    return;
  if (status != QProcess::NormalExit || exitCode != 0) {
    const QString details = QString::fromUtf8(m_errors).trimmed();
    // clap rejects an unknown subcommand with exit code 2.
    if (details.contains("unrecognized subcommand"))
      setBusy(false, tr("This CLF3 engine does not support OpenMW lists yet. Update CLF3, "
                        "or point FLUORINE_CLF3_PATH at a newer build."));
    else
      setBusy(false, tr("CLF3 failed (exit code %1). %2")
                         .arg(exitCode)
                         .arg(details.left(300)));
    return;
  }
  if (mode == Mode::Lists)
    showLists(m_output);
  else
    showPlan(m_output);
}

void Clf3MomwPage::showLists(const QByteArray& text)
{
  const QString previous = m_lists->currentData().toString();
  m_lists->clear();
  QStringList slugs;
  for (const QString& line : QString::fromUtf8(text).split('\n', Qt::SkipEmptyParts))
    slugs << line.trimmed();
  for (const QString& slug : slugs)
    m_lists->addItem(humanize(QString(slug).replace('-', ' ')), slug);
  if (slugs.isEmpty()) {
    setBusy(false, tr("modding-openmw.com returned no lists."));
    return;
  }
  m_listsLoaded = true;
  int index     = m_lists->findData(previous);
  if (index < 0)
    index = m_lists->findData(QStringLiteral("total-overhaul"));
  m_lists->setCurrentIndex(qMax(0, index));
  setBusy(false, {});
  reviewSelected();
}

void Clf3MomwPage::clearPlan()
{
  m_entries->clear();
  m_notes->clear();
  m_summary->clear();
}

void Clf3MomwPage::showPlan(const QByteArray& json)
{
  QJsonParseError error;
  const auto document = QJsonDocument::fromJson(json, &error);
  if (error.error != QJsonParseError::NoError || !document.isObject()) {
    setBusy(false, tr("CLF3 returned an unreadable plan. Update CLF3 and retry."));
    return;
  }
  const QJsonObject plan    = document.object();
  const QJsonObject summary = plan.value("summary").toObject();

  QStringList sources;
  const QJsonObject bySource = summary.value("sources").toObject();
  for (auto it = bySource.begin(); it != bySource.end(); ++it)
    sources << QStringLiteral("%1 %2").arg(humanize(it.key())).arg(it.value().toInt());

  m_summary->setText(
      tr("%1 mods · %2 archives (%3) · %4 Fluorine mods (%5 nested) · %6 plugins · "
         "%7 groundcover")
          .arg(summary.value("mods").toInt())
          .arg(summary.value("archives").toInt())
          .arg(sources.join(", "))
          .arg(summary.value("data_entries").toInt())
          .arg(summary.value("nested_data_entries").toInt())
          .arg(summary.value("content_files").toInt())
          .arg(summary.value("groundcover_files").toInt()));

  m_entries->setUpdatesEnabled(false);
  const QJsonArray entries = plan.value("data_entries").toArray();
  for (const auto& value : entries) {
    const QJsonObject entry = value.toObject();
    const QJsonObject kind  = entry.value("kind").toObject();
    const QString type      = kind.value("kind").toString();
    QString folder;
    if (type == "game_data")
      folder = tr("Morrowind Data Files (game)");
    else if (type == "mod")
      folder = kind.value("category").toString() + '/' + kind.value("data_path").toString();
    else
      folder = kind.value("raw").toString();
    auto* item = new QTreeWidgetItem(
        {QString::number(entry.value("order").toInt() + 1),
         entry.value("instance_name").toString(), folder});
    if (!entry.value("nested_in").isNull())
      item->setToolTip(1, tr("Inside another data folder; split out during install."));
    m_entries->addTopLevelItem(item);
  }
  m_entries->setUpdatesEnabled(true);

  QStringList notes;
  QStringList tools;
  for (const auto& tool : plan.value("tools").toArray())
    tools << humanize(tool.toString());
  if (!tools.isEmpty())
    notes << tr("Tools needed: %1").arg(tools.join(", "));
  QStringList generated;
  for (const auto& name : plan.value("generated_content").toArray())
    generated << name.toString();
  if (!generated.isEmpty())
    notes << tr("Created by those tools: %1").arg(generated.join(", "));
  const QJsonArray blockers = plan.value("blockers").toArray();
  const QJsonArray warnings = plan.value("warnings").toArray();
  for (const auto& b : blockers)
    notes << tr("BLOCKER: %1").arg(b.toString());
  for (const auto& w : warnings)
    notes << tr("Warning: %1").arg(w.toString());
  if (blockers.isEmpty())
    notes << tr("No blockers found.");
  m_notes->setPlainText(notes.join('\n'));

  setBusy(false, blockers.isEmpty()
                     ? tr("Reviewed %1.").arg(m_lists->currentText())
                     : tr("Reviewed %1: %2 blocker(s) would prevent a correct install.")
                           .arg(m_lists->currentText())
                           .arg(blockers.size()));
}

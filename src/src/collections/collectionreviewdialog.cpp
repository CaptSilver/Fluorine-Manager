#include "collectionreviewdialog.h"

#include <uibase/filesystemutilities.h>

#include <QComboBox>
#include <QDialogButtonBox>
#include <QFont>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QListWidget>
#include <QLocale>
#include <QPushButton>
#include <QRadioButton>
#include <QVBoxLayout>

#include <algorithm>

namespace
{

QString sizeText(qint64 bytes)
{
  return QLocale().formattedDataSize(bytes);
}

}  // namespace

CollectionReviewDialog::CollectionReviewDialog(const CollectionLink& link,
                                               const CollectionManifest& manifest,
                                               const CollectionState& state,
                                               int remainingRequests, QWidget* parent)
    : QDialog(parent), m_link(link), m_manifest(manifest), m_state(state),
      m_remaining(remainingRequests)
{
  buildUi();
  populateOptionalMods();
  chooseProfileDefaults();
  m_building = false;
  replan();
}

PlanChoices CollectionReviewDialog::choices() const
{
  PlanChoices result;
  for (int i = 0; i < m_optionalList->count(); ++i) {
    const QListWidgetItem* item = m_optionalList->item(i);
    if (item->checkState() == Qt::Checked) {
      result.optionalFileIds.insert(item->data(Qt::UserRole).toInt());
    }
  }
  result.targetProfile = selectedProfile();
  result.newProfile    = newProfileSelected();
  result.mode          = m_fullSyncRadio->isChecked() ? PlanChoices::Mode::FullSync
                                                      : PlanChoices::Mode::TopUp;
  return result;
}

const CollectionPlan& CollectionReviewDialog::currentPlan() const
{
  return m_plan;
}

bool CollectionReviewDialog::modeGroupVisible() const
{
  return m_modeGroupShown;
}

bool CollectionReviewDialog::newProfileSelected() const
{
  return m_newProfileRadio->isChecked();
}

QString CollectionReviewDialog::selectedProfile() const
{
  return newProfileSelected() ? newProfileName() : m_profileCombo->currentText();
}

QString CollectionReviewDialog::budgetText() const
{
  QString text = tr("%1 requests needed, %2 available")
                     .arg(m_plan.apiRequests)
                     .arg(m_remaining);
  if (m_plan.apiRequests > m_remaining) {
    text += tr(". Not enough: mods past the limit will fail and can be retried after the "
               "daily reset.");
  }
  return text;
}

QString CollectionReviewDialog::newProfileName() const
{
  QString name = m_manifest.info.name;
  if (!MOBase::fixDirectoryName(name)) {
    return {};
  }
  return name;
}

bool CollectionReviewDialog::newProfileAvailable() const
{
  const QString name = newProfileName();
  if (name.isEmpty()) {
    return false;
  }
  QStringList taken = m_state.profiles;
  taken << m_state.currentProfile;
  return std::none_of(taken.begin(), taken.end(), [&name](const QString& profile) {
    return profile.compare(name, Qt::CaseInsensitive) == 0;
  });
}

void CollectionReviewDialog::buildUi()
{
  setWindowTitle(tr("Install collection"));
  resize(720, 700);

  auto* root = new QVBoxLayout(this);

  m_titleLabel = new QLabel(this);
  m_titleLabel->setObjectName(QStringLiteral("titleLabel"));
  QFont titleFont = m_titleLabel->font();
  titleFont.setBold(true);
  if (titleFont.pointSizeF() > 0) {
    titleFont.setPointSizeF(titleFont.pointSizeF() * 1.25);
  }
  m_titleLabel->setFont(titleFont);
  root->addWidget(m_titleLabel);

  m_summaryLabel = new QLabel(this);
  m_summaryLabel->setObjectName(QStringLiteral("summaryLabel"));
  m_summaryLabel->setWordWrap(true);
  root->addWidget(m_summaryLabel);

  m_warningsLabel = new QLabel(this);
  m_warningsLabel->setObjectName(QStringLiteral("warningsLabel"));
  m_warningsLabel->setWordWrap(true);
  root->addWidget(m_warningsLabel);

  // Optional mods
  m_optionalGroup = new QGroupBox(tr("Optional mods (unticked ones are skipped)"), this);
  m_optionalGroup->setObjectName(QStringLiteral("optionalGroup"));
  auto* optionalLayout = new QVBoxLayout(m_optionalGroup);
  m_optionalList       = new QListWidget(m_optionalGroup);
  m_optionalList->setObjectName(QStringLiteral("optionalList"));
  optionalLayout->addWidget(m_optionalList);
  root->addWidget(m_optionalGroup, 1);

  // Mods that cannot be installed, with the planner's reason
  m_refusedGroup = new QGroupBox(tr("Will not be installed"), this);
  m_refusedGroup->setObjectName(QStringLiteral("refusedGroup"));
  auto* refusedLayout = new QVBoxLayout(m_refusedGroup);
  m_refusedList       = new QListWidget(m_refusedGroup);
  m_refusedList->setObjectName(QStringLiteral("refusedList"));
  refusedLayout->addWidget(m_refusedList);
  root->addWidget(m_refusedGroup, 1);

  // Profile. Both radios have the group box as parent so they exclude each other.
  auto* profileGroup  = new QGroupBox(tr("Profile"), this);
  auto* profileLayout = new QVBoxLayout(profileGroup);
  m_newProfileRadio   = new QRadioButton(profileGroup);
  m_newProfileRadio->setObjectName(QStringLiteral("newProfileRadio"));
  profileLayout->addWidget(m_newProfileRadio);

  auto* existingRow       = new QHBoxLayout;
  m_existingProfileRadio  = new QRadioButton(tr("Existing profile:"), profileGroup);
  m_existingProfileRadio->setObjectName(QStringLiteral("existingProfileRadio"));
  m_profileCombo = new QComboBox(profileGroup);
  m_profileCombo->setObjectName(QStringLiteral("profileCombo"));
  existingRow->addWidget(m_existingProfileRadio);
  existingRow->addWidget(m_profileCombo, 1);
  profileLayout->addLayout(existingRow);
  root->addWidget(profileGroup);

  // Top up or full sync
  m_modeGroup = new QGroupBox(tr("This profile already has mods from this collection"), this);
  m_modeGroup->setObjectName(QStringLiteral("modeGroup"));
  auto* modeLayout = new QVBoxLayout(m_modeGroup);
  m_topUpRadio     = new QRadioButton(
      tr("Top up: install what is new or changed, leave everything else as it is"), m_modeGroup);
  m_topUpRadio->setObjectName(QStringLiteral("topUpRadio"));
  m_topUpRadio->setChecked(true);
  m_fullSyncRadio = new QRadioButton(
      tr("Full sync: also disable mods this revision no longer lists"), m_modeGroup);
  m_fullSyncRadio->setObjectName(QStringLiteral("fullSyncRadio"));
  m_droppedLabel = new QLabel(tr("No longer in this revision:"), m_modeGroup);
  m_droppedLabel->setObjectName(QStringLiteral("droppedLabel"));
  m_droppedList = new QListWidget(m_modeGroup);
  m_droppedList->setObjectName(QStringLiteral("droppedList"));
  m_droppedList->setMaximumHeight(120);
  modeLayout->addWidget(m_topUpRadio);
  modeLayout->addWidget(m_fullSyncRadio);
  modeLayout->addWidget(m_droppedLabel);
  modeLayout->addWidget(m_droppedList);
  root->addWidget(m_modeGroup);

  m_budgetLabel = new QLabel(this);
  m_budgetLabel->setObjectName(QStringLiteral("budgetLabel"));
  m_budgetLabel->setWordWrap(true);
  root->addWidget(m_budgetLabel);

  auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
  m_installButton = buttons->button(QDialogButtonBox::Ok);
  m_installButton->setText(tr("Install"));
  m_installButton->setObjectName(QStringLiteral("installButton"));
  connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
  connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
  root->addWidget(buttons);

  const auto changed = [this] {
    if (!m_building) {
      replan();
    }
  };
  connect(m_optionalList, &QListWidget::itemChanged, this, changed);
  connect(m_newProfileRadio, &QRadioButton::toggled, this, changed);
  connect(m_profileCombo, &QComboBox::currentIndexChanged, this, changed);
  connect(m_fullSyncRadio, &QRadioButton::toggled, this, changed);
}

void CollectionReviewDialog::populateOptionalMods()
{
  for (const CollectionMod& mod : m_manifest.mods) {
    if (!mod.optional) {
      continue;
    }
    const QString text =
        mod.version.isEmpty() ? mod.name : tr("%1 (%2)").arg(mod.name, mod.version);
    auto* item = new QListWidgetItem(text, m_optionalList);
    item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
    item->setCheckState(Qt::Unchecked);
    item->setData(Qt::UserRole, mod.source.fileId);
    if (mod.source.fileSize > 0) {
      item->setToolTip(sizeText(mod.source.fileSize));
    }
  }
  m_optionalGroup->setVisible(m_optionalList->count() > 0);
}

void CollectionReviewDialog::chooseProfileDefaults()
{
  QStringList profiles = m_state.profiles;
  if (!m_state.currentProfile.isEmpty() && !profiles.contains(m_state.currentProfile)) {
    profiles.prepend(m_state.currentProfile);
  }
  m_profileCombo->addItems(profiles);

  // The profile named like the collection wins over the current one: installing a
  // collection again layers it back into its own profile.
  QString pick           = m_state.currentProfile;
  const QString wanted   = newProfileName();
  for (const QString& profile : profiles) {
    if (!wanted.isEmpty() && profile.compare(wanted, Qt::CaseInsensitive) == 0) {
      pick = profile;
      break;
    }
  }
  const int index = m_profileCombo->findText(pick);
  if (index >= 0) {
    m_profileCombo->setCurrentIndex(index);
  }

  const bool newAvailable = newProfileAvailable();
  m_newProfileRadio->setText(wanted.isEmpty() ? tr("New profile")
                                              : tr("New profile named \"%1\"").arg(wanted));
  m_newProfileRadio->setEnabled(newAvailable);
  if (!newAvailable) {
    m_newProfileRadio->setToolTip(wanted.isEmpty()
                                      ? tr("The collection's name cannot be used as a profile name.")
                                      : tr("A profile with this name already exists."));
  }

  const bool useNew = m_manifest.recommendNewProfile && newAvailable;
  m_newProfileRadio->setChecked(useNew);
  m_existingProfileRadio->setChecked(!useNew);
}

void CollectionReviewDialog::replan()
{
  m_plan = planCollection(m_link, m_manifest, m_state, choices());
  refreshView();
}

void CollectionReviewDialog::refreshView()
{
  const PlanChoices chosen = choices();

  m_titleLabel->setText(tr("%1, revision %2").arg(m_manifest.info.name).arg(m_link.revision));

  qint64 totalBytes = 0;
  int optionalCount = 0;
  for (const CollectionMod& mod : m_manifest.mods) {
    totalBytes += mod.source.fileSize;
    if (mod.optional) {
      ++optionalCount;
    }
  }

  int toInstall       = 0;
  int alreadyThere    = 0;
  m_refusedList->clear();
  for (const PlannedMod& planned : m_plan.mods) {
    switch (planned.action) {
    case PlannedMod::Action::Install:
    case PlannedMod::Action::Replace:
      ++toInstall;
      break;
    case PlannedMod::Action::AlreadyInstalled:
    case PlannedMod::Action::Adopt:
      ++alreadyThere;
      break;
    case PlannedMod::Action::Refused: {
      // An optional mod the user left unticked is not news.
      const bool leftOut = planned.mod->optional &&
                           chosen.optionalFileIds.count(planned.mod->source.fileId) == 0;
      if (!leftOut) {
        m_refusedList->addItem(tr("%1: %2").arg(planned.mod->name, planned.reason));
      }
      break;
    }
    }
  }
  m_refusedGroup->setVisible(m_refusedList->count() > 0);

  m_summaryLabel->setText(
      tr("%1 mods (%2 optional), %3 in total.\n"
         "%4 to install or replace, %5 already installed or adopted.\n"
         "%6 to download; %7 archives appear to be downloaded already.")
          .arg(m_manifest.mods.size())
          .arg(optionalCount)
          .arg(sizeText(totalBytes))
          .arg(toInstall)
          .arg(alreadyThere)
          .arg(sizeText(m_plan.bytesToDownload))
          .arg(m_plan.archivesOnDisk));

  m_warningsLabel->setText(m_plan.warnings.join(QLatin1Char('\n')));
  m_warningsLabel->setVisible(!m_plan.warnings.isEmpty());

  m_modeGroupShown = !m_plan.firstInstall;
  m_modeGroup->setVisible(m_modeGroupShown);

  const bool fullSync = chosen.mode == PlanChoices::Mode::FullSync;
  m_droppedList->clear();
  for (const QString& folder : m_plan.droppedFolders) {
    const bool disabled = std::find(m_plan.toDisable.begin(), m_plan.toDisable.end(), folder) !=
                          m_plan.toDisable.end();
    QString note;
    if (!fullSync) {
      note = tr("left as it is");
    } else if (disabled) {
      note = tr("will be disabled");
    } else {
      note = tr("stays enabled, another collection uses it");
    }
    m_droppedList->addItem(tr("%1 (%2)").arg(folder, note));
  }
  const bool anyDropped = !m_plan.droppedFolders.empty();
  m_droppedLabel->setVisible(anyDropped);
  m_droppedList->setVisible(anyDropped);

  m_profileCombo->setEnabled(!newProfileSelected());
  m_budgetLabel->setText(budgetText());
  m_installButton->setEnabled(!selectedProfile().isEmpty());
}

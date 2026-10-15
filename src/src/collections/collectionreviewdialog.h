#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"
#include "collectionplan.h"
#include "collectionstate.h"

#include <QDialog>

class QComboBox;
class QDialogButtonBox;
class QGroupBox;
class QLabel;
class QListWidget;
class QPushButton;
class QRadioButton;

// The one question before a collection install: which optional mods, which
// profile, top up or full sync. Every change re-plans, and everything shown
// comes from the plan.
//
// currentPlan().mods point into this dialog's copy of the manifest. Code that
// needs the plan after the dialog is gone plans again with its own manifest and
// choices().
class CollectionReviewDialog : public QDialog {
  Q_OBJECT

public:
  CollectionReviewDialog(const CollectionLink &link,
                         const CollectionManifest &manifest,
                         const CollectionState &state, int remainingRequests,
                         QWidget *parent = nullptr);

  PlanChoices choices() const;
  const CollectionPlan &currentPlan() const;

  // The top-up / full-sync choice is offered whenever this profile already
  // holds mods of the collection, which includes folders an earlier script
  // installed.
  bool modeGroupVisible() const;

  bool newProfileSelected() const;
  QString selectedProfile() const;

  // "N requests needed, M available", plus a warning when N exceeds M.
  QString budgetText() const;

private:
  void buildUi();
  void populateOptionalMods();
  void chooseProfileDefaults();
  void replan();
  void refreshView();

  QString newProfileName() const;
  bool newProfileAvailable() const;

  const CollectionLink m_link;
  const CollectionManifest m_manifest;
  const CollectionState m_state;
  const int m_remaining;

  CollectionPlan m_plan;
  bool m_building =
      true; // widgets are being filled, ignore their change signals
  bool m_modeGroupShown = false;

  QLabel *m_titleLabel = nullptr;
  QLabel *m_summaryLabel = nullptr;
  QLabel *m_warningsLabel = nullptr;
  QGroupBox *m_optionalGroup = nullptr;
  QListWidget *m_optionalList = nullptr;
  QGroupBox *m_refusedGroup = nullptr;
  QListWidget *m_refusedList = nullptr;
  QRadioButton *m_newProfileRadio = nullptr;
  QRadioButton *m_existingProfileRadio = nullptr;
  QComboBox *m_profileCombo = nullptr;
  QGroupBox *m_modeGroup = nullptr;
  QRadioButton *m_topUpRadio = nullptr;
  QRadioButton *m_fullSyncRadio = nullptr;
  QLabel *m_droppedLabel = nullptr;
  QListWidget *m_droppedList = nullptr;
  QLabel *m_budgetLabel = nullptr;
  QPushButton *m_installButton = nullptr;
};

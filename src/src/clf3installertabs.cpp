#include "clf3installertabs.h"
#include "clf3collectiondialog.h"
#include "clf3momwpage.h"

#include <QVBoxLayout>

Clf3InstallerTabs::Clf3InstallerTabs(QWidget* wabbajack, CollectionFactory factory,
                                   QWidget* parent)
    : QTabWidget(parent)
{
  setObjectName("clf3InstallerTabs");
  addTab(wabbajack, tr("Wabbajack"));
  auto* collectionPage = new QWidget;
  auto* layout = new QVBoxLayout(collectionPage);
  layout->setContentsMargins(0, 0, 0, 0);
  addTab(collectionPage, Clf3CollectionDialog::InstallationEnabled
                           ? tr("Nexus Collections") : tr("Nexus Collections (WIP)"));
  // Loads its list data when first shown; it only needs public site data.
  addTab(new Clf3MomwPage, tr("OpenMW"));
  connect(this, &QTabWidget::currentChanged, this,
          [this, layout, collectionPage, factory](int index) {
    if (index != 1 || m_collections) return;
    m_collections = factory(collectionPage);
    m_collections->setWindowFlags(Qt::Widget);
    layout->addWidget(m_collections);
    connect(m_collections, &QDialog::finished, this, &Clf3InstallerTabs::collectionsFinished);
    connect(m_collections, &Clf3CollectionDialog::nexusConnectionRequested,
            this, &Clf3InstallerTabs::nexusConnectionRequested);
    connect(m_collections, &Clf3CollectionDialog::busyChanged,
            this, [this](bool busy) { setTabEnabled(0, !busy); });
    m_collections->show();
  });
}

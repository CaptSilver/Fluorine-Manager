#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"
#include "collectionplan.h"
#include "collectionreport.h"

#include <QList>
#include <QObject>
#include <QString>

#include <atomic>
#include <memory>
#include <vector>

class OrganizerCore;

// Installs downloaded collection archives one at a time on the GUI thread, tags
// every folder it installs or adopts with the collection's owner entry, and
// keeps the report and the list of folders that are present after the run.
//
// The manifest and the plan are held by reference and must outlive the runner;
// the plan's PlannedMod::mod pointers point into the manifest.
//
// Extraction runs an event loop, so downloads can finish (and enqueue() can be
// called) while an install is in progress. Those jobs wait in the queue and are
// picked up by the loop in drain() once the running install returns.
class CollectionRunner : public QObject {
  Q_OBJECT

public:
  CollectionRunner(OrganizerCore &core, const CollectionLink &link,
                   const CollectionManifest &manifest,
                   const CollectionPlan &plan,
                   std::shared_ptr<std::atomic_bool> cancel,
                   QObject *parent = nullptr);

  // Writes this collection's owner tag onto every existing folder the plan
  // claims (Adopt, and AlreadyInstalled entries owned by another collection),
  // through the live ModInfo. Call it before the first install: a crash later
  // in the run then still leaves those folders recognisable. Placement does not
  // tag.
  void tagAdopted();

  // The archive for plan entry `planIndex` is on disk and verified.
  void enqueue(int planIndex, const QString &archivePath);

  // The archive for plan entry `planIndex` could not be fetched. Retryable.
  void fail(int planIndex, const QString &reason);

  // No install running and nothing queued.
  bool idle() const;

  // Parallel to plan.mods: the folder the mod occupies after the run, empty
  // when absent.
  const std::vector<QString> &presentFolders() const { return m_present; }

  CollectionReport &report() { return m_report; }

  // Gives every Install/Replace entry that has no result yet the outcome and
  // reason.
  void settleRemaining(CollectionReportRow::Outcome outcome,
                       const QString &reason, bool retryable);

signals:
  void progress(int settled, int total);
  void becameIdle();

private:
  struct Job {
    int planIndex = -1;
    QString archivePath;
  };

  static constexpr int BusyRetryMs = 500;

  void drain();
  void install(const Job &job);
  bool tagInstalled(const PlannedMod &planned, const QString &folder);
  void failRow(int planIndex, const QString &reason, bool retryable);
  void settle(int planIndex);
  bool isWork(int planIndex) const;
  CollectionReportRow *row(int planIndex);
  QString modLabel(int planIndex) const;

  OrganizerCore &m_core;
  CollectionLink m_link;
  const CollectionManifest &m_manifest;
  const CollectionPlan &m_plan;
  std::shared_ptr<std::atomic_bool> m_cancel;

  CollectionReport m_report;
  std::vector<QString> m_present;
  std::vector<char> m_settled;
  QList<Job> m_queue;
  bool m_installing = false;
  int m_total = 0;
  int m_settledCount = 0;
};

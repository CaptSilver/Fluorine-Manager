#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"
#include "collectionplan.h"
#include "collectionreport.h"
#include "collectionstate.h"

#include <QElapsedTimer>
#include <QNetworkAccessManager>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QTimer>

#include <atomic>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <utility>
#include <vector>

class CollectionDownloader;
class CollectionManifestFetcher;
class CollectionRunner;
class ConcurrentFileHasher;
class DownloadManagerPort;
class IUserInterface;
class OrganizerCore;
class QProgressDialog;

// One collection install at a time, from the clicked link to the results dialog.
//
//   Idle -> Fetching -> Review -> Running -> Placing -> Done -> Idle
//
// Nothing starts before the main window has finished initialising: a link that arrives
// earlier (Fluorine was started by the link) waits in a one-slot buffer, newest wins.
// Every failure before the review dialog ends the run with one tray notification and a
// log line; the dialogs are the Review and Results dialogs and a cancellable progress
// dialog while the run is going.
class CollectionSession : public QObject
{
  Q_OBJECT

public:
  enum class State
  {
    Idle,
    Fetching,
    Review,
    Running,
    Placing,
    Done
  };
  Q_ENUM(State)

  explicit CollectionSession(OrganizerCore& core, QObject* parent = nullptr);
  ~CollectionSession() override;

  // Called by OrganizerCore::setUserInterface; null when the window goes away.
  void setUserInterface(IUserInterface* ui);

  // Called by OrganizerCore::userInterfaceInitialized; releases a buffered link.
  void userInterfaceInitialized();

  void request(const CollectionLink& link);

  // Fetching: abandons the fetch. Running: stops submitting, finishes the install in
  // progress, skips placement and reports what is left. Other states: no effect.
  void cancel();

  State state() const { return m_state; }

signals:
  void stateChanged(CollectionSession::State state);
  void finished(const CollectionReport& report);

private slots:
  void manifestReady(const CollectionManifest& manifest, int revision,
                     const QString& cacheDir);
  void fetchFailed(const QString& reason);
  void onTick();
  void runnerProgress(int settled, int total);
  void checkFinished();
  void finishRun();

private:
  void begin(const CollectionLink& link);
  void startRun();
  void presentResults(const CollectionReport& report);
  CollectionReport collectReport(bool cancelled);
  void reset();
  void teardownRun();
  void releaseFetcher();
  void removeCache();
  void showProgress(int total);
  void closeProgress();
  void notify(const QString& message, bool problem = true);
  void setState(State state);

  OrganizerCore& m_core;
  IUserInterface* m_ui = nullptr;
  bool m_uiReady       = false;
  std::optional<CollectionLink> m_waiting;
  State m_state = State::Idle;

  CollectionLink m_link;
  std::optional<CollectionManifest> m_manifest;
  QString m_cacheDir;
  PlanChoices m_choices;
  CollectionState m_captured;
  CollectionPlan m_plan;  // the runner holds a reference; replaced only between runs

  // Retry re-plans everything and then keeps only the ticked failures.
  bool m_restrictToRetry = false;
  std::set<int> m_retryFileIds;

  std::shared_ptr<std::atomic_bool> m_cancel;
  bool m_finishQueued = false;
  QElapsedTimer m_clock;
  QTimer m_tick;
  QNetworkAccessManager m_public;  // unauthenticated mirror downloads, kept apart from Nexus

  // QObjects parented to this; raw pointers are cleared when they are released.
  CollectionManifestFetcher* m_fetcher = nullptr;
  DownloadManagerPort* m_port          = nullptr;
  ConcurrentFileHasher* m_hasher       = nullptr;
  CollectionRunner* m_runner           = nullptr;
  QPointer<QProgressDialog> m_progress;
  std::unique_ptr<CollectionDownloader> m_downloader;

  // (modId, fileId) -> plan entries that want that file; a file listed twice is
  // downloaded once.
  std::map<std::pair<int, int>, std::vector<int>> m_planByFile;
};

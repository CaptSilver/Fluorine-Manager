#pragma once

#include "downloadport.h"

#include <QElapsedTimer>
#include <QObject>
#include <QTimer>

#include <boost/signals2/connection.hpp>

#include <functional>
#include <map>
#include <optional>
#include <set>
#include <utility>
#include <vector>

class DownloadManager;
class NexusInterface;

// DownloadPort and NexusBudgetPort over the real DownloadManager and NexusInterface.
//
// Everything the listener hears is deferred with a queued call. DownloadManager raises its
// signals from inside setState()/addDownload(), and the downloader answers some events by
// submitting or removing downloads, which must not re-enter the manager mid-update.
class DownloadManagerPort : public QObject, public DownloadPort, public NexusBudgetPort
{
  Q_OBJECT

public:
  DownloadManagerPort(DownloadManager& downloads, NexusInterface& nexus,
                      QObject* parent = nullptr);
  ~DownloadManagerPort() override;

  void setListener(DownloadPortListener* listener) override;
  PortRow find(int modId, int fileId) const override;
  void submit(const QString& domain, int modId, int fileId) override;
  bool appeared(int modId, int fileId) const override;
  void resume(int index) override;
  void remove(int index) override;
  int remainingRequests() const override;

private slots:
  void scanForStarted();

private:
  using Pair = std::pair<int, int>;

  bool pendingExists(int modId, int fileId) const;
  // The final archive of a row that finished transferring and still waits for its
  // metadata, or nothing.
  std::optional<QString> archiveAwaitingMetadata(int modId, int fileId) const;
  void scheduleScan();
  void markStarted(int modId, int fileId);
  void settlePair(int modId, int fileId);
  void deliver(std::function<void()> call);
  void onRowCompleted(int row);
  void onRowFailed(int row);
  void onRequestFailed(int modId, int fileId, const QString& message);

  DownloadManager& m_downloads;
  NexusInterface& m_nexus;
  DownloadPortListener* m_listener = nullptr;

  // Submitted pairs for which transferStarted has not been reported yet.
  std::set<Pair> m_unstarted;
  // Submitted pairs for which neither downloadCompleted nor downloadFailed has been
  // reported yet.
  std::set<Pair> m_unfinished;
  // When a submitted pair was first seen waiting for its metadata after the transfer.
  std::map<Pair, qint64> m_waitingSince;
  bool m_scanQueued = false;
  QTimer m_poll;
  QElapsedTimer m_clock;
  std::vector<boost::signals2::connection> m_connections;
};

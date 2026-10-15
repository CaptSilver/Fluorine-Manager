#include "downloadmanagerport.h"

#include "downloadmanager.h"
#include "nexusinterface.h"

#include <log.h>

#include <QFileInfo>
#include <QMetaObject>
#include <QVariant>

#include <exception>

using namespace MOBase;

namespace
{
// A safety net next to the signals: a row can reach its state without a signal that
// names its ids, and a missed start would hold a window slot until the stall timeout.
constexpr int PollIntervalMs = 1000;

// How long a finished transfer may wait for its metadata before it is reported anyway.
constexpr qint64 PostTransferGraceMs = 30 * 1000;

// The archive is final in these states; what the row still waits for is metadata.
bool isPostTransfer(DownloadManager::DownloadState state)
{
  return state == DownloadManager::STATE_FETCHINGMODINFO ||
         state == DownloadManager::STATE_FETCHINGFILEINFO ||
         state == DownloadManager::STATE_FETCHINGMODINFO_MD5 ||
         state == DownloadManager::STATE_NOFETCH;
}

PortRowState classify(DownloadManager::DownloadState state)
{
  switch (state) {
  case DownloadManager::STATE_READY:
  case DownloadManager::STATE_INSTALLED:
  case DownloadManager::STATE_UNINSTALLED:
    return PortRowState::Ready;
  case DownloadManager::STATE_PAUSED:
    return PortRowState::Paused;
  case DownloadManager::STATE_ERROR:
  case DownloadManager::STATE_CANCELED:
    return PortRowState::Failed;
  case DownloadManager::STATE_STARTED:
  case DownloadManager::STATE_DOWNLOADING:
  case DownloadManager::STATE_CANCELING:  // resolves to a terminal state the listener sees
  case DownloadManager::STATE_PAUSING:
  case DownloadManager::STATE_FETCHINGMODINFO:
  case DownloadManager::STATE_FETCHINGFILEINFO:
  case DownloadManager::STATE_FETCHINGMODINFO_MD5:
  case DownloadManager::STATE_NOFETCH:
    return PortRowState::Active;
  }
  // A state added later is waited on, not resubmitted; the downloader's stall timeout
  // is the backstop if it never resolves.
  return PortRowState::Active;
}

// Several rows can carry the same ids (an old failed one and a new active one). The most
// useful wins; among equals the newest row (the one scanned first) stays.
int rank(PortRowState state)
{
  switch (state) {
  case PortRowState::Active:
    return 4;
  case PortRowState::Paused:
    return 3;
  case PortRowState::Ready:
    return 2;
  case PortRowState::Failed:
    return 1;
  case PortRowState::Missing:
    return 0;
  }
  return 0;
}
}  // namespace

DownloadManagerPort::DownloadManagerPort(DownloadManager& downloads,
                                         NexusInterface& nexus, QObject* parent)
    : QObject(parent), m_downloads(downloads), m_nexus(nexus)
{
  m_clock.start();
  m_poll.setInterval(PollIntervalMs);
  connect(&m_poll, &QTimer::timeout, this, &DownloadManagerPort::scanForStarted);

  // The row argument of stateChanged is meaningless for a download that is not in the
  // list yet, so these only trigger a rescan.
  connect(&m_downloads, &DownloadManager::downloadAdded, this,
          &DownloadManagerPort::scheduleScan);
  connect(&m_downloads, &DownloadManager::stateChanged, this,
          [this](int, DownloadManager::DownloadState) {
            scheduleScan();
          });

  m_connections.push_back(
      m_downloads.onDownloadComplete([this](int row) { onRowCompleted(row); }));
  m_connections.push_back(
      m_downloads.onDownloadFailed([this](int row) { onRowFailed(row); }));

  // nxmRequestFailed fires for every kind of API request, usually with fileID 0 or -1 for
  // the ones that are not file requests. They are forwarded as they come: the downloader
  // matches on (modId, fileId) and ignores pairs it is not waiting for, so filtering here
  // would only hide the real failures behind a second set of rules.
  connect(&m_nexus, &NexusInterface::nxmRequestFailed, this,
          [this](QString, int modId, int fileId, QVariant, int, int errorCode,
                 const QString& errorString) {
            onRequestFailed(modId, fileId,
                            tr("Nexus request failed (%1): %2")
                                .arg(errorCode)
                                .arg(errorString));
          });

  // An empty server list only removes the pending entry in DownloadManager; nothing else
  // would tell the downloader.
  connect(&m_nexus, &NexusInterface::nxmDownloadURLsAvailable, this,
          [this](QString, int modId, int fileId, QVariant, QVariant resultData, int) {
            if (resultData.toList().isEmpty()) {
              onRequestFailed(modId, fileId, tr("Nexus returned no download server"));
            }
          });
}

DownloadManagerPort::~DownloadManagerPort()
{
  for (auto& connection : m_connections) {
    connection.disconnect();
  }
}

void DownloadManagerPort::setListener(DownloadPortListener* listener)
{
  m_listener = listener;
}

PortRow DownloadManagerPort::find(int modId, int fileId) const
{
  PortRow best;
  for (int i = m_downloads.numTotalDownloads() - 1; i >= 0; --i) {
    const auto* info = m_downloads.getFileInfo(i);
    if (info == nullptr || info->modID != modId || info->fileID != fileId) {
      continue;
    }

    const DownloadManager::DownloadState raw = m_downloads.getState(i);
    PortRowState state                       = classify(raw);
    QString path;
    qint64 size = 0;
    if (state == PortRowState::Ready ||
        (state == PortRowState::Active && isPostTransfer(raw))) {
      const QString candidate = m_downloads.getFilePath(i);
      const QFileInfo file(candidate);
      if (file.isFile()) {
        // A row still fetching metadata has its final archive on disk already, so it
        // counts as ready; waiting for the metadata could last for ever.
        state = PortRowState::Ready;
        path  = candidate;
        size  = file.size();
      } else if (state == PortRowState::Ready) {
        // A ready row whose file is gone is as good as no row.
        continue;
      }
    }

    if (rank(state) > rank(best.state)) {
      best.index = i;
      best.state = state;
      best.path  = path;
      best.size  = size;
    }
  }
  return best;
}

void DownloadManagerPort::submit(const QString& domain, int modId, int fileId)
{
  m_unstarted.insert({modId, fileId});
  m_unfinished.insert({modId, fileId});

  // addNXMDownload answers a second request for a pending pair with a message box.
  if (!pendingExists(modId, fileId)) {
    m_downloads.addNXMDownload(QStringLiteral("nxm://%1/mods/%2/files/%3")
                                   .arg(domain)
                                   .arg(modId)
                                   .arg(fileId));
  }

  if (!m_poll.isActive()) {
    m_poll.start();
  }
}

bool DownloadManagerPort::appeared(int modId, int fileId) const
{
  if (pendingExists(modId, fileId)) {
    return true;
  }
  const PortRowState state = find(modId, fileId).state;
  return state != PortRowState::Missing && state != PortRowState::Failed;
}

void DownloadManagerPort::resume(int index)
{
  m_downloads.resumeDownload(index);
}

void DownloadManagerPort::remove(int index)
{
  m_downloads.removeDownload(index, true);
}

int DownloadManagerPort::remainingRequests() const
{
  return m_nexus.getAPIUserAccount().remainingRequests();
}

std::optional<QString> DownloadManagerPort::archiveAwaitingMetadata(int modId,
                                                                    int fileId) const
{
  for (int i = m_downloads.numTotalDownloads() - 1; i >= 0; --i) {
    const auto* info = m_downloads.getFileInfo(i);
    if (info == nullptr || info->modID != modId || info->fileID != fileId ||
        !isPostTransfer(m_downloads.getState(i))) {
      continue;
    }
    const QString path = m_downloads.getFilePath(i);
    if (QFileInfo(path).isFile()) {
      return path;
    }
  }
  return std::nullopt;
}

bool DownloadManagerPort::pendingExists(int modId, int fileId) const
{
  const int pending = m_downloads.numPendingDownloads();
  for (int i = 0; i < pending; ++i) {
    const auto [game, pendingMod, pendingFile] = m_downloads.getPendingDownload(i);
    if (pendingMod == modId && pendingFile == fileId) {
      return true;
    }
  }
  return false;
}

void DownloadManagerPort::scheduleScan()
{
  if (m_scanQueued) {
    return;
  }
  m_scanQueued = true;
  QMetaObject::invokeMethod(this, [this] { scanForStarted(); }, Qt::QueuedConnection);
}

void DownloadManagerPort::scanForStarted()
{
  m_scanQueued = false;

  // The listener may submit more files from transferStarted, so iterate a copy.
  const auto waiting = m_unstarted;
  for (const Pair& pair : waiting) {
    if (m_unstarted.count(pair) == 0) {
      continue;
    }
    const PortRowState state = find(pair.first, pair.second).state;
    if (state == PortRowState::Active || state == PortRowState::Paused ||
        state == PortRowState::Ready) {
      markStarted(pair.first, pair.second);
    }
  }

  // A finished transfer normally reaches READY a moment later, when the description
  // request answers, and onDownloadComplete reports it. If that answer never comes (the
  // account's request queue was cleared), the archive on disk is final, so it is reported
  // after a grace period instead of leaving the run waiting for good.
  const qint64 now  = m_clock.elapsed();
  const auto unfinished = m_unfinished;
  for (const Pair& pair : unfinished) {
    const std::optional<QString> path = archiveAwaitingMetadata(pair.first, pair.second);
    if (!path) {
      m_waitingSince.erase(pair);
      continue;
    }
    const qint64 since = m_waitingSince.emplace(pair, now).first->second;
    if (now - since < PostTransferGraceMs) {
      continue;
    }
    settlePair(pair.first, pair.second);
    markStarted(pair.first, pair.second);
    if (m_listener != nullptr) {
      m_listener->downloadCompleted(pair.first, pair.second, *path);
    }
  }

  if (m_unstarted.empty() && m_unfinished.empty()) {
    m_poll.stop();
  }
}

void DownloadManagerPort::settlePair(int modId, int fileId)
{
  m_unfinished.erase({modId, fileId});
  m_waitingSince.erase({modId, fileId});
}

void DownloadManagerPort::markStarted(int modId, int fileId)
{
  if (m_unstarted.erase({modId, fileId}) > 0 && m_listener != nullptr) {
    m_listener->transferStarted(modId, fileId);
  }
}

void DownloadManagerPort::deliver(std::function<void()> call)
{
  QMetaObject::invokeMethod(this, std::move(call), Qt::QueuedConnection);
}

void DownloadManagerPort::onRowCompleted(int row)
{
  try {
    const auto* info   = m_downloads.getFileInfo(row);
    const int modId    = info->modID;
    const int fileId   = info->fileID;
    const QString path = m_downloads.getFilePath(row);
    deliver([this, modId, fileId, path] {
      settlePair(modId, fileId);
      // A small file can finish between two scans; the window slot is still owed.
      markStarted(modId, fileId);
      if (m_listener != nullptr) {
        m_listener->downloadCompleted(modId, fileId, path);
      }
    });
  } catch (const std::exception& e) {
    log::error("collection: cannot read completed download row {}: {}", row, e.what());
  }
}

void DownloadManagerPort::onRowFailed(int row)
{
  try {
    const auto* info = m_downloads.getFileInfo(row);
    const QString reason =
        m_downloads.getState(row) == DownloadManager::STATE_CANCELED
            ? tr("the download was canceled")
            : tr("the download failed, see the Downloads tab");
    onRequestFailed(info->modID, info->fileID, reason);
  } catch (const std::exception& e) {
    log::error("collection: cannot read failed download row {}: {}", row, e.what());
  }
}

void DownloadManagerPort::onRequestFailed(int modId, int fileId, const QString& message)
{
  deliver([this, modId, fileId, message] {
    settlePair(modId, fileId);
    markStarted(modId, fileId);
    if (m_listener != nullptr) {
      m_listener->downloadFailed(modId, fileId, message);
    }
  });
}

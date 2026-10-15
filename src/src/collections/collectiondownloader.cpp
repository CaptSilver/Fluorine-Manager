#include "collectiondownloader.h"

namespace {
// A file-info request, a download-link request, and the mod description that
// DownloadManager fetches once the transfer ends (STATE_FETCHINGMODINFO).
constexpr int RequestsPerFile = 3;

QString budgetUsedUpReason() {
  return QStringLiteral(
      "Nexus request budget used up, retry after the daily reset");
}

QString refusedReason() {
  return QStringLiteral("download manager refused the request");
}
} // namespace

CollectionDownloader::CollectionDownloader(
    DownloadPort &port, NexusBudgetPort &budget, FileHasher &hasher,
    std::shared_ptr<std::atomic_bool> cancel, std::function<qint64()> nowMs,
    ReadyCallback ready, FailedCallback failed)
    : m_port(port), m_budget(budget), m_hasher(hasher),
      m_cancel(std::move(cancel)), m_nowMs(std::move(nowMs)),
      m_ready(std::move(ready)), m_failed(std::move(failed)) {
  m_port.setListener(this);
}

CollectionDownloader::~CollectionDownloader() { m_port.setListener(nullptr); }

void CollectionDownloader::start(const QString &domain,
                                 std::vector<DownloadRequest> requests) {
  m_domain = domain;
  for (DownloadRequest &request : requests) {
    const auto ids = std::make_pair(request.modId, request.fileId);
    if (m_byIds.count(ids) != 0) {
      continue; // one download per Nexus file, however many mods name it
    }
    m_byIds.emplace(ids, m_files.size());
    File file;
    file.request = std::move(request);
    file.lastChangeMs = m_nowMs();
    m_files.push_back(std::move(file));
  }
  tick();
}

void CollectionDownloader::tick() {
  classifyExisting();
  submitNext();
}

bool CollectionDownloader::idle() const {
  for (const File &file : m_files) {
    if (file.phase != Phase::Settled) {
      return false;
    }
  }
  return true;
}

void CollectionDownloader::transferStarted(int modId, int fileId) {
  File *file = lookup(modId, fileId);
  if (file != nullptr && file->phase == Phase::Submitted) {
    enterTransfer(*file);
  }
}

void CollectionDownloader::downloadCompleted(int modId, int fileId,
                                             const QString &path) {
  File *file = lookup(modId, fileId);
  if (file == nullptr || file->phase == Phase::Queued ||
      file->phase == Phase::Settled) {
    return; // not ours, or a download the user started on their own
  }
  fileDownloaded(*file, path);
}

void CollectionDownloader::downloadFailed(int modId, int fileId,
                                          const QString &reason) {
  File *file = lookup(modId, fileId);
  if (file == nullptr || file->phase == Phase::Queued ||
      file->phase == Phase::Settled) {
    return;
  }
  settleFailed(*file, reason);
}

CollectionDownloader::File *CollectionDownloader::lookup(int modId,
                                                         int fileId) {
  const auto it = m_byIds.find(std::make_pair(modId, fileId));
  return it == m_byIds.end() ? nullptr : &m_files[it->second];
}

int CollectionDownloader::inFlight() const {
  int count = 0;
  for (const File &file : m_files) {
    if (file.phase == Phase::Submitted) {
      ++count;
    }
  }
  return count;
}

// Looks once at what the download manager already holds for each file. Files
// already on disk never need the budget; rows someone else started are only
// waited for. submit() looks again for the files that wait longer.
void CollectionDownloader::classifyExisting() {
  for (File &file : m_files) {
    if (file.phase != Phase::Queued || file.classified) {
      continue;
    }
    file.classified = true;
    adoptExistingRow(file);
  }
}

// Takes over whatever the download manager already holds for the file. Returns
// true when the file left the queue (settled or awaited), false when it still
// needs to be submitted.
bool CollectionDownloader::adoptExistingRow(File &file) {
  const int modId = file.request.modId;
  const int fileId = file.request.fileId;
  const PortRow row = m_port.find(modId, fileId);
  switch (row.state) {
  case PortRowState::Ready:
    fileDownloaded(file, row.path);
    return true;
  case PortRowState::Active:
    // Not submitted by this run, so it takes no slot in the submission
    // window: the window bounds the API requests we have in flight, and this
    // row's are not ours.
    enterTransfer(file);
    return true;
  case PortRowState::Paused:
    m_port.resume(row.index);
    enterTransfer(file);
    return true;
  case PortRowState::Failed:
    // A failed or canceled row would make addNXMDownload ask the user; clear
    // it and let the file be submitted fresh.
    m_port.remove(row.index);
    return false;
  case PortRowState::Missing:
    // A request queued by someone else has no row yet; submitting it again
    // would pop an "Already Queued" box.
    if (m_port.appeared(modId, fileId)) {
      enterTransfer(file);
      return true;
    }
    return false;
  }
  return false;
}

void CollectionDownloader::submitNext() {
  for (File &file : m_files) {
    if (file.phase != Phase::Queued) {
      continue;
    }

    const int remaining = m_budget.remainingRequests();
    if (remaining <= RequestsPerFile) {
      // Not even one more file can be afforded, and waiting would mean hours.
      failWaiting(budgetUsedUpReason());
      return;
    }

    const int pending = inFlight();
    if (pending >= Window || remaining <= RequestsPerFile * (pending + 1)) {
      return; // the files in flight have the budget reserved; wait for them
    }
    submit(file);
  }
}

void CollectionDownloader::submit(File &file) {
  // A file can wait a long time for its slot, and the user or an nxm link may
  // have queued it meanwhile; submitting it then would show a message box.
  if (adoptExistingRow(file)) {
    return;
  }

  file.phase = Phase::Submitted;
  file.lastChangeMs = m_nowMs();

  const int modId = file.request.modId;
  const int fileId = file.request.fileId;
  m_port.submit(m_domain, modId, fileId);
  // addNXMDownload returns without a word when it declines; no row means no
  // answer will ever come for this file.
  if (!m_port.appeared(modId, fileId)) {
    settleFailed(file, refusedReason());
  }
}

void CollectionDownloader::failWaiting(const QString &reason) {
  for (File &file : m_files) {
    if (file.phase == Phase::Queued) {
      settleFailed(file, reason);
    }
  }
}

void CollectionDownloader::enterTransfer(File &file) {
  file.phase = Phase::Transferring;
  file.lastChangeMs = m_nowMs();
}

void CollectionDownloader::fileDownloaded(File &file, const QString &path) {
  settleReady(file, path);
}

void CollectionDownloader::settleReady(File &file, const QString &path) {
  const int modId = file.request.modId;
  const int fileId = file.request.fileId;
  file.phase = Phase::Settled;
  file.lastChangeMs = m_nowMs();
  m_ready(modId, fileId, path);
}

void CollectionDownloader::settleFailed(File &file, const QString &reason) {
  const int modId = file.request.modId;
  const int fileId = file.request.fileId;
  file.phase = Phase::Settled;
  file.lastChangeMs = m_nowMs();
  m_failed(modId, fileId, reason);
}

#include "collectiondownloader.h"

#include <algorithm>

namespace
{
// A file-info request, a download-link request, and the mod description that
// DownloadManager fetches once the transfer ends (STATE_FETCHINGMODINFO).
constexpr int RequestsPerFile = 3;

QString stallReason()
{
  return QStringLiteral("Nexus did not answer within 10 minutes; the request limit may "
                        "have been reached, retry later");
}
}  // namespace

CollectionDownloader::CollectionDownloader(DownloadPort& port, NexusBudgetPort& budget,
                                           FileHasher& hasher,
                                           std::shared_ptr<std::atomic_bool> cancel,
                                           std::function<qint64()> nowMs,
                                           ReadyCallback ready, FailedCallback failed)
    : m_port(port), m_budget(budget), m_hasher(hasher), m_cancel(std::move(cancel)),
      m_nowMs(std::move(nowMs)), m_ready(std::move(ready)), m_failed(std::move(failed))
{
  m_port.setListener(this);
}

CollectionDownloader::~CollectionDownloader()
{
  *m_alive = false;
  m_port.setListener(nullptr);
}

bool CollectionDownloader::cancelled() const
{
  return m_cancel != nullptr && m_cancel->load();
}

CollectionDownloader::Item* CollectionDownloader::lookup(int modId, int fileId)
{
  const auto it = m_byIds.find({modId, fileId});
  return it == m_byIds.end() ? nullptr : &m_items[it->second];
}

int CollectionDownloader::inFlight() const
{
  return static_cast<int>(
      std::count_if(m_items.begin(), m_items.end(), [](const Item& item) {
        return item.phase == Phase::Submitted;
      }));
}

void CollectionDownloader::start(const QString& domain,
                                 std::vector<DownloadRequest> requests)
{
  m_domain          = domain;
  const qint64 now  = m_nowMs();
  const std::size_t first = m_items.size();

  for (DownloadRequest& request : requests) {
    const std::pair<int, int> key{request.modId, request.fileId};
    if (m_byIds.count(key) != 0) {
      continue;
    }
    Item item;
    item.index     = m_items.size();
    item.changedMs = now;
    item.request   = std::move(request);
    m_byIds.emplace(key, item.index);
    m_items.push_back(std::move(item));
  }

  // A callback below may call start() again; only walk the items added by this call.
  const std::size_t last = m_items.size();

  // Classify every file before verifying any: verifying can end in a re-download, which
  // submits, and a file that is still unclassified would be submitted with it.
  std::vector<std::pair<std::size_t, QString>> onDisk;
  for (std::size_t i = first; i < last; ++i) {
    adoptExistingRow(m_items[i], onDisk);
  }
  for (const auto& file : onDisk) {
    if (cancelled()) {
      break;
    }
    verify(m_items[file.first], file.second);
  }
  pump();
}

void CollectionDownloader::tick()
{
  pump();
}

bool CollectionDownloader::idle() const
{
  if (cancelled()) {
    return true;
  }
  return std::all_of(m_items.begin(), m_items.end(), [](const Item& item) {
    return item.phase == Phase::Settled;
  });
}

void CollectionDownloader::adoptExistingRow(
    Item& item, std::vector<std::pair<std::size_t, QString>>& onDisk)
{
  const PortRow row = m_port.find(item.request.modId, item.request.fileId);
  switch (row.state) {
  case PortRowState::Ready:
    // Hashing from here on, so nothing submits it while the others are classified.
    item.reused = true;
    item.phase  = Phase::Hashing;
    onDisk.emplace_back(item.index, row.path);
    break;
  case PortRowState::Active:
    // Somebody already started it; wait for it rather than trip the manager's
    // "already started" prompt.
    item.reused    = true;
    item.phase     = Phase::Transferring;
    item.changedMs = m_nowMs();
    break;
  case PortRowState::Paused:
    item.reused    = true;
    item.phase     = Phase::Transferring;
    item.changedMs = m_nowMs();
    m_port.resume(row.index);
    break;
  case PortRowState::Failed:
    // An errored or canceled row would make the manager ask about it again, so it
    // goes first. The file stays Waiting and is submitted like any other.
    m_port.remove(row.index);
    break;
  case PortRowState::Missing:
    // A request somebody else queued has no row yet. Submitting it again would pop an
    // "already queued" box and take a window slot, so it is awaited like an active row.
    if (m_port.appeared(item.request.modId, item.request.fileId)) {
      item.reused    = true;
      item.phase     = Phase::Transferring;
      item.changedMs = m_nowMs();
    }
    break;
  }
}

void CollectionDownloader::pump()
{
  // A callback can call tick() or hand in a completion while a pass is running. Run
  // another pass afterwards instead of nesting.
  if (m_pumping) {
    m_pumpAgain = true;
    return;
  }
  m_pumping = true;
  do {
    m_pumpAgain = false;
    if (cancelled()) {
      break;
    }
    failStalled();
    submitWaiting();
  } while (m_pumpAgain);
  m_pumping = false;
}

void CollectionDownloader::failStalled()
{
  const qint64 now = m_nowMs();
  for (std::size_t i = 0; i < m_items.size(); ++i) {
    Item& item = m_items[i];
    // A transfer this run submitted is not watched once it has started: a running
    // transfer stays in one state until it ends. A row somebody else started is watched
    // from the moment we began waiting for it.
    const bool watched =
        item.phase == Phase::Submitted || (item.phase == Phase::Transferring && item.reused);
    if (watched && now - item.changedMs >= StallMs) {
      settleFailed(item, stallReason());
    }
  }
}

void CollectionDownloader::submitWaiting()
{
  for (std::size_t i = 0; i < m_items.size(); ++i) {
    if (cancelled()) {
      return;
    }
    Item& item = m_items[i];
    if (item.phase != Phase::Waiting) {
      continue;
    }

    const int flying = inFlight();
    if (flying >= Window) {
      return;
    }
    // Each file costs three requests. Files in flight have not spent theirs yet, so they
    // are reserved before this one is allowed.
    if (m_budget.remainingRequests() <= RequestsPerFile * (flying + 1)) {
      if (flying == 0) {
        failWaitingForBudget();
      }
      return;
    }
    submit(item);
  }
}

void CollectionDownloader::failWaitingForBudget()
{
  // The hourly pool refills slowly once the daily one is gone, so waiting would mean
  // hours. Fail fast and let Retry pick the files up after the reset.
  for (std::size_t i = 0; i < m_items.size(); ++i) {
    Item& item = m_items[i];
    if (item.phase == Phase::Waiting) {
      settleFailed(
          item,
          QStringLiteral("Nexus request budget used up, retry after the daily reset"));
    }
  }
}

void CollectionDownloader::submit(Item& item)
{
  // Set the phase first: a port may answer from inside submit().
  item.phase     = Phase::Submitted;
  item.changedMs = m_nowMs();
  m_port.submit(m_domain, item.request.modId, item.request.fileId);

  if (item.phase == Phase::Submitted &&
      !m_port.appeared(item.request.modId, item.request.fileId)) {
    settleFailed(item, QStringLiteral("download manager refused the request"));
  }
}

void CollectionDownloader::transferStarted(int modId, int fileId)
{
  if (cancelled()) {
    return;
  }
  Item* item = lookup(modId, fileId);
  if (item == nullptr || item->phase != Phase::Submitted) {
    return;
  }
  item->phase     = Phase::Transferring;
  item->changedMs = m_nowMs();
  pump();  // the freed slot may let the next file out
}

void CollectionDownloader::downloadCompleted(int modId, int fileId, const QString& path)
{
  if (cancelled()) {
    return;
  }
  Item* item = lookup(modId, fileId);
  if (item == nullptr ||
      (item->phase != Phase::Submitted && item->phase != Phase::Transferring)) {
    return;
  }
  verify(*item, path);
  pump();
}

void CollectionDownloader::downloadFailed(int modId, int fileId, const QString& reason)
{
  if (cancelled()) {
    return;
  }
  Item* item = lookup(modId, fileId);
  if (item == nullptr ||
      (item->phase != Phase::Submitted && item->phase != Phase::Transferring)) {
    return;
  }
  settleFailed(*item, reason);
  pump();
}

void CollectionDownloader::verify(Item& item, const QString& path)
{
  if (item.request.md5.isEmpty()) {
    settleReady(item, path);
    return;
  }

  item.phase               = Phase::Hashing;
  const std::size_t index  = item.index;
  const std::shared_ptr<bool> alive = m_alive;
  m_hasher.md5(path, [this, alive, index, path](const QString& hex) {
    if (*alive) {
      onHashed(index, path, hex);
    }
  });
}

void CollectionDownloader::onHashed(std::size_t index, const QString& path,
                                    const QString& hex)
{
  // A cancel between the request and the answer drops the answer: the session is
  // tearing down and nobody wants a callback for this file any more.
  if (cancelled()) {
    return;
  }
  Item& item = m_items[index];
  if (item.phase != Phase::Hashing) {
    return;
  }

  if (hex.compare(item.request.md5, Qt::CaseInsensitive) == 0) {
    settleReady(item, path);
    return;
  }

  if (item.reused && !item.resubmitted) {
    // The archive was there before this run, so it may be stale or cut short. Throw it
    // away and download it once more; the download is paid for like any other.
    resubmit(item);
    pump();
    return;
  }

  QString reason;
  if (hex.isEmpty()) {
    reason = QStringLiteral("could not read the archive to check its MD5");
  } else if (item.resubmitted) {
    reason = QStringLiteral(
                 "the archive downloaded again still fails its MD5 check (got %1, "
                 "the collection says %2)")
                 .arg(hex, item.request.md5);
  } else {
    reason = QStringLiteral("MD5 mismatch (got %1, the collection says %2)")
                 .arg(hex, item.request.md5);
  }
  settleFailed(item, reason);
  pump();
}

void CollectionDownloader::resubmit(Item& item)
{
  const PortRow row = m_port.find(item.request.modId, item.request.fileId);
  if (row.state != PortRowState::Missing && row.index >= 0) {
    m_port.remove(row.index);
  }
  item.reused      = false;
  item.resubmitted = true;
  item.phase       = Phase::Waiting;
}

void CollectionDownloader::settleReady(Item& item, const QString& path)
{
  item.phase = Phase::Settled;
  if (m_ready) {
    m_ready(item.request.modId, item.request.fileId, path);
  }
}

void CollectionDownloader::settleFailed(Item& item, const QString& reason)
{
  item.phase = Phase::Settled;
  if (m_failed) {
    m_failed(item.request.modId, item.request.fileId, reason);
  }
}

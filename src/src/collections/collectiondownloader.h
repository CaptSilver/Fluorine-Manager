#pragma once

#include "downloadport.h"
#include "filehash.h"

#include <QString>

#include <atomic>
#include <cstddef>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <utility>
#include <vector>

struct DownloadRequest
{
  int modId = 0;
  int fileId = 0;
  QString md5;  // empty: the manifest has no hash, so nothing is verified
  qint64 fileSize = 0;
};

// Moves a list of Nexus files from "wanted" to "on disk and verified", one callback per
// file. Everything outside this class is behind DownloadPort, NexusBudgetPort and
// FileHasher, so the rules below are tested with fakes.
//
// A file ends in exactly one of `ready` (path of a verified archive) or `failed`
// (reason). Nothing is retried except the one re-download of a stale archive.
//
// Not thread-safe: every call and every callback is on the thread that owns the port.
class CollectionDownloader : public DownloadPortListener
{
public:
  // Files submitted whose transfer has not started. Each has two API requests in flight
  // (file info, download link), so three files are six requests, the ceiling
  // NexusInterface runs at once. The mod description fetched after the transfer is a
  // third request per file that the budget counts and the window does not.
  static constexpr int Window = 3;

  // How long a submitted file may go without its transfer starting, and how long a file
  // that only waits on a row someone else started may stay unfinished.
  static constexpr qint64 StallMs = 10LL * 60 * 1000;

  using ReadyCallback  = std::function<void(int modId, int fileId, const QString& path)>;
  using FailedCallback = std::function<void(int modId, int fileId, const QString& reason)>;

  CollectionDownloader(DownloadPort& port, NexusBudgetPort& budget, FileHasher& hasher,
                       std::shared_ptr<std::atomic_bool> cancel,
                       std::function<qint64()> nowMs, ReadyCallback ready,
                       FailedCallback failed);
  ~CollectionDownloader() override;

  CollectionDownloader(const CollectionDownloader&)            = delete;
  CollectionDownloader& operator=(const CollectionDownloader&) = delete;

  // Takes over the files, settles the ones already on disk, and submits what the window
  // and budget allow. A (modId, fileId) pair listed twice is downloaded and reported
  // once. May be called again to add files.
  void start(const QString& domain, std::vector<DownloadRequest> requests);

  // Budget rule: submit only while budget.remainingRequests() > 3 * (inFlight + 1),
  // where inFlight = files submitted that have had no transferStarted, completion or
  // failure yet. When that fails with nothing in flight the budget is gone and every
  // file still waiting fails. Also runs the stall check. Driven by a timer in the
  // session and idempotent.
  void tick();

  // True when every file has settled, or the cancel flag is set and nothing more will
  // be reported.
  bool idle() const;

  void transferStarted(int modId, int fileId) override;
  void downloadCompleted(int modId, int fileId, const QString& path) override;
  void downloadFailed(int modId, int fileId, const QString& reason) override;

private:
  enum class Phase
  {
    Waiting,       // not submitted
    Submitted,     // submitted, transfer not started: counts toward the window
    Transferring,  // transfer running, or an existing row being awaited
    Hashing,       // on disk, MD5 job in flight
    Settled        // ready or failed callback fired
  };

  struct Item
  {
    DownloadRequest request;
    std::size_t index = 0;
    Phase phase       = Phase::Waiting;
    bool reused       = false;      // the archive was already known to the port at start
    bool resubmitted  = false;      // the one re-download of a stale archive is spent
    qint64 changedMs  = 0;          // when it was submitted, or when we began waiting on a
                                    // row someone else started; the stall clock
  };

  bool cancelled() const;
  Item* lookup(int modId, int fileId);
  int inFlight() const;

  // Looks the file up in the port. A row already on disk is returned through `onDisk`
  // to be verified once every file has been classified.
  void adoptExistingRow(Item& item, std::vector<std::pair<std::size_t, QString>>& onDisk);
  void pump();
  void failStalled();
  void submitWaiting();
  void failWaitingForBudget();
  void submit(Item& item);

  void verify(Item& item, const QString& path);
  void onHashed(std::size_t index, const QString& path, const QString& hex);
  void resubmit(Item& item);

  void settleReady(Item& item, const QString& path);
  void settleFailed(Item& item, const QString& reason);

  DownloadPort& m_port;
  NexusBudgetPort& m_budget;
  FileHasher& m_hasher;
  std::shared_ptr<std::atomic_bool> m_cancel;
  std::function<qint64()> m_nowMs;
  ReadyCallback m_ready;
  FailedCallback m_failed;

  QString m_domain;
  // A deque so a callback that calls start() again cannot move items under a loop.
  std::deque<Item> m_items;
  std::map<std::pair<int, int>, std::size_t> m_byIds;

  // Hash jobs call back after this object may be gone; they check this first.
  std::shared_ptr<bool> m_alive = std::make_shared<bool>(true);

  bool m_pumping    = false;
  bool m_pumpAgain  = false;
};

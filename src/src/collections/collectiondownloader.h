#pragma once

#include "downloadport.h"
#include "filehash.h"

#include <QString>

#include <atomic>
#include <cstddef>
#include <functional>
#include <map>
#include <memory>
#include <utility>
#include <vector>

struct DownloadRequest {
  int modId = 0;
  int fileId = 0;
  QString md5;
  qint64 fileSize = 0;
};

// Gets a list of Nexus files into the download manager without overrunning its
// API queue or the account's request quota, and reports each file once as ready
// (with the path on disk) or failed (with a reason). Files are matched by
// (modId, fileId). Single-threaded: every call and every callback happens on
// the thread that owns it.
class CollectionDownloader : public DownloadPortListener {
public:
  // Files between submission and the transfer starting. Each has two API
  // requests in flight (file info, download link), so three files fill the
  // download manager's six-request API window. The third request per file, the
  // mod description fetched after the transfer, is counted in the budget but is
  // not part of the window.
  static constexpr int Window = 3;
  // A submitted file whose transfer has not started this long after submission,
  // or a file that only waits on a row someone else started, fails with a
  // reason that names the request limit.
  static constexpr qint64 StallMs = 10LL * 60 * 1000;

  using ReadyCallback =
      std::function<void(int modId, int fileId, const QString &path)>;
  using FailedCallback =
      std::function<void(int modId, int fileId, const QString &reason)>;

  CollectionDownloader(DownloadPort &port, NexusBudgetPort &budget,
                       FileHasher &hasher,
                       std::shared_ptr<std::atomic_bool> cancel,
                       std::function<qint64()> nowMs, ReadyCallback ready,
                       FailedCallback failed);
  ~CollectionDownloader() override;

  CollectionDownloader(const CollectionDownloader &) = delete;
  CollectionDownloader &operator=(const CollectionDownloader &) = delete;

  // Queues the requests, in order, and runs the first tick. A Nexus file named
  // twice is downloaded once.
  void start(const QString &domain, std::vector<DownloadRequest> requests);

  // Budget rule: submit only while budget.remainingRequests() > 3 * (inFlight +
  // 1), where inFlight = files submitted that have had no transferStarted,
  // completion or failure yet. When fewer than that is left and no further file
  // can be afforded at all (remaining <= 3), the files still waiting for a
  // download fail.
  void tick(); // submissions and stall check; driven by a timer in the session

  // Every file has been reported ready or failed.
  bool idle() const;

  void transferStarted(int modId, int fileId) override;
  void downloadCompleted(int modId, int fileId, const QString &path) override;
  void downloadFailed(int modId, int fileId, const QString &reason) override;

private:
  enum class Phase {
    Queued,    // nothing sent yet (or sent back after a failed row was removed)
    Submitted, // addNXMDownload called, transfer not started: counts toward
               // Window
    Transferring, // started, or an existing row we only wait for
    Settled       // reported ready or failed
  };

  struct File {
    DownloadRequest request;
    Phase phase = Phase::Queued;
    bool classified = false; // an existing row has been looked for
    qint64 lastChangeMs = 0; // when the phase last changed
  };

  File *lookup(int modId, int fileId);
  int inFlight() const;

  void classifyExisting();
  bool adoptExistingRow(File &file);
  void submitNext();
  void submit(File &file);
  void failWaiting(const QString &reason);
  void enterTransfer(File &file);

  void fileDownloaded(File &file, const QString &path);
  void settleReady(File &file, const QString &path);
  void settleFailed(File &file, const QString &reason);

  DownloadPort &m_port;
  NexusBudgetPort &m_budget;
  FileHasher &m_hasher;
  std::shared_ptr<std::atomic_bool> m_cancel;
  std::function<qint64()> m_nowMs;
  ReadyCallback m_ready;
  FailedCallback m_failed;

  QString m_domain;
  std::vector<File> m_files;
  std::map<std::pair<int, int>, std::size_t> m_byIds;
};

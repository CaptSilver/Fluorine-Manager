#include "collections/collectiondownloader.h"

#include <gtest/gtest.h>

#include <atomic>
#include <functional>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace
{

const QString Domain  = QStringLiteral("cyberpunk2077");
const QString GoodMd5 = QStringLiteral("0123456789abcdef0123456789abcdef");
const QString BadMd5  = QStringLiteral("fedcba9876543210fedcba9876543210");
const std::string BudgetReason =
    "Nexus request budget used up, retry after the daily reset";

class FakeDownloadPort : public DownloadPort
{
public:
  struct Submission
  {
    QString domain;
    int modId  = 0;
    int fileId = 0;
  };

  void setListener(DownloadPortListener* listener) override { m_listener = listener; }

  PortRow find(int modId, int fileId) const override
  {
    const auto it = rows.find({modId, fileId});
    return it == rows.end() ? PortRow{} : it->second;
  }

  void submit(const QString& domain, int modId, int fileId) override
  {
    submissions.push_back({domain, modId, fileId});
    if (rowsAppear) {
      addRow(modId, fileId, PortRowState::Active);
    }
  }

  bool appeared(int modId, int fileId) const override
  {
    if (pending.count({modId, fileId}) > 0) {
      return true;
    }
    const auto it = rows.find({modId, fileId});
    return it != rows.end() && it->second.state == PortRowState::Active;
  }

  void resume(int index) override { resumed.push_back(index); }

  void remove(int index) override
  {
    removed.push_back(index);
    for (auto it = rows.begin(); it != rows.end(); ++it) {
      if (it->second.index == index) {
        rows.erase(it);
        break;
      }
    }
  }

  void addRow(int modId, int fileId, PortRowState state, const QString& path = QString())
  {
    PortRow row;
    row.index = nextIndex++;
    row.state = state;
    row.path  = path;
    rows[{modId, fileId}] = row;
  }

  DownloadPortListener* listener() const { return m_listener; }

  std::map<std::pair<int, int>, PortRow> rows;
  std::set<std::pair<int, int>> pending;  // queued by someone else, no row yet
  std::vector<Submission> submissions;
  std::vector<int> resumed;
  std::vector<int> removed;
  bool rowsAppear = true;
  int nextIndex   = 100;

private:
  DownloadPortListener* m_listener = nullptr;
};

class FakeBudget : public NexusBudgetPort
{
public:
  int remainingRequests() const override { return remaining; }
  int remaining = 1000;
};

// Answers at once by default. With `deferred` set the answers wait in `pending` until
// flush(), which lets a test act between a hash request and its result.
class FakeHasher : public FileHasher
{
public:
  void md5(const QString& path, std::function<void(const QString&)> done) override
  {
    requested.push_back(path);
    if (deferred) {
      pending.emplace_back(path, std::move(done));
      return;
    }
    done(hexFor(path));
  }

  void flush()
  {
    auto jobs = std::move(pending);
    pending.clear();
    for (auto& job : jobs) {
      job.second(hexFor(job.first));
    }
  }

  QString hexFor(const QString& path) const
  {
    const auto it = hashes.find(path);
    return it == hashes.end() ? QString() : it->second;
  }

  std::map<QString, QString> hashes;
  std::vector<QString> requested;
  std::vector<std::pair<QString, std::function<void(const QString&)>>> pending;
  bool deferred = false;
};

struct Event
{
  int modId  = 0;
  int fileId = 0;
  QString text;
};

struct Harness
{
  Harness()
      : cancel(std::make_shared<std::atomic_bool>(false)),
        downloader(
            port, budget, hasher, cancel, [this] { return now; },
            [this](int modId, int fileId, const QString& path) {
              ready.push_back({modId, fileId, path});
            },
            [this](int modId, int fileId, const QString& reason) {
              failed.push_back({modId, fileId, reason});
            })
  {}

  // Declaration order matters: the downloader's callbacks use the members above it.
  FakeDownloadPort port;
  FakeBudget budget;
  FakeHasher hasher;
  std::shared_ptr<std::atomic_bool> cancel;
  qint64 now = 0;
  std::vector<Event> ready;
  std::vector<Event> failed;
  CollectionDownloader downloader;
};

// Requests (1, 10), (2, 20), ... so a test can name a file by its position.
std::vector<DownloadRequest> numbered(int count, const QString& md5 = QString())
{
  std::vector<DownloadRequest> requests;
  for (int i = 1; i <= count; ++i) {
    DownloadRequest request;
    request.modId    = i;
    request.fileId   = i * 10;
    request.md5      = md5;
    request.fileSize = 1000;
    requests.push_back(request);
  }
  return requests;
}

constexpr qint64 Minute = 60LL * 1000;

}  // namespace

// ---- submission window and budget ----

TEST(Downloader, SubmitsAtMostThreeBeforeTransferStarts)
{
  Harness h;
  h.downloader.start(Domain, numbered(5));
  h.downloader.tick();

  ASSERT_EQ(h.port.submissions.size(), 3u);
  EXPECT_EQ(h.port.submissions[0].domain.toStdString(), "cyberpunk2077");
  EXPECT_EQ(h.port.submissions[0].modId, 1);
  EXPECT_EQ(h.port.submissions[1].modId, 2);
  EXPECT_EQ(h.port.submissions[2].fileId, 30);

  h.downloader.tick();
  EXPECT_EQ(h.port.submissions.size(), 3u);
}

TEST(Downloader, TransferStartFreesWindowSlot)
{
  Harness h;
  h.downloader.start(Domain, numbered(4));
  ASSERT_EQ(h.port.submissions.size(), 3u);

  h.downloader.transferStarted(1, 10);

  ASSERT_EQ(h.port.submissions.size(), 4u);
  EXPECT_EQ(h.port.submissions[3].modId, 4);
}

TEST(Downloader, NoSubmissionAtBudgetBoundary)
{
  Harness h;
  // One file in flight reserves its three requests (file info, download link, mod
  // description), and the next one needs three more, so the second submission needs
  // strictly more than 6 remaining.
  h.budget.remaining = 6;
  h.downloader.start(Domain, numbered(3));
  EXPECT_EQ(h.port.submissions.size(), 1u);
  EXPECT_TRUE(h.failed.empty()) << "a file in flight may still free budget, nobody fails yet";

  h.downloader.tick();
  EXPECT_EQ(h.port.submissions.size(), 1u);

  h.budget.remaining = 7;
  h.downloader.tick();
  EXPECT_EQ(h.port.submissions.size(), 2u);

  h.budget.remaining = 9;  // two in flight now: the third needs more than 9
  h.downloader.tick();
  EXPECT_EQ(h.port.submissions.size(), 2u);

  h.budget.remaining = 10;
  h.downloader.tick();
  EXPECT_EQ(h.port.submissions.size(), 3u);
  EXPECT_TRUE(h.failed.empty());
}

TEST(Downloader, BudgetExhaustionFailsWaitingModsOnly)
{
  Harness h;
  h.budget.remaining = 3;
  h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/one.zip"));
  h.port.addRow(2, 20, PortRowState::Active);

  h.downloader.start(Domain, numbered(4));

  EXPECT_TRUE(h.port.submissions.empty());
  ASSERT_EQ(h.failed.size(), 2u);
  EXPECT_EQ(h.failed[0].modId, 3);
  EXPECT_EQ(h.failed[1].modId, 4);
  EXPECT_EQ(h.failed[0].text.toStdString(), BudgetReason);
  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].modId, 1);
  EXPECT_EQ(h.ready[0].text.toStdString(), "/dl/one.zip");

  EXPECT_FALSE(h.downloader.idle()) << "mod 2 is still transferring";
  h.downloader.downloadCompleted(2, 20, QStringLiteral("/dl/two.zip"));
  ASSERT_EQ(h.ready.size(), 2u);
  EXPECT_EQ(h.ready[1].modId, 2);
  EXPECT_TRUE(h.downloader.idle());
}

TEST(Downloader, ReadyFilesOnDiskProceedWithoutBudget)
{
  Harness h;
  h.budget.remaining = 0;
  h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/one.zip"));
  h.port.addRow(2, 20, PortRowState::Ready, QStringLiteral("/dl/two.zip"));

  h.downloader.start(Domain, numbered(2));

  EXPECT_EQ(h.ready.size(), 2u);
  EXPECT_TRUE(h.failed.empty());
  EXPECT_TRUE(h.port.submissions.empty());
  EXPECT_TRUE(h.downloader.idle());
}

TEST(Downloader, SubmitWithoutRowFailsAtOnce)
{
  Harness h;
  h.port.rowsAppear = false;

  h.downloader.start(Domain, numbered(2));

  ASSERT_EQ(h.failed.size(), 2u);
  EXPECT_EQ(h.failed[0].modId, 1);
  EXPECT_EQ(h.failed[0].text.toStdString(), "download manager refused the request");
  EXPECT_EQ(h.failed[1].modId, 2) << "a refused file does not hold a window slot";
  EXPECT_TRUE(h.downloader.idle());
}

// ---- rows that already exist ----

TEST(Downloader, ActiveExistingRowIsAwaitedNotResubmitted)
{
  Harness h;
  h.port.addRow(1, 10, PortRowState::Active);

  h.downloader.start(Domain, numbered(1));

  EXPECT_TRUE(h.port.submissions.empty());
  EXPECT_TRUE(h.port.resumed.empty());
  EXPECT_FALSE(h.downloader.idle());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/one.zip"));
  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].text.toStdString(), "/dl/one.zip");
}

TEST(Downloader, PendingRequestFromElsewhereIsAwaitedNotResubmitted)
{
  Harness h;
  h.port.pending.insert({1, 10});  // queued by the user, no row yet

  h.downloader.start(Domain, numbered(1));

  EXPECT_TRUE(h.port.submissions.empty());
  EXPECT_FALSE(h.downloader.idle());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/one.zip"));
  EXPECT_EQ(h.ready.size(), 1u);
}

TEST(Downloader, PausedExistingRowIsResumed)
{
  Harness h;
  h.port.addRow(1, 10, PortRowState::Paused);  // gets index 100

  h.downloader.start(Domain, numbered(1));

  ASSERT_EQ(h.port.resumed.size(), 1u);
  EXPECT_EQ(h.port.resumed[0], 100);
  EXPECT_TRUE(h.port.submissions.empty());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/one.zip"));
  EXPECT_EQ(h.ready.size(), 1u);
}

TEST(Downloader, ErrorExistingRowIsRemovedAndResubmitted)
{
  Harness h;
  h.port.addRow(1, 10, PortRowState::Failed);  // an errored row, index 100

  h.downloader.start(Domain, numbered(1));

  ASSERT_EQ(h.port.removed.size(), 1u);
  EXPECT_EQ(h.port.removed[0], 100);
  ASSERT_EQ(h.port.submissions.size(), 1u);
  EXPECT_EQ(h.port.submissions[0].modId, 1);
  EXPECT_TRUE(h.failed.empty()) << "the old failure is not this run's failure";
}

TEST(Downloader, CanceledExistingRowIsRemovedAndResubmitted)
{
  // The port reports a canceled row in the same Failed state as an errored one.
  Harness h;
  h.port.addRow(1, 10, PortRowState::Failed);
  h.port.addRow(2, 20, PortRowState::Failed);

  h.downloader.start(Domain, numbered(2));

  ASSERT_EQ(h.port.removed.size(), 2u);
  EXPECT_EQ(h.port.removed[0], 100);
  EXPECT_EQ(h.port.removed[1], 101);
  ASSERT_EQ(h.port.submissions.size(), 2u);
  EXPECT_EQ(h.port.submissions[1].fileId, 20);
  EXPECT_TRUE(h.failed.empty());
}

TEST(Downloader, NeverSubmitsSamePairTwice)
{
  Harness h;
  std::vector<DownloadRequest> requests = numbered(2);
  requests.insert(requests.begin() + 1, requests[0]);  // (1, 10) listed twice

  h.downloader.start(Domain, requests);
  h.downloader.tick();
  h.downloader.tick();

  EXPECT_EQ(h.port.submissions.size(), 2u);

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/one.zip"));
  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/one.zip"));
  EXPECT_EQ(h.ready.size(), 1u) << "a pair listed twice is reported once";
}

TEST(Downloader, OnDiskFilesAreClassifiedBeforeAnythingIsSubmitted)
{
  Harness h;
  // File 1 is on disk but stale, so it goes back out. File 2 is on disk and fine; it must
  // not be submitted just because file 1's re-download happened first.
  h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/one.zip"));
  h.port.addRow(2, 20, PortRowState::Ready, QStringLiteral("/dl/two.zip"));
  h.hasher.hashes[QStringLiteral("/dl/one.zip")] = BadMd5;
  h.hasher.hashes[QStringLiteral("/dl/two.zip")] = GoodMd5;

  h.downloader.start(Domain, numbered(2, GoodMd5));

  ASSERT_EQ(h.port.submissions.size(), 1u);
  EXPECT_EQ(h.port.submissions[0].modId, 1);
  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].modId, 2);
  EXPECT_TRUE(h.failed.empty());
}

// ---- completion and failure ----

TEST(Downloader, CompletionMatchedByIds)
{
  Harness h;
  h.downloader.start(Domain, numbered(2));

  h.downloader.downloadCompleted(2, 20, QStringLiteral("/dl/b.zip"));

  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].modId, 2);
  EXPECT_EQ(h.ready[0].fileId, 20);
  EXPECT_EQ(h.ready[0].text.toStdString(), "/dl/b.zip");
  EXPECT_FALSE(h.downloader.idle()) << "file 1 is still outstanding";

  h.downloader.downloadCompleted(9, 90, QStringLiteral("/dl/other.zip"));
  EXPECT_EQ(h.ready.size(), 1u) << "a file nobody asked for is ignored";
  EXPECT_TRUE(h.failed.empty());
}

TEST(Downloader, FailureMatchedByIds)
{
  Harness h;
  h.downloader.start(Domain, numbered(2));

  h.downloader.downloadFailed(1, 10, QStringLiteral("transfer error"));

  ASSERT_EQ(h.failed.size(), 1u);
  EXPECT_EQ(h.failed[0].modId, 1);
  EXPECT_EQ(h.failed[0].fileId, 10);
  EXPECT_EQ(h.failed[0].text.toStdString(), "transfer error");
  EXPECT_TRUE(h.ready.empty());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/late.zip"));
  EXPECT_TRUE(h.ready.empty()) << "a settled file stays settled";
}

// ---- MD5 verification ----

TEST(Downloader, FreshFileWithBadMd5Fails)
{
  Harness h;
  h.hasher.hashes[QStringLiteral("/dl/a.zip")] = BadMd5;
  h.downloader.start(Domain, numbered(1, GoodMd5));

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/a.zip"));

  ASSERT_EQ(h.failed.size(), 1u);
  EXPECT_TRUE(h.failed[0].text.contains(QStringLiteral("MD5")));
  EXPECT_TRUE(h.ready.empty());
  EXPECT_TRUE(h.port.removed.empty()) << "a fresh download is not retried";
  EXPECT_EQ(h.port.submissions.size(), 1u);
}

TEST(Downloader, FreshFileWithGoodMd5IsReady)
{
  Harness h;
  // The manifest spells the hash in upper case, the hasher in lower case.
  h.hasher.hashes[QStringLiteral("/dl/a.zip")] = GoodMd5;
  h.downloader.start(Domain, numbered(1, GoodMd5.toUpper()));

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/a.zip"));

  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].text.toStdString(), "/dl/a.zip");
  EXPECT_TRUE(h.failed.empty());
}

TEST(Downloader, NoMd5SkipsHash)
{
  Harness h;
  h.downloader.start(Domain, numbered(1));

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/a.zip"));

  EXPECT_TRUE(h.hasher.requested.empty());
  EXPECT_EQ(h.ready.size(), 1u);
}

TEST(Downloader, ReusedFileWithBadMd5IsDownloadedOnceMore)
{
  Harness h;
  h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/old.zip"));  // index 100
  h.hasher.hashes[QStringLiteral("/dl/old.zip")] = BadMd5;
  h.hasher.hashes[QStringLiteral("/dl/new.zip")] = GoodMd5;

  h.downloader.start(Domain, numbered(1, GoodMd5));

  ASSERT_EQ(h.port.removed.size(), 1u);
  EXPECT_EQ(h.port.removed[0], 100);
  ASSERT_EQ(h.port.submissions.size(), 1u);
  EXPECT_TRUE(h.ready.empty());
  EXPECT_TRUE(h.failed.empty());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/new.zip"));

  ASSERT_EQ(h.ready.size(), 1u);
  EXPECT_EQ(h.ready[0].text.toStdString(), "/dl/new.zip");
  EXPECT_TRUE(h.failed.empty());
  EXPECT_EQ(h.port.submissions.size(), 1u);
}

TEST(Downloader, SecondMismatchFailsTheMod)
{
  Harness h;
  h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/old.zip"));
  h.hasher.hashes[QStringLiteral("/dl/old.zip")] = BadMd5;
  h.hasher.hashes[QStringLiteral("/dl/new.zip")] = BadMd5;
  h.downloader.start(Domain, numbered(1, GoodMd5));

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/new.zip"));
  h.downloader.tick();

  ASSERT_EQ(h.failed.size(), 1u);
  EXPECT_TRUE(h.failed[0].text.contains(QStringLiteral("again")));
  EXPECT_TRUE(h.ready.empty());
  EXPECT_EQ(h.port.submissions.size(), 1u) << "one extra download, never a third";
  EXPECT_EQ(h.port.removed.size(), 1u);
}

TEST(Downloader, ResubmissionCountsAgainstBudget)
{
  {
    // Nothing left to spend: the stale file is removed and the mod fails with the
    // budget reason instead of waiting for a reset that may be hours away.
    Harness h;
    h.budget.remaining = 3;
    h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/old.zip"));
    h.hasher.hashes[QStringLiteral("/dl/old.zip")] = BadMd5;

    h.downloader.start(Domain, numbered(1, GoodMd5));

    EXPECT_EQ(h.port.removed.size(), 1u);
    EXPECT_TRUE(h.port.submissions.empty());
    ASSERT_EQ(h.failed.size(), 1u);
    EXPECT_EQ(h.failed[0].text.toStdString(), BudgetReason);
  }
  {
    Harness h;
    h.budget.remaining = 4;  // more than the 3 requests one file costs
    h.port.addRow(1, 10, PortRowState::Ready, QStringLiteral("/dl/old.zip"));
    h.hasher.hashes[QStringLiteral("/dl/old.zip")] = BadMd5;

    h.downloader.start(Domain, numbered(1, GoodMd5));

    EXPECT_EQ(h.port.submissions.size(), 1u);
    EXPECT_TRUE(h.failed.empty());
  }
}

// ---- stall backstop ----

TEST(Downloader, StallAfterTenMinutesFailsRetryable)
{
  Harness h;
  h.downloader.start(Domain, numbered(1));
  ASSERT_EQ(h.port.submissions.size(), 1u);

  h.now = CollectionDownloader::StallMs - 1;
  h.downloader.tick();
  EXPECT_TRUE(h.failed.empty());

  h.now = CollectionDownloader::StallMs;
  h.downloader.tick();
  ASSERT_EQ(h.failed.size(), 1u);
  EXPECT_TRUE(h.failed[0].text.contains(QStringLiteral("Nexus did not answer")));
  EXPECT_TRUE(h.failed[0].text.contains(QStringLiteral("request limit")))
      << "the reason names the limit that probably caused the silence";

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/late.zip"));
  EXPECT_TRUE(h.ready.empty()) << "a failed file is not revived by a late answer";
}

TEST(Downloader, ReusedActiveRowStallsAfterTenMinutes)
{
  // A row somebody else started, whose description request was wiped with the queue,
  // would otherwise leave the run waiting for good.
  Harness h;
  h.port.addRow(1, 10, PortRowState::Active);
  h.downloader.start(Domain, numbered(1));
  ASSERT_TRUE(h.port.submissions.empty());

  h.now = CollectionDownloader::StallMs - 1;
  h.downloader.tick();
  EXPECT_TRUE(h.failed.empty());

  h.now = CollectionDownloader::StallMs;
  h.downloader.tick();
  ASSERT_EQ(h.failed.size(), 1u);
  EXPECT_TRUE(h.failed[0].text.contains(QStringLiteral("request limit")));
  EXPECT_TRUE(h.downloader.idle());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/late.zip"));
  EXPECT_TRUE(h.ready.empty());
}

TEST(Downloader, StateChangeResetsStallClock)
{
  Harness h;
  h.downloader.start(Domain, numbered(4));  // 1, 2 and 3 go out at t = 0
  ASSERT_EQ(h.port.submissions.size(), 3u);

  h.now = 9 * Minute;
  h.downloader.transferStarted(1, 10);  // file 1 leaves the stall watch; file 4 goes out
  ASSERT_EQ(h.port.submissions.size(), 4u);

  h.now = CollectionDownloader::StallMs;
  h.downloader.tick();
  ASSERT_EQ(h.failed.size(), 2u) << "files 2 and 3 never started";
  EXPECT_EQ(h.failed[0].modId, 2);
  EXPECT_EQ(h.failed[1].modId, 3);

  h.now = 9 * Minute + CollectionDownloader::StallMs;
  h.downloader.tick();
  ASSERT_EQ(h.failed.size(), 3u) << "file 4 is measured from its own submission";
  EXPECT_EQ(h.failed[2].modId, 4);

  h.now = 60 * Minute;
  h.downloader.tick();
  EXPECT_EQ(h.failed.size(), 3u) << "file 1 is mid-transfer and never stalls";
  EXPECT_FALSE(h.downloader.idle());
}

// ---- cancel ----

TEST(Downloader, CancelStopsSubmissions)
{
  Harness h;
  h.downloader.start(Domain, numbered(5));
  ASSERT_EQ(h.port.submissions.size(), 3u);

  h.cancel->store(true);
  h.downloader.transferStarted(1, 10);
  h.downloader.tick();
  h.downloader.downloadCompleted(2, 20, QStringLiteral("/dl/b.zip"));
  h.downloader.downloadFailed(3, 30, QStringLiteral("boom"));

  EXPECT_EQ(h.port.submissions.size(), 3u);
  EXPECT_TRUE(h.ready.empty());
  EXPECT_TRUE(h.failed.empty());
  EXPECT_TRUE(h.downloader.idle());
}

TEST(Downloader, CancelIgnoresLateHashResults)
{
  Harness h;
  h.hasher.deferred = true;
  h.hasher.hashes[QStringLiteral("/dl/a.zip")] = GoodMd5;
  h.downloader.start(Domain, numbered(1, GoodMd5));
  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/a.zip"));
  ASSERT_EQ(h.hasher.pending.size(), 1u);

  h.cancel->store(true);
  h.hasher.flush();

  EXPECT_TRUE(h.ready.empty());
  EXPECT_TRUE(h.failed.empty());
  EXPECT_TRUE(h.downloader.idle());
}

TEST(Downloader, IdleAfterAllSettled)
{
  Harness h;
  EXPECT_TRUE(h.downloader.idle()) << "nothing started, nothing outstanding";

  h.downloader.start(Domain, numbered(2));
  EXPECT_FALSE(h.downloader.idle());

  h.downloader.downloadCompleted(1, 10, QStringLiteral("/dl/a.zip"));
  EXPECT_FALSE(h.downloader.idle());

  h.downloader.downloadFailed(2, 20, QStringLiteral("boom"));
  EXPECT_TRUE(h.downloader.idle());
}

TEST(Downloader, DestructionDetachesFromThePort)
{
  FakeDownloadPort port;
  FakeBudget budget;
  FakeHasher hasher;
  {
    CollectionDownloader downloader(
        port, budget, hasher, std::make_shared<std::atomic_bool>(false),
        [] { return qint64(0); }, [](int, int, const QString&) {},
        [](int, int, const QString&) {});
    EXPECT_NE(port.listener(), nullptr);
  }
  EXPECT_EQ(port.listener(), nullptr);
}

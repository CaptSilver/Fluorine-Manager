#include "collections/collectiondownloader.h"

#include <gtest/gtest.h>

#include <atomic>
#include <map>
#include <memory>
#include <ostream>
#include <set>
#include <utility>
#include <vector>

// Readable QString output in gtest failure messages. Internal linkage so other
// test files can define their own without a duplicate symbol.
static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << qPrintable(text) << '"';
}

namespace {
using Ids = std::pair<int, int>;

class FakePort : public DownloadPort {
public:
  void setListener(DownloadPortListener *newListener) override {
    listener = newListener;
  }

  PortRow find(int modId, int fileId) const override {
    const auto it = rows.find(Ids(modId, fileId));
    return it == rows.end() ? PortRow{} : it->second;
  }

  void submit(const QString &domain, int modId, int fileId) override {
    submitted.push_back(Ids(modId, fileId));
    domains.push_back(domain);
    if (appearsAfterSubmit) {
      pending.insert(Ids(modId, fileId));
    }
  }

  bool appeared(int modId, int fileId) const override {
    const Ids ids(modId, fileId);
    const auto row = rows.find(ids);
    return pending.count(ids) > 0 ||
           (row != rows.end() && row->second.state == PortRowState::Active);
  }

  void resume(int index) override { resumed.push_back(index); }

  void remove(int index) override {
    removed.push_back(index);
    for (auto it = rows.begin(); it != rows.end();) {
      it = it->second.index == index ? rows.erase(it) : std::next(it);
    }
  }

  DownloadPortListener *listener = nullptr;
  std::map<Ids, PortRow> rows;
  std::set<Ids> pending;
  bool appearsAfterSubmit = true;
  std::vector<Ids> submitted;
  std::vector<QString> domains;
  std::vector<int> resumed;
  std::vector<int> removed;
};

class FakeBudget : public NexusBudgetPort {
public:
  int remainingRequests() const override { return remaining; }
  int remaining = 1000;
};

// Queues the callback so a test decides when a digest arrives.
class FakeHasher : public FileHasher {
public:
  void md5(const QString &path,
           std::function<void(const QString &)> done) override {
    queue.emplace_back(path, std::move(done));
  }
  std::vector<std::pair<QString, std::function<void(const QString &)>>> queue;
};

struct Event {
  int modId;
  int fileId;
  QString text;
};

PortRow row(PortRowState state, int index = 1,
            const QString &path = QString()) {
  PortRow result;
  result.state = state;
  result.index = index;
  result.path = path;
  return result;
}

// Request i is mod 100 + i, file 1000 + i.
Ids ids(int i) { return Ids(100 + i, 1000 + i); }

std::vector<DownloadRequest> requests(int count) {
  std::vector<DownloadRequest> result;
  for (int i = 0; i < count; ++i) {
    DownloadRequest request;
    request.modId = ids(i).first;
    request.fileId = ids(i).second;
    result.push_back(request);
  }
  return result;
}

struct Fixture {
  FakePort port;
  FakeBudget budget;
  FakeHasher hasher;
  qint64 now = 0;
  std::vector<Event> ready;
  std::vector<Event> failed;
  CollectionDownloader downloader; // last: it uses everything above

  Fixture()
      : downloader(
            port, budget, hasher, std::make_shared<std::atomic_bool>(false),
            [this] { return now; },
            [this](int modId, int fileId, const QString &path) {
              ready.push_back({modId, fileId, path});
            },
            [this](int modId, int fileId, const QString &reason) {
              failed.push_back({modId, fileId, reason});
            }) {}
};

const QString BudgetReason =
    QStringLiteral("Nexus request budget used up, retry after the daily reset");
const QString RefusedReason =
    QStringLiteral("download manager refused the request");
} // namespace

// --- window
// ----------------------------------------------------------------------------

TEST(Downloader, SubmitsAtMostThreeBeforeTransferStarts) {
  Fixture f;
  f.downloader.start("cyberpunk2077", requests(5));
  EXPECT_EQ(f.port.submitted, (std::vector<Ids>{ids(0), ids(1), ids(2)}));
  EXPECT_EQ(f.port.domains, (std::vector<QString>(3, "cyberpunk2077")));

  f.downloader.tick();
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 3u);
  EXPECT_TRUE(f.failed.empty());
  EXPECT_FALSE(f.downloader.idle());
}

TEST(Downloader, TransferStartFreesWindowSlot) {
  Fixture f;
  f.downloader.start("cyberpunk2077", requests(5));
  ASSERT_EQ(f.port.submitted.size(), 3u);

  f.downloader.transferStarted(ids(0).first, ids(0).second);
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 4u);
  EXPECT_EQ(f.port.submitted.back(), ids(3));

  // Starting the same transfer again must not free a second slot.
  f.downloader.transferStarted(ids(0).first, ids(0).second);
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 4u);

  // A completion also leaves the window.
  f.downloader.downloadCompleted(ids(1).first, ids(1).second, "/dl/b.7z");
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 5u);
  EXPECT_EQ(f.port.submitted.back(), ids(4));
}

// --- budget
// ------------------------------------------------------------------------------

TEST(Downloader, NoSubmissionAtBudgetBoundary) {
  Fixture f;
  f.budget.remaining = 9;
  f.downloader.start("cyberpunk2077", requests(5));
  // The third file would need remaining > 3 * (2 + 1) = 9.
  EXPECT_EQ(f.port.submitted.size(), 2u);
  // Waiting for in-flight files to settle is not a failure.
  EXPECT_TRUE(f.failed.empty());

  f.budget.remaining = 10;
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 3u);

  // Between "can still afford one" (more than 3) and "can afford a full
  // window": wait, do not fail.
  f.budget.remaining = 4;
  f.downloader.transferStarted(ids(0).first, ids(0).second);
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 3u);
  EXPECT_TRUE(f.failed.empty());
}

TEST(Downloader, BudgetExhaustionFailsWaitingModsOnly) {
  Fixture f;
  f.budget.remaining = 3;
  f.port.rows[ids(0)] = row(PortRowState::Ready, 10, "/dl/a.7z");
  f.port.rows[ids(1)] = row(PortRowState::Active, 11);
  f.downloader.start("cyberpunk2077", requests(4));

  // On disk: proceeds without touching the budget.
  ASSERT_EQ(f.ready.size(), 1u);
  EXPECT_EQ(f.ready[0].modId, ids(0).first);
  EXPECT_EQ(f.ready[0].text, QString("/dl/a.7z"));
  // Already downloading: still awaited, not failed.
  // Never downloaded: nothing left to pay for it.
  ASSERT_EQ(f.failed.size(), 2u);
  EXPECT_EQ(f.failed[0].modId, ids(2).first);
  EXPECT_EQ(f.failed[0].text, BudgetReason);
  EXPECT_EQ(f.failed[1].modId, ids(3).first);
  EXPECT_EQ(f.failed[1].text, BudgetReason);
  EXPECT_TRUE(f.port.submitted.empty());
  EXPECT_FALSE(f.downloader.idle());

  f.downloader.downloadCompleted(ids(1).first, ids(1).second, "/dl/b.7z");
  ASSERT_EQ(f.ready.size(), 2u);
  EXPECT_EQ(f.ready[1].text, QString("/dl/b.7z"));
  EXPECT_TRUE(f.downloader.idle());
}

TEST(Downloader, ReadyFilesOnDiskProceedWithoutBudget) {
  Fixture f;
  f.budget.remaining = 0;
  f.port.rows[ids(0)] = row(PortRowState::Ready, 10, "/dl/a.7z");
  f.port.rows[ids(1)] = row(PortRowState::Ready, 11, "/dl/b.7z");
  f.port.rows[ids(2)] = row(PortRowState::Ready, 12, "/dl/c.7z");
  f.downloader.start("cyberpunk2077", requests(3));

  ASSERT_EQ(f.ready.size(), 3u);
  EXPECT_EQ(f.ready[0].text, QString("/dl/a.7z"));
  EXPECT_EQ(f.ready[1].text, QString("/dl/b.7z"));
  EXPECT_EQ(f.ready[2].text, QString("/dl/c.7z"));
  EXPECT_TRUE(f.failed.empty());
  EXPECT_TRUE(f.port.submitted.empty());
  EXPECT_TRUE(f.downloader.idle());
}

TEST(Downloader, SubmitWithoutRowFailsAtOnce) {
  Fixture f;
  f.port.appearsAfterSubmit = false;
  f.downloader.start("cyberpunk2077", requests(2));

  // A refused submission frees its slot straight away, so both are tried.
  EXPECT_EQ(f.port.submitted.size(), 2u);
  ASSERT_EQ(f.failed.size(), 2u);
  EXPECT_EQ(f.failed[0].text, RefusedReason);
  EXPECT_EQ(f.failed[1].text, RefusedReason);
  EXPECT_TRUE(f.downloader.idle());
}

// --- rows that already exist
// -------------------------------------------------------------

TEST(Downloader, ActiveExistingRowIsAwaitedNotResubmitted) {
  Fixture f;
  f.port.rows[ids(0)] = row(PortRowState::Active, 10);
  f.downloader.start("cyberpunk2077", requests(4));

  // Request 0 was not submitted by this run, so it takes no window slot: the
  // other three still go out together.
  EXPECT_EQ(f.port.submitted, (std::vector<Ids>{ids(1), ids(2), ids(3)}));
  EXPECT_TRUE(f.ready.empty());

  f.downloader.downloadCompleted(ids(0).first, ids(0).second, "/dl/a.7z");
  ASSERT_EQ(f.ready.size(), 1u);
  EXPECT_EQ(f.ready[0].modId, ids(0).first);
}

TEST(Downloader, PendingRequestFromElsewhereIsAwaitedNotResubmitted) {
  Fixture f;
  f.port.pending.insert(ids(0)); // queued by the user, no row yet
  f.downloader.start("cyberpunk2077", requests(1));
  EXPECT_TRUE(f.port.submitted.empty());
  EXPECT_FALSE(f.downloader.idle());
}

TEST(Downloader, PausedExistingRowIsResumed) {
  Fixture f;
  f.port.rows[ids(0)] = row(PortRowState::Paused, 7);
  f.downloader.start("cyberpunk2077", requests(1));

  EXPECT_EQ(f.port.resumed, std::vector<int>{7});
  EXPECT_TRUE(f.port.submitted.empty());
  EXPECT_FALSE(f.downloader.idle());

  f.downloader.downloadCompleted(ids(0).first, ids(0).second, "/dl/a.7z");
  ASSERT_EQ(f.ready.size(), 1u);
  EXPECT_TRUE(f.downloader.idle());
}

TEST(Downloader, ErrorExistingRowIsRemovedAndResubmitted) {
  Fixture f;
  f.port.rows[ids(0)] = row(PortRowState::Failed, 9);
  f.downloader.start("cyberpunk2077", requests(1));

  EXPECT_EQ(f.port.removed, std::vector<int>{9});
  EXPECT_EQ(f.port.submitted, std::vector<Ids>{ids(0)});
  EXPECT_TRUE(f.failed.empty());
}

TEST(Downloader, CanceledExistingRowIsRemovedAndResubmitted) {
  // The port reports both canceled and errored rows as Failed.
  Fixture f;
  f.port.rows[ids(0)] = row(PortRowState::Failed, 4);
  f.port.rows[ids(1)] = row(PortRowState::Failed, 5);
  f.downloader.start("cyberpunk2077", requests(2));

  EXPECT_EQ(f.port.removed, (std::vector<int>{4, 5}));
  EXPECT_EQ(f.port.submitted, (std::vector<Ids>{ids(0), ids(1)}));
}

TEST(Downloader, NeverSubmitsSamePairTwice) {
  Fixture f;
  std::vector<DownloadRequest> list = requests(2);
  list.insert(list.begin() + 1, list[0]); // the same Nexus file named twice
  f.downloader.start("cyberpunk2077", list);
  f.downloader.tick();
  f.downloader.tick();

  EXPECT_EQ(f.port.submitted, (std::vector<Ids>{ids(0), ids(1)}));

  // Still in flight: ticking again must not "Already Queued" the download
  // manager.
  f.downloader.transferStarted(ids(0).first, ids(0).second);
  f.downloader.tick();
  EXPECT_EQ(f.port.submitted.size(), 2u);
}

TEST(Downloader, DestructionDetachesFromThePort) {
  FakePort port;
  FakeBudget budget;
  FakeHasher hasher;
  {
    CollectionDownloader downloader(
        port, budget, hasher, std::make_shared<std::atomic_bool>(false),
        [] { return qint64(0); }, [](int, int, const QString &) {},
        [](int, int, const QString &) {});
    EXPECT_NE(port.listener, nullptr);
  }
  EXPECT_EQ(port.listener, nullptr);
}

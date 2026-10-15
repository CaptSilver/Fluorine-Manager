#include "collections/collectionmanifestfetcher.h"

#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>

#include <gtest/gtest.h>

#include <deque>
#include <functional>
#include <optional>

#ifndef FLUORINE_TEST_SOURCE_DIR
#error "FLUORINE_TEST_SOURCE_DIR must name the repository root"
#endif

namespace {

// A reply that serves fixed bytes, optionally claiming a different
// Content-Length, or never finishing at all.
class ScriptedReply : public QNetworkReply {
public:
  ScriptedReply(const QNetworkRequest &request,
                QNetworkAccessManager::Operation operation, QByteArray bytes,
                int status, qint64 declaredLength, bool hang, QObject *parent)
      : QNetworkReply(parent), m_bytes(std::move(bytes)) {
    setRequest(request);
    setUrl(request.url());
    setOperation(operation);
    setAttribute(QNetworkRequest::HttpStatusCodeAttribute, status);
    if (declaredLength >= 0) {
      setHeader(QNetworkRequest::ContentLengthHeader,
                QVariant::fromValue<qlonglong>(declaredLength));
    }
    if (status >= 400) {
      setError(QNetworkReply::InternalServerError,
               QStringLiteral("scripted failure"));
    }
    open(QIODevice::ReadOnly | QIODevice::Unbuffered);
    if (!hang) {
      QTimer::singleShot(0, this, [this] {
        if (!isFinished()) {
          if (!m_bytes.isEmpty()) {
            emit readyRead();
          }
          setFinished(true);
          emit finished();
        }
      });
    }
  }

  void abort() override {
    if (isFinished()) {
      return;
    }
    setError(OperationCanceledError, QStringLiteral("aborted"));
    setFinished(true);
    emit finished();
  }

  qint64 bytesAvailable() const override {
    return m_bytes.size() - m_offset + QNetworkReply::bytesAvailable();
  }

protected:
  qint64 readData(char *data, qint64 max) override {
    const qint64 count = qMin(max, qint64(m_bytes.size() - m_offset));
    if (count <= 0) {
      return -1;
    }
    memcpy(data, m_bytes.constData() + m_offset, count);
    m_offset += count;
    return count;
  }

private:
  QByteArray m_bytes;
  qsizetype m_offset = 0;
};

// Hands out scripted answers in order and records what was asked.
class ScriptedNetwork : public QNetworkAccessManager {
public:
  struct Answer {
    QByteArray bytes;
    int status = 200;
    qint64 declaredLength = -1;
    bool hang = false;
  };

  std::deque<Answer> answers;
  QList<QNetworkRequest> requests;
  QList<QByteArray> bodies;

protected:
  QNetworkReply *createRequest(Operation operation,
                               const QNetworkRequest &request,
                               QIODevice *outgoing) override {
    requests << request;
    bodies << (outgoing ? outgoing->readAll() : QByteArray{});
    Answer answer;
    if (answers.empty()) {
      answer.status = 500;
    } else {
      answer = std::move(answers.front());
      answers.pop_front();
    }
    return new ScriptedReply(request, operation, answer.bytes, answer.status,
                             answer.declaredLength, answer.hang, this);
  }
};

ScriptedNetwork::Answer jsonAnswer(const QByteArray &json) {
  ScriptedNetwork::Answer answer;
  answer.bytes = json;
  return answer;
}

ScriptedNetwork::Answer statusAnswer(int status) {
  ScriptedNetwork::Answer answer;
  answer.status = status;
  return answer;
}

QByteArray fixtureArchive() {
  QFile file(
      QString::fromUtf8(FLUORINE_TEST_SOURCE_DIR
                        "/src/tests/fixtures/collections/collection-min.7z"));
  EXPECT_TRUE(file.open(QIODevice::ReadOnly));
  return file.readAll();
}

QByteArray revisionAnswer(int number, int schema, const QString &downloadLink) {
  const QJsonObject revision{{"revisionNumber", number},
                             {"collectionSchemaId", schema},
                             {"downloadLink", downloadLink}};
  const QJsonObject root{
      {"data", QJsonObject{{"collectionRevision", revision}}}};
  return QJsonDocument(root).toJson(QJsonDocument::Compact);
}

QByteArray mirrorList(const QStringList &uris) {
  QJsonArray links;
  for (const QString &uri : uris) {
    links.append(QJsonObject{{"URI", uri}, {"name", "mirror"}});
  }
  return QJsonDocument(QJsonObject{{"download_links", links}})
      .toJson(QJsonDocument::Compact);
}

const QString DownloadLink =
    QStringLiteral("/v2/collections/99/revisions/3/download_link");

bool spinUntil(const std::function<bool()> &done, int timeoutMs = 15000) {
  QElapsedTimer timer;
  timer.start();
  while (!done() && timer.elapsed() < timeoutMs) {
    QTest::qWait(5);
  }
  return done();
}

// A fetcher wired to two scripted networks: `api` plays api.nexusmods.com,
// `mirrors` plays the CDN. The cache root is a private temporary folder.
struct Rig {
  Rig()
      : fetcher(
            [this](const QUrl &url, const QByteArray &body) -> QNetworkReply * {
              if (apiOffline) {
                return nullptr;
              }
              QNetworkRequest request(url);
              return body.isEmpty() ? api.get(request)
                                    : api.post(request, body);
            },
            [this](const QUrl &url) -> QNetworkReply * {
              return mirrors.get(QNetworkRequest(url));
            },
            cache.path() + QStringLiteral("/collections")) {
    link.domain = QStringLiteral("cyberpunk2077");
    link.slug = QStringLiteral("mini");
    link.revision = 3;

    QObject::connect(
        &fetcher, &CollectionManifestFetcher::ready, &fetcher,
        [this](const CollectionManifest &parsed, int rev, const QString &dir) {
          ++readyCount;
          manifest = parsed;
          revision = rev;
          cacheDir = dir;
        });
    QObject::connect(&fetcher, &CollectionManifestFetcher::failed, &fetcher,
                     [this](const QString &reason) {
                       ++failedCount;
                       failure = reason;
                     });
  }

  // The revision answer and a mirror list; a test appends the mirror answers.
  void queueApi(const QStringList &mirrorUris, int number = 3, int schema = 1) {
    api.answers.push_back(
        jsonAnswer(revisionAnswer(number, schema, DownloadLink)));
    api.answers.push_back(jsonAnswer(mirrorList(mirrorUris)));
  }

  void queueArchive() {
    ScriptedNetwork::Answer answer;
    answer.bytes = fixtureArchive();
    mirrors.answers.push_back(answer);
  }

  bool finished() const { return readyCount + failedCount > 0; }

  QString expectedCacheDir() const {
    return cache.path() + QStringLiteral("/collections/mini-r3");
  }

  ScriptedNetwork api;
  ScriptedNetwork mirrors;
  QTemporaryDir cache;
  bool apiOffline = false;
  CollectionManifestFetcher fetcher;
  CollectionLink link;

  int readyCount = 0;
  int failedCount = 0;
  QString failure;
  std::optional<CollectionManifest> manifest;
  int revision = 0;
  QString cacheDir;
};

} // namespace

// ---- request shape ----

TEST(Fetcher, GraphqlBodyCarriesSlugDomainRevisionAndAdultFlag) {
  CollectionLink link;
  link.domain = QStringLiteral("cyberpunk2077");
  link.slug = QStringLiteral("mini-pack_2");
  link.revision = 7;

  const QJsonObject body =
      QJsonDocument::fromJson(CollectionManifestFetcher::graphqlBody(link))
          .object();
  const QJsonObject variables = body.value("variables").toObject();

  EXPECT_EQ(variables.value("slug").toString().toStdString(), "mini-pack_2");
  EXPECT_EQ(variables.value("domain").toString().toStdString(),
            "cyberpunk2077");
  EXPECT_EQ(variables.value("revision").toInt(), 7);

  const QString query = body.value("query").toString();
  EXPECT_TRUE(query.contains(QStringLiteral("viewAdultContent:true")));
  EXPECT_TRUE(query.contains(QStringLiteral("$revision:Int!")));
  EXPECT_TRUE(query.contains(QStringLiteral("revisionNumber")));
  EXPECT_TRUE(query.contains(QStringLiteral("collectionSchemaId")));
  EXPECT_TRUE(query.contains(QStringLiteral("downloadLink")));
}

TEST(Fetcher, DownloadLinkMustStartWithV2Collections) {
  EXPECT_TRUE(CollectionManifestFetcher::downloadLinkAllowed(DownloadLink));
  EXPECT_FALSE(CollectionManifestFetcher::downloadLinkAllowed(QString()));
  EXPECT_FALSE(CollectionManifestFetcher::downloadLinkAllowed(
      QStringLiteral("/v2/graphql")));
  EXPECT_FALSE(CollectionManifestFetcher::downloadLinkAllowed(
      QStringLiteral("/v1/games/x/mods/1/files/2/download_link.json")));
  EXPECT_FALSE(CollectionManifestFetcher::downloadLinkAllowed(
      QStringLiteral("v2/collections/1/revisions/2/download_link")));
}

TEST(Fetcher, DownloadLinkWithDotDotQueryFragmentOrHostRefused) {
  const QStringList refused = {
      QStringLiteral("/v2/collections/../graphql"),
      QStringLiteral("/v2/collections/1/revisions/2/download_link?key=secret"),
      QStringLiteral("/v2/collections/1/revisions/2/download_link#frag"),
      QStringLiteral(
          "https://evil.test/v2/collections/1/revisions/2/download_link"),
      QStringLiteral("//evil.test/v2/collections/1/revisions/2/download_link"),
      QStringLiteral("/v2/collections//evil.test/x"),
      QStringLiteral("/v2/collections/user@evil.test/x"),
  };
  for (const QString &path : refused) {
    EXPECT_FALSE(CollectionManifestFetcher::downloadLinkAllowed(path))
        << path.toStdString();
  }
}

// ---- API answers ----

TEST(Fetcher, NullApiReplyIsNotLoggedIn) {
  Rig rig;
  rig.apiOffline = true;

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("not logged in")));
  EXPECT_TRUE(rig.api.requests.isEmpty());
}

TEST(Fetcher, SlugIsAcceptedAsTypedInTheLink) {
  // CollectionLink::parse keeps the slug's case, and the link precheck has
  // already passed by the time the fetcher sees it. It must not fail with "not
  // a valid collection link".
  Rig rig;
  rig.apiOffline = true;
  rig.link.slug = QStringLiteral("MiNi-Pack_2");

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("not logged in")))
      << rig.failure.toStdString();
}

TEST(Fetcher, RevisionMismatchFails) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")}, /*number=*/4);

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("revision")));
  EXPECT_EQ(rig.api.requests.size(), 1)
      << "no package request after a wrong revision";
  EXPECT_TRUE(rig.mirrors.requests.isEmpty());
}

TEST(Fetcher, SchemaOtherThanOneFails) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")}, 3,
               /*schema=*/2);

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("schema")));
  EXPECT_EQ(rig.api.requests.size(), 1);
}

TEST(Fetcher, NullRevisionIsNotFound) {
  Rig rig;
  rig.api.answers.push_back(
      jsonAnswer(QByteArrayLiteral(R"({"data":{"collectionRevision":null}})")));

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("not found")));
}

TEST(Fetcher, UnsafeDownloadLinkFailsBeforeAnySecondRequest) {
  Rig rig;
  rig.api.answers.push_back(jsonAnswer(revisionAnswer(
      3, 1, QStringLiteral("https://evil.test/v2/collections/1"))));

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_EQ(rig.api.requests.size(), 1);
}

// ---- mirrors ----

TEST(Fetcher, FirstMirrorErrorTriesNext) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn1.example.test/c.7z"),
                QStringLiteral("https://cdn2.example.test/c.7z")});
  rig.mirrors.answers.push_back(statusAnswer(500));
  rig.queueArchive();

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 0) << rig.failure.toStdString();
  EXPECT_EQ(rig.readyCount, 1);
  ASSERT_EQ(rig.mirrors.requests.size(), 2);
  EXPECT_EQ(rig.mirrors.requests[0].url().host().toStdString(),
            "cdn1.example.test");
  EXPECT_EQ(rig.mirrors.requests[1].url().host().toStdString(),
            "cdn2.example.test");
}

TEST(Fetcher, MirrorsUsePublicGetNotApi) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")});
  rig.queueArchive();

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  ASSERT_EQ(rig.readyCount, 1) << rig.failure.toStdString();

  ASSERT_EQ(rig.api.requests.size(), 2);
  EXPECT_EQ(rig.api.requests[0].url().toString().toStdString(),
            "https://api.nexusmods.com/v2/graphql");
  EXPECT_FALSE(rig.api.bodies[0].isEmpty()) << "the GraphQL call is a POST";
  EXPECT_EQ(rig.api.requests[1].url().toString().toStdString(),
            "https://api.nexusmods.com" + DownloadLink.toStdString());
  EXPECT_TRUE(rig.api.bodies[1].isEmpty()) << "the package location is a GET";

  ASSERT_EQ(rig.mirrors.requests.size(), 1);
  EXPECT_EQ(rig.mirrors.requests[0].url().host().toStdString(),
            "cdn.example.test");
  EXPECT_FALSE(rig.mirrors.requests[0].hasRawHeader("Authorization"));
  EXPECT_FALSE(rig.mirrors.requests[0].hasRawHeader("APIKEY"));
}

TEST(Fetcher, NonHttpsMirrorRefused) {
  {
    Rig rig;
    rig.queueApi({QStringLiteral("http://cdn.example.test/c.7z"),
                  QStringLiteral("ftp://cdn.example.test/c.7z")});

    rig.fetcher.fetch(rig.link);

    ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
    EXPECT_EQ(rig.failedCount, 1);
    EXPECT_TRUE(rig.failure.contains(QStringLiteral("https")));
    EXPECT_TRUE(rig.mirrors.requests.isEmpty());
  }
  {
    // A plain-http mirror is skipped, not fatal, when an https one is on the
    // list.
    Rig rig;
    rig.queueApi({QStringLiteral("http://insecure.example.test/c.7z"),
                  QStringLiteral("https://secure.example.test/c.7z")});
    rig.queueArchive();

    rig.fetcher.fetch(rig.link);

    ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
    EXPECT_EQ(rig.readyCount, 1) << rig.failure.toStdString();
    ASSERT_EQ(rig.mirrors.requests.size(), 1);
    EXPECT_EQ(rig.mirrors.requests[0].url().host().toStdString(),
              "secure.example.test");
  }
}

TEST(Fetcher, OversizeArchiveRefused) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")});
  ScriptedNetwork::Answer huge;
  huge.bytes = QByteArrayLiteral("tiny");
  huge.declaredLength = 5LL * 1024 * 1024 * 1024;
  rig.mirrors.answers.push_back(huge);

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  EXPECT_EQ(rig.failedCount, 1);
  EXPECT_TRUE(rig.failure.contains(QStringLiteral("4 GiB")));
  EXPECT_FALSE(
      QFile::exists(rig.expectedCacheDir() + QStringLiteral("/collection.7z")))
      << "a refused download leaves no partial archive behind";
}

// ---- result ----

TEST(Fetcher, ExtractsOnlyCollectionJsonAndParses) {
  Rig rig;
  rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")});
  rig.queueArchive();

  rig.fetcher.fetch(rig.link);

  ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
  ASSERT_EQ(rig.readyCount, 1) << rig.failure.toStdString();
  EXPECT_EQ(rig.failedCount, 0);

  ASSERT_TRUE(rig.manifest.has_value());
  EXPECT_EQ(rig.manifest->info.name.toStdString(), "Mini Collection");
  EXPECT_EQ(rig.manifest->info.domainName.toStdString(), "cyberpunk2077");
  ASSERT_EQ(rig.manifest->mods.size(), 1u);
  EXPECT_EQ(rig.manifest->mods[0].source.fileId, 22);
  EXPECT_EQ(rig.revision, 3);

  EXPECT_EQ(rig.cacheDir.toStdString(), rig.expectedCacheDir().toStdString());
  EXPECT_TRUE(QFile::exists(rig.cacheDir + QStringLiteral("/collection.json")));
  EXPECT_FALSE(QDir(rig.cacheDir + QStringLiteral("/bundled")).exists())
      << "only collection.json comes out of the archive";
  EXPECT_TRUE(QFile::exists(rig.cacheDir + QStringLiteral("/collection.7z")))
      << "the archive stays for a retry";
}

TEST(Fetcher, CancelAlwaysCallsBack) {
  {
    // A reply that never answers: cancel ends the fetch with exactly one
    // callback.
    Rig rig;
    ScriptedNetwork::Answer stuck;
    stuck.hang = true;
    rig.api.answers.push_back(stuck);

    rig.fetcher.fetch(rig.link);
    EXPECT_EQ(rig.failedCount, 0);
    rig.fetcher.cancel();

    EXPECT_EQ(rig.failedCount, 1);
    EXPECT_EQ(rig.failure.toStdString(), "cancelled");
    QTest::qWait(50);
    EXPECT_EQ(rig.failedCount, 1)
        << "a late finish of the aborted reply is ignored";
    EXPECT_EQ(rig.readyCount, 0);

    rig.fetcher.cancel();
    EXPECT_EQ(rig.failedCount, 1)
        << "nothing is running, so there is nothing to report";
  }
  {
    // After the fetch finished, cancel() is not another failure.
    Rig rig;
    rig.queueApi({QStringLiteral("https://cdn.example.test/c.7z")});
    rig.queueArchive();
    rig.fetcher.fetch(rig.link);
    ASSERT_TRUE(spinUntil([&] { return rig.finished(); }));
    ASSERT_EQ(rig.readyCount, 1) << rig.failure.toStdString();

    rig.fetcher.cancel();

    EXPECT_EQ(rig.failedCount, 0);
  }
}

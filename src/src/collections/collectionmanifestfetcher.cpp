#include "collectionmanifestfetcher.h"

#include <archive/archive.h>
#include <uibase/log.h>

#include <QDir>
#include <QFile>
#include <QFutureWatcher>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSaveFile>
#include <QtConcurrent/QtConcurrentRun>

#include <optional>

namespace
{

constexpr auto GraphqlUrl = "https://api.nexusmods.com/v2/graphql";
constexpr auto ApiOrigin  = "https://api.nexusmods.com";
constexpr int MaxRedirects = 5;

// A manifest with a few thousand mods is a few megabytes. Anything near this is not one.
constexpr qint64 MaxManifestBytes = 64LL * 1024 * 1024;

QString describeFailure(QNetworkReply* reply)
{
  const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
  if (status == 401 || status == 403) {
    return QStringLiteral("Nexus refused the request (HTTP %1), log in to Nexus again")
        .arg(status);
  }
  if (status == 429) {
    return QStringLiteral("Nexus request limit reached (HTTP 429)");
  }
  if (status != 0 && status != 200) {
    return QStringLiteral("Nexus answered HTTP %1").arg(status);
  }
  // Only the error code: the message can carry the URL, and mirror URLs are signed.
  return QStringLiteral("network error %1").arg(static_cast<int>(reply->error()));
}

// Runs on a worker thread. Returns an error message, empty on success.
QString extractCollectionJson(const QString& archivePath, const QString& outputDir)
{
  std::unique_ptr<Archive> archive = CreateArchive();
  if (!archive->isValid()) {
    return QStringLiteral("7z support is not available");
  }
  if (!archive->open(archivePath.toStdWString(), {})) {
    return QStringLiteral("cannot open collection.7z");
  }

  FileData* manifest = nullptr;
  for (FileData* entry : archive->getFileList()) {
    if (entry->isDirectory()) {
      continue;
    }
    QString path = QString::fromStdWString(entry->getArchiveFilePath());
    path.replace(QLatin1Char('\\'), QLatin1Char('/'));
    if (path.startsWith(QStringLiteral("./"))) {
      path.remove(0, 2);
    }
    if (path.compare(QStringLiteral("collection.json"), Qt::CaseInsensitive) == 0) {
      manifest = entry;
      break;
    }
  }
  if (manifest == nullptr) {
    archive->close();
    return QStringLiteral("collection.7z has no collection.json");
  }

  // Only entries with an output path are extracted, so everything else in the archive
  // (bundled files, patches) stays inside it.
  manifest->addOutputFilePath(L"collection.json");
  QString error;
  const bool ok = archive->extract(outputDir.toStdWString(), {}, {},
                                   [&error](const std::wstring& message) {
                                     error = QString::fromStdWString(message);
                                   });
  archive->close();
  if (!ok) {
    return error.isEmpty() ? QStringLiteral("cannot extract collection.json") : error;
  }
  return {};
}

}  // namespace

CollectionManifestFetcher::CollectionManifestFetcher(ApiRequest api, PublicGet publicGet,
                                                     QString cacheRoot, QObject* parent)
    : QObject(parent), m_api(std::move(api)), m_publicGet(std::move(publicGet)),
      m_cacheRoot(std::move(cacheRoot))
{}

CollectionManifestFetcher::~CollectionManifestFetcher() = default;

QByteArray CollectionManifestFetcher::graphqlBody(const CollectionLink& link)
{
  static const QString query = QStringLiteral(
      "query($slug:String!,$domain:String!,$revision:Int!){"
      "collectionRevision(slug:$slug,domainName:$domain,revision:$revision,"
      "viewAdultContent:true){revisionNumber collectionSchemaId downloadLink}}");

  const QJsonObject variables{
      {"slug", link.slug}, {"domain", link.domain}, {"revision", link.revision}};
  const QJsonObject body{{"query", query}, {"variables", variables}};
  return QJsonDocument(body).toJson(QJsonDocument::Compact);
}

bool CollectionManifestFetcher::downloadLinkAllowed(const QString& path)
{
  // Plain path characters only: no dots (so no ".."), no '?', '#', '@' or ':' (so no
  // query, fragment, credentials or host), and no empty segment that could read as a
  // network-path reference.
  static const QRegularExpression allowed(
      QStringLiteral("^/v2/collections/[A-Za-z0-9_/-]+$"));
  return allowed.match(path).hasMatch() && !path.contains(QStringLiteral("//"));
}

void CollectionManifestFetcher::fetch(const CollectionLink& link)
{
  if (m_active) {
    MOBase::log::warn("a collection fetch is already running, ignoring the second one");
    return;
  }

  // CollectionLink::parse keeps the slug as typed, so the check has to accept both cases.
  static const QRegularExpression slugPattern(QStringLiteral("^[a-z0-9_-]+$"),
                                              QRegularExpression::CaseInsensitiveOption);

  m_active = true;
  m_link   = link;
  m_mirrors.clear();
  m_mirrorProblem.clear();
  m_transferProblem.clear();
  m_archive.reset();
  m_reply     = nullptr;
  m_redirects = 0;
  m_received  = 0;

  if (!slugPattern.match(link.slug).hasMatch() || link.domain.isEmpty() ||
      link.revision < 1) {
    fail(QStringLiteral("not a valid collection link"));
    return;
  }

  m_cacheDir = m_cacheRoot + QLatin1Char('/') + link.slug + QStringLiteral("-r") +
               QString::number(link.revision);

  QNetworkReply* reply =
      m_api(QUrl(QString::fromLatin1(GraphqlUrl)), graphqlBody(link));
  if (reply == nullptr) {
    fail(QStringLiteral("not logged in to Nexus"));
    return;
  }
  watch(reply, &CollectionManifestFetcher::onRevisionAnswer);
}

void CollectionManifestFetcher::cancel()
{
  if (!m_active) {
    return;
  }
  // Clear the flag before aborting: abort() emits finished(), and a finished handler
  // must find nothing left to do.
  m_active = false;
  m_archive.reset();
  if (m_reply != nullptr) {
    QNetworkReply* reply = m_reply;
    m_reply              = nullptr;
    reply->abort();
  }
  emit failed(QStringLiteral("cancelled"));
}

void CollectionManifestFetcher::fail(const QString& reason)
{
  if (!m_active) {
    return;
  }
  m_active = false;
  m_archive.reset();
  emit failed(reason);
}

void CollectionManifestFetcher::watch(QNetworkReply* reply, Handler handler)
{
  m_reply = reply;
  connect(reply, &QNetworkReply::finished, this, [this, reply, handler] {
    reply->deleteLater();
    if (m_reply == reply) {
      m_reply = nullptr;
    }
    if (m_active) {
      (this->*handler)(reply);
    }
  });
}

bool CollectionManifestFetcher::readJson(QNetworkReply* reply, QJsonObject& out)
{
  const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
  if (reply->error() != QNetworkReply::NoError || status != 200) {
    fail(describeFailure(reply));
    return false;
  }
  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(reply->readAll(), &parseError);
  if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
    fail(QStringLiteral("Nexus sent an answer that is not JSON"));
    return false;
  }
  out = document.object();
  return true;
}

void CollectionManifestFetcher::onRevisionAnswer(QNetworkReply* reply)
{
  QJsonObject root;
  if (!readJson(reply, root)) {
    return;
  }

  const QJsonArray errors = root.value("errors").toArray();
  if (!errors.isEmpty()) {
    fail(QStringLiteral("Nexus API error: %1")
             .arg(errors.first().toObject().value("message").toString()));
    return;
  }

  const QJsonValue found = root.value("data").toObject().value("collectionRevision");
  if (!found.isObject()) {
    fail(QStringLiteral("collection %1/%2 revision %3 was not found")
             .arg(m_link.domain, m_link.slug)
             .arg(m_link.revision));
    return;
  }

  const QJsonObject revision = found.toObject();
  const int number           = revision.value("revisionNumber").toInt();
  if (number != m_link.revision) {
    fail(QStringLiteral("Nexus returned revision %1 instead of revision %2")
             .arg(number)
             .arg(m_link.revision));
    return;
  }
  const int schema = revision.value("collectionSchemaId").toInt();
  if (schema != 1) {
    fail(QStringLiteral("unsupported collection schema %1").arg(schema));
    return;
  }
  const QString path = revision.value("downloadLink").toString();
  if (!downloadLinkAllowed(path)) {
    fail(QStringLiteral("Nexus returned an unsafe package location"));
    return;
  }

  QNetworkReply* next =
      m_api(QUrl(QString::fromLatin1(ApiOrigin) + path), QByteArray());
  if (next == nullptr) {
    fail(QStringLiteral("not logged in to Nexus"));
    return;
  }
  watch(next, &CollectionManifestFetcher::onMirrorList);
}

void CollectionManifestFetcher::onMirrorList(QNetworkReply* reply)
{
  QJsonObject root;
  if (!readJson(reply, root)) {
    return;
  }

  m_mirrors.clear();
  bool skippedInsecure = false;
  for (const QJsonValue& entry : root.value("download_links").toArray()) {
    const QUrl url(entry.toObject().value("URI").toString());
    if (url.isValid() && url.scheme() == QLatin1String("https") &&
        !url.host().isEmpty() && url.userInfo().isEmpty()) {
      m_mirrors.append(url);
    } else {
      skippedInsecure = true;
    }
  }
  if (m_mirrors.isEmpty()) {
    fail(skippedInsecure ? QStringLiteral("Nexus offered no https download mirror")
                         : QStringLiteral("Nexus offered no download mirror"));
    return;
  }
  if (!QDir().mkpath(m_cacheDir)) {
    fail(QStringLiteral("cannot create the collection cache folder"));
    return;
  }
  tryNextMirror();
}

void CollectionManifestFetcher::tryNextMirror()
{
  if (m_mirrors.isEmpty()) {
    fail(QStringLiteral("no mirror delivered collection.7z (%1)").arg(m_mirrorProblem));
    return;
  }
  m_redirects = 0;
  startTransfer(m_mirrors.takeFirst());
}

void CollectionManifestFetcher::startTransfer(const QUrl& url)
{
  auto archive = std::make_unique<QSaveFile>(m_cacheDir + QStringLiteral("/collection.7z"));
  if (!archive->open(QIODevice::WriteOnly)) {
    fail(QStringLiteral("cannot write the collection cache"));
    return;
  }

  QNetworkReply* reply = m_publicGet(url);
  if (reply == nullptr) {
    m_mirrorProblem = QStringLiteral("%1 could not be reached").arg(url.host());
    tryNextMirror();
    return;
  }

  m_archive  = std::move(archive);
  m_received = 0;
  m_transferProblem.clear();
  m_reply = reply;
  connect(reply, &QNetworkReply::readyRead, this, [this, reply] { readTransfer(reply); });
  connect(reply, &QNetworkReply::finished, this,
          [this, reply, url] { onTransferFinished(reply, url); });

  // A server that states its size up front is refused before a byte is stored.
  checkDeclaredSize(reply);
}

void CollectionManifestFetcher::checkDeclaredSize(QNetworkReply* reply)
{
  const QVariant declared = reply->header(QNetworkRequest::ContentLengthHeader);
  if (declared.isValid() && declared.toLongLong() > MaxArchiveBytes) {
    abortTransfer(reply, QStringLiteral("%1 offers a collection.7z larger than the 4 GiB limit")
                             .arg(reply->url().host()));
  }
}

void CollectionManifestFetcher::abortTransfer(QNetworkReply* reply, const QString& problem)
{
  if (!m_transferProblem.isEmpty()) {
    return;
  }
  m_transferProblem = problem;
  // This may emit finished() at once, which moves on to the next mirror. Callers return
  // straight after.
  reply->abort();
}

void CollectionManifestFetcher::readTransfer(QNetworkReply* reply)
{
  if (!m_active || !m_archive || !m_transferProblem.isEmpty()) {
    return;
  }
  checkDeclaredSize(reply);
  if (!m_transferProblem.isEmpty()) {
    return;
  }

  const QByteArray bytes = reply->readAll();
  m_received += bytes.size();
  if (m_received > MaxArchiveBytes) {
    abortTransfer(reply, QStringLiteral("%1 sent more than the 4 GiB limit")
                             .arg(reply->url().host()));
    return;
  }
  if (m_archive->write(bytes) != bytes.size()) {
    abortTransfer(reply, QStringLiteral("cannot write the collection cache"));
  }
}

void CollectionManifestFetcher::onTransferFinished(QNetworkReply* reply, const QUrl& url)
{
  reply->deleteLater();
  if (m_reply == reply) {
    m_reply = nullptr;
  }
  if (!m_active) {
    return;
  }

  // The last chunk can arrive together with finished().
  if (m_transferProblem.isEmpty()) {
    readTransfer(reply);
  }
  if (!m_transferProblem.isEmpty()) {
    m_mirrorProblem = m_transferProblem;
    m_transferProblem.clear();
    m_archive.reset();
    tryNextMirror();
    return;
  }

  const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
  if (status >= 300 && status < 400) {
    const QUrl target =
        url.resolved(reply->attribute(QNetworkRequest::RedirectionTargetAttribute).toUrl());
    m_archive.reset();
    if (target.scheme() != QLatin1String("https") || target.host().isEmpty() ||
        !target.userInfo().isEmpty() || ++m_redirects > MaxRedirects) {
      m_mirrorProblem = QStringLiteral("%1 redirected somewhere unusable").arg(url.host());
      tryNextMirror();
      return;
    }
    startTransfer(target);
    return;
  }

  if (reply->error() != QNetworkReply::NoError || status != 200) {
    m_mirrorProblem = QStringLiteral("%1 answered HTTP %2").arg(url.host()).arg(status);
    m_archive.reset();
    tryNextMirror();
    return;
  }

  if (!m_archive || !m_archive->commit()) {
    m_mirrorProblem = QStringLiteral("cannot finish writing the collection cache");
    m_archive.reset();
    tryNextMirror();
    return;
  }
  m_archive.reset();
  extract();
}

void CollectionManifestFetcher::extract()
{
  const QString archivePath = m_cacheDir + QStringLiteral("/collection.7z");
  const QString outputDir   = m_cacheDir;

  // An older manifest from a previous attempt must not stand in for this one.
  QFile::remove(m_cacheDir + QStringLiteral("/collection.json"));

  // A solid archive can take seconds to unpack up to collection.json, so it runs off the
  // GUI thread. The job captures values only: the fetcher may be gone when it finishes,
  // and then the watcher (a child of the fetcher) is gone with it.
  auto* watcher = new QFutureWatcher<QString>(this);
  connect(watcher, &QFutureWatcher<QString>::finished, this, [this, watcher] {
    const QString problem = watcher->result();
    watcher->deleteLater();
    if (!m_active) {
      return;
    }
    if (!problem.isEmpty()) {
      fail(problem);
      return;
    }
    parseExtracted();
  });
  watcher->setFuture(QtConcurrent::run(
      [archivePath, outputDir] { return extractCollectionJson(archivePath, outputDir); }));
}

void CollectionManifestFetcher::parseExtracted()
{
  QFile file(m_cacheDir + QStringLiteral("/collection.json"));
  if (!file.open(QIODevice::ReadOnly) || file.size() > MaxManifestBytes) {
    fail(QStringLiteral("cannot read collection.json"));
    return;
  }

  QString error;
  const std::optional<CollectionManifest> manifest =
      CollectionManifest::parse(file.readAll(), error);
  if (!manifest) {
    fail(QStringLiteral("collection.json is not usable: %1").arg(error));
    return;
  }

  m_active = false;
  emit ready(*manifest, m_link.revision, m_cacheDir);
}

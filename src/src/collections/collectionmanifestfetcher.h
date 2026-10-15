#pragma once

#include "collectionlink.h"
#include "collectionmanifest.h"

#include <QList>
#include <QNetworkReply>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QUrl>

#include <functional>
#include <memory>

class QSaveFile;

// Fetches and parses the collection.json of one collection revision.
//
// The authenticated Nexus calls and the public mirror download are separate functions
// on purpose. NXMAccessManager::makeCollectionRequest only talks to api.nexusmods.com,
// and the mirrors are CDN hosts that must never see the account's credentials.
class CollectionManifestFetcher : public QObject
{
  Q_OBJECT

public:
  // Authenticated request to api.nexusmods.com. An empty body means GET, otherwise POST.
  // Returns null when the user is not logged in.
  using ApiRequest = std::function<QNetworkReply*(const QUrl& url, const QByteArray& body)>;

  // Plain GET with no credentials. The caller sets ManualRedirectPolicy; the fetcher
  // follows redirects itself so it can insist on https.
  using PublicGet = std::function<QNetworkReply*(const QUrl& url)>;

  static constexpr qint64 MaxArchiveBytes = 4LL * 1024 * 1024 * 1024;

  // cacheRoot holds one folder per revision: <cacheRoot>/<slug>-r<n>/.
  CollectionManifestFetcher(ApiRequest api, PublicGet publicGet, QString cacheRoot,
                            QObject* parent = nullptr);

  // Defined in the .cpp, where QSaveFile is a complete type for m_archive.
  ~CollectionManifestFetcher() override;

  // Starts a fetch; every fetch ends in exactly one ready() or failed(). Ignored while
  // another fetch is running.
  void fetch(const CollectionLink& link);

  // Ends the running fetch with failed("cancelled"). Does nothing when none is running.
  void cancel();

  static QByteArray graphqlBody(const CollectionLink& link);

  // The package location the revision answer names. It is appended to the API origin,
  // so it has to be a plain path under /v2/collections/.
  static bool downloadLinkAllowed(const QString& path);

signals:
  void ready(const CollectionManifest& manifest, int revision, const QString& cacheDir);
  void failed(const QString& reason);

private:
  using Handler = void (CollectionManifestFetcher::*)(QNetworkReply*);

  void fail(const QString& reason);
  void watch(QNetworkReply* reply, Handler handler);
  bool readJson(QNetworkReply* reply, QJsonObject& out);

  void onRevisionAnswer(QNetworkReply* reply);
  void onMirrorList(QNetworkReply* reply);

  void tryNextMirror();
  void startTransfer(const QUrl& url);
  void readTransfer(QNetworkReply* reply);
  void checkDeclaredSize(QNetworkReply* reply);
  void abortTransfer(QNetworkReply* reply, const QString& problem);
  void onTransferFinished(QNetworkReply* reply, const QUrl& url);

  void extract();
  void parseExtracted();

  ApiRequest m_api;
  PublicGet m_publicGet;
  QString m_cacheRoot;

  bool m_active = false;
  CollectionLink m_link;
  QString m_cacheDir;
  QPointer<QNetworkReply> m_reply;

  QList<QUrl> m_mirrors;
  QString m_mirrorProblem;  // why the last mirror was given up on, host names only
  int m_redirects = 0;

  std::unique_ptr<QSaveFile> m_archive;
  qint64 m_received = 0;
  QString m_transferProblem;  // set when this class aborts a transfer itself
};

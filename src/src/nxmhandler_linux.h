#ifndef NXMHANDLER_LINUX_H
#define NXMHANDLER_LINUX_H

#include <QObject>
#include <QString>

#include <cstdint>
#include <optional>

struct NxmLink
{
  QString game_domain;
  uint64_t mod_id = 0;
  uint64_t file_id = 0;
  QString key;
  uint64_t expires = 0;
  int user_id      = 0;

  static std::optional<NxmLink> parse(const QString& url);
  QString lookupKey() const;
};

Q_DECLARE_METATYPE(NxmLink)

class QLocalServer;
class QLocalSocket;

class NxmHandlerLinux : public QObject
{
  Q_OBJECT

public:
  explicit NxmHandlerLinux(QObject* parent = nullptr);
  ~NxmHandlerLinux() override;

  static void registerHandler();
  static void unregisterHandler();
  bool startListener();

  static bool sendToSocket(const QString& url);
  static QString socketPath();

signals:
  void nxmReceived(NxmLink link);
  void directDownloadReceived(QString url, QString gameDomain);
  // A website "Download collection" link; carries the raw line because the session
  // parses it again and a collection has no mod or file id to put in an NxmLink.
  void collectionReceived(QString url);

private:
  void onNewConnection();
  void processSocketData(QLocalSocket* socket);

private:
  QLocalServer* m_server = nullptr;
};

#endif  // NXMHANDLER_LINUX_H

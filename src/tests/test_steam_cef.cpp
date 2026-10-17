#include "steamcef.h"

#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTcpServer>
#include <QTcpSocket>
#include <QWebSocket>
#include <QWebSocketServer>
#include <functional>
#include <gtest/gtest.h>

namespace {

// One listening socket that answers /json/list with plain HTTP and hands any
// other request to a websocket server, the way Steam's CEF port does.
class FakeSteam : public QObject {
public:
  using Handler = std::function<void(QWebSocket*, const QJsonObject&)>;

  FakeSteam() : m_ws("fake", QWebSocketServer::NonSecureMode)
  {
    connect(&m_tcp, &QTcpServer::newConnection, this, [this] {
      while (auto* socket = m_tcp.nextPendingConnection()) {
        connect(socket, &QTcpSocket::readyRead, this, [this, socket] { route(socket); });
        if (socket->bytesAvailable() > 0) route(socket);
      }
    });
    connect(&m_ws, &QWebSocketServer::newConnection, this, [this] {
      auto* client = m_ws.nextPendingConnection();
      connect(client, &QWebSocket::textMessageReceived, this, [this, client](const QString& text) {
        const auto message = QJsonDocument::fromJson(text.toUtf8()).object();
        m_requests.append(message);
        if (m_handler) m_handler(client, message);
      });
    });
    EXPECT_TRUE(m_tcp.listen(QHostAddress::LocalHost, 0));
  }

  quint16 port() const { return m_tcp.serverPort(); }
  void close() { m_tcp.close(); }

  // Target list served at /json/list; the websocket URL is filled in unless
  // the test overrides it.
  QString title = "SharedJSContext";
  int wsPortOffset = 0;
  Handler m_handler;
  QList<QJsonObject> m_requests;

  void replyWith(const QJsonObject& body)
  {
    m_handler = [body](QWebSocket* client, const QJsonObject& message) {
      QJsonObject reply = body;
      reply["id"] = message.value("id");
      client->sendTextMessage(QString::fromUtf8(QJsonDocument(reply).toJson()));
    };
  }

private:
  void route(QTcpSocket* socket)
  {
    if (socket->property("routed").toBool()) return;
    const auto head = socket->peek(64);
    if (!head.contains('\n')) return;
    socket->setProperty("routed", true);
    socket->disconnect(this);
    if (head.startsWith("GET /json/list")) {
      const QJsonArray list{QJsonObject{
        {"title", title},
        {"webSocketDebuggerUrl",
         QString("ws://127.0.0.1:%1/devtools/page/1").arg(port() + wsPortOffset)}}};
      const auto body = QJsonDocument(list).toJson();
      socket->readAll();
      socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                    QByteArray::number(body.size()) + "\r\nConnection: close\r\n\r\n" + body);
      socket->disconnectFromHost();
    } else {
      m_ws.handleConnection(socket);
    }
  }

  QTcpServer m_tcp;
  QWebSocketServer m_ws;
};

QJsonObject valueResult(const QJsonValue& value)
{
  return {{"result", QJsonObject{{"result", QJsonObject{{"type", "x"}, {"value", value}}}}}};
}

SteamCef::Error::Kind kindOf(const SteamCef::Client& client, int timeoutMs = 10000)
{
  try {
    client.evaluate("1", timeoutMs);
  } catch (const SteamCef::Error& e) {
    return e.kind();
  }
  ADD_FAILURE() << "evaluate did not throw";
  return SteamCef::Error::Kind::Unavailable;
}

}  // namespace

TEST(SteamCef, EvaluateReturnsNumberAndObject)
{
  FakeSteam fake;
  SteamCef::Client client("127.0.0.1", fake.port());
  fake.replyWith(valueResult(42));
  EXPECT_EQ(client.evaluate("6*7").toInt(), 42);
  fake.replyWith(valueResult(QJsonObject{{"ok", true}}));
  EXPECT_TRUE(client.evaluate("({ok:true})").toObject().value("ok").toBool());
}

TEST(SteamCef, SendsRuntimeEvaluateWithAwaitPromise)
{
  FakeSteam fake;
  fake.replyWith(valueResult(1));
  SteamCef::Client client("127.0.0.1", fake.port());
  client.evaluate("expr()");
  ASSERT_EQ(fake.m_requests.size(), 1);
  const auto request = fake.m_requests.first();
  const auto params = request.value("params").toObject();
  EXPECT_EQ(request.value("id").toInt(), 1);
  EXPECT_EQ(request.value("method").toString(), "Runtime.evaluate");
  EXPECT_EQ(params.value("expression").toString(), "expr()");
  EXPECT_TRUE(params.value("returnByValue").toBool());
  EXPECT_TRUE(params.value("awaitPromise").toBool());
}

TEST(SteamCef, ThrowsUnavailableWhenNothingListens)
{
  quint16 port;
  {
    FakeSteam fake;
    port = fake.port();
    fake.close();
  }
  EXPECT_EQ(kindOf(SteamCef::Client("127.0.0.1", port)), SteamCef::Error::Kind::Unavailable);
}

TEST(SteamCef, ThrowsNoContextWithoutSharedJSContext)
{
  FakeSteam fake;
  fake.title = "Something else";
  EXPECT_EQ(kindOf(SteamCef::Client("127.0.0.1", fake.port())), SteamCef::Error::Kind::NoContext);
}

TEST(SteamCef, RejectsEndpointOnOtherPort)
{
  FakeSteam fake;
  fake.wsPortOffset = 1;
  EXPECT_EQ(kindOf(SteamCef::Client("127.0.0.1", fake.port())), SteamCef::Error::Kind::NoContext);
}

TEST(SteamCef, ThrowsScriptFailedOnExceptionDetails)
{
  FakeSteam fake;
  SteamCef::Client client("127.0.0.1", fake.port());
  fake.replyWith({{"result", QJsonObject{{"exceptionDetails", QJsonObject{{"text", "boom"}}}}}});
  EXPECT_EQ(kindOf(client), SteamCef::Error::Kind::ScriptFailed);
  fake.replyWith({{"error", QJsonObject{{"code", -32000}, {"message", "bad"}}}});
  EXPECT_EQ(kindOf(client), SteamCef::Error::Kind::ScriptFailed);
}

TEST(SteamCef, ThrowsNoResponseOnTimeout)
{
  FakeSteam fake;  // accepts the websocket and never answers
  EXPECT_EQ(kindOf(SteamCef::Client("127.0.0.1", fake.port()), 300),
            SteamCef::Error::Kind::NoResponse);
}

TEST(SteamCef, ThrowsNoResponseWhenSocketDrops)
{
  FakeSteam fake;
  fake.m_handler = [](QWebSocket* client, const QJsonObject&) { client->close(); };
  EXPECT_EQ(kindOf(SteamCef::Client("127.0.0.1", fake.port())), SteamCef::Error::Kind::NoResponse);
}

TEST(SteamCef, JsStringEscapes)
{
  const QStringList samples = {
      "plain", "say \"hi\"", "it's", "back\\slash", "line\nbreak", "</script>",
      QString("a") + QChar(0x2028) + "b" + QChar(0x2029) + "c",
      QString::fromUtf8("smile \xF0\x9F\x98\x80 done"), "' # %command%"};
  for (const auto& s : samples) {
    const auto literal = SteamCef::jsString(s);
    EXPECT_FALSE(literal.contains(QChar(0x2028)));
    EXPECT_FALSE(literal.contains(QChar(0x2029)));
    const auto parsed = QJsonDocument::fromJson(("[" + literal + "]").toUtf8()).array();
    ASSERT_EQ(parsed.size(), 1) << qPrintable(s);
    EXPECT_EQ(parsed.at(0).toString(), s);
  }
}

int main(int argc, char** argv)
{
  QCoreApplication app(argc, argv);
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}

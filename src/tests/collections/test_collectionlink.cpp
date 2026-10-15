#include "collections/collectionlink.h"

#include <gtest/gtest.h>

namespace
{
CollectionLink parsed(const QString& url)
{
  const auto link = CollectionLink::parse(url);
  EXPECT_TRUE(link.has_value()) << url.toStdString();
  return link.value_or(CollectionLink{});
}

bool rejected(const QString& url)
{
  return !CollectionLink::parse(url).has_value();
}
}  // namespace

TEST(CollectionLink, ParsesNxmLink)
{
  const auto link =
      parsed(QStringLiteral("  nxm://cyberpunk2077/collections/rcuccp/revisions/189\n"));
  EXPECT_EQ(link.domain, QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(link.slug, QStringLiteral("rcuccp"));
  EXPECT_EQ(link.revision, 189);
}

TEST(CollectionLink, ParsesHttpsGamesLink)
{
  const auto link = parsed(QStringLiteral(
      "https://www.nexusmods.com/games/cyberpunk2077/collections/rcuccp/revisions/189"));
  EXPECT_EQ(link.domain, QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(link.slug, QStringLiteral("rcuccp"));
  EXPECT_EQ(link.revision, 189);

  // no www, plain http
  const auto bare = parsed(QStringLiteral(
      "http://nexusmods.com/games/cyberpunk2077/collections/rcuccp/revisions/2"));
  EXPECT_EQ(bare.slug, QStringLiteral("rcuccp"));
  EXPECT_EQ(bare.revision, 2);
}

TEST(CollectionLink, ParsesHttpsLinkWithoutGamesPrefix)
{
  const auto link = parsed(QStringLiteral(
      "https://www.nexusmods.com/cyberpunk2077/collections/srpv39/revisions/129"));
  EXPECT_EQ(link.domain, QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(link.slug, QStringLiteral("srpv39"));
  EXPECT_EQ(link.revision, 129);
}

TEST(CollectionLink, AcceptsSlugWithDashAndUnderscore)
{
  const auto link = parsed(
      QStringLiteral("nxm://skyrimspecialedition/collections/my-cool_list/revisions/3"));
  EXPECT_EQ(link.slug, QStringLiteral("my-cool_list"));
  EXPECT_EQ(link.domain, QStringLiteral("skyrimspecialedition"));
}

TEST(CollectionLink, DomainIsCaseInsensitiveAndLowercased)
{
  const auto link =
      parsed(QStringLiteral("NXM://CyberPunk2077/collections/RCUCCP/revisions/1"));
  EXPECT_EQ(link.domain, QStringLiteral("cyberpunk2077"));
  // the slug is passed to the API as typed
  EXPECT_EQ(link.slug, QStringLiteral("RCUCCP"));

  const auto web = parsed(QStringLiteral(
      "HTTPS://WWW.NEXUSMODS.COM/Games/Cyberpunk2077/Collections/rcuccp/Revisions/7"));
  EXPECT_EQ(web.domain, QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(web.revision, 7);
}

TEST(CollectionLink, AcceptsTrailingSlash)
{
  EXPECT_EQ(parsed(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/189/"))
                .revision,
            189);
  EXPECT_EQ(parsed(QStringLiteral("https://www.nexusmods.com/games/cyberpunk2077/"
                                  "collections/rcuccp/revisions/189/"))
                .revision,
            189);
}

TEST(CollectionLink, IgnoresQueryString)
{
  const auto nxm = parsed(
      QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/189?foo=bar"));
  EXPECT_EQ(nxm.slug, QStringLiteral("rcuccp"));
  EXPECT_EQ(nxm.revision, 189);

  EXPECT_EQ(parsed(QStringLiteral("https://www.nexusmods.com/games/cyberpunk2077/"
                                  "collections/rcuccp/revisions/189?tab=mods"))
                .revision,
            189);
  EXPECT_EQ(parsed(QStringLiteral("https://www.nexusmods.com/games/cyberpunk2077/"
                                  "collections/rcuccp/revisions/189/#mods"))
                .revision,
            189);
}

TEST(CollectionLink, RejectsModLink)
{
  EXPECT_TRUE(rejected(QStringLiteral(
      "nxm://cyberpunk2077/mods/28966/files/158871?key=abc&expires=1&user_id=2")));
  EXPECT_TRUE(rejected(
      QStringLiteral("https://www.nexusmods.com/cyberpunk2077/mods/28966")));
}

TEST(CollectionLink, RejectsMissingRevision)
{
  EXPECT_TRUE(rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp")));
  EXPECT_TRUE(rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/")));
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/abc")));
  EXPECT_TRUE(rejected(QStringLiteral(
      "https://www.nexusmods.com/games/cyberpunk2077/collections/rcuccp")));
}

TEST(CollectionLink, RejectsRevisionZero)
{
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/0")));
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/007")));
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/-1")));
  // does not fit an int
  EXPECT_TRUE(rejected(
      QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/99999999999")));
}

TEST(CollectionLink, RejectsOtherHosts)
{
  EXPECT_TRUE(rejected(QStringLiteral(
      "https://evil.example.com/games/cyberpunk2077/collections/rcuccp/revisions/1")));
  EXPECT_TRUE(rejected(QStringLiteral("https://www.nexusmods.com.evil.example/games/"
                                      "cyberpunk2077/collections/rcuccp/revisions/1")));
  EXPECT_TRUE(rejected(QStringLiteral("https://notnexusmods.com/games/cyberpunk2077/"
                                      "collections/rcuccp/revisions/1")));
  EXPECT_TRUE(rejected(QStringLiteral("https://www.nexusmods.com@evil.example/games/"
                                      "cyberpunk2077/collections/rcuccp/revisions/1")));
  EXPECT_TRUE(rejected(QStringLiteral(
      "ftp://www.nexusmods.com/games/cyberpunk2077/collections/rcuccp/revisions/1")));
}

TEST(CollectionLink, RejectsJunk)
{
  EXPECT_TRUE(rejected(QString()));
  EXPECT_TRUE(rejected(QStringLiteral("nxm://")));
  EXPECT_TRUE(rejected(QStringLiteral("not a url")));
  EXPECT_TRUE(rejected(QStringLiteral("collections/rcuccp/revisions/1")));
  EXPECT_TRUE(rejected(QStringLiteral("nxm://cyberpunk2077/collections//revisions/1")));
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/a b/revisions/1")));
  EXPECT_TRUE(
      rejected(QStringLiteral("nxm://cyberpunk2077/collections/../revisions/1")));
  EXPECT_TRUE(rejected(
      QStringLiteral("nxm://cyberpunk2077/collections/rcuccp/revisions/1/extra")));
}

TEST(CollectionDomain, MatchesShortNameCaseInsensitively)
{
  const QStringList valid{"Skyrim", "SkyrimSE", "SkyrimSE GOG"};
  EXPECT_TRUE(collectionDomainMatches(QStringLiteral("skyrimse"), QStringLiteral("SkyrimSE"),
                                      QStringLiteral("skyrimspecialedition"), valid));
  EXPECT_TRUE(collectionDomainMatches(QStringLiteral("SKYRIMSE"), QStringLiteral("SkyrimSE"),
                                      QStringLiteral("skyrimspecialedition"), valid));
}

TEST(CollectionDomain, MatchesNexusName)
{
  const QStringList valid{"Skyrim", "SkyrimSE", "SkyrimSE GOG"};
  EXPECT_TRUE(collectionDomainMatches(QStringLiteral("skyrimspecialedition"),
                                      QStringLiteral("SkyrimSE"),
                                      QStringLiteral("skyrimspecialedition"), valid));
}

TEST(CollectionDomain, MatchesValidShortName)
{
  const QStringList valid{"Skyrim", "SkyrimSE", "SkyrimSE GOG"};
  EXPECT_TRUE(collectionDomainMatches(QStringLiteral("skyrim"), QStringLiteral("SkyrimSE"),
                                      QStringLiteral("skyrimspecialedition"), valid));
}

TEST(CollectionDomain, RejectsOtherGame)
{
  const QStringList valid{"Skyrim", "SkyrimSE", "SkyrimSE GOG"};
  EXPECT_FALSE(collectionDomainMatches(QStringLiteral("fallout4"), QStringLiteral("SkyrimSE"),
                                       QStringLiteral("skyrimspecialedition"), valid));
  // an empty domain or empty game names never match each other
  EXPECT_FALSE(collectionDomainMatches(QString(), QString(), QString(), {}));
  EXPECT_FALSE(collectionDomainMatches(QString(), QStringLiteral("SkyrimSE"),
                                       QStringLiteral("skyrimspecialedition"), valid));
}

TEST(CollectionPrecheck, LoginCheckedBeforePremiumBeforeGame)
{
  EXPECT_EQ(collectionPrecheck(false, false, false), CollectionPrecheck::NotLoggedIn);
  EXPECT_EQ(collectionPrecheck(false, true, true), CollectionPrecheck::NotLoggedIn);
  EXPECT_EQ(collectionPrecheck(true, false, false), CollectionPrecheck::NotPremium);
  EXPECT_EQ(collectionPrecheck(true, false, true), CollectionPrecheck::NotPremium);
  EXPECT_EQ(collectionPrecheck(true, true, false), CollectionPrecheck::WrongGame);
  EXPECT_EQ(collectionPrecheck(true, true, true), CollectionPrecheck::Ok);
}

TEST(CollectionPrecheck, MessagesNameTheProblem)
{
  const QString login = collectionPrecheckMessage(CollectionPrecheck::NotLoggedIn);
  const QString premium = collectionPrecheckMessage(CollectionPrecheck::NotPremium);
  const QString game = collectionPrecheckMessage(CollectionPrecheck::WrongGame);

  EXPECT_TRUE(login.contains(QStringLiteral("logged in"), Qt::CaseInsensitive));
  EXPECT_TRUE(premium.contains(QStringLiteral("Premium")));
  EXPECT_TRUE(game.contains(QStringLiteral("game"), Qt::CaseInsensitive));
  EXPECT_TRUE(collectionPrecheckMessage(CollectionPrecheck::Ok).isEmpty());

  EXPECT_NE(login, premium);
  EXPECT_NE(premium, game);
  EXPECT_NE(login, game);
}

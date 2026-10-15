#pragma once

#include <QString>
#include <QStringList>

#include <optional>

// A collection revision named by a link from the Nexus website. Pure parsing,
// no Qt GUI and no organizer state, so it tests like a library.
struct CollectionLink
{
  QString domain;  // Nexus game domain, lowercased
  QString slug;    // as typed in the link
  int revision = 0;

  // Accepts nxm://<game>/collections/<slug>/revisions/<n> and the website's
  // https://www.nexusmods.com/[games/]<game>/collections/<slug>/revisions/<n>.
  // A trailing slash, query string or fragment is ignored. A link without
  // /revisions/<n> (n >= 1) is not a collection link: the revision query
  // needs a number.
  static std::optional<CollectionLink> parse(const QString& url);
};

// True when a link's or manifest's game domain names the managed game: its
// short name, its Nexus name, or one of its valid short names, ignoring case.
bool collectionDomainMatches(const QString& domain, const QString& gameShortName,
                             const QString& gameNexusName,
                             const QStringList& validShortNames);

enum class CollectionPrecheck
{
  Ok,
  NotLoggedIn,
  NotPremium,
  WrongGame
};

// The three checks made before a manifest is fetched, in the order they are
// reported when more than one fails.
CollectionPrecheck collectionPrecheck(bool loggedIn, bool premium, bool domainMatches);

// One line for the user; empty for Ok.
QString collectionPrecheckMessage(CollectionPrecheck check);

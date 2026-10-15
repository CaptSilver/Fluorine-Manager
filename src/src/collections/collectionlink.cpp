#include "collectionlink.h"

#include <QRegularExpression>

#include <algorithm>

std::optional<CollectionLink> CollectionLink::parse(const QString& url)
{
  // The host check is part of the pattern: only nxm:// and nexusmods.com links
  // are collection links, whatever else the text looks like.
  static const QRegularExpression pattern(
      QStringLiteral("^(?:nxm://(?<nxmGame>[a-z0-9_-]+)"
                     "|https?://(?:www\\.)?nexusmods\\.com/(?:games/)?(?<webGame>[a-z0-9_-]+))"
                     "/collections/(?<slug>[a-z0-9_-]+)"
                     "/revisions/(?<revision>[1-9][0-9]*)"
                     "/?(?:[?#].*)?$"),
      QRegularExpression::CaseInsensitiveOption);

  const auto match = pattern.match(url.trimmed());
  if (!match.hasMatch()) {
    return std::nullopt;
  }

  bool ok            = false;
  const int revision = match.captured(QStringLiteral("revision")).toInt(&ok);
  if (!ok || revision < 1) {
    return std::nullopt;
  }

  QString domain = match.captured(QStringLiteral("nxmGame"));
  if (domain.isEmpty()) {
    domain = match.captured(QStringLiteral("webGame"));
  }

  CollectionLink link;
  link.domain   = domain.toLower();
  link.slug     = match.captured(QStringLiteral("slug"));
  link.revision = revision;
  return link;
}

bool collectionDomainMatches(const QString& domain, const QString& gameShortName,
                             const QString& gameNexusName,
                             const QStringList& validShortNames)
{
  if (domain.isEmpty()) {
    return false;
  }

  const auto same = [&](const QString& candidate) {
    return !candidate.isEmpty() && domain.compare(candidate, Qt::CaseInsensitive) == 0;
  };

  return same(gameShortName) || same(gameNexusName) ||
         std::any_of(validShortNames.begin(), validShortNames.end(), same);
}

CollectionPrecheck collectionPrecheck(bool loggedIn, bool premium, bool domainMatches)
{
  if (!loggedIn) {
    return CollectionPrecheck::NotLoggedIn;
  }
  if (!premium) {
    return CollectionPrecheck::NotPremium;
  }
  if (!domainMatches) {
    return CollectionPrecheck::WrongGame;
  }
  return CollectionPrecheck::Ok;
}

QString collectionPrecheckMessage(CollectionPrecheck check)
{
  switch (check) {
  case CollectionPrecheck::Ok:
    return {};
  case CollectionPrecheck::NotLoggedIn:
    return QStringLiteral(
        "Not logged in to Nexus. Log in from the Nexus settings, then click the "
        "collection link again.");
  case CollectionPrecheck::NotPremium:
    return QStringLiteral("Installing a collection needs a Nexus Premium account.");
  case CollectionPrecheck::WrongGame:
    return QStringLiteral(
        "This collection is for a different game than the one being managed.");
  }
  return {};
}

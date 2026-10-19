#include "nexuslinkpolicy.h"

namespace NexusLinkPolicy
{

QUrl baseUrl()
{
  return QUrl("https://www.nexusmods.com/");
}

namespace
{
  // Descriptions are written by mod authors, so only schemes a browser or mail
  // client would normally handle are passed on to the desktop.
  Action external(const QUrl& target)
  {
    const QString scheme = target.scheme().toLower();
    if (scheme == "http" || scheme == "https" || scheme == "mailto") {
      return Action::OpenExternally;
    }
    return Action::Block;
  }

  bool isSamePage(const QUrl& target)
  {
    return target.adjusted(QUrl::RemoveFragment) ==
           baseUrl().adjusted(QUrl::RemoveFragment);
  }
}  // namespace

Action decide(const QUrl& target, Kind kind, bool isMainFrame, bool isPopup)
{
  if (isPopup) {
    return external(target);
  }

  switch (kind) {
  case Kind::FormSubmitted:
    return external(target);

  case Kind::LinkClicked:
    if (isMainFrame && target.hasFragment() && isSamePage(target)) {
      return Action::Allow;
    }
    return external(target);

  case Kind::Other:
    break;
  }

  // setHtml() navigates to a data: URL carrying the HTML, or to the base URL.
  if (!isMainFrame || target.scheme() == "data" || isSamePage(target)) {
    return Action::Allow;
  }
  // Redirects and meta refreshes from the description land here. Only an
  // explicit click or submit may reach the desktop.
  return Action::Block;
}

}  // namespace NexusLinkPolicy

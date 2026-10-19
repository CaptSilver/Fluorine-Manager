#ifndef NEXUSLINKPOLICY_H
#define NEXUSLINKPOLICY_H

#include <QUrl>

// Navigation decisions for the Nexus description page. Kept free of WebEngine
// types so they can be unit tested without it.
namespace NexusLinkPolicy
{

enum class Kind
{
  LinkClicked,
  FormSubmitted,
  Other  // typed, reload, redirect, history, and the page's own setHtml load
};

enum class Action
{
  Allow,           // let the page navigate
  OpenExternally,  // hand the URL to the system browser, refuse in-page
  Block            // refuse, nothing to open
};

// The base URL the description HTML is loaded against.
QUrl baseUrl();

// isPopup is set for the throwaway page a target="_blank" link opens; its first
// navigation is the link target, so it never stays in-page.
Action decide(const QUrl& target, Kind kind, bool isMainFrame, bool isPopup = false);

}  // namespace NexusLinkPolicy

#endif  // NEXUSLINKPOLICY_H

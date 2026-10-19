#include "nexuslinkpolicy.h"

#include <gtest/gtest.h>

using namespace NexusLinkPolicy;

TEST(NexusLinkPolicy, ClickedHttpsLinkOpensExternally)
{
  EXPECT_EQ(decide(QUrl("https://example.com/page"), Kind::LinkClicked, true),
            Action::OpenExternally);
}

TEST(NexusLinkPolicy, ClickedLinkToNexusPageOpensExternally)
{
  EXPECT_EQ(decide(QUrl("https://www.nexusmods.com/skyrim/mods/1"), Kind::LinkClicked,
                   true),
            Action::OpenExternally);
}

TEST(NexusLinkPolicy, ClickedMailtoOpensExternally)
{
  EXPECT_EQ(decide(QUrl("mailto:author@example.com"), Kind::LinkClicked, true),
            Action::OpenExternally);
}

TEST(NexusLinkPolicy, ClickedFileOrCustomSchemeIsBlocked)
{
  EXPECT_EQ(decide(QUrl("file:///etc/passwd"), Kind::LinkClicked, true),
            Action::Block);
  EXPECT_EQ(decide(QUrl("steam://run/1"), Kind::LinkClicked, true), Action::Block);
}

TEST(NexusLinkPolicy, SamePageFragmentStaysInPage)
{
  EXPECT_EQ(decide(baseUrl().resolved(QUrl("#section")), Kind::LinkClicked, true),
            Action::Allow);
}

TEST(NexusLinkPolicy, FragmentOnAnotherPageOpensExternally)
{
  EXPECT_EQ(decide(QUrl("https://example.com/#section"), Kind::LinkClicked, true),
            Action::OpenExternally);
}

TEST(NexusLinkPolicy, PagesOwnLoadIsAllowed)
{
  EXPECT_EQ(decide(baseUrl(), Kind::Other, true), Action::Allow);
  EXPECT_EQ(decide(QUrl("data:text/html;charset=UTF-8,%3Chtml%3E"), Kind::Other, true),
            Action::Allow);
}

TEST(NexusLinkPolicy, NonClickNavigationAwayFromThePageIsBlocked)
{
  EXPECT_EQ(decide(QUrl("https://example.com/"), Kind::Other, true), Action::Block);
}

TEST(NexusLinkPolicy, PopupNavigationToHttpsOpensExternally)
{
  EXPECT_EQ(decide(QUrl("https://example.com/"), Kind::Other, true, true),
            Action::OpenExternally);
  EXPECT_EQ(decide(baseUrl(), Kind::Other, true, true), Action::OpenExternally);
}

TEST(NexusLinkPolicy, PopupNavigationToFileIsBlocked)
{
  EXPECT_EQ(decide(QUrl("file:///etc/passwd"), Kind::Other, true, true), Action::Block);
}

TEST(NexusLinkPolicy, SubframeLoadsAreAllowed)
{
  EXPECT_EQ(decide(QUrl("https://www.youtube.com/embed/x"), Kind::Other, false),
            Action::Allow);
}

TEST(NexusLinkPolicy, FormSubmitOpensExternally)
{
  EXPECT_EQ(decide(QUrl("https://example.com/submit"), Kind::FormSubmitted, true),
            Action::OpenExternally);
  EXPECT_EQ(decide(baseUrl(), Kind::FormSubmitted, true), Action::OpenExternally);
}

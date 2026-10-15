#include "collections/collectionstate.h"

#include <gtest/gtest.h>

namespace
{
InstalledMod mod(const char* folder)
{
  InstalledMod installed;
  installed.folder = QString::fromUtf8(folder);
  return installed;
}

CollectionState stateWith(std::initializer_list<const char*> folders)
{
  CollectionState state;
  for (const char* folder : folders) {
    state.mods.push_back(mod(folder));
  }
  return state;
}
}  // namespace

TEST(Modlist, ParsesPlusMinusAndBareLines)
{
  const auto entries =
      parseModlist(QByteArray("+Alpha\n-Beta\nGamma\n+Mod With Spaces\n+Caf\xC3\xA9\n"));

  ASSERT_EQ(entries.size(), 5u);
  EXPECT_EQ(entries[0].name, QStringLiteral("Alpha"));
  EXPECT_TRUE(entries[0].enabled);
  EXPECT_EQ(entries[1].name, QStringLiteral("Beta"));
  EXPECT_FALSE(entries[1].enabled);
  EXPECT_EQ(entries[2].name, QStringLiteral("Gamma"));
  EXPECT_TRUE(entries[2].enabled);
  EXPECT_EQ(entries[3].name, QStringLiteral("Mod With Spaces"));
  EXPECT_EQ(entries[4].name, QStringLiteral("Caf\u00e9"));
  for (const auto& entry : entries) {
    EXPECT_FALSE(entry.foreign) << entry.name.toStdString();
  }
}

TEST(Modlist, SkipsCommentsAndEmptyLines)
{
  const auto entries =
      parseModlist(QByteArray("# This file was automatically generated\n\n+Alpha\n   \n"
                              "#-Hidden\n-Beta\n+\n-\n"));

  ASSERT_EQ(entries.size(), 2u);
  EXPECT_EQ(entries[0].name, QStringLiteral("Alpha"));
  EXPECT_EQ(entries[1].name, QStringLiteral("Beta"));
}

TEST(Modlist, HandlesCrlf)
{
  const auto entries = parseModlist(QByteArray("+Alpha\r\n-Beta\r\n\r\nGamma\r\n"));

  ASSERT_EQ(entries.size(), 3u);
  EXPECT_EQ(entries[0].name, QStringLiteral("Alpha"));
  EXPECT_EQ(entries[1].name, QStringLiteral("Beta"));
  EXPECT_FALSE(entries[1].enabled);
  EXPECT_EQ(entries[2].name, QStringLiteral("Gamma"));
}

TEST(Modlist, MarksForeignStarLines)
{
  const auto entries = parseModlist(QByteArray("*DLC: Foo\n+Alpha\n"));

  ASSERT_EQ(entries.size(), 2u);
  EXPECT_EQ(entries[0].name, QStringLiteral("DLC: Foo"));
  EXPECT_TRUE(entries[0].foreign);
  EXPECT_TRUE(entries[0].enabled);
  EXPECT_FALSE(entries[1].foreign);
}

TEST(Modlist, DuplicateNameKeepsFirstLine)
{
  const auto entries = parseModlist(QByteArray("-Alpha\n+Alpha\n+Beta\n"));

  ASSERT_EQ(entries.size(), 2u);
  EXPECT_EQ(entries[0].name, QStringLiteral("Alpha"));
  EXPECT_FALSE(entries[0].enabled);
}

TEST(Modlist, ApplySetsEnabled)
{
  CollectionState state = stateWith({"Alpha", "Beta", "Gamma"});
  applyModlist(state, parseModlist(QByteArray("+Alpha\n-Beta\n+Gamma\n")));

  ASSERT_TRUE(state.mods[0].listedEnabled.has_value());
  EXPECT_TRUE(*state.mods[0].listedEnabled);

  ASSERT_TRUE(state.mods[1].listedEnabled.has_value());
  EXPECT_FALSE(*state.mods[1].listedEnabled);

  ASSERT_TRUE(state.mods[2].listedEnabled.has_value());
  EXPECT_TRUE(*state.mods[2].listedEnabled);
}

TEST(Modlist, ApplyMatchesNamesCaseInsensitively)
{
  CollectionState state = stateWith({"alpha beta"});
  applyModlist(state, parseModlist(QByteArray("-Alpha Beta\n")));

  ASSERT_TRUE(state.mods[0].listedEnabled.has_value());
  EXPECT_FALSE(*state.mods[0].listedEnabled);
}

TEST(Modlist, ApplyLeavesUnlistedAsNullopt)
{
  CollectionState state = stateWith({"Alpha", "Unlisted"});
  applyModlist(state, parseModlist(QByteArray("+Alpha\n+SomethingElse\n")));

  EXPECT_TRUE(state.mods[0].listedEnabled.has_value());
  EXPECT_FALSE(state.mods[1].listedEnabled.has_value());
}

// Foreign lines (game DLC and the like) are not mods the planner manages: a mod of the
// same name stays "not listed".
TEST(Modlist, ForeignEntriesAreNotApplied)
{
  CollectionState state = stateWith({"Alpha", "Beta", "DLC: Foo"});
  applyModlist(state, parseModlist(QByteArray("+Alpha\n*DLC: Foo\n-Beta\n")));

  ASSERT_TRUE(state.mods[0].listedEnabled.has_value());
  EXPECT_TRUE(*state.mods[0].listedEnabled);
  ASSERT_TRUE(state.mods[1].listedEnabled.has_value());
  EXPECT_FALSE(*state.mods[1].listedEnabled);
  EXPECT_FALSE(state.mods[2].listedEnabled.has_value());
}

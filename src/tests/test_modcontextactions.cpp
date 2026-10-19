#include "../src/modcontextactions.h"

#include <stdexcept>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include <log.h>

using MOBase::IModInterface;

namespace
{
// The registry never dereferences the mod or the parent widget, so distinct
// addresses are enough to tell them apart.
int g_modA, g_modB, g_parent;
IModInterface* const modA = reinterpret_cast<IModInterface*>(&g_modA);
IModInterface* const modB = reinterpret_cast<IModInterface*>(&g_modB);
QWidget* const parent     = reinterpret_cast<QWidget*>(&g_parent);

ModContextActions::Action action(const QString& label,
                                 std::function<bool(IModInterface*)> visible = {})
{
  return {label, [](IModInterface*, QWidget*) {}, std::move(visible)};
}

QStringList labels(const std::vector<ModContextActions::Handle>& handles)
{
  QStringList out;
  for (const auto& h : handles) {
    out.append(h->label);
  }
  return out;
}
}  // namespace

TEST(ModContextActions, VisibleForKeepsRegistrationOrder)
{
  ModContextActions registry;
  const auto a = registry.add(action("a"));
  const auto b = registry.add(action("b"));
  const auto c = registry.add(action("c"));

  EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"a", "b", "c"}));
}

TEST(ModContextActions, EmptyVisibleMeansShownAndFalseHidesOnlyThatAction)
{
  ModContextActions registry;
  const auto always = registry.add(action("always"));
  const auto never  = registry.add(action("never", [](IModInterface*) {
    return false;
  }));
  const auto onlyB  = registry.add(action("onlyB", [](IModInterface* mod) {
    return mod == modB;
  }));

  EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"always"}));
  EXPECT_EQ(labels(registry.visibleFor(modB)), (QStringList{"always", "onlyB"}));
}

TEST(ModContextActions, VisibleThatRegistersAnotherActionDoesNotInvalidateTheWalk)
{
  ModContextActions registry;
  std::vector<ModContextActions::Handle> added;

  // Enough registrations to force the registry's storage to grow mid-walk.
  const auto first = registry.add(action("first", [&](IModInterface*) {
    if (added.empty()) {
      for (int i = 0; i < 64; ++i) {
        added.push_back(registry.add(action("extra")));
      }
    }
    return true;
  }));
  const auto last = registry.add(action("last"));

  // The new actions are not part of the walk that registered them.
  EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"first", "last"}));
  EXPECT_EQ(added.size(), 64u);
  EXPECT_EQ(registry.visibleFor(modA).size(), 66u);
}

TEST(ModContextActions, DroppingTheLastHandleRemovesTheAction)
{
  ModContextActions registry;
  const auto keep = registry.add(action("keep"));
  {
    const auto temporary = registry.add(action("temporary"));
    EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"keep", "temporary"}));
  }

  EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"keep"}));
}

TEST(ModContextActions, ActionsOutliveTheRegistryThatHeldThem)
{
  ModContextActions::Handle handle;
  {
    ModContextActions registry;
    handle = registry.add(action("orphan"));
  }
  EXPECT_EQ(handle->label, "orphan");
}

TEST(ModContextActions, ThrowingVisibleHidesThatActionOnly)
{
  ModContextActions registry;
  const auto before = registry.add(action("before"));
  const auto broken = registry.add(action("broken", [](IModInterface*) -> bool {
    throw std::runtime_error("boom");
  }));
  const auto after  = registry.add(action("after"));

  EXPECT_EQ(labels(registry.visibleFor(modA)), (QStringList{"before", "after"}));
}

TEST(ModContextActions, TriggerPassesModAndParentThrough)
{
  IModInterface* seenMod = nullptr;
  QWidget* seenParent    = nullptr;
  ModContextActions registry;
  const auto handle = registry.add({"x",
                                    [&](IModInterface* mod, QWidget* widget) {
                                      seenMod    = mod;
                                      seenParent = widget;
                                    },
                                    {}});

  ModContextActions::trigger(*handle, modB, parent);

  EXPECT_EQ(seenMod, modB);
  EXPECT_EQ(seenParent, parent);
}

TEST(ModContextActions, TriggerSwallowsExceptions)
{
  ModContextActions::Action throwsStd{"std",
                                      [](IModInterface*, QWidget*) {
                                        throw std::runtime_error("boom");
                                      },
                                      {}};
  ModContextActions::Action throwsInt{"int",
                                      [](IModInterface*, QWidget*) {
                                        throw 42;
                                      },
                                      {}};

  EXPECT_NO_THROW(ModContextActions::trigger(throwsStd, modA, parent));
  EXPECT_NO_THROW(ModContextActions::trigger(throwsInt, modA, parent));
}

TEST(ModContextActionsGuarded, DisabledPluginHidesWithoutCallingVisible)
{
  bool visibleCalled = false;
  auto guarded       = ModContextActions::guarded(
      action("x",
             [&](IModInterface*) {
               visibleCalled = true;
               return true;
             }),
      [] {
        return false;
      },
      "plugin");

  EXPECT_FALSE(guarded.visible(modA));
  EXPECT_FALSE(visibleCalled);
}

TEST(ModContextActionsGuarded, EmptyVisibleIsShownWhileEnabled)
{
  bool enabled = true;
  auto guarded = ModContextActions::guarded(
      action("x"),
      [&] {
        return enabled;
      },
      "plugin");

  EXPECT_TRUE(guarded.visible(modA));
  enabled = false;
  EXPECT_FALSE(guarded.visible(modA));
}

TEST(ModContextActionsGuarded, VisibleResultPassesThroughWhileEnabled)
{
  auto guarded = ModContextActions::guarded(
      action("x",
             [](IModInterface* mod) {
               return mod == modB;
             }),
      [] {
        return true;
      },
      "plugin");

  EXPECT_FALSE(guarded.visible(modA));
  EXPECT_TRUE(guarded.visible(modB));
}

TEST(ModContextActionsGuarded, ThrowingVisibleIsHidden)
{
  auto guarded = ModContextActions::guarded(
      action("x",
             [](IModInterface*) -> bool {
               throw std::runtime_error("boom");
             }),
      [] {
        return true;
      },
      "plugin");

  bool shown = true;
  EXPECT_NO_THROW(shown = guarded.visible(modA));
  EXPECT_FALSE(shown);
}

TEST(ModContextActionsGuarded, ThrowingTriggeredReturnsNormally)
{
  auto stdThrow = ModContextActions::guarded(
      {"std",
       [](IModInterface*, QWidget*) {
         throw std::runtime_error("boom");
       },
       {}},
      [] {
        return true;
      },
      "plugin");
  auto intThrow = ModContextActions::guarded(
      {"int",
       [](IModInterface*, QWidget*) {
         throw 42;
       },
       {}},
      [] {
        return true;
      },
      "plugin");

  EXPECT_NO_THROW(stdThrow.triggered(modA, parent));
  EXPECT_NO_THROW(intThrow.triggered(modA, parent));
}

TEST(ModContextActionsGuarded, TriggeredPassesModAndParentThrough)
{
  IModInterface* seenMod = nullptr;
  QWidget* seenParent    = nullptr;
  auto guarded           = ModContextActions::guarded(
      {"x",
       [&](IModInterface* mod, QWidget* widget) {
         seenMod    = mod;
         seenParent = widget;
       },
       {}},
      [] {
        return true;
      },
      "plugin");

  guarded.triggered(modB, parent);

  EXPECT_EQ(seenMod, modB);
  EXPECT_EQ(seenParent, parent);
}

int main(int argc, char** argv)
{
  // the registry logs what plugin callbacks throw, which needs a default logger
  MOBase::log::LoggerConfiguration configuration;
  configuration.name = "test_modcontextactions";
  MOBase::log::createDefault(configuration);
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}

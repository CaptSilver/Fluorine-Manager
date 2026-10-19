#include "modcontextactions.h"

#include <exception>

#include <log.h>

using namespace MOBase;

ModContextActions::Handle ModContextActions::add(Action action)
{
  auto handle = std::make_shared<const Action>(std::move(action));
  m_Actions.push_back(handle);
  return handle;
}

std::vector<ModContextActions::Handle>
ModContextActions::visibleFor(IModInterface* mod) const
{
  std::erase_if(m_Actions, [](const auto& weak) {
    return weak.expired();
  });

  // A visible() may register another action, which can reallocate m_Actions, so
  // walk a copy. Actions added during the walk are not part of it.
  std::vector<Handle> candidates;
  for (const auto& weak : m_Actions) {
    if (auto handle = weak.lock()) {
      candidates.push_back(std::move(handle));
    }
  }

  std::vector<Handle> shown;
  for (const auto& handle : candidates) {
    bool visible = true;
    if (handle->visible) {
      try {
        visible = handle->visible(mod);
      } catch (const std::exception& e) {
        log::error("context menu action '{}': visible check failed: {}", handle->label,
                   e.what());
        visible = false;
      } catch (...) {
        log::error("context menu action '{}': visible check failed", handle->label);
        visible = false;
      }
    }

    if (visible) {
      shown.push_back(handle);
    }
  }
  return shown;
}

void ModContextActions::trigger(const Action& action, IModInterface* mod,
                                QWidget* parent)
{
  try {
    action.triggered(mod, parent);
  } catch (const std::exception& e) {
    log::error("context menu action '{}' failed: {}", action.label, e.what());
  } catch (...) {
    log::error("context menu action '{}' failed", action.label);
  }
}

ModContextActions::Action
ModContextActions::guarded(Action action, std::function<bool()> enabled, QString owner)
{
  Action wrapped;
  wrapped.label = action.label;

  wrapped.triggered = [triggered = std::move(action.triggered), label = action.label,
                       owner](IModInterface* mod, QWidget* parent) {
    try {
      triggered(mod, parent);
    } catch (const std::exception& e) {
      log::error("plugin '{}': context menu action '{}' failed: {}", owner, label,
                 e.what());
    } catch (...) {
      log::error("plugin '{}': context menu action '{}' failed", owner, label);
    }
  };

  wrapped.visible = [visible = std::move(action.visible), enabled = std::move(enabled),
                     label = action.label, owner](IModInterface* mod) {
    if (!enabled()) {
      return false;
    }
    if (!visible) {
      return true;
    }
    try {
      return visible(mod);
    } catch (const std::exception& e) {
      log::error("plugin '{}': context menu action '{}' visible check failed: {}",
                 owner, label, e.what());
    } catch (...) {
      log::error("plugin '{}': context menu action '{}' visible check failed", owner,
                 label);
    }
    return false;
  };

  return wrapped;
}

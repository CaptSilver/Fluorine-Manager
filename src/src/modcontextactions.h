#ifndef MODCONTEXTACTIONS_H
#define MODCONTEXTACTIONS_H

#include <functional>
#include <memory>
#include <vector>

#include <QString>

#include <imodinterface.h>

class QWidget;

// Entries that plugins add to the mod list's context menu through
// IModList::addContextMenuAction().
class ModContextActions
{
public:
  struct Action
  {
    QString label;
    std::function<void(MOBase::IModInterface*, QWidget*)> triggered;
    std::function<bool(MOBase::IModInterface*)> visible;  // empty = always
  };

  // Shared handle: the registry keeps only a weak reference, so an action is gone as
  // soon as the last handle is dropped, whichever side outlives the other.
  using Handle = std::shared_ptr<const Action>;

  Handle add(Action action);

  // Actions to show for mod, in registration order. A visible() that throws is logged
  // and treated as hidden; expired handles are pruned on the way.
  std::vector<Handle> visibleFor(MOBase::IModInterface* mod) const;

  // Runs action.triggered, logging instead of propagating anything it throws. The
  // menu calls this from a QAction slot, and Qt does not support an exception
  // unwinding through its event dispatch.
  static void trigger(const Action& action, MOBase::IModInterface* mod,
                      QWidget* parent);

  // What a plugin proxy forwards for one registration: visible is gated on enabled()
  // (an empty visible becomes "true while enabled", and enabled() returning false
  // hides the action without calling visible), and both callables catch everything,
  // logging owner and label.
  static Action guarded(Action action, std::function<bool()> enabled, QString owner);

private:
  mutable std::vector<std::weak_ptr<const Action>> m_Actions;
};

#endif  // MODCONTEXTACTIONS_H

#include "collectionplacementmoves.h"

#include <QtGlobal>

#include <algorithm>

namespace
{
bool sameName(const QString& a, const QString& b)
{
  return a.compare(b, Qt::CaseInsensitive) == 0;
}

int indexOf(const std::vector<QString>& layout, const QString& folder)
{
  for (std::size_t i = 0; i < layout.size(); ++i) {
    if (sameName(layout[i], folder))
      return static_cast<int>(i);
  }
  return -1;
}

// Profile::setModPriority puts the mod at the new slot and shifts every mod between the
// old and the new slot one place toward the old one. On a low-to-high list that is
// erase-then-insert.
void moveInLayout(std::vector<QString>& layout, int from, int to)
{
  QString folder = std::move(layout[from]);
  layout.erase(layout.begin() + from);
  layout.insert(layout.begin() + to, std::move(folder));
}

// Records the move and applies it to the working layout; a move to the same slot is
// dropped, as Profile::setModPriority would.
void emitMove(std::vector<QString>& layout, std::vector<PriorityMove>& moves, int from,
              int to)
{
  if (from == to)
    return;
  moves.push_back({layout[from], to});
  moveInLayout(layout, from, to);
}

const InstalledMod* findInState(const CollectionState& state, const QString& folder)
{
  for (const InstalledMod& installed : state.mods) {
    if (sameName(installed.folder, folder))
      return &installed;
  }
  return nullptr;
}

// A mod the top-up has to place: freshly installed, or present without a listed
// enabled state in the profile (an existing folder the profile never listed sits
// wherever the refresh put it, not in the block).
bool isMover(const CollectionState& state, const PlannedMod& planned,
             const QString& presentFolder)
{
  if (presentFolder.isEmpty())
    return false;
  if (planned.action == PlannedMod::Action::Install)
    return true;
  const InstalledMod* known = findInState(state, presentFolder);
  return !known || !known->listedEnabled.has_value();
}

// Layout index a new install is placed directly above: the nearest preceding manifest
// entry that is present, else the highest mod of the existing block, else -1.
int topUpAnchor(const CollectionPlan& plan, const CollectionState& state,
                const std::vector<QString>& present,
                const std::vector<QString>& layout, std::size_t index, std::size_t count)
{
  for (std::size_t j = index; j-- > 0;) {
    if (present[j].isEmpty() || sameName(present[j], present[index]))
      continue;
    const int at = indexOf(layout, present[j]);
    if (at >= 0)
      return at;
  }
  int top = -1;
  for (std::size_t j = 0; j < count; ++j) {
    if (j == index || present[j].isEmpty() || isMover(state, plan.mods[j], present[j]) ||
        sameName(present[j], present[index]))
      continue;
    top = std::max(top, indexOf(layout, present[j]));
  }
  return top;
}
}  // namespace

std::vector<QString> simulateMoves(std::vector<QString> layoutLowToHigh,
                                   const std::vector<PriorityMove>& moves)
{
  for (const PriorityMove& move : moves) {
    const int from = indexOf(layoutLowToHigh, move.folder);
    if (from < 0)
      continue;
    const int to = std::clamp(move.newPriority, 0,
                              static_cast<int>(layoutLowToHigh.size()) - 1);
    moveInLayout(layoutLowToHigh, from, to);
  }
  return layoutLowToHigh;
}

std::vector<PriorityMove> placementMoves(const CollectionPlan& plan,
                                         const CollectionState& state,
                                         const std::vector<QString>& presentFolders,
                                         const std::vector<QString>& layoutLowToHigh)
{
  Q_ASSERT(presentFolders.size() == plan.mods.size());
  const std::size_t count = std::min(plan.mods.size(), presentFolders.size());

  std::vector<QString> layout = layoutLowToHigh;
  std::vector<PriorityMove> moves;

  if (plan.firstInstall) {
    // Every present mod goes above everything already in the profile, manifest order,
    // last entry on top. They are moved from the top down so a move never shifts a
    // slot that is already final, which also makes a replay move nothing.
    std::vector<QString> block;
    for (std::size_t i = 0; i < count; ++i) {
      const QString& folder = presentFolders[i];
      if (!folder.isEmpty() && indexOf(layout, folder) >= 0 && indexOf(block, folder) < 0)
        block.push_back(folder);
    }
    for (std::size_t k = block.size(); k-- > 0;) {
      const int target = static_cast<int>(layout.size() - (block.size() - k));
      emitMove(layout, moves, indexOf(layout, block[k]), target);
    }
    return moves;
  }

  // Top-up: the listed existing block and everything above it stay where they are.
  for (std::size_t i = 0; i < count; ++i) {
    if (!isMover(state, plan.mods[i], presentFolders[i]))
      continue;
    const int current = indexOf(layout, presentFolders[i]);
    if (current < 0)
      continue;
    const int anchor = topUpAnchor(plan, state, presentFolders, layout, i, count);
    int target       = static_cast<int>(layout.size()) - 1;
    if (anchor >= 0)
      target = current < anchor ? anchor : anchor + 1;
    emitMove(layout, moves, current, target);
  }
  return moves;
}

QStringList placementEnableSet(const CollectionPlan& plan, const CollectionState& state,
                               const std::vector<QString>& presentFolders)
{
  Q_ASSERT(presentFolders.size() == plan.mods.size());
  const std::size_t count = std::min(plan.mods.size(), presentFolders.size());

  QStringList enable;
  for (std::size_t i = 0; i < count; ++i) {
    const QString& folder = presentFolders[i];
    if (folder.isEmpty())
      continue;

    const PlannedMod::Action action = plan.mods[i].action;
    bool wanted = action == PlannedMod::Action::Install || action == PlannedMod::Action::Replace;
    if (!wanted) {
      // A mod the user had listed keeps its + or -; one the profile did not list
      // before the run is switched on.
      const InstalledMod* known = findInState(state, folder);
      wanted                    = !known || !known->listedEnabled.has_value();
    }
    if (wanted && !enable.contains(folder, Qt::CaseInsensitive))
      enable.push_back(folder);
  }
  return enable;
}

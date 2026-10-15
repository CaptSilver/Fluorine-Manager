#pragma once

#include "collectionplan.h"
#include "collectionstate.h"

#include <QString>
#include <QStringList>

#include <vector>

struct PriorityMove
{
  QString folder;
  int newPriority = 0;
};

// presentFolders is parallel to plan.mods: the folder name when the mod is present after the run, else empty.
// state is what the profile held before the run: a top-up moves a present mod it did not list.
// layoutLowToHigh lists regular mods by priority after the pre-placement refresh (index = priority).
std::vector<PriorityMove> placementMoves(const CollectionPlan& plan,
                                         const CollectionState& state,
                                         const std::vector<QString>& presentFolders,
                                         const std::vector<QString>& layoutLowToHigh);

std::vector<QString> simulateMoves(std::vector<QString> layoutLowToHigh,
                                   const std::vector<PriorityMove>& moves);  // mirrors Profile::setModPriority shifting

QStringList placementEnableSet(const CollectionPlan& plan, const CollectionState& state,
                               const std::vector<QString>& presentFolders);

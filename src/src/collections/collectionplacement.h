#pragma once

#include "collectionlink.h"
#include "collectionplan.h"
#include "collectionstate.h"

#include <QString>

#include <vector>

class IUserInterface;
class OrganizerCore;

namespace CollectionPlacement {
// Runs once, after the last install and only when the run was not cancelled.
//
//  1. creates the profile when `choices.newProfile` is set and it does not
//  exist yet,
//  2. switches to it through the UI (so the profile box handlers run),
//  3. refreshes so the new folders have mod indices,
//  4. sets the priorities as one batch,
//  5. enables the mods installed or first seen in this run, disables
//  `plan.toDisable`,
//  6. writes modlist.txt now and refreshes again.
//
// `plan` must be the plan as it ran, `planAsRun(plan, report)`: a Replace whose
// download failed has to be passed as AlreadyInstalled, or placement moves and
// enables a folder the run never touched. `presentFolders` is parallel to
// `plan.mods`. On failure `error` says why and
// nothing after the failing step is touched.
bool place(OrganizerCore &core, IUserInterface *ui, const CollectionLink &link,
           const CollectionPlan &plan, const CollectionState &state,
           const PlanChoices &choices,
           const std::vector<QString> &presentFolders, QString &error);
} // namespace CollectionPlacement

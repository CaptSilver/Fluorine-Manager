#include "collectionplacement.h"

#include "collectionplacementmoves.h"

#include "game_features.h"
#include "iuserinterface.h"
#include "modinfo.h"
#include "modlist.h"
#include "organizercore.h"
#include "profile.h"

#include <log.h>
#include <uibase/filesystemutilities.h>

#include <QList>
#include <QObject>

#include <climits>
#include <exception>
#include <optional>
#include <utility>

using namespace MOBase;

namespace {
// Mod names by priority, lowest first. Every mod with a priority of its own
// takes part: foreign mods and separators count towards the number of ranked
// mods, so leaving them out would put every index off by their count.
bool readLayout(Profile &profile, std::vector<QString> &layout,
                QString &error) {
  const unsigned int count = ModInfo::getNumMods();

  int ranked = 0;
  for (unsigned int i = 0; i < count; ++i) {
    if (!ModInfo::getByIndex(i)->hasAutomaticPriority()) {
      ++ranked;
    }
  }

  layout.assign(ranked, QString());
  for (unsigned int i = 0; i < count; ++i) {
    const ModInfo::Ptr info = ModInfo::getByIndex(i);
    if (info->hasAutomaticPriority()) {
      continue;
    }
    const int priority = profile.getModPriority(i);
    if (priority < 0 || priority >= ranked || !layout[priority].isEmpty()) {
      error =
          QObject::tr(
              "the priorities in the profile are not contiguous (\"%1\" has "
              "priority %2)")
              .arg(info->name())
              .arg(priority);
      return false;
    }
    layout[priority] = info->name();
  }
  return true;
}

std::optional<unsigned int> indexOf(const QString &folder) {
  const unsigned int index = ModInfo::getIndex(folder);
  if (index == UINT_MAX) {
    return std::nullopt;
  }
  return index;
}
} // namespace

bool CollectionPlacement::place(OrganizerCore &core, IUserInterface *ui,
                                const CollectionLink &link,
                                const CollectionPlan &plan,
                                const CollectionState &state,
                                const PlanChoices &choices,
                                const std::vector<QString> &presentFolders,
                                QString &error) {
  const auto fail = [&error](const QString &message) {
    error = message;
    log::error("collection placement: {}", message);
    return false;
  };

  try {
    // 1. the profile
    QString profileName = choices.targetProfile;
    if (profileName.isEmpty()) {
      return fail(QObject::tr("no target profile was chosen"));
    }
    if (choices.newProfile && !fixDirectoryName(profileName)) {
      return fail(QObject::tr("\"%1\" is not a valid profile name")
                      .arg(choices.targetProfile));
    }

    const bool exists = core.profileNames().contains(profileName);
    if (!exists && !choices.newProfile) {
      return fail(
          QObject::tr("the profile \"%1\" does not exist").arg(profileName));
    }
    if (!exists) {
      // Created the way OrganizerCore::createDefaultProfile does; the profile
      // object only has to live long enough to write its files, the combo box
      // picks the profile up from the directory.
      Profile created(profileName, core.managedGame(), core.gameFeatures(),
                      false);
      core.profileCreated(&created);
      log::info("collection {}: created profile '{}'", link.slug, profileName);
    }

    // 2. switch through the UI: OrganizerCore::setCurrentProfile alone skips
    // the profile box handlers (saveCurrentLists, LocalSavegames,
    // BSAInvalidation).
    const auto before = core.currentProfile();
    const bool alreadyCurrent =
        before != nullptr && before->name() == profileName;
    if (!alreadyCurrent) {
      if (ui == nullptr) {
        return fail(
            QObject::tr("there is no main window to switch the profile with"));
      }
      if (!ui->selectProfile(profileName)) {
        return fail(QObject::tr("could not switch to the profile \"%1\"")
                        .arg(profileName));
      }
    }

    const auto profile = core.currentProfile();
    if (profile == nullptr || profile->name() != profileName) {
      return fail(QObject::tr("the profile \"%1\" is not the current profile")
                      .arg(profileName));
    }

    // 3. the new folders need mod indices. Switching already refreshed; staying
    // on the current profile did not. OrganizerCore::doInstall does the same
    // thing: refresh, then set the priority straight away, without waiting for
    // directoryStructureReady.
    if (alreadyCurrent) {
      core.refresh();
    }

    // 4. priorities, as one batch
    std::vector<QString> layout;
    if (!readLayout(*profile, layout, error)) {
      log::error("collection placement: {}", error);
      return false;
    }

    const std::vector<PriorityMove> moves =
        placementMoves(plan, state, presentFolders, layout);
    std::vector<std::pair<int, int>> batch;
    batch.reserve(moves.size());
    for (const PriorityMove &move : moves) {
      const auto index = indexOf(move.folder);
      if (!index) {
        return fail(
            QObject::tr("\"%1\" is not in the mod list after the refresh")
                .arg(move.folder));
      }
      batch.emplace_back(static_cast<int>(*index), move.newPriority);
    }

    // 5. enabled state, resolved before anything is changed so a missing folder
    // cannot leave a half-applied result
    QList<unsigned int> enable;
    for (const QString &folder :
         placementEnableSet(plan, state, presentFolders)) {
      const auto index = indexOf(folder);
      if (!index) {
        return fail(
            QObject::tr("\"%1\" is not in the mod list after the refresh")
                .arg(folder));
      }
      enable.append(*index);
    }
    QList<unsigned int> disable;
    for (const QString &folder : plan.toDisable) {
      if (const auto index = indexOf(folder)) {
        disable.append(*index);
      } else {
        log::warn("collection placement: '{}' to disable is no longer in the "
                  "mod list",
                  folder);
      }
    }

    core.modList()->setPriorities(batch);

    std::vector<QString> after;
    QString ignored;
    if (readLayout(*profile, after, ignored) &&
        after != simulateMoves(layout, moves)) {
      log::warn("collection placement: the resulting order differs from the "
                "simulated one");
    }

    profile->setModsEnabled(enable, disable);

    // 6. write now (the writer is delayed) and refresh so every view and the
    // virtual file system see the final state
    profile->writeModlistNow();
    core.refresh();

    log::info("collection {}: placed {} moves, enabled {}, disabled {} in '{}'",
              link.slug, moves.size(), enable.size(), disable.size(),
              profileName);
    return true;
  } catch (const std::exception &e) {
    return fail(QObject::tr("placement failed: %1").arg(e.what()));
  }
}

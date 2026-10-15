#include "collectionrunner.h"

#include "collectionarchiveinstaller.h"
#include "collectionlive.h"
#include "collectionmetatag.h"

#include "downloadmanager.h"
#include "game_features.h"
#include "installationmanager.h"
#include "modinfo.h"
#include "modinforegular.h"
#include "organizercore.h"
#include "settings.h"

#include <guessedvalue.h>
#include <ifiletree.h>
#include <log.h>
#include <uibase/game_features/moddatachecker.h>

#include <QDir>
#include <QFileInfo>
#include <QTimer>

#include <climits>
#include <exception>
#include <optional>

using namespace MOBase;

namespace {
// The ModInfo of a folder that exists in the mod list, or null. Separators and
// backups derive from ModInfoRegular, so they are excluded by name.
ModInfoRegular *liveRegular(const QString &folder, ModInfo::Ptr &keepAlive) {
  const unsigned int index = ModInfo::getIndex(folder);
  if (index == UINT_MAX) {
    return nullptr;
  }
  keepAlive = ModInfo::getByIndex(index);
  if (!keepAlive->isRegular() || keepAlive->isSeparator() ||
      keepAlive->isBackup()) {
    return nullptr;
  }
  return dynamic_cast<ModInfoRegular *>(keepAlive.data());
}

bool isWorkAction(PlannedMod::Action action) {
  return action == PlannedMod::Action::Install ||
         action == PlannedMod::Action::Replace;
}
} // namespace

CollectionRunner::CollectionRunner(OrganizerCore &core,
                                   const CollectionLink &link,
                                   const CollectionManifest &manifest,
                                   const CollectionPlan &plan,
                                   std::shared_ptr<std::atomic_bool> cancel,
                                   QObject *parent)
    : QObject(parent), m_core(core), m_link(link), m_manifest(manifest),
      m_plan(plan), m_cancel(std::move(cancel)) {
  m_report = seedReport(m_manifest, m_link, m_plan);
  if (m_report.rows.size() != m_plan.mods.size()) {
    // Rows are addressed by plan position; keep every index valid and say so
    // loudly.
    log::error("collection: report has {} rows for {} planned mods",
               m_report.rows.size(), m_plan.mods.size());
    m_report.sessionError =
        tr("internal error: the report does not match the plan");
    m_report.rows.resize(m_plan.mods.size());
  }

  m_present.assign(m_plan.mods.size(), QString());
  m_settled.assign(m_plan.mods.size(), 0);

  for (std::size_t i = 0; i < m_plan.mods.size(); ++i) {
    const PlannedMod &planned = m_plan.mods[i];
    if (planned.action == PlannedMod::Action::AlreadyInstalled ||
        planned.action == PlannedMod::Action::Adopt ||
        planned.action == PlannedMod::Action::Replace) {
      // A Replace keeps its old folder until its install starts, so a mod whose
      // download fails still counts as present.
      m_present[i] = planned.folder;
    }
    if (isWorkAction(planned.action)) {
      ++m_total;
    }
  }
}

void CollectionRunner::tagAdopted() {
  for (std::size_t i = 0; i < m_plan.mods.size(); ++i) {
    const PlannedMod &planned = m_plan.mods[i];
    const bool existing =
        planned.action == PlannedMod::Action::Adopt ||
        planned.action == PlannedMod::Action::AlreadyInstalled;
    if (!planned.addOwner || !existing) {
      continue;
    }

    const CollectionModSource &source = planned.mod->source;
    const CollectionOwner owner{.slug = m_link.slug,
                                .revision = m_link.revision,
                                .modId = source.modId,
                                .fileId = source.fileId};

    ModInfo::Ptr keepAlive;
    ModInfoRegular *live = liveRegular(planned.folder, keepAlive);
    if (live != nullptr &&
        addCollectionOwner(*live, owner, source.modId, source.fileId)) {
      continue;
    }

    // A folder the mod list does not know yet has no ModInfo to go through; the
    // file is the only place to write, as for a fresh install.
    if (live == nullptr) {
      const QString metaPath =
          m_core.settings().paths().mods() + "/" + planned.folder + "/meta.ini";
      if (CollectionMetaTag::write(
              metaPath,
              CollectionMetaTag::withOwner(
                  CollectionMetaTag::readOwners(metaPath), owner),
              source.modId, source.fileId)) {
        continue;
      }
    }

    log::warn("collection: could not tag '{}' with {} r{}", planned.folder,
              m_link.slug, m_link.revision);
    if (CollectionReportRow *r = row(static_cast<int>(i))) {
      r->notes << tr(
          "the collection tag could not be written to meta.ini; the next run "
          "adopts this folder again");
    }
  }
}

void CollectionRunner::enqueue(int planIndex, const QString &archivePath) {
  if (!isWork(planIndex) || m_settled[planIndex]) {
    log::warn(
        "collection: ignoring archive for plan entry {} (not waiting for one)",
        planIndex);
    return;
  }
  m_queue.append(Job{planIndex, archivePath});
  QTimer::singleShot(0, this, &CollectionRunner::drain);
}

void CollectionRunner::fail(int planIndex, const QString &reason) {
  if (!isWork(planIndex) || m_settled[planIndex]) {
    return;
  }
  failRow(planIndex, reason, true);
}

bool CollectionRunner::idle() const {
  return !m_installing && m_queue.isEmpty();
}

void CollectionRunner::settleRemaining(CollectionReportRow::Outcome outcome,
                                       const QString &reason, bool retryable) {
  for (int i = 0; i < static_cast<int>(m_plan.mods.size()); ++i) {
    if (!isWork(i) || m_settled[i]) {
      continue;
    }
    if (CollectionReportRow *r = row(i)) {
      r->outcome = outcome;
      r->reason = reason;
      r->retryable = retryable;
    }
    settle(i);
  }
}

void CollectionRunner::drain() {
  // An install runs an event loop, so this slot can fire inside one. The loop
  // below picks up whatever arrived once the running install returns.
  if (m_installing) {
    return;
  }

  while (!m_queue.isEmpty()) {
    if (m_cancel->load()) {
      while (!m_queue.isEmpty()) {
        const Job skipped = m_queue.takeFirst();
        if (CollectionReportRow *r = row(skipped.planIndex)) {
          r->outcome = CollectionReportRow::Outcome::Skipped;
          r->reason =
              tr("run cancelled before this mod was installed; run the link "
                 "again to finish");
        }
        settle(skipped.planIndex);
      }
      break;
    }

    // The user may have an install of their own open.
    if (m_core.installationManager()->isRunning()) {
      QTimer::singleShot(BusyRetryMs, this, &CollectionRunner::drain);
      return;
    }

    const Job job = m_queue.takeFirst();
    m_installing = true;
    try {
      install(job);
    } catch (const std::exception &e) {
      failRow(job.planIndex, tr("installing failed: %1").arg(e.what()), false);
    } catch (...) {
      failRow(job.planIndex, tr("installing failed with an unknown error"),
              false);
    }
    m_installing = false;
  }

  if (idle()) {
    emit becameIdle();
  }
}

void CollectionRunner::install(const Job &job) {
  const PlannedMod &planned = m_plan.mods[job.planIndex];
  const CollectionMod &mod = *planned.mod;
  InstallationManager *manager = m_core.installationManager();

  const QFileInfo archive(job.archivePath);
  if (!archive.isFile()) {
    failRow(job.planIndex,
            tr("the downloaded archive is missing: %1").arg(job.archivePath),
            true);
    return;
  }

  // install() reports an unsupported extension with a message box.
  if (!manager->getSupportedExtensions().contains(archive.suffix(),
                                                  Qt::CaseInsensitive)) {
    failRow(job.planIndex,
            tr("archives of type \".%1\" cannot be installed")
                .arg(archive.suffix()),
            false);
    return;
  }

  const std::shared_ptr<ModDataChecker> checker =
      m_core.gameFeatures().gameFeature<ModDataChecker>();

  QStringList notes;
  QString installerReason;

  UnattendedInstall unattended;
  unattended.replaceExistingFolder =
      planned.action == PlannedMod::Action::Replace;
  unattended.installer =
      [&](GuessedValue<QString> &, std::shared_ptr<IFileTree> &tree,
          const std::function<QString(std::shared_ptr<const FileTreeEntry>)>
              &readFile,
          QString &reason) {
        const auto result = CollectionArchiveInstaller::install(
            tree, mod.choices, checker.get(), readFile, notes, reason);
        installerReason = reason;
        return result;
      };

  manager->setModsDirectory(m_core.settings().paths().mods());
  manager->notifyInstallationStart(job.archivePath, false, nullptr);

  // GUESS_USER outranks the name taken from the download's .meta, and the
  // constructor stores the value unfiltered, so the folder is exactly the
  // planner's name.
  GuessedValue<QString> modName(planned.folder, GUESS_USER);

  if (planned.action == PlannedMod::Action::Replace) {
    // From here the old folder may be emptied; do not place it unless the
    // install succeeds.
    m_present[job.planIndex].clear();
  }

  QString managerReason;
  std::optional<InstallationResult> result;
  {
    UnattendedInstallGuard guard(*manager, std::move(unattended));
    result.emplace(
        manager->install(job.archivePath, modName, mod.source.modId));
    managerReason = guard.failureReason();
  }

  CollectionReportRow *r = row(job.planIndex);

  if (!*result) {
    QString reason =
        !installerReason.isEmpty() ? installerReason : managerReason;
    if (reason.isEmpty()) {
      reason = result->result() == IPluginInstaller::RESULT_CANCELED
                   ? tr("the installation was cancelled")
                   : tr("the installation failed");
    }
    if (r != nullptr) {
      r->notes += notes;
      // The manager refuses an existing folder it was not told to replace; that
      // is the case the results dialog offers Replace for.
      if (planned.action != PlannedMod::Action::Replace &&
          QDir(m_core.settings().paths().mods() + "/" + planned.folder)
              .exists()) {
        r->folderExists = true;
      }
    }
    // Not retryable as it stands: the same install would be refused again. A
    // "folder exists" row offers Replace instead, which the session turns into
    // a retry.
    failRow(job.planIndex, reason, false);
    return;
  }

  const QString folder = result->name();
  m_present[job.planIndex] = folder;

  const bool tagged = tagInstalled(planned, folder);
  if (r != nullptr) {
    r->outcome = planned.action == PlannedMod::Action::Replace
                     ? CollectionReportRow::Outcome::Replaced
                     : CollectionReportRow::Outcome::Installed;
    r->folder = folder;
    r->notes += notes;
    if (!tagged) {
      r->notes << tr(
          "the collection tag could not be written to meta.ini; the next run "
          "will not recognise this folder");
    }
  }

  // By file name: row numbers move whenever a download is removed or the list
  // refreshes.
  m_core.downloadManager()->markInstalled(archive.fileName());
  settle(job.planIndex);
}

bool CollectionRunner::tagInstalled(const PlannedMod &planned,
                                    const QString &folder) {
  const CollectionModSource &source = planned.mod->source;
  const CollectionOwner owner{.slug = m_link.slug,
                              .revision = m_link.revision,
                              .modId = source.modId,
                              .fileId = source.fileId};

  // A replaced folder still has a live ModInfo whose destructor and next save
  // would write its old settings over the file, so it is tagged through that
  // object. Reload it first: the install just rewrote meta.ini and the object
  // still holds the old values.
  ModInfo::Ptr keepAlive;
  if (ModInfoRegular *live = liveRegular(folder, keepAlive)) {
    live->readMeta();
    return addCollectionOwner(*live, owner, source.modId, source.fileId);
  }

  // A fresh folder has no ModInfo until the placement refresh, so the file is
  // the only place to write.
  const QString metaPath =
      m_core.settings().paths().mods() + "/" + folder + "/meta.ini";
  return CollectionMetaTag::write(
      metaPath,
      CollectionMetaTag::withOwner(CollectionMetaTag::readOwners(metaPath),
                                   owner),
      source.modId, source.fileId);
}

void CollectionRunner::failRow(int planIndex, const QString &reason,
                               bool retryable) {
  if (CollectionReportRow *r = row(planIndex)) {
    r->outcome = CollectionReportRow::Outcome::Failed;
    r->reason = reason;
    r->retryable = retryable;
  }
  log::warn("collection {}: {} failed: {}", m_link.slug, modLabel(planIndex),
            reason);
  settle(planIndex);
}

void CollectionRunner::settle(int planIndex) {
  if (!isWork(planIndex) || m_settled[planIndex]) {
    return;
  }
  m_settled[planIndex] = 1;
  ++m_settledCount;
  emit progress(m_settledCount, m_total);
  if (idle()) {
    emit becameIdle();
  }
}

bool CollectionRunner::isWork(int planIndex) const {
  return planIndex >= 0 && planIndex < static_cast<int>(m_plan.mods.size()) &&
         isWorkAction(m_plan.mods[planIndex].action);
}

CollectionReportRow *CollectionRunner::row(int planIndex) {
  if (planIndex < 0 || planIndex >= static_cast<int>(m_report.rows.size())) {
    return nullptr;
  }
  return &m_report.rows[planIndex];
}

QString CollectionRunner::modLabel(int planIndex) const {
  if (planIndex < 0 || planIndex >= static_cast<int>(m_plan.mods.size())) {
    return QStringLiteral("#%1").arg(planIndex);
  }
  return m_plan.mods[planIndex].mod->name;
}

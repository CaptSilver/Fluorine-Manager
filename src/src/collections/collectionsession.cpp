#include "collectionsession.h"

#include "collectiondownloader.h"
#include "collectionlive.h"
#include "collectionmanifestfetcher.h"
#include "collectionplacement.h"
#include "collectionresultsdialog.h"
#include "collectionreviewdialog.h"
#include "collectionrunner.h"
#include "downloadmanagerport.h"
#include "filehashconcurrent.h"

#include "downloadmanager.h"
#include "iuserinterface.h"
#include "nexusinterface.h"
#include "nxmaccessmanager.h"
#include "organizercore.h"

#include <iplugingame.h>
#include <log.h>
#include <uibase/filesystemutilities.h>

#include <QDir>
#include <QMainWindow>
#include <QNetworkRequest>
#include <QProgressDialog>
#include <QStandardPaths>
#include <QSystemTrayIcon>

using namespace MOBase;

namespace
{
// Downloader submissions and the stall check run off this timer.
constexpr int TickMs = 500;

bool isWork(PlannedMod::Action action)
{
  return action == PlannedMod::Action::Install || action == PlannedMod::Action::Replace;
}

bool gameMatches(const QString& domain, const IPluginGame* game)
{
  return game != nullptr &&
         collectionDomainMatches(domain, game->gameShortName(), game->gameNexusName(),
                                 game->validShortNames());
}
}  // namespace

CollectionSession::CollectionSession(OrganizerCore& core, QObject* parent)
    : QObject(parent), m_core(core)
{
  m_tick.setInterval(TickMs);
  connect(&m_tick, &QTimer::timeout, this, &CollectionSession::onTick);
}

CollectionSession::~CollectionSession()
{
  if (m_cancel) {
    m_cancel->store(true);
  }
  m_tick.stop();
  closeProgress();
  teardownRun();
}

void CollectionSession::setUserInterface(IUserInterface* ui)
{
  m_ui = ui;
  if (ui == nullptr) {
    m_uiReady = false;
    cancel();
  }
}

void CollectionSession::userInterfaceInitialized()
{
  m_uiReady = true;
  if (m_waiting && m_state == State::Idle) {
    const CollectionLink link = *m_waiting;
    m_waiting.reset();
    begin(link);
  }
}

void CollectionSession::request(const CollectionLink& link)
{
  if (m_state != State::Idle) {
    notify(tr("A collection install is already running."));
    return;
  }

  if (!m_uiReady) {
    log::info("holding collection link {} r{} until the main window is ready", link.slug,
              link.revision);
    m_waiting = link;
    return;
  }

  begin(link);
}

void CollectionSession::cancel()
{
  switch (m_state) {
  case State::Fetching:
    // ends in fetchFailed("cancelled")
    if (m_fetcher != nullptr) {
      m_fetcher->cancel();
    }
    break;
  case State::Running:
    if (m_cancel && !m_cancel->exchange(true)) {
      log::info("collection {}: cancelled", m_link.slug);
      if (m_progress) {
        // Leave the button alone: this runs from its clicked signal.
        m_progress->setLabelText(tr("Cancelling after the current mod..."));
        m_progress->show();
      }
      checkFinished();
    }
    break;
  case State::Idle:
  case State::Review:   // a modal dialog is open
  case State::Placing:  // one synchronous pass
  case State::Done:
    break;
  }
}

void CollectionSession::begin(const CollectionLink& link)
{
  const IPluginGame* game = m_core.managedGame();
  if (game == nullptr) {
    notify(tr("No game is loaded, so the collection cannot be installed."));
    return;
  }

  // Login, then Premium, then game. The login check has to be validated(): the nexusApi()
  // queue in OrganizerCore only waits for a login attempt, and an invalid key gets past it.
  NexusInterface& nexus = NexusInterface::instance();
  const bool loggedIn   = nexus.getAccessManager()->validated();
  const bool premium =
      nexus.getAPIUserAccount().type() == APIUserAccountTypes::Premium;
  const CollectionPrecheck check =
      collectionPrecheck(loggedIn, premium, gameMatches(link.domain, game));
  if (check != CollectionPrecheck::Ok) {
    notify(collectionPrecheckMessage(check));
    return;
  }

  m_link = link;
  m_manifest.reset();
  m_restrictToRetry = false;
  m_retryFileIds.clear();
  setState(State::Fetching);
  notify(tr("Fetching collection %1, revision %2...").arg(link.slug).arg(link.revision),
         false);

  // Requests to the Nexus API carry the user's credentials; makeCollectionRequest refuses
  // every other host. The mirror download is a plain GET with no credentials at all.
  const auto api = [](const QUrl& url, const QByteArray& body) {
    return NexusInterface::instance().getAccessManager()->makeCollectionRequest(url, body);
  };
  // The fetcher follows redirects itself: https only, at most five.
  const auto publicGet = [this](const QUrl& url) {
    QNetworkRequest request(url);
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                         QNetworkRequest::ManualRedirectPolicy);
    request.setTransferTimeout(120000);
    return m_public.get(request);
  };

  // The fetcher keeps one folder per revision below this root.
  const QString cacheRoot =
      QStandardPaths::writableLocation(QStandardPaths::CacheLocation) +
      QStringLiteral("/collections");
  m_fetcher = new CollectionManifestFetcher(api, publicGet, cacheRoot, this);
  connect(m_fetcher, &CollectionManifestFetcher::ready, this,
          &CollectionSession::manifestReady);
  connect(m_fetcher, &CollectionManifestFetcher::failed, this,
          &CollectionSession::fetchFailed);
  m_fetcher->fetch(link);
}

void CollectionSession::fetchFailed(const QString& reason)
{
  releaseFetcher();
  if (m_state != State::Fetching) {
    return;
  }

  if (reason == QLatin1String("cancelled")) {
    log::info("collection {}: fetch cancelled", m_link.slug);
  } else {
    notify(tr("Could not fetch collection %1, revision %2: %3")
               .arg(m_link.slug)
               .arg(m_link.revision)
               .arg(reason));
  }
  reset();
}

void CollectionSession::manifestReady(const CollectionManifest& manifest, int,
                                      const QString& cacheDir)
{
  // The fetcher has already checked that the revision number is the one asked for.
  releaseFetcher();
  m_cacheDir = cacheDir;

  if (!gameMatches(manifest.info.domainName, m_core.managedGame())) {
    notify(tr("Collection %1 is for \"%2\", not for the game this instance manages.")
               .arg(m_link.slug)
               .arg(manifest.info.domainName));
    reset();
    return;
  }

  m_manifest = manifest;

  const auto current = m_core.currentProfile();
  m_captured = captureCollectionState(m_core, current ? current->name() : QString());

  setState(State::Review);
  QWidget* parent = m_ui != nullptr ? m_ui->mainWindow() : nullptr;
  CollectionReviewDialog dialog(
      m_link, *m_manifest, m_captured,
      NexusInterface::instance().getAPIUserAccount().remainingRequests(), parent);
  if (dialog.exec() != QDialog::Accepted) {
    log::info("collection {}: review declined", m_link.slug);
    reset();
    return;
  }

  m_choices = dialog.choices();
  if (m_choices.newProfile && m_choices.targetProfile.isEmpty()) {
    m_choices.targetProfile = m_manifest->info.name;
  }

  // Profile creation sanitises the name. Do it once here so the directory name and
  // m_choices.targetProfile stay equal, which the retry check in presentResults relies on.
  if (m_choices.newProfile && !fixDirectoryName(m_choices.targetProfile)) {
    notify(tr("\"%1\" is not a valid profile name.").arg(m_choices.targetProfile));
    reset();
    return;
  }
  startRun();
}

void CollectionSession::startRun()
{
  // listedEnabled depends on the profile the mods land in, so state is captured again for
  // it. A profile that does not exist yet has no mod list, which is exactly "nothing
  // listed". The current profile's list is flushed first (captureCollectionState does).
  m_captured = captureCollectionState(
      m_core, m_choices.newProfile ? QString() : m_choices.targetProfile);
  m_plan = planCollection(m_link, *m_manifest, m_captured, m_choices);

  if (m_restrictToRetry) {
    for (PlannedMod& planned : m_plan.mods) {
      if (isWork(planned.action) && !m_retryFileIds.contains(planned.mod->source.fileId)) {
        planned.action        = PlannedMod::Action::Refused;
        planned.reason        = tr("not selected for retry");
        planned.needsDownload = false;
      }
    }
  }

  m_cancel = std::make_shared<std::atomic_bool>(false);
  m_clock.restart();
  m_finishQueued = false;
  setState(State::Running);

  m_port   = new DownloadManagerPort(*m_core.downloadManager(), NexusInterface::instance(), this);
  m_hasher = new ConcurrentFileHasher(this);
  m_runner = new CollectionRunner(m_core, m_link, *m_manifest, m_plan, m_cancel, this);
  connect(m_runner, &CollectionRunner::progress, this, &CollectionSession::runnerProgress);
  connect(m_runner, &CollectionRunner::becameIdle, this, &CollectionSession::checkFinished);

  // Adopted folders get their tag before the first install, so a crash leaves them
  // recognisable for the next run.
  m_runner->tagAdopted();

  // Every Install and Replace goes to the downloader, not only the ones the planner
  // thinks are missing: the downloader reuses files that are already on disk (and checks
  // their MD5) without spending API requests. A file listed twice is requested once.
  m_planByFile.clear();
  std::vector<DownloadRequest> requests;
  int work = 0;
  for (int i = 0; i < static_cast<int>(m_plan.mods.size()); ++i) {
    const PlannedMod& planned = m_plan.mods[i];
    if (!isWork(planned.action)) {
      continue;
    }
    ++work;
    const CollectionModSource& source = planned.mod->source;
    auto& indices = m_planByFile[{source.modId, source.fileId}];
    if (indices.empty()) {
      requests.push_back({source.modId, source.fileId, source.md5, source.fileSize});
    }
    indices.push_back(i);
  }

  m_downloader = std::make_unique<CollectionDownloader>(
      *m_port, *m_port, *m_hasher, m_cancel,
      [this] {
        return static_cast<qint64>(m_clock.elapsed());
      },
      [this](int modId, int fileId, const QString& path) {
        const auto it = m_planByFile.find({modId, fileId});
        if (m_runner == nullptr || it == m_planByFile.end()) {
          return;
        }
        for (const int index : it->second) {
          m_runner->enqueue(index, path);
        }
      },
      [this](int modId, int fileId, const QString& reason) {
        const auto it = m_planByFile.find({modId, fileId});
        if (m_runner == nullptr || it == m_planByFile.end()) {
          return;
        }
        for (const int index : it->second) {
          m_runner->fail(index, reason);
        }
      });
  m_port->setListener(m_downloader.get());

  showProgress(work);
  m_downloader->start(m_link.domain, std::move(requests));
  m_tick.start();
  m_downloader->tick();
  checkFinished();
}

void CollectionSession::onTick()
{
  if (m_state != State::Running) {
    return;
  }
  if (m_downloader) {
    m_downloader->tick();
  }
  checkFinished();
}

void CollectionSession::runnerProgress(int settled, int total)
{
  if (m_progress) {
    m_progress->setMaximum(total > 0 ? total : 1);
    m_progress->setValue(settled);
    m_progress->setLabelText(tr("Installing %1: %2 of %3 mods done")
                                 .arg(m_manifest ? m_manifest->info.name : m_link.slug)
                                 .arg(settled)
                                 .arg(total));
  }
}

void CollectionSession::checkFinished()
{
  if (m_state != State::Running || m_finishQueued || m_runner == nullptr ||
      !m_runner->idle()) {
    return;
  }

  // After a cancel the downloader may still hold transfers that DownloadManager keeps
  // for later; only the install in progress has to end.
  const bool cancelled = m_cancel && m_cancel->load();
  if (!cancelled && m_downloader && !m_downloader->idle()) {
    return;
  }

  // Not inline: this can be reached from inside the runner's own signal.
  m_finishQueued = true;
  QTimer::singleShot(0, this, &CollectionSession::finishRun);
}

CollectionReport CollectionSession::collectReport(bool cancelled)
{
  if (cancelled) {
    m_runner->settleRemaining(CollectionReportRow::Outcome::Skipped,
                              tr("run cancelled before this mod was installed; run the "
                                 "link again to finish"),
                              false);
  } else {
    // The downloader settles every request it was given; a mod that is neither installed
    // nor failed means a bug upstream, and it must not look like a success.
    m_runner->settleRemaining(CollectionReportRow::Outcome::Failed,
                              tr("no result was reported for this mod"), true);
  }

  CollectionReport report = m_runner->report();
  report.droppedFolders   = m_plan.droppedFolders;
  report.cancelled        = cancelled;
  return report;
}

void CollectionSession::finishRun()
{
  m_finishQueued = false;
  if (m_state != State::Running || m_runner == nullptr) {
    return;
  }

  m_tick.stop();
  closeProgress();

  const bool cancelled    = m_cancel && m_cancel->load();
  CollectionReport report = collectReport(cancelled);

  if (!cancelled) {
    setState(State::Placing);
    QString error;
    report.placed = CollectionPlacement::place(m_core, m_ui, m_link,
                                               planAsRun(m_plan, report), m_captured,
                                               m_choices, m_runner->presentFolders(), error);
    if (!report.placed) {
      report.sessionError = error;
      notify(tr("Collection %1 could not be placed: %2").arg(m_link.slug).arg(error));
    }
  }

  teardownRun();
  setState(State::Done);
  emit finished(report);
  presentResults(report);
}

void CollectionSession::presentResults(const CollectionReport& report)
{
  QWidget* parent = m_ui != nullptr ? m_ui->mainWindow() : nullptr;
  CollectionResultsDialog dialog(report, parent);
  dialog.exec();

  if (dialog.retryRequested() && !report.cancelled && m_manifest) {
    // Same manifest, same choices. The plan is rebuilt from what is installed now, so
    // everything that succeeded is AlreadyInstalled and only the ticked failures run.
    m_choices.replaceFileIds = dialog.replaceFileIds();
    m_retryFileIds           = dialog.retryFileIds();
    m_retryFileIds.insert(m_choices.replaceFileIds.begin(), m_choices.replaceFileIds.end());
    m_restrictToRetry = true;
    // The first run created the profile if it had to; later runs top it up.
    m_choices.newProfile = !m_core.profileNames().contains(m_choices.targetProfile);
    startRun();
    return;
  }

  reset();
}

void CollectionSession::reset()
{
  m_tick.stop();
  closeProgress();
  teardownRun();
  releaseFetcher();
  removeCache();
  m_manifest.reset();
  m_plan            = {};
  m_restrictToRetry = false;
  m_retryFileIds.clear();
  setState(State::Idle);
}

void CollectionSession::teardownRun()
{
  // The order is load-bearing. A hash job that is still running has already queued its
  // finished event, and the callback it ends in belongs to the downloader. Deleting the
  // hasher later (deleteLater) would let that event run against a freed downloader, so the
  // hasher goes first and synchronously (no hasher slot ever calls this), then the port's
  // pointer to the downloader, then the downloader. Raising the token covers any other
  // late callback that checks it.
  if (m_cancel) {
    m_cancel->store(true);
  }
  delete m_hasher;
  m_hasher = nullptr;
  if (m_port != nullptr) {
    m_port->setListener(nullptr);
  }
  m_downloader.reset();

  for (QObject* object :
       {static_cast<QObject*>(m_port), static_cast<QObject*>(m_runner)}) {
    if (object != nullptr) {
      object->disconnect(this);
      object->deleteLater();
    }
  }
  m_port   = nullptr;
  m_runner = nullptr;
  m_planByFile.clear();
}

void CollectionSession::releaseFetcher()
{
  if (m_fetcher != nullptr) {
    m_fetcher->disconnect(this);
    m_fetcher->deleteLater();
    m_fetcher = nullptr;
  }
}

void CollectionSession::removeCache()
{
  if (m_cacheDir.isEmpty()) {
    return;
  }

  // Only ever delete inside the application's own cache directory.
  const QString root =
      QDir(QStandardPaths::writableLocation(QStandardPaths::CacheLocation)).absolutePath();
  const QString dir = QDir(m_cacheDir).absolutePath();
  if (!root.isEmpty() && dir.startsWith(root + QLatin1Char('/'))) {
    QDir(dir).removeRecursively();
  }
  m_cacheDir.clear();
}

void CollectionSession::showProgress(int total)
{
  if (total <= 0 || m_ui == nullptr) {
    return;
  }

  // Application-modal keeps the user out of the main window (no profile switch, no
  // install of their own) while the event loop still runs the downloads.
  m_progress = new QProgressDialog(tr("Installing %1...").arg(m_manifest->info.name),
                                   tr("Cancel"), 0, total, m_ui->mainWindow());
  m_progress->setWindowTitle(tr("Nexus collection"));
  m_progress->setWindowModality(Qt::ApplicationModal);
  m_progress->setMinimumDuration(0);
  m_progress->setAutoClose(false);
  m_progress->setAutoReset(false);
  m_progress->setValue(0);
  connect(m_progress, &QProgressDialog::canceled, this, &CollectionSession::cancel);
  m_progress->show();
}

void CollectionSession::closeProgress()
{
  if (!m_progress) {
    return;
  }
  // hide(), not close(): closing a QProgressDialog emits canceled().
  m_progress->disconnect(this);
  m_progress->hide();
  m_progress->deleteLater();
  m_progress = nullptr;
}

void CollectionSession::notify(const QString& message, bool problem)
{
  if (problem) {
    log::warn("collection: {}", message);
  } else {
    log::info("collection: {}", message);
  }
  if (m_ui != nullptr) {
    m_ui->showNotification(tr("Nexus collection"), message,
                           problem ? QSystemTrayIcon::Warning
                                   : QSystemTrayIcon::Information);
  }
}

void CollectionSession::setState(State state)
{
  if (m_state == state) {
    return;
  }
  m_state = state;
  emit stateChanged(state);
}

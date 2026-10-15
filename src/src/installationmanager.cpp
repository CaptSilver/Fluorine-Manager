/*
Copyright (C) 2012 Sebastian Herbord. All rights reserved.

This file is part of Mod Organizer.

Mod Organizer is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Mod Organizer is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Mod Organizer.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <tuple>

#include "installationmanager.h"

#include "categories.h"
#include "filesystemutilities.h"
#include "iplugininstallercustom.h"
#include "iplugininstallersimple.h"
#include "messagedialog.h"
#include "metainiutils.h"
#include "modinfo.h"
#include "nexusinterface.h"
#include "queryoverwritedialog.h"
#include "questionboxmemory.h"
#include "report.h"
#include "selectiondialog.h"
#include "settings.h"
#include <scopeguard.h>
#include <utility.h>

#include <QApplication>
#include <QDateTime>
#include <QDebug>
#include <QDir>
#include <QDirIterator>
#include <QFileInfo>
#include <QInputDialog>
#include <QLibrary>
#include <QMessageBox>
#include <QPushButton>
#include <QSettings>
#include <QTemporaryDir>
#include <QTextDocument>
#include <QtConcurrent/QtConcurrentRun>

#include <boost/assign.hpp>
#include <boost/scoped_ptr.hpp>

#include "archivefiletree.h"

using namespace MOBase;
using namespace MOShared;

namespace
{
QString storeMetaPath(const QString& value)
{
  if (value.isEmpty()) {
    return value;
  }

  if (MOBase::isWindowsDrivePath(value) || QDir::isAbsolutePath(value)) {
    return MOBase::normalizePathForWine(value);
  }

  return QDir::fromNativeSeparators(value);
}
}  // namespace

InstallationResult::InstallationResult(IPluginInstaller::EInstallResult result)
    : m_result(result)

{}

template <typename T>
static T resolveFunction(QLibrary& lib, const char* name)
{
  T temp = reinterpret_cast<T>(lib.resolve(name));
  if (temp == nullptr) {
    throw std::runtime_error(QObject::tr("invalid 7z.so: %1")
                                 .arg(lib.errorString())
                                 .toLatin1()
                                 .constData());
  }
  return temp;
}

InstallationManager::InstallationManager()
{
  m_ArchiveHandler = CreateArchive();
  if (!m_ArchiveHandler->isValid()) {
    throw MyException(getErrorString(m_ArchiveHandler->getLastError()));
  }
  m_ArchiveHandler->setLogCallback([](auto level, auto const& message) {
    using LogLevel = Archive::LogLevel;
    switch (level) {
    case LogLevel::Debug:
      log::debug("{}", message);
      break;
    case LogLevel::Info:
      log::info("{}", message);
      break;
    case LogLevel::Warning:
      log::warn("{}", message);
      break;
    case LogLevel::Error:
      log::error("{}", message);
      break;
    }
  });

  // Connect the query password slot - This is the only way I found to be able to query
  // user from a separate thread. We use a BlockingQueuedConnection so that calling
  // passwordRequested() will block until the end of the slot.
  connect(this, &InstallationManager::passwordRequested, this,
          &InstallationManager::queryPassword, Qt::BlockingQueuedConnection);
}

InstallationManager::~InstallationManager() = default;

void InstallationManager::setParentWidget(QWidget* widget)
{
  m_ParentWidget = widget;
}

void InstallationManager::setPluginContainer(const PluginContainer* pluginContainer)
{
  m_PluginContainer = pluginContainer;
}

void InstallationManager::queryPassword()
{
  if (m_Unattended) {
    // Nobody to ask. An empty password makes an encrypted archive fail to open
    // (or to extract), which the caller reports.
    m_Password.clear();
    return;
  }
  m_Password = QInputDialog::getText(m_ParentWidget, tr("Password required"),
                                     tr("Password"), QLineEdit::Password);
}

bool InstallationManager::extractFiles(QString extractPath, QString title,
                                       bool showFilenames, bool silent)
{
  TimeThis const tt("InstallationManager::extractFiles");

  // Callback for errors:
  QString errorMessage;
  auto errorCallback = [&errorMessage, this](std::wstring const& message) {
    m_ArchiveHandler->cancel();
    errorMessage = QString::fromStdWString(message);
  };

  // The future that will hold the result:
  QFuture<bool> future;

  // Unattended installs never take the blocking branch: waiting here would stop the
  // GUI thread from serving downloads and sockets for the whole archive.
  if (silent && !m_Unattended) {
    future = QtConcurrent::run([&]() -> bool {
      return m_ArchiveHandler->extract(extractPath.toStdWString(), nullptr, nullptr,
                                       errorCallback);
    });
    future.waitForFinished();
  } else {
    // Unattended installs run the same event loop but show no dialog, so this
    // pointer is null for them and every use below checks it.
    QProgressDialog* installationProgress =
        m_Unattended ? nullptr : new QProgressDialog(m_ParentWidget);
    ON_BLOCK_EXIT([=]() {
      if (installationProgress != nullptr) {
        installationProgress->cancel();
        installationProgress->hide();
        installationProgress->deleteLater();
      }
    });
    if (installationProgress != nullptr) {
      installationProgress->setWindowFlags(installationProgress->windowFlags() &
                                           (~Qt::WindowContextHelpButtonHint));
      if (!title.isEmpty()) {
        installationProgress->setWindowTitle(title);
      }
      installationProgress->setWindowModality(Qt::WindowModal);
      installationProgress->setFixedSize(600, 100);

      // Turn off auto-reset otherwize the progress dialog is reset before the end.
      // This is kind of annoying because updateProgress consider percentage of
      // progression through the archive (pack), while we are waiting for extracting
      // archive entries, so the percentage of in updateProgress is not really related
      // to the percentage of files extracted...
      installationProgress->setAutoReset(false);
    }

    // Note: Using a loop with a progressUpdate() that only wake-up the loop. The
    // event-loop will be used in a loop and not via exec() because connecting to
    // QProgressDialog::setValue and using .exec() creates huge recursion that leads to
    // stack-overflow. See https://bugreports.qt.io/browse/QTBUG-10561
    QEventLoop loop;
    connect(this, &InstallationManager::progressUpdate, &loop, &QEventLoop::wakeUp,
            Qt::QueuedConnection);

    // Cancelling progress only cancel the extraction, we do not force exiting the
    // event-loop:
    if (installationProgress != nullptr) {
      connect(installationProgress, &QProgressDialog::canceled, [this]() {
        m_ArchiveHandler->cancel();
      });
    }

    std::mutex mutex;
    int currentProgress = 0;
    QString currentFileName;

    // The callbacks:
    auto progressCallback = [this, &currentProgress, &mutex](
                                auto progressType, uint64_t current, uint64_t total) {
      if (progressType == Archive::ProgressType::EXTRACTION) {
        {
          std::scoped_lock const guard(mutex);
          currentProgress = static_cast<int>(100 * current / total);
        }
        emit progressUpdate();
      }
    };
    Archive::FileChangeCallback fileChangeCallback =
        [this, &currentFileName, &mutex](auto changeType, std::wstring const& file) {
          if (changeType == Archive::FileChangeType::EXTRACTION_START) {
            {
              std::scoped_lock const guard(mutex);
              currentFileName = QString::fromStdWString(file);
            }
            emit progressUpdate();
          }
        };

    // unpack only the files we need for the installer
    QFutureWatcher<bool> futureWatcher;
    connect(&futureWatcher, &QFutureWatcher<bool>::finished, &loop, &QEventLoop::wakeUp,
            Qt::QueuedConnection);
    futureWatcher.setFuture(QtConcurrent::run([&]() -> bool {
      return m_ArchiveHandler->extract(extractPath.toStdWString(), progressCallback,
                                       showFilenames ? fileChangeCallback : nullptr,
                                       errorCallback);
    }));

    if (installationProgress != nullptr) {
      installationProgress->setModal(true);
      installationProgress->show();
    }

    while (!futureWatcher.isFinished()) {
      loop.processEvents(QEventLoop::AllEvents | QEventLoop::WaitForMoreEvents);
      if (installationProgress != nullptr) {
        std::scoped_lock const guard(mutex);
        if (currentProgress != installationProgress->value()) {
          installationProgress->setValue(currentProgress);
        }
        if (currentFileName != installationProgress->labelText()) {
          installationProgress->setLabelText(currentFileName);
        }
      }
    }

    if (installationProgress != nullptr) {
      installationProgress->hide();
    }

    future = futureWatcher.future();
  }

  // Check the result:
  if (!future.result()) {
    if (m_ArchiveHandler->getLastError() == Archive::Error::ERROR_EXTRACT_CANCELLED) {
      if (!errorMessage.isEmpty()) {
        throw MyException(tr("Extraction failed: %1").arg(errorMessage));
      } else {
        return false;
      }
    } else {
      throw MyException(tr("Extraction failed: %1")
                            .arg(static_cast<int>(m_ArchiveHandler->getLastError())));
    }
  }

  return true;
}

QString InstallationManager::extractFile(std::shared_ptr<const FileTreeEntry> entry,
                                         bool silent)
{
  QStringList result = this->extractFiles({entry}, silent);
  return result.isEmpty() ? QString() : result[0];
}

QStringList InstallationManager::extractFiles(
    std::vector<std::shared_ptr<const FileTreeEntry>> const& entries, bool silent)
{
  if (!m_TempExtractionDirectory || !m_TempExtractionDirectory->isValid()) {
    throw MyException(tr("Temporary extraction directory is unavailable."));
  }

  const QString extractionRoot = m_TempExtractionDirectory->path();

  // Remove the directory since mapToArchive would add them:
  std::vector<std::shared_ptr<const FileTreeEntry>> files;
  std::copy_if(entries.begin(), entries.end(), std::back_inserter(files),
               [](auto const& entry) {
                 return entry->isFile();
               });

  // Update the archive:
  ArchiveFileTree::mapToArchive(*m_ArchiveHandler, files);

  // Retrieve the file path:
  QStringList result;

  for (auto& entry : files) {
    // FileTreeEntry paths default to Windows separators. Use archive-style
    // separators here so the returned path names the file actually extracted
    // on case-sensitive Unix filesystems.
    auto path = entry->path("/");
    result.append(QDir(extractionRoot).filePath(path));
    m_TempFilesToDelete.insert(path);
  }

  if (!extractFiles(extractionRoot, tr("Extracting files"), false, silent)) {
    return {};
  }

  return result;
}

QString
InstallationManager::createFile(std::shared_ptr<const MOBase::FileTreeEntry> entry)
{
  if (!m_TempExtractionDirectory || !m_TempExtractionDirectory->isValid()) {
    throw MyException(tr("Temporary extraction directory is unavailable."));
  }

  // Use QTemporaryFile to create the temporary file with the given template:
  QTemporaryFile tempFile(
      QDir(m_TempExtractionDirectory->path()).filePath("mo2-install-XXXXXX"));

  // Turn-off autoRemove otherwise the file is deleted when destructor is called:
  tempFile.setAutoRemove(false);

  // Open/Close the file so that installer can use it properly:
  if (!tempFile.open()) {
    return {};
  }
  tempFile.close();

  // fileName() returns the full path since we provide a full path in the constructor:
  const QString absPath = tempFile.fileName();

  m_CreatedFiles[entry] = absPath;
  m_TempFilesToDelete.insert(
      QDir(m_TempExtractionDirectory->path()).relativeFilePath(absPath));

  // Returns the path with native separators:
  return QDir::toNativeSeparators(absPath);
}

void InstallationManager::cleanCreatedFiles(
    std::shared_ptr<const MOBase::IFileTree> fileTree)
{
  // We simply have to check if all the entries have fileTree as a parent:
  for (auto it = std::begin(m_CreatedFiles); it != std::end(m_CreatedFiles);) {

    // Find the parent - Could this be in FileTreeEntry?
    bool found = false;
    {
      auto parent = it->first->parent();
      while (parent && !found) {
        if (parent == fileTree) {
          found = true;
        } else {
          parent = parent->parent();
        }
      }
    }

    // If the parent was not found, we remove the entry, otherwize we move to the next
    // one:
    if (!found) {
      it = m_CreatedFiles.erase(it);
    } else {
      ++it;
    }
  }
}

IPluginInstaller::EInstallResult
InstallationManager::installArchive(GuessedValue<QString>& modName,
                                    const QString& archiveName, int modId)
{
  // in earlier versions the modName was copied here and the copy passed to install. I
  // don't know why I did this and it causes a problem if this is called by the bundle
  // installer and the bundled installer adds additional names that then end up being
  // used, because the caller will then not have the right name.
  return install(archiveName, modName, modId).result();
}

QString InstallationManager::generateBackupName(const QString& directoryName)
{
  QString backupName = directoryName + "_backup";
  if (QDir(backupName).exists()) {
    int idx      = 2;
    QString temp = backupName + QString::number(idx);
    while (QDir(temp).exists()) {
      ++idx;
      temp = backupName + QString::number(idx);
    }
    backupName = temp;
  }
  return backupName;
}

void InstallationManager::resetModDirectory(const QString& modName,
                                            const QString& targetDirectory)
{
  unsigned int const idx = ModInfo::getIndex(modName);
  if (idx != UINT_MAX) {
    auto modInfo = ModInfo::getByIndex(idx);
    // mark the old install file as uninstalled
    emit modReplaced(modInfo->installationFile());
  }
  // save original settings like categories. Because it makes sense
  QString metaFilename = targetDirectory + "/meta.ini";
  QFile settingsFile(metaFilename);
  QByteArray originalSettings;
  if (settingsFile.open(QIODevice::ReadOnly)) {
    originalSettings = settingsFile.readAll();
    settingsFile.close();
  }

  // remove the directory with all content, then recreate it empty
  shellDelete(QStringList(targetDirectory));
  if (!QDir().mkdir(targetDirectory)) {
    // The retry exists because Windows can keep a directory around for a
    // moment after delete. Linux doesn't have that problem; the sleep is
    // kept for safety in case the underlying filesystem is slow.
    QThread::msleep(100);
    QDir().mkdir(targetDirectory);
  }
  // restore the saved settings
  if (settingsFile.open(QIODevice::WriteOnly)) {
    settingsFile.write(originalSettings);
    settingsFile.close();
  } else {
    log::error("failed to restore original settings: {}", metaFilename);
  }
}

InstallationResult InstallationManager::testOverwrite(GuessedValue<QString>& modName)
{
  QString targetDirectory =
      QDir::fromNativeSeparators(m_ModsDirectory + "/" + modName);

  // this is only returned on success
  InstallationResult result{IPluginInstaller::RESULT_SUCCESS};

  if (m_Unattended && QDir(targetDirectory).exists()) {
    // No dialog to ask merge, replace or rename: the caller decided up front.
    if (!m_Unattended->replaceExistingFolder) {
      m_Unattended->failureReason =
          tr("folder \"%1\" already exists").arg(QString(modName));
      return {IPluginInstaller::RESULT_FAILED};
    }
    result.m_replaced = true;
    resetModDirectory(modName, targetDirectory);
    return result;
  }

  while (QDir(targetDirectory).exists()) {
    Settings& settings(Settings::instance());

    const bool backup = settings.keepBackupOnInstall();
    QueryOverwriteDialog overwriteDialog(m_ParentWidget,
                                         backup ? QueryOverwriteDialog::BACKUP_YES
                                                : QueryOverwriteDialog::BACKUP_NO);

    if (overwriteDialog.exec()) {
      settings.setKeepBackupOnInstall(overwriteDialog.backup());

      if (overwriteDialog.backup()) {
        QString const backupDirectory = generateBackupName(targetDirectory);
        if (!copyDir(targetDirectory, backupDirectory, false)) {
          reportError(tr("Failed to create backup"));
          return {IPluginInstaller::RESULT_FAILED};
        }
      }

      result.m_merged   = overwriteDialog.action() == QueryOverwriteDialog::ACT_MERGE;
      result.m_replaced = overwriteDialog.action() == QueryOverwriteDialog::ACT_REPLACE;
      result.m_backup   = overwriteDialog.backup();

      if (overwriteDialog.action() == QueryOverwriteDialog::ACT_RENAME) {
        bool ok      = false;
        QString const name = QInputDialog::getText(m_ParentWidget, tr("Mod Name"), tr("Name"),
                                             QLineEdit::Normal, modName, &ok);
        if (ok && !name.isEmpty()) {
          modName.update(name, GUESS_USER);
          if (!ensureValidModName(modName)) {
            return {IPluginInstaller::RESULT_FAILED};
          }
          targetDirectory = QDir::fromNativeSeparators(m_ModsDirectory) + "/" + modName;
        }
      } else if (overwriteDialog.action() == QueryOverwriteDialog::ACT_REPLACE) {
        resetModDirectory(modName, targetDirectory);
        return result;
      } else if (overwriteDialog.action() == QueryOverwriteDialog::ACT_MERGE) {
        return result;
      } else /* if (overwriteDialog.action() == QueryOverwriteDialog::ACT_NONE) */
      {
        return {IPluginInstaller::RESULT_CANCELED};
      }
    } else {
      return {IPluginInstaller::RESULT_CANCELED};
    }
  }

  QDir().mkdir(targetDirectory);

  return result;
}

bool InstallationManager::ensureValidModName(GuessedValue<QString>& name) const
{
  while (name->isEmpty()) {
    bool ok;
    name.update(
        QInputDialog::getText(
            m_ParentWidget, tr("Invalid name"),
            tr("The name you entered is invalid, please enter a different one."),
            QLineEdit::Normal, "", &ok),
        GUESS_USER);
    if (!ok) {
      return false;
    }
  }
  return true;
}

InstallationResult InstallationManager::doInstall(ModInstallationInfo& info)
{
  if (m_Unattended && info.modName->isEmpty()) {
    m_Unattended->failureReason = tr("the mod folder name is empty");
    return {IPluginInstaller::RESULT_FAILED};
  }
  if (!ensureValidModName(info.modName)) {
    return {IPluginInstaller::RESULT_FAILED};
  }

  bool const merge = false;
  // determine target directory
  InstallationResult result = testOverwrite(info.modName);
  if (!result) {
    return result;
  }

  result.m_name = info.modName;

  QString const targetDirectory =
      QDir(m_ModsDirectory + "/" + info.modName).canonicalPath();
  QString targetDirectoryNative = QDir::toNativeSeparators(targetDirectory);

  log::debug("installing to \"{}\"", targetDirectoryNative);
  if (!extractFiles(targetDirectory, "", true, false)) {
    return {IPluginInstaller::RESULT_CANCELED};
  }

  // Copy the created files:
  for (auto& p : m_CreatedFiles) {
    QString destPath =
        QDir::cleanPath(targetDirectory + QDir::separator() + p.first->path());
    log::debug("Moving {} to {}.", p.second, destPath);

    // We need to remove the path if it exists:
    if (QFile::exists(destPath)) {
      QFile::remove(destPath);
    }

    QDir const dir = QFileInfo(destPath).absoluteDir();
    if (!dir.exists()) {
      dir.mkpath(".");
    }

    QFile::copy(p.second, destPath);
  }

  const QString metaPath = targetDirectory + "/meta.ini";
  MetaIniUtils::normalizeMetaIniCase(metaPath);
  QSettings settingsFile(metaPath, QSettings::IniFormat);

  // overwrite settings only if they are actually are available or haven't been set
  // before
  if ((info.gameName != "") || !settingsFile.contains("gameName")) {
    settingsFile.setValue("gameName", info.gameName);
  }
  if ((info.modID != 0) || !settingsFile.contains("modid")) {
    settingsFile.setValue("modid", info.modID);
  }
  if (!settingsFile.contains("version") ||
      (!info.version.isEmpty() &&
       (!merge || (VersionInfo(info.version) >=
                   VersionInfo(settingsFile.value("version").toString()))))) {
    settingsFile.setValue("version", info.version);
  }
  if (!info.newestVersion.isEmpty() || !settingsFile.contains("newestVersion")) {
    settingsFile.setValue("newestVersion", info.newestVersion);
  }
  // issue #51 used to overwrite the manually set categories
  if (!settingsFile.contains("category")) {
    settingsFile.setValue("category", QString::number(info.categoryID));
  }
  settingsFile.setValue("nexusFileStatus", info.fileCategoryID);
  settingsFile.setValue("installationFile", storeMetaPath(m_CurrentFile));
  settingsFile.setValue("repository", info.repository);
  settingsFile.setValue("author", info.author);
  settingsFile.setValue("uploader", info.uploader);
  settingsFile.setValue("uploaderUrl", info.uploaderUrl);

  if (!merge) {
    // this does not clear the list we have in memory but the mod is going to have to be
    // re-read anyway btw.: installedFiles were written with beginWriteArray but we can
    // still clear it with beginGroup. nice
    settingsFile.beginGroup("installedFiles");
    settingsFile.remove("");
    settingsFile.endGroup();
  }

  return result;
}

bool InstallationManager::wasCancelled() const
{
  return m_ArchiveHandler->getLastError() == Archive::Error::ERROR_EXTRACT_CANCELLED;
}

bool InstallationManager::isRunning() const
{
  return m_IsRunning;
}

void InstallationManager::postInstallCleanup()
{
  // Clear the list of created files:
  m_CreatedFiles.clear();

  // Close the archive:
  m_ArchiveHandler->close();

  const QString extractionRoot =
      m_TempExtractionDirectory ? m_TempExtractionDirectory->path() : QString();

  // directories we may want to remove. sorted from longest to shortest to ensure we
  // remove subdirectories first.
  auto longestFirst = [](const QString& LHS, const QString& RHS) -> bool {
    if (LHS.size() != RHS.size())
      return LHS.size() > RHS.size();
    else
      return LHS < RHS;
  };

  std::set<QString, std::function<bool(const QString&, const QString&)>>
      directoriesToRemove(longestFirst);

  // clean up temp files
  // TODO: this doesn't yet remove directories. Also, the files may be left there if
  // this point isn't reached
  for (const QString& tempFile : m_TempFilesToDelete) {
    QFileInfo const fileInfo(QDir(extractionRoot).filePath(tempFile));
    if (fileInfo.exists()) {
      if (!fileInfo.isReadable() || !fileInfo.isWritable()) {
        QFile::setPermissions(fileInfo.absoluteFilePath(),
                              QFile::ReadOther | QFile::WriteOther);
      }
      if (!QFile::remove(fileInfo.absoluteFilePath())) {
        log::warn("Unable to delete {}", fileInfo.absoluteFilePath());
      }
    }
    directoriesToRemove.insert(fileInfo.absolutePath());
  }

  m_TempFilesToDelete.clear();

  // try to delete each directory we had temporary files in. the call fails for
  // non-empty directories which is ok
  for (const QString& dir : directoriesToRemove) {
    QDir().rmdir(dir);
  }

  // QTemporaryDir removes any paths an installer did not explicitly track,
  // including differently-cased files. This prevents one installation from
  // leaking FOMOD metadata into the next on case-sensitive filesystems.
  m_TempExtractionDirectory.reset();
}

InstallationResult InstallationManager::install(const QString& fileName,
                                                GuessedValue<QString>& modName,
                                                int modID)
{
  // The extraction event loop lets other code run during an unattended install, a
  // drag-and-drop install among it. Two installs would share one archive handler.
  if (m_IsRunning && m_Unattended) {
    log::warn("refusing to install \"{}\": another installation is still running",
              fileName);
    return {IPluginInstaller::RESULT_CANCELED};
  }

  m_IsRunning = true;
  ON_BLOCK_EXIT([this]() {
    m_IsRunning = false;
  });

  QFileInfo const fileInfo(fileName);
  if (!getSupportedExtensions().contains(fileInfo.suffix(), Qt::CaseInsensitive)) {
    reportError(tr("File format \"%1\" not supported").arg(fileInfo.suffix()));
    return {IPluginInstaller::RESULT_FAILED};
  }

  m_TempExtractionDirectory = std::make_unique<QTemporaryDir>(
      QDir::temp().filePath(QStringLiteral("fluorine-install-XXXXXX")));
  if (!m_TempExtractionDirectory->isValid()) {
    reportError(tr("Unable to create a private temporary directory for installation."));
    m_TempExtractionDirectory.reset();
    return {IPluginInstaller::RESULT_FAILED};
  }
  ON_BLOCK_EXIT(std::bind(&InstallationManager::postInstallCleanup, this));

  modName.setFilter(&fixDirectoryName);

  modName.update(QFileInfo(fileName).completeBaseName(), GUESS_FALLBACK);

  // read out meta information from the download if available
  QString gameName      = "";
  QString version       = "";
  QString newestVersion = "";
  int category          = 0;
  int categoryID        = 0;
  int fileCategoryID    = 1;
  QString repository    = "Nexus";
  QString author        = "";
  QString uploader      = "";
  QString uploaderUrl   = "";

  QString const metaName = fileName + ".meta";
  if (QFile(metaName).exists()) {
    QSettings const metaFile(metaName, QSettings::IniFormat);
    gameName = metaFile.value("gameName", "").toString();
    modID    = metaFile.value("modID", 0).toInt();
    QTextDocument doc;
    doc.setHtml(metaFile.value("name", "").toString());
    modName.update(doc.toPlainText(), GUESS_FALLBACK);
    modName.update(metaFile.value("modName", "").toString(), GUESS_META);

    version                    = metaFile.value("version", "").toString();
    newestVersion              = metaFile.value("newestVersion", "").toString();
    category                   = metaFile.value("category", 0).toInt();
    unsigned int const categoryIndex = CategoryFactory::instance().resolveNexusID(category);
    const bool unmappedCategory = category != 0 && categoryIndex == 0U &&
                                  Settings::instance().nexus().categoryMappings();
    if (unmappedCategory && m_Unattended) {
      // Nobody can answer the mapping question, so the mod stays uncategorised.
      log::debug("no category mapping for nexus category {}, installing without one",
                 category);
    } else if (unmappedCategory) {
      QMessageBox nexusQuery;
      nexusQuery.setWindowTitle(tr("No category found"));
      nexusQuery.setText(tr(
          "This Nexus category has not yet been mapped. Do you wish to proceed without "
          "setting a category, proceed and disable automatic Nexus mappings, or stop "
          "and configure your category mappings?"));
      nexusQuery.addButton(tr("&Proceed"), QMessageBox::YesRole);
      QPushButton* disableButton =
          nexusQuery.addButton(tr("&Disable"), QMessageBox::AcceptRole);
      QPushButton* stopButton =
          nexusQuery.addButton(tr("&Stop && Configure"), QMessageBox::DestructiveRole);
      nexusQuery.exec();
      if (nexusQuery.clickedButton() == disableButton) {
        Settings::instance().nexus().setCategoryMappings(false);
      } else if (nexusQuery.clickedButton() == stopButton) {
        return MOBase::IPluginInstaller::RESULT_CATEGORYREQUESTED;
      }
    } else {
      categoryID = CategoryFactory::instance().getCategoryID(categoryIndex);
    }
    repository     = metaFile.value("repository", "").toString();
    fileCategoryID = metaFile.value("fileCategory", 1).toInt();
    author         = metaFile.value("author", "").toString();
    uploader       = metaFile.value("uploader", "").toString();
    uploaderUrl    = metaFile.value("uploaderUrl", "").toString();
  }

  if (version.isEmpty()) {
    QDateTime const lastMod = fileInfo.lastModified();
    version           = "d" + lastMod.toString("yyyy.M.d");
  }

  {  // guess the mod name and mod if from the file name if there was no meta
     // information
    QString guessedModName;
    int guessedModID = modID;
    NexusInterface::interpretNexusFileName(QFileInfo(fileName).fileName(),
                                           guessedModName, guessedModID, false);
    if ((modID == 0) && (guessedModID != -1)) {
      modID = guessedModID;
    } else if (modID != guessedModID) {
      log::debug("passed mod id: {}, guessed id: {}", modID, guessedModID);
    }

    modName.update(guessedModName, GUESS_GOOD);
  }

  m_CurrentFile = fileInfo.absoluteFilePath();
  if (fileInfo.dir() == QDir(m_DownloadsDirectory)) {
    m_CurrentFile = fileInfo.fileName();
  }
  log::debug("using mod name \"{}\" (id {}) -> {}", QString(modName), modID,
             m_CurrentFile);

  // If there's an archive already open, close it. This happens with the bundle
  // installer when it uncompresses a split archive, then finds it has a real archive
  // to deal with.
  m_ArchiveHandler->close();

  // open the archive and construct the directory tree the installers work on

  bool const archiveOpen =
      m_ArchiveHandler->open(fileName.toStdWString(), [this]() -> std::wstring {
        m_Password = QString();

        // Note: If we are not in the Qt event thread, we cannot use queryPassword()
        // directly, so we emit passwordRequested() that is connected to
        // queryPassword(). The connection is made using Qt::BlockingQueuedConnection,
        // so the emit "call" is actually blocking. We cannot use emit if we are in the
        // even thread, otherwize we have a deadlock.
        if (QThread::currentThread() != QApplication::instance()->thread()) {
          emit passwordRequested();
        } else {
          queryPassword();
        }
        return m_Password.toStdWString();
      });
  if (!archiveOpen) {
    log::debug("integrated archiver can't open {}: {} ({})", fileName,
               getErrorString(m_ArchiveHandler->getLastError()),
               m_ArchiveHandler->getLastError());
  }
  std::shared_ptr<IFileTree> filesTree =
      archiveOpen ? ArchiveFileTree::makeTree(*m_ArchiveHandler) : nullptr;

  if (m_Unattended) {
    // Collection installs have nobody to answer prompts. Every refusal below is a
    // reason string for the caller's report, and nothing after this block (the
    // installer loop and its two message boxes) is reachable.
    UnattendedInstall& hook = *m_Unattended;
    hook.failureReason.clear();
    InstallationResult hooked(IPluginInstaller::RESULT_FAILED);

    if (filesTree == nullptr) {
      hook.failureReason = tr("cannot open the archive (%1)")
                               .arg(getErrorString(m_ArchiveHandler->getLastError()));
      return hooked;
    }
    if (!hook.installer) {
      hook.failureReason = tr("no installer was provided");
      return hooked;
    }

    try {
      QString reason;
      // With unattended set, a silent extraction runs the dialog-free event loop.
      const auto readFile = [this](std::shared_ptr<const FileTreeEntry> entry) {
        return extractFile(entry, true);
      };
      hooked.m_result = hook.installer(modName, filesTree, readFile, reason);
      if (!hooked) {
        hook.failureReason =
            reason.isEmpty() ? tr("the archive layout was not accepted") : reason;
        return hooked;
      }

      ModInstallationInfo info{
          modName,        gameName,   modID,  version,  newestVersion, categoryID,
          fileCategoryID, repository, author, uploader, uploaderUrl};
      hooked = completeSimpleInstall(filesTree, info);
      if (hooked) {
        auto iniTweakEntry = filesTree->find("INI Tweaks", FileTreeEntry::DIRECTORY);
        hooked.m_iniTweaks =
            iniTweakEntry != nullptr && !iniTweakEntry->astree()->empty();
      } else if (hook.failureReason.isEmpty()) {
        hook.failureReason = tr("the installation did not complete");
      }
    } catch (const std::exception& e) {
      // Extraction errors are exceptions here. The caller reads a reason, not a throw.
      hook.failureReason = QString::fromUtf8(e.what());
      hooked             = InstallationResult(IPluginInstaller::RESULT_FAILED);
    }
    return hooked;
  }

  auto installers = m_PluginContainer->plugins<IPluginInstaller>();

  std::sort(installers.begin(), installers.end(),
            [](IPluginInstaller* lhs, IPluginInstaller* rhs) {
              return lhs->priority() > rhs->priority();
            });

  InstallationResult installResult(IPluginInstaller::RESULT_NOTATTEMPTED);

  for (IPluginInstaller* installer : installers) {
    // don't use inactive installers (installer can't be null here but vc static code
    // analysis thinks it could)
    if ((installer == nullptr) || !m_PluginContainer->isEnabled(installer)) {
      continue;
    }

    // try only manual installers if that was requested
    if (installResult.result() == IPluginInstaller::RESULT_MANUALREQUESTED) {
      if (!installer->isManualInstaller()) {
        continue;
      }
    } else if (installResult.result() != IPluginInstaller::RESULT_NOTATTEMPTED) {
      break;
    }

    try {
      {  // simple case
        IPluginInstallerSimple* installerSimple =
            dynamic_cast<IPluginInstallerSimple*>(installer);
        if ((installerSimple != nullptr) && (filesTree != nullptr) &&
            (installer->isArchiveSupported(filesTree))) {
          installResult.m_result =
              installerSimple->install(modName, filesTree, version, modID);
          if (installResult) {
            // the simple installer only prepares the installation, the rest
            // works the same for all installers
            ModInstallationInfo info{
                modName,        gameName,   modID,  version,  newestVersion, categoryID,
                fileCategoryID, repository, author, uploader, uploaderUrl};

            installResult = completeSimpleInstall(filesTree, info);
          }
        }
      }

      if (installResult.result() != IPluginInstaller::RESULT_CANCELED) {  // custom case
        IPluginInstallerCustom* installerCustom =
            dynamic_cast<IPluginInstallerCustom*>(installer);
        if ((installerCustom != nullptr) &&
            (((filesTree != nullptr) && installer->isArchiveSupported(filesTree)) ||
             ((filesTree == nullptr) &&
              installerCustom->isArchiveSupported(fileName)))) {
          std::set<QString> const installerExt = installerCustom->supportedExtensions();
          if (installerExt.contains(fileInfo.suffix())) {
            installResult.m_result =
                installerCustom->install(modName, gameName, fileName, version, modID);
            unsigned int const idx = ModInfo::getIndex(modName);
            if (idx != UINT_MAX) {
              ModInfo::Ptr const info = ModInfo::getByIndex(idx);
              info->setRepository(repository);
            }
          }
        }
      }
    } catch (const IncompatibilityException& e) {
      log::error("plugin \"{}\" incompatible: {}", installer->name(), e.what());
    }

    // act upon the installation result. at this point the files have already been
    // extracted to the correct location
    switch (installResult.result()) {
    case IPluginInstaller::RESULT_FAILED: {
      QMessageBox::information(qApp->activeWindow(), tr("Installation failed"),
                               tr("Something went wrong while installing this mod."),
                               QMessageBox::Ok);
      return installResult;
    } break;
    case IPluginInstaller::RESULT_SUCCESS:
    case IPluginInstaller::RESULT_SUCCESSCANCEL: {
      if (filesTree != nullptr) {
        auto iniTweakEntry = filesTree->find("INI Tweaks", FileTreeEntry::DIRECTORY);
        installResult.m_iniTweaks =
            iniTweakEntry != nullptr && !iniTweakEntry->astree()->empty();
      }
      installResult.m_result = IPluginInstaller::RESULT_SUCCESS;
      return installResult;
    } break;
    case IPluginInstaller::RESULT_NOTATTEMPTED:
    case IPluginInstaller::RESULT_MANUALREQUESTED: {
      continue;
    }
    default:
      return installResult;
    }
  }
  if (installResult.result() == IPluginInstaller::RESULT_NOTATTEMPTED) {
    reportError(
        tr("None of the available installer plugins were able to handle that archive.\n"
           "This is likely due to a corrupted or incompatible download or unrecognized "
           "archive format."));
  }

  return installResult;
}

InstallationResult
InstallationManager::completeSimpleInstall(std::shared_ptr<IFileTree>& filesTree,
                                           ModInstallationInfo& info)
{
  // Downcast to an actual ArchiveFileTree and map to the archive. Test if
  // the tree is still an ArchiveFileTree, otherwize it means the installer
  // did some bad stuff.
  ArchiveFileTree* p = dynamic_cast<ArchiveFileTree*>(filesTree.get());
  if (p == nullptr) {
    throw IncompatibilityException(tr("Invalid file tree returned by plugin."));
  }

  // Detach the file tree (this ensure the parent is null and call to path()
  // stops at this root):
  p->detach();

  p->mapToArchive(*m_ArchiveHandler);

  // Clean the created files:
  cleanCreatedFiles(filesTree);

  return doInstall(info);
}

QString InstallationManager::getErrorString(Archive::Error errorCode)
{
  switch (errorCode) {
  case Archive::Error::ERROR_NONE: {
    return tr("no error");
  } break;
  case Archive::Error::ERROR_LIBRARY_NOT_FOUND: {
    return tr("7z.so not found");
  } break;
  case Archive::Error::ERROR_LIBRARY_INVALID: {
    return tr("7z.so isn't valid");
  } break;
  case Archive::Error::ERROR_ARCHIVE_NOT_FOUND: {
    return tr("archive not found");
  } break;
  case Archive::Error::ERROR_FAILED_TO_OPEN_ARCHIVE: {
    return tr("failed to open archive");
  } break;
  case Archive::Error::ERROR_INVALID_ARCHIVE_FORMAT: {
    return tr("unsupported archive type");
  } break;
  case Archive::Error::ERROR_LIBRARY_ERROR: {
    return tr("internal library error");
  } break;
  case Archive::Error::ERROR_ARCHIVE_INVALID: {
    return tr("archive invalid");
  } break;
  default: {
    // this probably means the archiver.dll is newer than this
    return tr("unknown archive error");
  } break;
  }
}

QStringList InstallationManager::getSupportedExtensions() const
{
  std::set<QString, CaseInsensitive> supportedExtensions(
      {"zip", "rar", "7z", "fomod", "001"});
  for (auto* installer : m_PluginContainer->plugins<IPluginInstaller>()) {
    if (m_PluginContainer->isEnabled(installer)) {
      if (auto* installerCustom = dynamic_cast<IPluginInstallerCustom*>(installer)) {
        std::set<QString> const extensions = installerCustom->supportedExtensions();
        supportedExtensions.insert(extensions.begin(), extensions.end());
      }
    }
  }
  return {supportedExtensions.begin(), supportedExtensions.end()};
}

void InstallationManager::notifyInstallationStart(QString const& archive,
                                                  bool reinstallation,
                                                  ModInfo::Ptr currentMod)
{
  const auto& installers = m_PluginContainer->plugins<IPluginInstaller>();
  for (auto* installer : installers) {
    if (m_PluginContainer->isEnabled(installer)) {
      installer->onInstallationStart(archive, reinstallation, currentMod.get());
    }
  }
}

void InstallationManager::notifyInstallationEnd(const InstallationResult& result,
                                                ModInfo::Ptr newMod)
{
  const auto& installers = m_PluginContainer->plugins<IPluginInstaller>();
  for (auto* installer : installers) {
    if (m_PluginContainer->isEnabled(installer)) {
      installer->onInstallationEnd(result.result(), newMod.get());
    }
  }
}

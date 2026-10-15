#include "fluorinepaths.h"
#include "fluorineconfig.h"
#include "nxmhandler_linux.h"

#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QSettings>
#include <QTemporaryDir>
#include <gtest/gtest.h>
#include <uibase/log.h>

#include <map>
#include <csignal>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

namespace
{
QByteArray readFile(const QString& path)
{
  QFile file(path);
  return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray{};
}

void writeFile(const QString& path, const QByteArray& content)
{
  ASSERT_TRUE(QDir().mkpath(QFileInfo(path).absolutePath()));
  QFile file(path);
  ASSERT_TRUE(file.open(QIODevice::WriteOnly));
  ASSERT_EQ(file.write(content), content.size());
}

class XdgPaths : public ::testing::Test
{
protected:
  QTemporaryDir temp;
  std::map<QByteArray, QByteArray> saved;

  void setEnv(const QByteArray& name, const QByteArray& value)
  {
    saved.try_emplace(name, qgetenv(name.constData()));
    if (value.isNull()) {
      qunsetenv(name.constData());
    } else {
      qputenv(name.constData(), value);
    }
  }

  void SetUp() override
  {
    ASSERT_TRUE(temp.isValid());
    setEnv("HOME", temp.filePath("home").toUtf8());
    setEnv("XDG_CONFIG_HOME", temp.filePath("custom config").toUtf8());
    setEnv("XDG_DATA_HOME", temp.filePath("custom data").toUtf8());
    // Desktop-cache commands and portal calls must never affect the real user.
    setEnv("PATH", temp.filePath("no-tools").toUtf8());
    setEnv("FLUORINE_ORIG_PATH", temp.filePath("no-tools").toUtf8());
    setEnv("MO2_BASE_DIR", {});
    setEnv("MO2_LIBS_DIR", {});
  }

  void TearDown() override
  {
    for (const auto& [name, value] : saved) {
      if (value.isNull()) {
        qunsetenv(name.constData());
      } else {
        qputenv(name.constData(), value);
      }
    }
  }
};

TEST_F(XdgPaths, CustomDirectoriesApplyToDataCacheSocketAndCredentials)
{
  EXPECT_EQ(fluorineDataDir(), temp.filePath("custom data/fluorine"));
  EXPECT_EQ(fluorineVfsCacheDir(), temp.filePath("custom data/fluorine/vfs_cache"));
  EXPECT_EQ(NxmHandlerLinux::socketPath(),
            temp.filePath("custom data/fluorine/tmp/mo2-nxm.sock"));
  const QString credentials = temp.filePath("custom config/ModOrganizer/credentials.ini");
  writeFile(credentials, "[General]\nModOrganizer2_test=relocated\n");
  QSettings settings(fluorineCredentialsPath(), QSettings::IniFormat);
  EXPECT_EQ(settings.value("ModOrganizer2_test").toString(), "relocated");
  EXPECT_FALSE(QFileInfo::exists(temp.filePath("home/.config")));
}

TEST_F(XdgPaths, MissingEmptyAndRelativeVariablesUseDefaults)
{
  for (const auto& value : {QByteArray{}, QByteArray(""), QByteArray("relative/path")}) {
    setEnv("XDG_DATA_HOME", value);
    setEnv("XDG_CONFIG_HOME", value);
    EXPECT_EQ(fluorineDataDir(), temp.filePath("home/.local/share/fluorine"));
    EXPECT_EQ(fluorineCredentialsPath(),
              temp.filePath("home/.config/ModOrganizer/credentials.ini"));
  }
}

TEST_F(XdgPaths, CopiesLegacyCredentialsWithoutOverwritingCustomCredentials)
{
  const QString legacy = temp.filePath("home/.config/ModOrganizer/credentials.ini");
  const QString current = temp.filePath("custom config/ModOrganizer/credentials.ini");
  writeFile(legacy, "[General]\nModOrganizer2_test=legacy\n");
  ASSERT_TRUE(QFile::setPermissions(legacy, QFileDevice::ReadOwner | QFileDevice::WriteOwner));
  EXPECT_EQ(fluorineCredentialsPath(), current);
  EXPECT_EQ(readFile(current), readFile(legacy));
  EXPECT_EQ(QFile::permissions(current), QFile::permissions(legacy));
  writeFile(current, "[General]\nModOrganizer2_test=current\n");
  EXPECT_EQ(fluorineCredentialsPath(), current);
  EXPECT_TRUE(readFile(current).contains("=current"));
  EXPECT_TRUE(readFile(legacy).contains("=legacy"));
}

TEST_F(XdgPaths, LegacyCredentialMigrationSecuresBothCopies)
{
  const QString legacy = temp.filePath("home/.config/ModOrganizer/credentials.ini");
  writeFile(legacy, "[General]\nModOrganizer2_test=synthetic\n");
  ASSERT_TRUE(QFile::setPermissions(legacy, QFileDevice::ReadOwner |
      QFileDevice::WriteOwner | QFileDevice::ReadGroup | QFileDevice::ReadOther));
  const QString current = fluorineCredentialsPath();
  EXPECT_EQ(readFile(current), readFile(legacy));
  for (const auto& path : {legacy, current}) {
    EXPECT_FALSE(QFile::permissions(path) & (QFileDevice::ReadGroup |
        QFileDevice::WriteGroup | QFileDevice::ReadOther | QFileDevice::WriteOther));
  }
}

TEST_F(XdgPaths, LegacyCredentialMigrationRejectsSpecialFilesAndSymlinks)
{
  const QString legacy = temp.filePath("home/.config/ModOrganizer/credentials.ini");
  ASSERT_TRUE(QDir().mkpath(QFileInfo(legacy).absolutePath()));
  ASSERT_EQ(::mkfifo(QFile::encodeName(legacy).constData(), 0600), 0);
  EXPECT_FALSE(QFileInfo::exists(fluorineCredentialsPath()));
  ASSERT_TRUE(QFile::remove(legacy));
  const QString target = temp.filePath("not-credentials");
  writeFile(target, "keep");
  ASSERT_TRUE(QFile::link(target, legacy));
  EXPECT_FALSE(QFileInfo::exists(fluorineCredentialsPath()));
  EXPECT_EQ(readFile(target), "keep");
}

TEST_F(XdgPaths, RegistersAndUnregistersInCustomDirectories)
{
  const QString mimeapps = temp.filePath("custom config/mimeapps.list");
  const QString desktop = temp.filePath(
      "custom data/applications/com.fluorine.manager.nxm-handler.desktop");
  writeFile(mimeapps, "[Default Applications]\ntext/plain=editor.desktop;\n");
  NxmHandlerLinux::registerHandler();
  EXPECT_TRUE(QFileInfo::exists(desktop));
  EXPECT_TRUE(readFile(mimeapps).contains(
      "x-scheme-handler/nxm=com.fluorine.manager.nxm-handler.desktop;"));
  EXPECT_TRUE(readFile(mimeapps).contains(
      "x-scheme-handler/modl=com.fluorine.manager.nxm-handler.desktop;"));
  EXPECT_TRUE(readFile(mimeapps).contains("text/plain=editor.desktop;"));
  EXPECT_FALSE(QFileInfo::exists(temp.filePath("home/.config")));
  EXPECT_FALSE(QFileInfo::exists(temp.filePath("home/.local/share")));

  NxmHandlerLinux::unregisterHandler();
  EXPECT_FALSE(QFileInfo::exists(desktop));
  const QByteArray defaults = readFile(mimeapps).split('[').value(1);
  EXPECT_FALSE(defaults.contains("x-scheme-handler/"));
  EXPECT_TRUE(defaults.contains("text/plain=editor.desktop;"));
}

TEST_F(XdgPaths, MigrationPublishesPrefixBeforeChangingConfig)
{
  const QString old = temp.filePath("home/.var/app/com.fluorine.manager");
  writeFile(old + "/Prefix/pfx/drive_c/.hidden", "prefix data");
  FluorineConfig config;
  config.prefix_path = old + "/Prefix/pfx";
  ASSERT_TRUE(config.save());
  fluorineMigrateDataDir();
  const auto migrated = FluorineConfig::load();
  ASSERT_TRUE(migrated.has_value());
  EXPECT_EQ(migrated->prefix_path, fluorineDataDir() + "/Prefix/pfx");
  EXPECT_EQ(readFile(migrated->prefix_path + "/drive_c/.hidden"), "prefix data");
  EXPECT_FALSE(QFileInfo::exists(old + "/Prefix"));
  EXPECT_TRUE(QFileInfo::exists(old + "/MOVED.txt"));
  fluorineMigrateDataDir();
  EXPECT_TRUE(FluorineConfig::load()->prefixExists());
}

TEST_F(XdgPaths, FailedMigrationRetainsOldPrefixAndRetriesWithoutMarker)
{
  const QString old = temp.filePath("home/.var/app/com.fluorine.manager");
  writeFile(old + "/Prefix/pfx/drive_c/keep", "old prefix");
  writeFile(fluorineDataDir() + "/Prefix/blocker", "unrelated destination");
  writeFile(old + "/MOVED.txt", "stale marker from failed migration");
  FluorineConfig config;
  config.prefix_path = old + "/Prefix/pfx";
  ASSERT_TRUE(config.save());
  fluorineMigrateDataDir();
  ASSERT_TRUE(FluorineConfig::load().has_value());
  EXPECT_EQ(FluorineConfig::load()->prefix_path, config.prefix_path);
  EXPECT_EQ(readFile(config.prefix_path + "/drive_c/keep"), "old prefix");
  EXPECT_FALSE(QFileInfo::exists(old + "/MOVED.txt"));
  ASSERT_TRUE(QDir(fluorineDataDir() + "/Prefix").removeRecursively());
  fluorineMigrateDataDir();
  EXPECT_TRUE(FluorineConfig::load()->prefixExists());
  EXPECT_TRUE(QFileInfo::exists(old + "/MOVED.txt"));
}

TEST_F(XdgPaths, RepairsPrematurePrefixRewriteWhenDestinationIsBlocked)
{
  const QString old = temp.filePath("home/.var/app/com.fluorine.manager");
  writeFile(old + "/Prefix/pfx/drive_c/keep", "old prefix");
  writeFile(fluorineDataDir(), "not a directory");
  FluorineConfig config;
  config.prefix_path = fluorineDataDir() + "/Prefix/pfx";
  ASSERT_TRUE(config.save());
  fluorineMigrateDataDir();
  ASSERT_TRUE(FluorineConfig::load().has_value());
  EXPECT_EQ(FluorineConfig::load()->prefix_path, old + "/Prefix/pfx");
  EXPECT_TRUE(FluorineConfig::load()->prefixExists());
  EXPECT_FALSE(QFileInfo::exists(old + "/MOVED.txt"));
}

TEST_F(XdgPaths, MigrationCopiesAcrossFilesystemsIncludingWineLinks)
{
  QTemporaryDir other("/dev/shm/fluorine-migration-XXXXXX");
  if (!other.isValid()) GTEST_SKIP() << "Separate temporary filesystem unavailable";
  struct stat sourceStat{}, destinationStat{};
  ASSERT_EQ(::stat(QFile::encodeName(temp.path()).constData(), &sourceStat), 0);
  ASSERT_EQ(::stat(QFile::encodeName(other.path()).constData(), &destinationStat), 0);
  if (sourceStat.st_dev == destinationStat.st_dev) GTEST_SKIP() << "Same filesystem";
  setEnv("XDG_DATA_HOME", other.path().toUtf8());
  const QString old = temp.filePath("home/.var/app/com.fluorine.manager");
  writeFile(old + "/Prefix/pfx/drive_c/.hidden", "complete prefix");
  ASSERT_TRUE(QDir().mkpath(old + "/Prefix/pfx/dosdevices"));
  ASSERT_EQ(::symlink("../drive_c", QFile::encodeName(old + "/Prefix/pfx/dosdevices/c:").constData()), 0);
  FluorineConfig config;
  config.prefix_path = old + "/Prefix/pfx";
  ASSERT_TRUE(config.save());
  fluorineMigrateDataDir();
  const auto migrated = FluorineConfig::load();
  ASSERT_TRUE(migrated.has_value());
  EXPECT_EQ(migrated->prefix_path, fluorineDataDir() + "/Prefix/pfx");
  EXPECT_EQ(readFile(migrated->prefix_path + "/dosdevices/c:/.hidden"), "complete prefix");
  EXPECT_FALSE(QFileInfo::exists(old + "/Prefix"));
  EXPECT_TRUE(QFileInfo::exists(old + "/MOVED.txt"));
}

TEST_F(XdgPaths, ConfigWriteFailureRollsBackPrefixMove)
{
  const QString old = temp.filePath("home/.var/app/com.fluorine.manager");
  writeFile(old + "/Prefix/pfx/drive_c/keep", "prefix");
  FluorineConfig config;
  config.prefix_path = old + "/Prefix/pfx";
  ASSERT_TRUE(config.save());
  const pid_t child = fork();
  ASSERT_GE(child, 0);
  if (child == 0) {
    signal(SIGXFSZ, SIG_IGN);
    const rlimit limit{32, 32};
    if (setrlimit(RLIMIT_FSIZE, &limit) != 0) _exit(2);
    fluorineMigrateDataDir();
    _exit(0);
  }
  int status = 0;
  ASSERT_EQ(waitpid(child, &status, 0), child);
  ASSERT_TRUE(WIFEXITED(status));
  EXPECT_EQ(WEXITSTATUS(status), 0);
  EXPECT_TRUE(FluorineConfig::load()->prefixExists());
  EXPECT_EQ(FluorineConfig::load()->prefix_path, config.prefix_path);
  EXPECT_EQ(readFile(config.prefix_path + "/drive_c/keep"), "prefix");
  EXPECT_FALSE(QFileInfo::exists(old + "/MOVED.txt"));
}

TEST_F(XdgPaths, CustomDataHomeDoesNotExpandLegacyPrefixDeletionPermission)
{
  FluorineConfig config;
  config.prefix_path = temp.filePath("custom data/fluorine/Prefix/pfx");
  ASSERT_TRUE(QDir().mkpath(config.prefix_path + "/drive_c"));
  EXPECT_FALSE(config.canDestroyPrefix());
  ASSERT_TRUE(config.markPrefixOwned());
  EXPECT_TRUE(config.canDestroyPrefix());

  config.prefix_path = temp.filePath("home/.local/share/fluorine/Prefix/pfx");
  ASSERT_TRUE(QDir().mkpath(config.prefix_path + "/drive_c"));
  EXPECT_TRUE(config.canDestroyPrefix());
}

TEST_F(XdgPaths, CollectionLinkOnSocketIsForwardedAndModLinkStillIs)
{
  NxmHandlerLinux handler;
  ASSERT_TRUE(handler.startListener());

  QStringList collections;
  int mods = 0;
  QObject::connect(&handler, &NxmHandlerLinux::collectionReceived,
                   [&](QString url) { collections << url; });
  QObject::connect(&handler, &NxmHandlerLinux::nxmReceived,
                   [&](NxmLink) { ++mods; });

  const QString collection = "nxm://cyberpunk2077/collections/rcuccp/revisions/189";
  ASSERT_TRUE(NxmHandlerLinux::sendToSocket(collection));
  ASSERT_TRUE(NxmHandlerLinux::sendToSocket(
      "nxm://cyberpunk2077/mods/107/files/1234?key=k&expires=1&user_id=2"));

  // The server runs in this thread, so it only sees the connections once events spin.
  QElapsedTimer waited;
  waited.start();
  while ((collections.isEmpty() || mods == 0) && waited.elapsed() < 5000) {
    QCoreApplication::processEvents(QEventLoop::AllEvents, 50);
  }

  EXPECT_EQ(collections, QStringList{collection});
  EXPECT_EQ(mods, 1);
}
}  // namespace

int main(int argc, char** argv)
{
  QTemporaryDir bus;
  qputenv("DBUS_SESSION_BUS_ADDRESS", ("unix:path=" + bus.filePath("absent")).toUtf8());
  QCoreApplication app(argc, argv);
  MOBase::log::LoggerConfiguration configuration;
  configuration.name = "test_xdgpaths";
  MOBase::log::createDefault(configuration);
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}

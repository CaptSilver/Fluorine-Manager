#include "collections/collectionmetatag.h"

#include <QFile>
#include <QSettings>
#include <QTemporaryDir>

#include <gtest/gtest.h>

#include <QStringList>
#include <QVariant>

#include <map>

using CollectionMetaTag::OwnersKey;
using CollectionMetaTag::PluginName;

namespace
{
std::vector<CollectionOwner> twoOwners()
{
  return {{QStringLiteral("rcuccp"), 189, 28966, 158871},
          {QStringLiteral("srpv39-extras_x"), 129, 28966, 158871}};
}

QString slurp(const QString& path)
{
  QFile file(path);
  EXPECT_TRUE(file.open(QIODevice::ReadOnly)) << path.toStdString();
  return QString::fromUtf8(file.readAll());
}

// Copies the stopgap fixture into `dir` so the test never writes to the source tree.
QString copyStopgapMeta(const QTemporaryDir& dir)
{
  const QString source = QString::fromUtf8(FLUORINE_TEST_SOURCE_DIR
                                           "/src/tests/fixtures/collections/stopgap-meta.ini");
  const QString target = dir.filePath(QStringLiteral("meta.ini"));
  EXPECT_TRUE(QFile::copy(source, target));
  return target;
}
}  // namespace

TEST(MetaTag, EncodeDecodeRoundTripTwoOwners)
{
  const auto owners = twoOwners();
  const QString encoded = CollectionMetaTag::encodeOwners(owners);

  EXPECT_FALSE(encoded.contains(QLatin1Char('\n')));
  EXPECT_TRUE(encoded.startsWith(QLatin1Char('[')));
  EXPECT_EQ(CollectionMetaTag::decodeOwners(encoded), owners);
  EXPECT_EQ(CollectionMetaTag::encodeOwners({}), QStringLiteral("[]"));
}

TEST(MetaTag, DecodeMalformedIsEmpty)
{
  EXPECT_TRUE(CollectionMetaTag::decodeOwners(QString()).empty());
  EXPECT_TRUE(CollectionMetaTag::decodeOwners(QStringLiteral("not json")).empty());
  EXPECT_TRUE(CollectionMetaTag::decodeOwners(QStringLiteral("{}")).empty());
  EXPECT_TRUE(CollectionMetaTag::decodeOwners(QStringLiteral("[1,2]")).empty());
  EXPECT_TRUE(CollectionMetaTag::decodeOwners(QStringLiteral("[{\"revision\":1}]")).empty());
}

TEST(MetaTag, WithOwnerReplacesSameSlug)
{
  const auto updated = CollectionMetaTag::withOwner(
      twoOwners(), {QStringLiteral("rcuccp"), 190, 28966, 999});

  ASSERT_EQ(updated.size(), 2u);
  EXPECT_EQ(updated[0], (CollectionOwner{QStringLiteral("rcuccp"), 190, 28966, 999}));
  EXPECT_EQ(updated[1], twoOwners()[1]);
}

TEST(MetaTag, WithOwnerAppendsOtherSlug)
{
  const auto updated = CollectionMetaTag::withOwner(
      twoOwners(), {QStringLiteral("vfy7w1"), 112, 28966, 158871});

  ASSERT_EQ(updated.size(), 3u);
  EXPECT_EQ(updated[2].slug, QStringLiteral("vfy7w1"));
  EXPECT_EQ(updated[0], twoOwners()[0]);
  EXPECT_EQ(updated[1], twoOwners()[1]);
}

TEST(MetaTag, WriteThenReadThroughQSettings)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = dir.filePath(QStringLiteral("meta.ini"));

  ASSERT_TRUE(CollectionMetaTag::write(path, twoOwners(), 28966, 158871));

  EXPECT_EQ(CollectionMetaTag::readOwners(path), twoOwners());
  EXPECT_EQ(CollectionMetaTag::readInstalledFiles(path),
            (std::set<std::pair<int, int>>{{28966, 158871}}));

  QSettings settings(path, QSettings::IniFormat);
  EXPECT_EQ(settings.value(QStringLiteral("Plugins/%1/%2").arg(PluginName, OwnersKey))
                .toString(),
            CollectionMetaTag::encodeOwners(twoOwners()));
}

TEST(MetaTag, WriteKeepsExistingGeneralKeys)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = copyStopgapMeta(dir);

  ASSERT_TRUE(CollectionMetaTag::write(path, twoOwners(), 28966, 158871));

  // QSettings maps the [General] section of an INI file to root keys, which is how
  // ModInfoRegular reads them. "General/modid" would look for a group stored as
  // [%General] and find nothing.
  QSettings settings(path, QSettings::IniFormat);
  EXPECT_EQ(settings.value(QStringLiteral("modid")).toInt(), 28966);
  EXPECT_EQ(settings.value(QStringLiteral("installationFile")).toString(),
            QStringLiteral("NightlyNow-28966-1-1-8-1700000000.zip"));
  EXPECT_EQ(settings.value(QStringLiteral("repository")).toString(),
            QStringLiteral("Nexus"));
  EXPECT_EQ(CollectionMetaTag::readOwners(path), twoOwners());
  // the pair the stopgap wrote is not duplicated
  EXPECT_EQ(CollectionMetaTag::readInstalledFiles(path),
            (std::set<std::pair<int, int>>{{28966, 158871}}));
}

TEST(MetaTag, InstalledFilesArrayShape)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = dir.filePath(QStringLiteral("meta.ini"));

  ASSERT_TRUE(CollectionMetaTag::write(path, twoOwners(), 10, 20));
  QString text = slurp(path);
  EXPECT_TRUE(text.contains(QStringLiteral("[installedFiles]")));
  EXPECT_TRUE(text.contains(QStringLiteral("1\\modid=10")));
  EXPECT_TRUE(text.contains(QStringLiteral("1\\fileid=20")));
  EXPECT_TRUE(text.contains(QStringLiteral("size=1")));

  // a second pair is added next to the first, in modid, fileid order
  ASSERT_TRUE(CollectionMetaTag::write(path, twoOwners(), 11, 21));
  text = slurp(path);
  EXPECT_TRUE(text.contains(QStringLiteral("1\\modid=10")));
  EXPECT_TRUE(text.contains(QStringLiteral("2\\modid=11")));
  EXPECT_TRUE(text.contains(QStringLiteral("2\\fileid=21")));
  EXPECT_TRUE(text.contains(QStringLiteral("size=2")));
  EXPECT_EQ(CollectionMetaTag::readInstalledFiles(path),
            (std::set<std::pair<int, int>>{{10, 20}, {11, 21}}));
}

// ModInfoRegular keeps only the childKeys() of each Plugins/<plugin> group,
// removes the whole Plugins group when it saves, and writes those keys back.
// The tag has to come through that unchanged.
TEST(MetaTag, OwnersSurviveModInfoStyleRewrite)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = dir.filePath(QStringLiteral("meta.ini"));
  ASSERT_TRUE(CollectionMetaTag::write(path, twoOwners(), 28966, 158871));
  {
    QSettings other(path, QSettings::IniFormat);
    other.setValue(QStringLiteral("Plugins/SomeOtherPlugin/setting"), 5);
  }

  // load, as ModInfoRegular::readMeta does
  std::map<QString, std::map<QString, QVariant>> pluginSettings;
  {
    QSettings settings(path, QSettings::IniFormat);
    settings.beginGroup(QStringLiteral("Plugins"));
    for (const auto& pluginName : settings.childGroups()) {
      settings.beginGroup(pluginName);
      for (const auto& key : settings.childKeys()) {
        pluginSettings[pluginName][key] = settings.value(key);
      }
      settings.endGroup();
    }
    settings.endGroup();
  }

  // the tag must be visible as a plain key, or the mod object never sees it
  ASSERT_TRUE(pluginSettings.count(PluginName));
  ASSERT_TRUE(pluginSettings[PluginName].count(OwnersKey));
  EXPECT_EQ(pluginSettings[PluginName][OwnersKey].toString(),
            CollectionMetaTag::encodeOwners(twoOwners()));

  // save, as ModInfoRegular::saveMeta does
  {
    QSettings settings(path, QSettings::IniFormat);
    settings.remove(QStringLiteral("Plugins"));
    settings.beginGroup(QStringLiteral("Plugins"));
    for (const auto& [pluginName, values] : pluginSettings) {
      settings.beginGroup(pluginName);
      for (const auto& [key, value] : values) {
        settings.setValue(key, value);
      }
      settings.endGroup();
    }
    settings.endGroup();
    settings.sync();
  }

  EXPECT_EQ(CollectionMetaTag::readOwners(path), twoOwners());
  EXPECT_EQ(CollectionMetaTag::readInstalledFiles(path),
            (std::set<std::pair<int, int>>{{28966, 158871}}));
  QSettings settings(path, QSettings::IniFormat);
  EXPECT_EQ(settings.value(QStringLiteral("Plugins/SomeOtherPlugin/setting")).toInt(), 5);
}

TEST(MetaTag, StopgapMetaHasNoOwnersAndTheInstalledPair)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = copyStopgapMeta(dir);

  EXPECT_TRUE(CollectionMetaTag::readOwners(path).empty());
  EXPECT_EQ(CollectionMetaTag::readInstalledFiles(path),
            (std::set<std::pair<int, int>>{{28966, 158871}}));

  // a path that does not exist reads as empty, and reading creates nothing
  const QString missing = dir.filePath(QStringLiteral("absent.ini"));
  EXPECT_TRUE(CollectionMetaTag::readOwners(missing).empty());
  EXPECT_TRUE(CollectionMetaTag::readInstalledFiles(missing).empty());
  EXPECT_FALSE(QFile::exists(missing));
}

TEST(MetaTag, StoredStringJoinsTheListQSettingsReturnsForUnquotedCommas)
{
  EXPECT_EQ(CollectionMetaTag::storedString(QVariant(QStringList{"[1", "2]"})),
            QStringLiteral("[1,2]"));
  EXPECT_EQ(CollectionMetaTag::storedString(QVariant(QStringLiteral("[]"))),
            QStringLiteral("[]"));
  EXPECT_TRUE(CollectionMetaTag::storedString(QVariant()).isEmpty());
}

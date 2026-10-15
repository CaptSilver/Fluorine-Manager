#include "collections/collectionmanifest.h"

#include <QFile>
#include <QJsonArray>

#include <gtest/gtest.h>

#include <algorithm>

namespace
{
QByteArray fixture(const char* name)
{
  QFile file(QString::fromUtf8(FLUORINE_TEST_SOURCE_DIR "/src/tests/fixtures/collections/") +
             QString::fromUtf8(name));
  EXPECT_TRUE(file.open(QIODevice::ReadOnly)) << name;
  return file.readAll();
}

CollectionManifest parseFixture(const char* name)
{
  QString error;
  const auto manifest = CollectionManifest::parse(fixture(name), error);
  EXPECT_TRUE(manifest.has_value()) << error.toStdString();
  return manifest.value_or(CollectionManifest{});
}

qint64 totalSize(const CollectionManifest& manifest)
{
  qint64 total = 0;
  for (const auto& mod : manifest.mods) {
    total += mod.source.fileSize;
  }
  return total;
}

int unsupportedCount(const CollectionManifest& manifest)
{
  return static_cast<int>(
      std::count_if(manifest.mods.begin(), manifest.mods.end(),
                    [](const CollectionMod& mod) { return !mod.unsupported.isEmpty(); }));
}

const CollectionMod* findMod(const CollectionManifest& manifest, const QString& name)
{
  const auto it = std::find_if(manifest.mods.begin(), manifest.mods.end(),
                               [&](const CollectionMod& mod) { return mod.name == name; });
  return it == manifest.mods.end() ? nullptr : &*it;
}

// A manifest with the given mods array and nothing else of note.
QByteArray manifestWithMods(const QByteArray& modsJson, const QByteArray& extra = {})
{
  return QByteArray(R"({"info":{"name":"T","domainName":"cyberpunk2077","author":"a",)"
                    R"("gameVersions":[]},"mods":)") +
         modsJson + extra + "}";
}
}  // namespace

TEST(CollectionManifest, ParsesCoreR189)
{
  const auto manifest = parseFixture("rcuccp-r189.json");

  EXPECT_EQ(manifest.mods.size(), 407u);
  EXPECT_EQ(manifest.info.name, QStringLiteral("NCR Core 2.31a"));
  EXPECT_EQ(manifest.info.domainName, QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(manifest.info.author, QStringLiteral("rVeinz"));
  EXPECT_TRUE(manifest.recommendNewProfile);
  EXPECT_FALSE(manifest.hasUnhandled());
  EXPECT_EQ(unsupportedCount(manifest), 0);

  ASSERT_FALSE(manifest.mods.empty());
  const CollectionMod& first = manifest.mods.front();
  EXPECT_EQ(first.name, QStringLiteral("NightlyNow"));
  EXPECT_EQ(first.version, QStringLiteral("1.1.8"));
  EXPECT_FALSE(first.optional);
  EXPECT_EQ(first.phase, 0);
  EXPECT_EQ(first.source.type, QStringLiteral("nexus"));
  EXPECT_EQ(first.source.modId, 28966);
  EXPECT_EQ(first.source.fileId, 158871);
  EXPECT_EQ(first.source.md5, QStringLiteral("eda75918f2cbef0e3359b1ed03534675"));
  EXPECT_EQ(first.source.fileSize, 23795);
  EXPECT_EQ(first.source.logicalFilename, QStringLiteral("NightlyNow"));
  EXPECT_TRUE(first.choicesType.isEmpty());
  EXPECT_TRUE(first.choices.isEmpty());

  for (const auto& mod : manifest.mods) {
    EXPECT_EQ(mod.source.md5.size(), 32) << mod.name.toStdString();
    EXPECT_GT(mod.source.modId, 0) << mod.name.toStdString();
    EXPECT_GT(mod.source.fileId, 0) << mod.name.toStdString();
  }
}

TEST(CollectionManifest, ParsesExtrasR129WithThreeFomodChoices)
{
  const auto manifest = parseFixture("srpv39-r129.json");

  EXPECT_EQ(manifest.mods.size(), 278u);
  EXPECT_EQ(manifest.info.name, QStringLiteral("NCR - Extras"));
  EXPECT_FALSE(manifest.recommendNewProfile);

  QStringList withChoices;
  for (const auto& mod : manifest.mods) {
    if (!mod.choicesType.isEmpty()) {
      withChoices << mod.name;
      EXPECT_EQ(mod.choicesType, QStringLiteral("fomod")) << mod.name.toStdString();
      EXPECT_TRUE(mod.choices.value(QStringLiteral("options")).isArray())
          << mod.name.toStdString();
      EXPECT_TRUE(mod.unsupported.isEmpty()) << mod.name.toStdString();
    } else {
      EXPECT_TRUE(mod.choices.isEmpty()) << mod.name.toStdString();
    }
  }
  withChoices.sort();
  EXPECT_EQ(withChoices, (QStringList{QStringLiteral("void_Honda_CRX_Mugen"),
                                      QStringLiteral("void_Lotus_Esprit_Turbo"),
                                      QStringLiteral("void_Toyota_GT86")}));

  const CollectionMod* gt86 = findMod(manifest, QStringLiteral("void_Toyota_GT86"));
  ASSERT_NE(gt86, nullptr);
  EXPECT_EQ(gt86->source.modId, 23632);
  EXPECT_EQ(gt86->source.fileId, 163173);
  const QJsonArray options = gt86->choices.value(QStringLiteral("options")).toArray();
  ASSERT_EQ(options.size(), 7);
  const QJsonObject intro = options.at(0).toObject();
  EXPECT_EQ(intro.value(QStringLiteral("name")).toString(), QStringLiteral("intro"));
  const QJsonObject group = intro.value(QStringLiteral("groups")).toArray().at(0).toObject();
  EXPECT_EQ(group.value(QStringLiteral("name")).toString(), QStringLiteral("intro"));
  const QJsonObject choice = group.value(QStringLiteral("choices")).toArray().at(0).toObject();
  EXPECT_EQ(choice.value(QStringLiteral("name")).toString(),
            QStringLiteral("skip tuning parts"));
  EXPECT_EQ(choice.value(QStringLiteral("idx")).toInt(), 1);
}

TEST(CollectionManifest, ParsesBodyR112)
{
  const auto manifest = parseFixture("vfy7w1-r112.json");

  EXPECT_EQ(manifest.mods.size(), 45u);
  EXPECT_EQ(manifest.info.name, QStringLiteral("NCR - Body Add-on (Adult)"));
  EXPECT_FALSE(manifest.recommendNewProfile);
  EXPECT_FALSE(manifest.hasUnhandled());
  EXPECT_EQ(unsupportedCount(manifest), 0);

  ASSERT_FALSE(manifest.mods.empty());
  const CollectionMod& first = manifest.mods.front();
  EXPECT_EQ(first.version, QStringLiteral("1.0"));
  EXPECT_EQ(first.source.modId, 9461);
  EXPECT_EQ(first.source.fileId, 50544);
  EXPECT_EQ(first.source.md5, QStringLiteral("6a741b1ff5d559ad557b2e56524a3557"));
}

TEST(CollectionManifest, GameVersionsAreRead)
{
  const QStringList expected{QStringLiteral("2.3.1.0")};
  EXPECT_EQ(parseFixture("rcuccp-r189.json").info.gameVersions, expected);
  EXPECT_EQ(parseFixture("srpv39-r129.json").info.gameVersions, expected);
  EXPECT_EQ(parseFixture("vfy7w1-r112.json").info.gameVersions, expected);
}

TEST(CollectionManifest, TotalSizesMatchFixtures)
{
  EXPECT_EQ(totalSize(parseFixture("rcuccp-r189.json")), 560254669);
  EXPECT_EQ(totalSize(parseFixture("srpv39-r129.json")), 6347144107);
  EXPECT_EQ(totalSize(parseFixture("vfy7w1-r112.json")), 1150201986);
}

TEST(CollectionManifest, UnknownSourceTypeFlaggedNotFatal)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods(R"([
        {"name":"Browse Mod","version":"1","optional":false,"phase":0,
         "source":{"type":"browse","url":"https://example.com/x.zip"}},
        {"name":"Nexus Mod","version":"2","optional":false,"phase":0,
         "source":{"type":"nexus","modId":10,"fileId":20,"md5":"","fileSize":5}}
      ])"),
      error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  ASSERT_EQ(manifest->mods.size(), 2u);
  EXPECT_TRUE(manifest->mods[0].unsupported.contains(QStringLiteral("browse")));
  EXPECT_TRUE(manifest->mods[1].unsupported.isEmpty());
}

TEST(CollectionManifest, NonFomodChoicesTypeFlagged)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods(R"([
        {"name":"Bain Mod","version":"1","optional":false,"phase":0,
         "source":{"type":"nexus","modId":10,"fileId":20},
         "choices":{"type":"bain","options":[]}}
      ])"),
      error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  ASSERT_EQ(manifest->mods.size(), 1u);
  EXPECT_EQ(manifest->mods[0].choicesType, QStringLiteral("bain"));
  EXPECT_TRUE(manifest->mods[0].unsupported.contains(QStringLiteral("bain")));
}

TEST(CollectionManifest, ChoicesWithoutTypeAreFomod)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods(R"([
        {"name":"With Choices","version":"1","optional":false,"phase":0,
         "source":{"type":"nexus","modId":10,"fileId":20},
         "choices":{"options":[]}},
        {"name":"Empty Choices","version":"1","optional":false,"phase":0,
         "source":{"type":"nexus","modId":11,"fileId":21},
         "choices":{}},
        {"name":"Null Choices","version":"1","optional":false,"phase":0,
         "source":{"type":"nexus","modId":12,"fileId":22},
         "choices":null}
      ])"),
      error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  ASSERT_EQ(manifest->mods.size(), 3u);
  EXPECT_EQ(manifest->mods[0].choicesType, QStringLiteral("fomod"));
  EXPECT_TRUE(manifest->mods[0].unsupported.isEmpty());
  EXPECT_TRUE(manifest->mods[1].choicesType.isEmpty());
  EXPECT_TRUE(manifest->mods[2].choicesType.isEmpty());
}

TEST(CollectionManifest, MissingMd5AndFileSizeAccepted)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods(R"([
        {"name":"Bare","version":"1","optional":true,"phase":2,
         "source":{"type":"nexus","modId":10,"fileId":20}}
      ])"),
      error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  ASSERT_EQ(manifest->mods.size(), 1u);
  const CollectionMod& mod = manifest->mods[0];
  EXPECT_TRUE(mod.source.md5.isEmpty());
  EXPECT_EQ(mod.source.fileSize, 0);
  EXPECT_TRUE(mod.optional);
  EXPECT_EQ(mod.phase, 2);
  EXPECT_TRUE(mod.unsupported.isEmpty());
}

TEST(CollectionManifest, MalformedTopLevelIsError)
{
  for (const QByteArray& json :
       {QByteArray("not json"), QByteArray(), QByteArray("[]"),
        QByteArray(R"({"mods":[]})") /* no info */}) {
    QString error;
    EXPECT_FALSE(CollectionManifest::parse(json, error).has_value()) << json.toStdString();
    EXPECT_FALSE(error.isEmpty()) << json.toStdString();
  }
}

TEST(CollectionManifest, ModsNotAnArrayIsError)
{
  QString error;
  EXPECT_FALSE(CollectionManifest::parse(
                   R"({"info":{"name":"T"},"mods":{"a":1}})", error)
                   .has_value());
  EXPECT_TRUE(error.contains(QStringLiteral("mods")));

  error.clear();
  EXPECT_FALSE(CollectionManifest::parse(R"({"info":{"name":"T"}})", error).has_value());
  EXPECT_TRUE(error.contains(QStringLiteral("mods")));

  error.clear();
  EXPECT_FALSE(
      CollectionManifest::parse(R"({"info":{"name":"T"},"mods":[1]})", error).has_value());
  EXPECT_FALSE(error.isEmpty());
}

TEST(CollectionManifest, ReportsNonEmptyUnhandledSections)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods("[]", R"(,"modRules":[{"type":"before"}],"tools":{"a":1},)"
                             R"("loadOrder":[],"externalResources":[{"x":1}])"),
      error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  EXPECT_TRUE(manifest->hasUnhandled());
  EXPECT_EQ(manifest->unhandledSections,
            (QStringList{QStringLiteral("modRules"), QStringLiteral("externalResources"),
                         QStringLiteral("tools")}));
}

TEST(CollectionManifest, EmptyOrNullSectionsAreNotReported)
{
  QString error;
  const auto manifest = CollectionManifest::parse(
      manifestWithMods("[]", R"(,"modRules":[],"loadOrder":null,"tools":{})"), error);

  ASSERT_TRUE(manifest.has_value()) << error.toStdString();
  EXPECT_FALSE(manifest->hasUnhandled());
  EXPECT_TRUE(manifest->unhandledSections.isEmpty());
}

#include <gtest/gtest.h>

#include "TESData/FileConflictParser.h"
#include "TESData/FileInfo.h"
#include "TESFile/Reader.h"
#include <esptk/espfile.h>
#include <uibase/bethesdaplugin.h>

#include <QDataStream>
#include <QFile>
#include <QFileInfo>
#include <QTemporaryDir>

namespace
{
using MOBase::BethesdaPluginDisplay;
using MOBase::BethesdaPluginFlags;
using MOBase::BethesdaPluginIndexes;

constexpr BethesdaPluginDisplay fnv{true, true, false, true, false};
constexpr BethesdaPluginDisplay skyrim{true, false, false, false, true};
constexpr BethesdaPluginDisplay starfield{true, true, true, false, true};

// Real TES4 header bytes, including a HEDR and a dependency, are consumed by
// both parser implementations. No game installation or user profile is needed.
QString writePlugin(const QTemporaryDir& dir, const QString& name, quint32 flags)
{
  QByteArray payload;
  QDataStream body(&payload, QIODevice::WriteOnly);
  body.setByteOrder(QDataStream::LittleEndian);
  body.setFloatingPointPrecision(QDataStream::SinglePrecision);
  body.writeRawData("HEDR", 4);
  body << quint16(12) << float(1.34) << quint32(0) << quint32(0x800);
  const QByteArray master("FalloutNV.esm\0", 14);
  body.writeRawData("MAST", 4);
  body << quint16(master.size());
  body.writeRawData(master.constData(), master.size());
  body.writeRawData("DATA", 4);
  body << quint16(8) << quint64(0);

  QFile file(dir.filePath(name));
  if (!file.open(QIODevice::WriteOnly))
    return {};
  QDataStream stream(&file);
  stream.setByteOrder(QDataStream::LittleEndian);
  stream.writeRawData("TES4", 4);
  stream << quint32(payload.size()) << flags << quint32(0) << quint32(0) << quint16(15)
         << quint16(0);
  stream.writeRawData(payload.constData(), payload.size());
  return file.fileName();
}

BethesdaPluginFlags readBoth(const QString& path, BethesdaPluginDisplay support)
{
  ESP::File core(path.toStdString());
  const auto result = BethesdaPluginFlags::fromHeader(core.headerFlags(), support);
  TESData::FileInfo info(nullptr, QFileInfo(path).fileName(), false, false, false,
                         support.light && support.respectsExtensions, support);
  TESData::FileConflictParser parser(nullptr, &info, support);
  TESFile::Reader<TESData::FileConflictParser> reader;
  reader.parse(std::filesystem::path(path.toStdString()), parser);
  EXPECT_EQ(info.isMasterFlagged(), result.master);
  EXPECT_EQ(info.isLightFlagged(), result.light);
  EXPECT_EQ(info.isMediumFlagged(), result.medium);
  EXPECT_EQ(info.isOverlayFile(), result.overlay);
  EXPECT_EQ(info.isBlueprintFlagged(), result.blueprint);
  EXPECT_TRUE(core.masters().contains("FalloutNV.esm"));
  EXPECT_TRUE(info.masters().contains("FalloutNV.esm"));
  return result;
}
}  // namespace

TEST(FalloutNVPluginTypes, BothParsersDistinguishLightAndOverlayHeaders)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  auto light = readBoth(writePlugin(dir, "Light.esp", 0x100), fnv);
  EXPECT_TRUE(light.light);
  EXPECT_FALSE(light.overlay);
  EXPECT_FALSE(light.master);

  auto overlay = readBoth(writePlugin(dir, "Overlay.esp", 0x200), fnv);
  EXPECT_TRUE(overlay.overlay);
  EXPECT_FALSE(overlay.light);
  auto medium = readBoth(writePlugin(dir, "Medium.esp", 0x400), fnv);
  EXPECT_TRUE(medium.medium);
  EXPECT_FALSE(medium.light);
  EXPECT_FALSE(medium.overlay);
  auto mixed = readBoth(writePlugin(dir, "Mixed.esm", 0xF01), fnv);
  EXPECT_TRUE(mixed.master);
  EXPECT_TRUE(mixed.overlay);
  EXPECT_FALSE(mixed.light);
  EXPECT_FALSE(mixed.medium);
  EXPECT_FALSE(mixed.blueprint);
}

TEST(FalloutNVPluginTypes, OtherGamesKeepTheirFlagInterpretation)
{
  QTemporaryDir dir;
  for (const quint32 bits : {0x100u, 0x200u, 0x400u}) {
    const auto path    = writePlugin(dir, QString::number(bits) + ".esp", bits);
    const auto vanilla = readBoth(path, {});
    EXPECT_FALSE(vanilla.light);
    EXPECT_FALSE(vanilla.medium);
    EXPECT_FALSE(vanilla.overlay);

    const auto sse = readBoth(path, skyrim);
    EXPECT_EQ(sse.light, bits == 0x200);
    EXPECT_FALSE(sse.overlay);

    const auto sf = readBoth(path, starfield);
    EXPECT_EQ(sf.light, bits == 0x100);
    EXPECT_EQ(sf.medium, bits == 0x400);
    EXPECT_FALSE(sf.overlay);
  }
}

TEST(FalloutNVPluginTypes, FnvDisplayUsesHeaderFlagsWithoutChangingMasterOrdering)
{
  TESData::FileInfo disguised(nullptr, "Regular.esm", false, false, false, false, fnv);
  TESData::FileInfo master(nullptr, "Master.esp", false, false, false, false, fnv);
  master.setMasterFlagged(true);
  EXPECT_TRUE(disguised.isMasterFile());
  EXPECT_TRUE(master.isMasterFile());
  TESData::FileInfo regular(nullptr, "Regular.esp", false, false, false, false, fnv);
  EXPECT_TRUE(regular.mustLoadAfter(disguised));
  EXPECT_TRUE(regular.mustLoadAfter(master));

  TESData::FileInfo extensionOnly(nullptr, "Regular.ESL", false, false, false, false,
                                  fnv);
  EXPECT_FALSE(extensionOnly.isSmallFile());
  EXPECT_FALSE(extensionOnly.isMasterFile());
  extensionOnly.setLightFlagged(true);
  EXPECT_TRUE(extensionOnly.isSmallFile());
  extensionOnly.setOverlayFlagged(true);
  EXPECT_FALSE(extensionOnly.isSmallFile());

  TESData::FileInfo sse(nullptr, "Extension.ESL", false, false, false, true, skyrim);
  EXPECT_TRUE(sse.isSmallFile());
  EXPECT_TRUE(sse.isMasterFile());
}

TEST(FalloutNVPluginTypes, OverlayDependenciesStillConstrainLoadOrder)
{
  TESData::FileInfo parent(nullptr, "Parent.esp", false, false, false, false, fnv);
  TESData::FileInfo overlay(nullptr, "Patch.esp", false, false, false, false, fnv);
  overlay.setOverlayFlagged(true);
  overlay.addMaster("Parent.esp");
  EXPECT_TRUE(overlay.mustLoadAfter(parent));
  EXPECT_FALSE(parent.mustLoadAfter(overlay));
}

TEST(FalloutNVPluginTypes, MixedLoadOrderDoesNotAllocateSlotsForOverlaysOrDisabledFiles)
{
  BethesdaPluginIndexes indexes;
  EXPECT_EQ(indexes.next(true, false, false, false), "00");
  EXPECT_EQ(indexes.next(true, false, false, true), "FE:000");
  EXPECT_EQ(indexes.next(true, true, false, false), "XX");
  EXPECT_EQ(indexes.next(false, false, false, true), "");
  EXPECT_EQ(indexes.next(false, true, false, false), "");
  EXPECT_EQ(indexes.next(true, false, false, false), "01");
  EXPECT_EQ(indexes.next(true, false, false, true), "FE:001");
  EXPECT_EQ(indexes.next(true, true, true, true), "XX");
  EXPECT_EQ(indexes.next(true, false, true, false), "FD:00");
  EXPECT_EQ(indexes.next(true, false, true, false), "FD:01");
  EXPECT_EQ(indexes.next(true, false, false, false), "02");
}

TEST(FalloutNVPluginTypes, OverlayCountDoesNotReduceRegularOrLightIndexSpace)
{
  BethesdaPluginIndexes indexes;
  for (int i = 0; i < 300; ++i)
    ASSERT_EQ(indexes.next(true, true, false, false), "XX");
  EXPECT_EQ(indexes.next(true, false, false, false), "00");
  for (int i = 0; i < 4095; ++i)
    indexes.next(true, false, false, true);
  EXPECT_EQ(indexes.next(true, false, false, true), "FE:FFF");
  EXPECT_EQ(indexes.next(true, false, false, false), "01");
}

TEST(FalloutNVPluginTypes, FnvAndTtwDisplayTypesWithoutRuntimeFeatures)
{
  for (const QString game : {QString("FalloutNV"), QString("TTW")}) {
    EXPECT_EQ(BethesdaPluginDisplay::forGame(nullptr, game), fnv);
  }
  EXPECT_EQ(BethesdaPluginDisplay::forGame(nullptr, "Fallout3"),
            BethesdaPluginDisplay{});
}

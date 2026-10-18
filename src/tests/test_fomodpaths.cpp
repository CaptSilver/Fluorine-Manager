#include "fomodpaths.h"

#include <gtest/gtest.h>

TEST(FomodPaths, FileDestinationWithTrailingBackslashStaysADirectory)
{
  // Flat World Map Framework installs a background with this destination; it
  // must land inside textures/terrain/tamriel, not replace that directory.
  EXPECT_EQ(FomodPaths::normalizeFileDestination(
                QStringLiteral("textures\\terrain\\tamriel\\")),
            QStringLiteral("textures/terrain/tamriel/"));
  EXPECT_EQ(FomodPaths::normalizeFileDestination(
                QStringLiteral("textures/terrain/tamriel/")),
            QStringLiteral("textures/terrain/tamriel/"));
}

TEST(FomodPaths, FileDestinationWithoutTrailingSeparatorIsARename)
{
  EXPECT_EQ(FomodPaths::normalizeFileDestination(
                QStringLiteral("textures\\terrain\\tamriel\\back.dds")),
            QStringLiteral("textures/terrain/tamriel/back.dds"));
}

TEST(FomodPaths, EmptyAndCurrentDirectoryDestinationsStayEmpty)
{
  EXPECT_TRUE(FomodPaths::normalizeFileDestination(QString()).isEmpty());
  EXPECT_TRUE(FomodPaths::normalizeFileDestination(QStringLiteral(".")).isEmpty());
  EXPECT_TRUE(FomodPaths::normalizeFileDestination(QStringLiteral(".\\")).isEmpty());
}

TEST(FomodPaths, FolderStyleNormalizationDropsTrailingSeparator)
{
  EXPECT_EQ(FomodPaths::normalizeArchivePath(QStringLiteral("Data\\Textures\\")),
            QStringLiteral("Data/Textures"));
}

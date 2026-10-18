#include <uibase/registry.h>

#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <gtest/gtest.h>

namespace
{
QByteArray readAll(const QString& path)
{
  QFile file(path);
  EXPECT_TRUE(file.open(QIODevice::ReadOnly));
  return file.readAll();
}
}  // namespace

TEST(RegistryIni, RewritingASectionKeepsKeyNamesCommentsAndOrder)
{
  // Morrowind.ini has spaces in key names and comments; QSettings used to
  // rewrite these as "Archive%200" and drop the comments.
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString path = QDir(dir.path()).filePath("Morrowind.ini");
  {
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::WriteOnly));
    file.write("[General]\n; keep me\nScreen Width=1920\n\n"
               "[Game Files]\nGameFile0=Old.esm\nGameFile1=Old2.esp\n\n"
               "[Archives]\nArchive 0=Morrowind.bsa\n");
  }

  ASSERT_TRUE(MOBase::RemoveRegistrySection("Game Files", path));
  ASSERT_TRUE(MOBase::WriteRegistryValue("Game Files", "GameFile0", "Morrowind.esm", path));
  ASSERT_TRUE(MOBase::WriteRegistryValue("Game Files", "GameFile1", "Tribunal.esm", path));
  ASSERT_TRUE(MOBase::WriteRegistryValue("Game Files", "GameFile2", "Bloodmoon.esm", path));

  EXPECT_EQ(readAll(path),
            QByteArray("[General]\n; keep me\nScreen Width=1920\n\n"
                       "[Game Files]\nGameFile0=Morrowind.esm\n"
                       "GameFile1=Tribunal.esm\nGameFile2=Bloodmoon.esm\n\n"
                       "[Archives]\nArchive 0=Morrowind.bsa\n"));
}

TEST(RegistryIni, RemovingAMissingSectionOrFileSucceeds)
{
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  EXPECT_TRUE(MOBase::RemoveRegistrySection("Nope", QDir(dir.path()).filePath("absent.ini")));
}

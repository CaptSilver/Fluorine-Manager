#include "collections/filehash.h"
#include "collections/filehashconcurrent.h"

#include <QCryptographicHash>
#include <QEventLoop>
#include <QFile>
#include <QTemporaryDir>
#include <QTimer>

#include <gtest/gtest.h>

#include <ostream>

// Readable QString output in gtest failure messages. Internal linkage so other
// test files can define their own without a duplicate symbol.
static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << qPrintable(text) << '"';
}

namespace {
QString writeFile(const QTemporaryDir &dir, const QString &name,
                  const QByteArray &bytes) {
  const QString path = dir.filePath(name);
  QFile file(path);
  EXPECT_TRUE(file.open(QIODevice::WriteOnly));
  file.write(bytes);
  return path;
}
} // namespace

TEST(FileHash, Md5OfKnownBytes) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  // Values from `printf ... | md5sum`.
  EXPECT_EQ(md5OfFile(writeFile(dir, "abc.bin", "abc")),
            QString("900150983cd24fb0d6963f7d28e17f72"));
  EXPECT_EQ(md5OfFile(writeFile(dir, "empty.bin", "")),
            QString("d41d8cd98f00b204e9800998ecf8427e"));
  EXPECT_EQ(md5OfFile(writeFile(dir, "hello.bin", "hello world\n")),
            QString("6f5902ac237024bdd0c176cb93063dc4"));
}

TEST(FileHash, Md5SpansReadBuffers) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  // Larger than any single read, with a length that is not a multiple of the
  // buffer.
  QByteArray bytes;
  for (int i = 0; i < 3 * 1024 * 1024 + 123; ++i) {
    bytes.append(static_cast<char>(i * 31 + i / 7));
  }
  const QString expected = QString::fromLatin1(
      QCryptographicHash::hash(bytes, QCryptographicHash::Md5).toHex());
  EXPECT_EQ(md5OfFile(writeFile(dir, "big.bin", bytes)), expected);
}

TEST(FileHash, Md5OfMissingFileIsEmpty) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  EXPECT_TRUE(md5OfFile(dir.filePath("nope.bin")).isEmpty());
}

TEST(FileHash, ConcurrentHasherCallsBack) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  const QString present = writeFile(dir, "abc.bin", "abc");
  const QString missing = dir.filePath("nope.bin");

  ConcurrentFileHasher hasher;
  QEventLoop loop;
  QString presentHex = "unset";
  QString missingHex = "unset";
  int calls = 0;
  const auto finishOne = [&] {
    if (++calls == 2) {
      loop.quit();
    }
  };
  hasher.md5(present, [&](const QString &hex) {
    presentHex = hex;
    finishOne();
  });
  hasher.md5(missing, [&](const QString &hex) {
    missingHex = hex;
    finishOne();
  });
  QTimer::singleShot(10000, &loop, &QEventLoop::quit);
  loop.exec();

  // A failed read still calls back, with an empty string.
  EXPECT_EQ(calls, 2);
  EXPECT_EQ(presentHex, QString("900150983cd24fb0d6963f7d28e17f72"));
  EXPECT_TRUE(missingHex.isEmpty());
}

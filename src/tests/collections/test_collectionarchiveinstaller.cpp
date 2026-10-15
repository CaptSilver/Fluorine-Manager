#include "collections/collectionarchiveinstaller.h"

#include "memoryfiletree.h"

#include <QFile>
#include <QJsonArray>
#include <QJsonObject>
#include <QTemporaryDir>

#include <gtest/gtest.h>

#include <ostream>
#include <vector>

using namespace MOBase;
using CollectionArchiveInstaller::ReadFile;
using CheckReturn = ModDataChecker::CheckReturn;

// Readable QString output in gtest failure messages. Internal linkage so other
// test files can define their own without a duplicate symbol.
static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << qPrintable(text) << '"';
}

namespace {
// Stands in for a game's checker, shaped like the Cyberpunk one: a top-level
// "archive" or "r6" directory is a valid layout, loose .archive files can be
// moved under archive/pc/mod, anything else is not recognised.
class FakeChecker : public ModDataChecker {
public:
  CheckReturn
  dataLooksValid(std::shared_ptr<const IFileTree> tree) const override {
    bool loose = false;
    for (const auto &entry : *tree) {
      if (entry->isDir() && (entry->compare(QStringLiteral("archive")) == 0 ||
                             entry->compare(QStringLiteral("r6")) == 0)) {
        return CheckReturn::VALID;
      }
      if (entry->isFile() && entry->hasSuffix(QStringLiteral("archive"))) {
        loose = true;
      }
    }
    return loose ? CheckReturn::FIXABLE : CheckReturn::INVALID;
  }

  std::shared_ptr<IFileTree>
  fix(std::shared_ptr<IFileTree> tree) const override {
    std::vector<std::shared_ptr<FileTreeEntry>> loose;
    for (const auto &entry : *tree) {
      if (entry->isFile() && entry->hasSuffix(QStringLiteral("archive"))) {
        loose.push_back(entry);
      }
    }
    const auto destination =
        tree->addDirectory(QStringLiteral("archive/pc/mod"));
    for (const auto &entry : loose) {
      entry->moveTo(destination);
    }
    return tree;
  }
};

class RefusingChecker : public FakeChecker {
public:
  // FIXABLE according to dataLooksValid, but fix() gives up.
  std::shared_ptr<IFileTree> fix(std::shared_ptr<IFileTree>) const override {
    return nullptr;
  }
};

std::shared_ptr<IFileTree> memoryTree(const QStringList &paths) {
  return MemoryFileTree::make(paths);
}

struct Pick {
  QString plugin;
  int idx;
};

// Choices as the manifest records them: step "Parts", group "Spoiler".
QJsonObject choose(const QString &plugin, int idx) {
  QJsonArray picks{QJsonObject{{"name", plugin}, {"idx", idx}}};
  QJsonArray groups{QJsonObject{{"name", "Spoiler"}, {"choices", picks}}};
  QJsonArray options{QJsonObject{{"name", "Parts"}, {"groups", groups}}};
  return QJsonObject{{"type", "fomod"}, {"options", options}};
}

// A ModuleConfig.xml with a required folder and one optional plugin whose file
// goes to `pluginDestination`.
QString fomodXml(const QString &pluginDestination =
                     QStringLiteral(R"(archive\pc\mod\sp01.archive)")) {
  return QStringLiteral(
             "<config>"
             "<requiredInstallFiles><folder source=\"core\" "
             "destination=\"archive\\pc\\mod\"/>"
             "</requiredInstallFiles>"
             "<installSteps><installStep name=\"Parts\"><optionalFileGroups>"
             "<group name=\"Spoiler\" type=\"SelectExactlyOne\"><plugins>"
             "<plugin name=\"sp01\"><files><file source=\"opt\\sp01.archive\" "
             "destination=\"%1\"/></files></plugin>"
             "</plugins></group></optionalFileGroups></installStep></"
             "installSteps>"
             "</config>")
      .arg(pluginDestination);
}

const QStringList FomodPaths{"fomod/ModuleConfig.xml", "core/base.archive",
                             "opt/sp01.archive"};

// Writes `xml` to a file and hands back a ReadFile that returns it for
// ModuleConfig.xml.
ReadFile configReader(const QTemporaryDir &dir, const QString &xml) {
  const QString path = dir.filePath(QStringLiteral("ModuleConfig.xml"));
  {
    QFile file(path);
    EXPECT_TRUE(file.open(QIODevice::WriteOnly));
    file.write(xml.toUtf8());
  }
  return [path](std::shared_ptr<const FileTreeEntry> entry) {
    return entry->compare(QStringLiteral("ModuleConfig.xml")) == 0 ? path
                                                                   : QString();
  };
}

ReadFile neverCalled() {
  return [](std::shared_ptr<const FileTreeEntry>) {
    ADD_FAILURE() << "readFile must not be called";
    return QString();
  };
}
} // namespace

// --- archives without a FOMOD
// ------------------------------------------------------------

TEST(ArchiveInstaller, ValidLayoutKept) {
  std::shared_ptr<IFileTree> tree = memoryTree({"archive/pc/mod/a.archive"});
  const auto original = tree;
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(tree, original);
  EXPECT_EQ(treeLeaves(tree), QStringList{"archive/pc/mod/a.archive"});
  EXPECT_TRUE(notes.isEmpty());
}

TEST(ArchiveInstaller, SingleWrapperDirectoryDescended) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"Wrap/archive/pc/mod/a.archive"});
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  // The result is the wrapper's subtree; the manager detaches it afterwards.
  EXPECT_EQ(tree->name(), QString("Wrap"));
  EXPECT_NE(tree->find("archive/pc/mod/a.archive"), nullptr);
}

TEST(ArchiveInstaller, FixableLayoutFixed) {
  std::shared_ptr<IFileTree> tree = memoryTree({"a.archive", "readme.txt"});
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(treeLeaves(tree),
            (QStringList{"archive/pc/mod/a.archive", "readme.txt"}));
  // readme.txt could not be placed, so it is reported.
  ASSERT_EQ(notes.size(), 1);
  EXPECT_NE(notes[0].indexOf("readme.txt"), -1) << qPrintable(notes[0]);
}

TEST(ArchiveInstaller, LeftoverTopLevelEntriesBecomeANote) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"archive/pc/mod/a.archive", "readme.txt", "docs/x.txt"});
  const auto original = tree;
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(tree, original) << "the leftovers stay in the mod";
  ASSERT_EQ(notes.size(), 1);
  EXPECT_NE(notes[0].indexOf("readme.txt"), -1) << qPrintable(notes[0]);
  EXPECT_NE(notes[0].indexOf("docs"), -1) << qPrintable(notes[0]);
  EXPECT_EQ(notes[0].indexOf("archive,"), -1)
      << "the recognised folder is not a leftover";
}

TEST(ArchiveInstaller, FixThatGivesUpFailsWithAReason) {
  std::shared_ptr<IFileTree> tree = memoryTree({"a.archive"});
  RefusingChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_FALSE(reason.isEmpty());
}

TEST(ArchiveInstaller, InvalidLayoutFailsWithManualInstallReason) {
  std::shared_ptr<IFileTree> tree = memoryTree({"readme.txt", "docs/x.txt"});
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_NE(reason.indexOf("manual install"), -1) << qPrintable(reason);
}

TEST(ArchiveInstaller, NullCheckerKeepsTree) {
  std::shared_ptr<IFileTree> tree = memoryTree({"anything/x.txt"});
  const auto original = tree;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, nullptr,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(tree, original);
}

// --- archives with a FOMOD
// -----------------------------------------------------------------

TEST(ArchiveInstaller, FomodWithChoicesReplayed) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker,
                configReader(dir, fomodXml()), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(treeLeaves(tree), (QStringList{"archive/pc/mod/base.archive",
                                           "archive/pc/mod/sp01.archive"}));
  EXPECT_TRUE(notes.isEmpty());
}

TEST(ArchiveInstaller, FomodInsideAWrapperDirectoryReplayedAgainstIt) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  QStringList wrapped;
  for (const QString &path : FomodPaths) {
    wrapped << "Wrap/" + path;
  }
  std::shared_ptr<IFileTree> tree = memoryTree(wrapped);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker,
                configReader(dir, fomodXml()), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  // FOMOD output is placed by its destination paths; the wrapper is not
  // descended.
  EXPECT_EQ(treeLeaves(tree), (QStringList{"archive/pc/mod/base.archive",
                                           "archive/pc/mod/sp01.archive"}));
}

TEST(ArchiveInstaller, FomodReplayResultCheckedAgain) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  // The plugin's file lands at the mod root, where the checker can still fix
  // it.
  std::shared_ptr<IFileTree> tree = memoryTree(
      {"fomod/ModuleConfig.xml", "core/keep.txt", "opt/sp01.archive"});
  FakeChecker checker;
  QStringList notes;
  QString reason;
  const QString xml = QStringLiteral(
      "<config><installSteps><installStep name=\"Parts\"><optionalFileGroups>"
      "<group name=\"Spoiler\"><plugins><plugin name=\"sp01\"><files>"
      "<file source=\"opt\\sp01.archive\" destination=\"\"/></files></plugin>"
      "</plugins></group></optionalFileGroups></installStep></installSteps></"
      "config>");
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker, configReader(dir, xml),
                notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(treeLeaves(tree), QStringList{"archive/pc/mod/sp01.archive"});
}

TEST(ArchiveInstaller, FomodResultTheCheckerDoesNotRecogniseBecomesANote) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  // No required files: the plugin's only file lands at the mod root as
  // readme.txt, which the checker can neither accept nor fix. (fomodXml() would
  // add a required folder under archive/pc/mod, and that result is valid.)
  const QString xml = QStringLiteral(
      "<config><installSteps><installStep name=\"Parts\"><optionalFileGroups>"
      "<group name=\"Spoiler\"><plugins><plugin name=\"sp01\"><files>"
      "<file source=\"core\\base.archive\" "
      "destination=\"readme.txt\"/></files></plugin>"
      "</plugins></group></optionalFileGroups></installStep></installSteps></"
      "config>");
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker, configReader(dir, xml),
                notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  EXPECT_EQ(treeLeaves(tree), QStringList{"readme.txt"});
  // One note for the whole result; the per-entry leftover check is skipped when
  // the layout is already reported as unrecognised.
  ASSERT_EQ(notes.size(), 1);
  EXPECT_NE(notes[0].indexOf("layout"), -1) << qPrintable(notes[0]);
}

TEST(ArchiveInstaller, FomodWithoutChoicesFails) {
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, {}, &checker,
                                                neverCalled(), notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_NE(reason.indexOf("FOMOD"), -1) << qPrintable(reason);
}

TEST(ArchiveInstaller, ChoicesWithoutFomodFails) {
  std::shared_ptr<IFileTree> tree = memoryTree({"archive/pc/mod/a.archive"});
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(tree, choose("sp01", 0),
                                                &checker, neverCalled(), notes,
                                                reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_NE(reason.indexOf("no FOMOD"), -1) << qPrintable(reason);
}

TEST(ArchiveInstaller, ReplayWarningsBecomeNotes) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  // sp01 sits at idx 0 but the manifest recorded 3: matched by name, with a
  // warning.
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 3), &checker,
                configReader(dir, fomodXml()), notes, reason),
            IPluginInstaller::RESULT_SUCCESS)
      << qPrintable(reason);
  ASSERT_EQ(notes.size(), 1);
  EXPECT_NE(notes[0].indexOf("idx"), -1) << qPrintable(notes[0]);
}

TEST(ArchiveInstaller, UnreadableModuleConfigFails) {
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  const ReadFile unreadable = [](std::shared_ptr<const FileTreeEntry>) {
    return QString();
  };
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker, unreadable, notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_NE(reason.indexOf("ModuleConfig.xml"), -1) << qPrintable(reason);
}

TEST(ArchiveInstaller, ReplayErrorFailsWithItsReason) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  const auto original = tree;
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp99", 0), &checker,
                configReader(dir, fomodXml()), notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_NE(reason.indexOf("sp99"), -1) << qPrintable(reason);
  EXPECT_EQ(tree, original);
}

TEST(ArchiveInstaller, MalformedModuleConfigFails) {
  QTemporaryDir dir;
  ASSERT_TRUE(dir.isValid());
  std::shared_ptr<IFileTree> tree = memoryTree(FomodPaths);
  FakeChecker checker;
  QStringList notes;
  QString reason;
  EXPECT_EQ(CollectionArchiveInstaller::install(
                tree, choose("sp01", 0), &checker,
                configReader(dir, "<config><oops>"), notes, reason),
            IPluginInstaller::RESULT_FAILED);
  EXPECT_FALSE(reason.isEmpty());
}

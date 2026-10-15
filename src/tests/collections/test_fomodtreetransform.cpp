#include "collections/fomodtreetransform.h"

#include "archivefiletree.h"
#include "memoryfiletree.h"

#include <archive/archive.h>

#include <QStringList>

#include <gtest/gtest.h>

#include <filesystem>
#include <memory>
#include <ostream>
#include <set>

#ifndef FLUORINE_COLLECTION_FOMOD_ZIP
#error "FLUORINE_COLLECTION_FOMOD_ZIP must name the FOMOD payload ZIP fixture"
#endif

using namespace MOBase;

// Readable QString output in gtest failure messages. Internal linkage so other
// test files can define their own without a duplicate symbol.
static void PrintTo(const QString &text, std::ostream *os) {
  *os << '"' << qPrintable(text) << '"';
}

namespace {
FileRule rule(const QString &source, const QString &destination,
              int priority = 0, int sequence = 0, bool isFolder = false) {
  FileRule result;
  result.source = source;
  result.destination = destination;
  result.priority = priority;
  result.sequence = sequence;
  result.isFolder = isFolder;
  return result;
}

std::shared_ptr<IFileTree> memoryTree(const QStringList &paths) {
  return MemoryFileTree::make(paths);
}
} // namespace

// --- finding fomod/
// ------------------------------------------------------------------

TEST(FomodTreeTransform, FindsFomodAtRoot) {
  const auto tree = memoryTree({"fomod/ModuleConfig.xml", "a.txt"});
  const std::optional<QString> parent =
      FomodTreeTransform::findFomodParent(tree);
  ASSERT_TRUE(parent.has_value());
  EXPECT_EQ(*parent, QString(""));
}

TEST(FomodTreeTransform, FindsFomodAtDepthTwo) {
  const auto tree =
      memoryTree({"Wrap/Inner/fomod/ModuleConfig.xml", "Wrap/Inner/a.txt"});
  const std::optional<QString> parent =
      FomodTreeTransform::findFomodParent(tree);
  ASSERT_TRUE(parent.has_value());
  EXPECT_EQ(*parent, QString("Wrap/Inner"));
}

TEST(FomodTreeTransform, FindsFomodIgnoringCase) {
  const auto tree = memoryTree({"Wrap/Fomod/moduleconfig.XML"});
  const std::optional<QString> parent =
      FomodTreeTransform::findFomodParent(tree);
  ASSERT_TRUE(parent.has_value());
  EXPECT_EQ(*parent, QString("Wrap"));

  const auto entry = FomodTreeTransform::moduleConfigEntry(tree, *parent);
  ASSERT_NE(entry, nullptr);
  EXPECT_EQ(entry->name(), QString("moduleconfig.XML"));
  EXPECT_TRUE(entry->isFile());
}

TEST(FomodTreeTransform, FirstFomodInSortedPathOrderWins) {
  const auto tree =
      memoryTree({"zeta/fomod/ModuleConfig.xml", "alpha/fomod/ModuleConfig.xml",
                  "alpha/deep/fomod/ModuleConfig.xml"});
  const std::optional<QString> parent =
      FomodTreeTransform::findFomodParent(tree);
  ASSERT_TRUE(parent.has_value());
  // "alpha/deep/..." sorts before "alpha/fomod/...", and both before
  // "zeta/...".
  EXPECT_EQ(*parent, QString("alpha/deep"));
}

TEST(FomodTreeTransform, NoFomodIsNullopt) {
  // ModuleConfig.xml must sit directly inside a directory named fomod.
  const auto tree = memoryTree(
      {"fomod/Info.xml", "ModuleConfig.xml", "sub/moduleconfig.xml"});
  EXPECT_FALSE(FomodTreeTransform::findFomodParent(tree).has_value());
  EXPECT_EQ(FomodTreeTransform::moduleConfigEntry(tree, ""), nullptr);
}

// --- applying rules
// --------------------------------------------------------------------

TEST(FomodTreeTransform, CopiesFileRuleToDestination) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"core/base.archive", "unused.txt"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "", {rule("core/base.archive", "archive/pc/mod/base.archive")},
      error))
      << qPrintable(error);
  // Only what a rule names is installed.
  EXPECT_EQ(treeLeaves(tree), QStringList{"archive/pc/mod/base.archive"});
}

TEST(FomodTreeTransform, FileRuleDirectoryDestinationKeepsTheArchiveName) {
  std::shared_ptr<IFileTree> tree = memoryTree({"opt/SP01.Archive"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "", {rule("opt/sp01.archive", "archive/pc/mod/")}, error))
      << qPrintable(error);
  EXPECT_EQ(treeLeaves(tree), QStringList{"archive/pc/mod/SP01.Archive"});

  std::shared_ptr<IFileTree> rootTree = memoryTree({"opt/SP01.Archive"});
  ASSERT_TRUE(FomodTreeTransform::apply(rootTree, "",
                                        {rule("opt/sp01.archive", "")}, error))
      << qPrintable(error);
  EXPECT_EQ(treeLeaves(rootTree), QStringList{"SP01.Archive"});
}

TEST(FomodTreeTransform, FolderRuleCopiesSubtree) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"dir/x.archive", "dir/sub/y.xl"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "", {rule("dir", "r6/tweaks", 0, 0, true)}, error))
      << qPrintable(error);
  EXPECT_EQ(treeLeaves(tree),
            (QStringList{"r6/tweaks/sub/y.xl", "r6/tweaks/x.archive"}));

  std::shared_ptr<IFileTree> rootTree =
      memoryTree({"dir/x.archive", "dir/sub/y.xl"});
  ASSERT_TRUE(FomodTreeTransform::apply(rootTree, "",
                                        {rule("dir", "", 0, 0, true)}, error))
      << qPrintable(error);
  EXPECT_EQ(treeLeaves(rootTree), (QStringList{"sub/y.xl", "x.archive"}));
}

TEST(FomodTreeTransform, FolderRulesLayerWithoutWipingEarlierFiles) {
  std::shared_ptr<IFileTree> tree = memoryTree(
      {"base/keep.txt", "base/shared.txt", "over/new.txt", "over/shared.txt"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "",
      {rule("base", "mod", 0, 0, true), rule("over", "mod", 0, 1, true)},
      error))
      << qPrintable(error);
  EXPECT_EQ(treeLeaves(tree),
            (QStringList{"mod/keep.txt", "mod/new.txt", "mod/shared.txt"}));
  EXPECT_EQ(treeOrigins(tree).at("mod/shared.txt"), QString("over/shared.txt"));
}

TEST(FomodTreeTransform, HigherPriorityWins) {
  std::shared_ptr<IFileTree> tree = memoryTree({"a.archive", "b.archive"});
  QString error;
  // Listed with the lower priority last: the order of the list does not matter.
  ASSERT_TRUE(FomodTreeTransform::apply(tree, "",
                                        {rule("a.archive", "z.archive", 5, 0),
                                         rule("b.archive", "z.archive", 1, 1)},
                                        error))
      << qPrintable(error);
  const auto origins = treeOrigins(tree);
  ASSERT_EQ(origins.size(), 1u);
  EXPECT_EQ(origins.at("z.archive"), QString("a.archive"));
}

TEST(FomodTreeTransform, EqualPriorityLaterRuleWins) {
  std::shared_ptr<IFileTree> tree = memoryTree({"a.archive", "b.archive"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(tree, "",
                                        {rule("b.archive", "z.archive", 0, 1),
                                         rule("a.archive", "z.archive", 0, 0)},
                                        error))
      << qPrintable(error);
  EXPECT_EQ(treeOrigins(tree).at("z.archive"), QString("b.archive"));
}

TEST(FomodTreeTransform, FileRuleOfHigherPriorityBeatsFolderLeaf) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"dir/x.archive", "own/x.archive"});
  QString error;
  ASSERT_TRUE(
      FomodTreeTransform::apply(tree, "",
                                {rule("own/x.archive", "mod/x.archive", 5, 0),
                                 rule("dir", "mod", 0, 1, true)},
                                error))
      << qPrintable(error);
  EXPECT_EQ(treeOrigins(tree).at("mod/x.archive"), QString("own/x.archive"));
}

// --- source resolution
// ----------------------------------------------------------------

TEST(FomodTreeTransform, SourcesResolveAgainstFomodParent) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"Wrap/fomod/ModuleConfig.xml", "Wrap/core/base.archive",
                  "core/base.archive"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "Wrap", {rule("core/base.archive", "x/base.archive")}, error))
      << qPrintable(error);
  EXPECT_EQ(treeOrigins(tree).at("x/base.archive"),
            QString("Wrap/core/base.archive"));
}

TEST(FomodTreeTransform, SourceResolvedCaseInsensitively) {
  std::shared_ptr<IFileTree> tree = memoryTree({"OPT/SP01.ARCHIVE"});
  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "", {rule("opt/sp01.archive", "out/sp01.archive")}, error))
      << qPrintable(error);
  EXPECT_EQ(treeOrigins(tree).at("out/sp01.archive"),
            QString("OPT/SP01.ARCHIVE"));
}

TEST(FomodTreeTransform, SourceResolvesThroughDirsDifferingOnlyByCase) {
  std::shared_ptr<IFileTree> tree =
      memoryTree({"Car/a.archive", "car/b.archive", "car/only_lower.archive"});
  // The double must really hold two sibling directories, or this test proves
  // nothing.
  ASSERT_EQ(tree->size(), 2u);

  QString error;
  ASSERT_TRUE(FomodTreeTransform::apply(
      tree, "",
      {rule("Car/a.archive", "archive/pc/mod/a.archive", 0, 0),
       rule("car/b.archive", "archive/pc/mod/b.archive", 0, 1),
       // Neither "CAR" spelling is exact; only the second directory has the
       // file.
       rule("CAR/only_lower.archive", "archive/pc/mod/o.archive", 0, 2)},
      error))
      << qPrintable(error);
  const auto origins = treeOrigins(tree);
  EXPECT_EQ(origins.at("archive/pc/mod/a.archive"), QString("Car/a.archive"));
  EXPECT_EQ(origins.at("archive/pc/mod/b.archive"), QString("car/b.archive"));
  EXPECT_EQ(origins.at("archive/pc/mod/o.archive"),
            QString("car/only_lower.archive"));
}

TEST(FomodTreeTransform, MissingSourceIsErrorAndTreeUnchanged) {
  const std::shared_ptr<IFileTree> original =
      memoryTree({"a.archive", "b.archive"});
  std::shared_ptr<IFileTree> tree = original;
  QString error;
  EXPECT_FALSE(
      FomodTreeTransform::apply(tree, "",
                                {rule("a.archive", "ok.archive", 0, 0),
                                 rule("nope.archive", "n.archive", 0, 1)},
                                error));
  EXPECT_NE(error.indexOf("nope.archive"), -1) << qPrintable(error);
  EXPECT_EQ(tree, original);
  EXPECT_EQ(treeLeaves(tree), (QStringList{"a.archive", "b.archive"}));
}

TEST(FomodTreeTransform, SourceOfTheWrongKindIsAnError) {
  std::shared_ptr<IFileTree> tree = memoryTree({"dir/x.archive"});
  QString error;
  EXPECT_FALSE(
      FomodTreeTransform::apply(tree, "", {rule("dir", "out")}, error));
  EXPECT_FALSE(error.isEmpty());

  error.clear();
  EXPECT_FALSE(FomodTreeTransform::apply(
      tree, "", {rule("dir/x.archive", "out", 0, 0, true)}, error));
  EXPECT_FALSE(error.isEmpty());
}

// --- a real archive tree
// ---------------------------------------------------------------

namespace {
struct RealArchive {
  std::unique_ptr<Archive> archive;
  std::shared_ptr<IFileTree> tree;
};

// The fixture holds Wrap/fomod/ModuleConfig.xml, Wrap/core/base.archive,
// Wrap/opt/sp01.archive and Wrap/opt/extra.xl.
RealArchive openPayload() {
  RealArchive real;
  auto archive = CreateArchive();
  if (!archive->isValid() ||
      !archive->open(
          std::filesystem::path(FLUORINE_COLLECTION_FOMOD_ZIP).wstring(), {})) {
    return real;
  }
  real.tree = ArchiveFileTree::makeTree(*archive);
  real.archive = std::move(archive);
  return real;
}

std::vector<FileRule> payloadRules() {
  return {rule("core/base.archive", "archive/pc/mod/base.archive", 0, 0),
          rule("opt", "r6/tweaks", 0, 1, true)};
}
} // namespace

TEST(FomodTreeTransform, RealArchiveResultIsArchiveFileTree) {
  RealArchive real = openPayload();
  ASSERT_NE(real.archive, nullptr);

  const std::optional<QString> parent =
      FomodTreeTransform::findFomodParent(real.tree);
  ASSERT_TRUE(parent.has_value());
  EXPECT_EQ(*parent, QString("Wrap"));

  QString error;
  ASSERT_TRUE(
      FomodTreeTransform::apply(real.tree, *parent, payloadRules(), error))
      << qPrintable(error);
  // InstallationManager::install() downcasts the installer's result to
  // ArchiveFileTree.
  EXPECT_NE(std::dynamic_pointer_cast<ArchiveFileTree>(real.tree), nullptr);
  EXPECT_EQ(treeLeaves(real.tree),
            (QStringList{"archive/pc/mod/base.archive", "r6/tweaks/extra.xl",
                         "r6/tweaks/sp01.archive"}));
}

TEST(FomodTreeTransform, RealArchiveMapsEveryLeafToAnArchiveEntry) {
  RealArchive real = openPayload();
  ASSERT_NE(real.archive, nullptr);

  QString error;
  ASSERT_TRUE(
      FomodTreeTransform::apply(real.tree, "Wrap", payloadRules(), error))
      << qPrintable(error);
  const auto archiveTree =
      std::dynamic_pointer_cast<ArchiveFileTree>(real.tree);
  ASSERT_NE(archiveTree, nullptr);

  // mapToArchive() writes each leaf's path onto the archive entry it came from.
  // A leaf that lost its archive index (an entry minted with addFile) would not
  // appear.
  for (FileData *file : real.archive->getFileList()) {
    file->clearOutputFilePaths();
  }
  archiveTree->mapToArchive(*real.archive);

  std::set<QString> mapped;
  for (FileData *file : real.archive->getFileList()) {
    for (const std::wstring &path : file->getOutputFilePaths()) {
      mapped.insert(QString::fromStdWString(path));
    }
  }
  EXPECT_EQ(QStringList(mapped.begin(), mapped.end()), treeLeaves(real.tree));
  EXPECT_EQ(mapped.size(), 3u);
}

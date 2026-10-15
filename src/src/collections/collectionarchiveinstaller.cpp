#include "collectionarchiveinstaller.h"

#include "fomodmodel.h"
#include "fomodreplay.h"
#include "fomodtreetransform.h"

#include <QFile>

using namespace MOBase;

namespace
{
using CheckReturn = ModDataChecker::CheckReturn;
using Result      = IPluginInstaller::EInstallResult;

// InstallerQuick::getSimpleArchiveBase without its "data folder plus text files" case:
// keep a valid tree, descend through a lone directory, otherwise give up.
std::shared_ptr<IFileTree> simpleArchiveBase(std::shared_ptr<IFileTree> tree,
                                             const ModDataChecker& checker)
{
  while (true) {
    if (checker.dataLooksValid(tree) == CheckReturn::VALID) {
      return tree;
    }
    if (tree->size() != 1 || !tree->at(0)->isDir()) {
      return nullptr;
    }
    tree = tree->at(0)->astree();
  }
}

// Top-level entries the game's checker would not accept on their own, such as a readme
// next to the real folders. They install with the mod; the note puts them in the results
// dialog's "needs a look" column. Each entry is judged in an orphan tree of its own, so
// nothing in the real tree is touched.
void noteLeftovers(const std::shared_ptr<IFileTree>& tree, const ModDataChecker& checker,
                   QStringList& notes)
{
  QStringList leftovers;
  for (const auto& entry : *tree) {
    const std::shared_ptr<IFileTree> alone = tree->createOrphanTree();
    if (alone && alone->copy(entry, QString(), IFileTree::InsertPolicy::FAIL_IF_EXISTS) &&
        checker.dataLooksValid(alone) == CheckReturn::INVALID) {
      leftovers << entry->name();
    }
  }
  if (!leftovers.isEmpty()) {
    notes << QStringLiteral("top-level entries the game's layout check does not recognise "
                            "stay in the mod: %1")
                 .arg(leftovers.join(QStringLiteral(", ")));
  }
}

Result installPlain(std::shared_ptr<IFileTree>& tree, const ModDataChecker* checker,
                    QStringList& notes, QString& reason)
{
  if (checker == nullptr) {
    return IPluginInstaller::RESULT_SUCCESS;
  }
  if (const auto base = simpleArchiveBase(tree, *checker)) {
    tree = base;
    noteLeftovers(tree, *checker, notes);
    return IPluginInstaller::RESULT_SUCCESS;
  }
  if (checker->dataLooksValid(tree) == CheckReturn::FIXABLE) {
    if (const auto fixed = checker->fix(tree)) {
      tree = fixed;
      noteLeftovers(tree, *checker, notes);
      return IPluginInstaller::RESULT_SUCCESS;
    }
    reason = QStringLiteral("the game's mod data checker could not fix the layout");
    return IPluginInstaller::RESULT_FAILED;
  }
  reason = QStringLiteral("layout not recognised by the game's mod data checker; Bundle, "
                          "BAIN and DataText archives need a manual install");
  return IPluginInstaller::RESULT_FAILED;
}

Result installFomod(std::shared_ptr<IFileTree>& tree, const QString& fomodParent,
                    const QJsonObject& choices, const ModDataChecker* checker,
                    const CollectionArchiveInstaller::ReadFile& readFile,
                    QStringList& notes, QString& reason)
{
  const auto configEntry = FomodTreeTransform::moduleConfigEntry(tree, fomodParent);
  const QString configPath = configEntry ? readFile(configEntry) : QString();
  QFile configFile(configPath);
  if (configPath.isEmpty() || !configFile.open(QIODevice::ReadOnly)) {
    reason = QStringLiteral("cannot read fomod/ModuleConfig.xml from the archive");
    return IPluginInstaller::RESULT_FAILED;
  }

  QString error;
  const std::optional<FomodModel> model = FomodModel::parse(configFile.readAll(), error);
  if (!model) {
    reason = QStringLiteral("cannot parse ModuleConfig.xml: %1").arg(error);
    return IPluginInstaller::RESULT_FAILED;
  }

  QStringList warnings;
  const FomodSelection selection = FomodReplay::select(*model, choices, warnings, error);
  if (!error.isEmpty()) {
    reason = error;
    return IPluginInstaller::RESULT_FAILED;
  }
  if (!FomodTreeTransform::apply(tree, fomodParent, selection.rules, error)) {
    reason = error;
    return IPluginInstaller::RESULT_FAILED;
  }
  notes += warnings;

  if (tree->empty()) {
    reason = QStringLiteral("the FOMOD choices install no files");
    return IPluginInstaller::RESULT_FAILED;
  }

  // The files are placed by their destination paths, so the wrapper is not descended;
  // the checker still gets a say on the result.
  if (checker != nullptr) {
    switch (checker->dataLooksValid(tree)) {
    case CheckReturn::VALID:
      noteLeftovers(tree, *checker, notes);
      break;
    case CheckReturn::FIXABLE: {
      const auto fixed = checker->fix(tree);
      if (!fixed) {
        reason = QStringLiteral("the game's mod data checker could not fix the FOMOD "
                                "result");
        return IPluginInstaller::RESULT_FAILED;
      }
      tree = fixed;
      noteLeftovers(tree, *checker, notes);
      break;
    }
    case CheckReturn::INVALID:
      notes << QStringLiteral("the game's layout check does not recognise the FOMOD "
                              "result; look it over after the install");
      break;
    }
  }
  return IPluginInstaller::RESULT_SUCCESS;
}
}  // namespace

namespace CollectionArchiveInstaller
{
IPluginInstaller::EInstallResult
install(std::shared_ptr<IFileTree>& tree, const QJsonObject& choices,
        const ModDataChecker* checker, const ReadFile& readFile, QStringList& notes,
        QString& reason)
{
  reason.clear();

  const std::optional<QString> fomodParent = FomodTreeTransform::findFomodParent(tree);
  if (fomodParent) {
    if (choices.isEmpty()) {
      reason = QStringLiteral("FOMOD installer needs a human; install it from Fluorine");
      return IPluginInstaller::RESULT_FAILED;
    }
    return installFomod(tree, *fomodParent, choices, checker, readFile, notes, reason);
  }

  if (!choices.isEmpty()) {
    reason = QStringLiteral("collection has installer choices but the archive has no FOMOD");
    return IPluginInstaller::RESULT_FAILED;
  }
  return installPlain(tree, checker, notes, reason);
}
}  // namespace CollectionArchiveInstaller

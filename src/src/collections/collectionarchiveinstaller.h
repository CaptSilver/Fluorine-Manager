#pragma once

#include <uibase/game_features/moddatachecker.h>
#include <uibase/iplugininstaller.h>

#include <QJsonObject>
#include <QString>
#include <QStringList>

#include <functional>
#include <memory>

namespace CollectionArchiveInstaller {
// Extracts one archive entry to disk and returns its local path, empty on
// failure.
using ReadFile =
    std::function<QString(std::shared_ptr<const MOBase::FileTreeEntry>)>;

// The body of an unattended collection install: decides what the mod's file
// tree should be, without asking anyone.
//
// An archive with a fomod/ModuleConfig.xml needs the manifest's recorded
// `choices`, which are replayed instead of showing the installer. Any other
// archive follows the quick installer's layout rules with the game's `checker`:
// a valid layout is kept, a single wrapper directory is descended, a fixable
// layout is fixed. With no checker the tree is kept as it is. `tree` is
// replaced on success; `notes` collects things worth a look (FOMOD replay
// warnings, top-level entries the layout check does not recognise) afterwards;
// `reason` says why a RESULT_FAILED happened. Returns RESULT_SUCCESS or
// RESULT_FAILED.
MOBase::IPluginInstaller::EInstallResult
install(std::shared_ptr<MOBase::IFileTree> &tree, const QJsonObject &choices,
        const MOBase::ModDataChecker *checker, const ReadFile &readFile,
        QStringList &notes, QString &reason);
} // namespace CollectionArchiveInstaller

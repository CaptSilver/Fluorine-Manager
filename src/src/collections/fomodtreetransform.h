#pragma once

#include "fomodreplay.h"

#include <uibase/ifiletree.h>

#include <QString>

#include <memory>
#include <optional>
#include <vector>

namespace FomodTreeTransform {
// Path ('/'-separated, "" = root) of the directory holding
// fomod/ModuleConfig.xml: any depth, first in sorted path order.
std::optional<QString>
findFomodParent(const std::shared_ptr<const MOBase::IFileTree> &tree);

// The fomod/ModuleConfig.xml entry below `fomodParent`, or null.
std::shared_ptr<const MOBase::FileTreeEntry>
moduleConfigEntry(const std::shared_ptr<const MOBase::IFileTree> &tree,
                  const QString &fomodParent);

// Replaces `tree` with a new tree holding what `rules` install. Sources are
// resolved below `fomodParent`, ignoring case. Entries are only copied out of
// the incoming tree, never created, so an archive tree stays an archive tree
// and every file keeps its archive index. On failure `tree` is left as it was
// and `error` says why.
bool apply(std::shared_ptr<MOBase::IFileTree> &tree, const QString &fomodParent,
           const std::vector<FileRule> &rules, QString &error);
} // namespace FomodTreeTransform

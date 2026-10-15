#pragma once

#include "fomodmodel.h"

#include <QJsonObject>
#include <QString>
#include <QStringList>

#include <map>
#include <optional>
#include <vector>

// One copy the replay wants made from the archive into the mod.
//
// For a file rule, `destination` is the target file path. When it is empty (the mod
// root) or ends in '/', it is a directory and the file keeps the name it has in the
// archive. For a folder rule it is the directory the folder's contents go into, and
// empty means the mod root. Rules with a higher `priority` win a conflict; at equal
// priority the higher `sequence` wins.
struct FileRule
{
  QString source;       // safe relative path, '/'-separated
  QString destination;  // safe relative path, '/'-separated, may be empty (mod root)
  int priority = 0;
  int sequence = 0;
  bool isFolder = false;
};

struct FomodSelection
{
  QStringList pluginNames;  // selected, document order
  std::vector<FileRule> rules;
};

namespace FomodReplay
{
// Replays the plugin picks recorded in a collection manifest ("choices") over a parsed
// ModuleConfig.xml. Only the recorded plugins are selected; nothing is forced in.
// On failure returns an empty selection and sets `error`. `warnings` collects
// mismatches that do not stop the install, such as a recorded idx that disagrees with
// the plugin found by name.
FomodSelection select(const FomodModel& model, const QJsonObject& choices,
                      QStringList& warnings, QString& error);

// Three-valued: File, Game and Fomm leaves cannot be decided without the installed
// mod list or the game version, so they evaluate to Unknown.
FomodTriState evaluate(const FomodCondition& condition,
                       const std::map<QString, QString>& flags);

// Splits on '/' and '\', drops empty and "." parts and joins the rest with '/'.
// Returns nothing for a path with a ".." part, and for an empty one unless allowEmpty.
std::optional<QString> safeRelative(const QString& path, bool allowEmpty);
}  // namespace FomodReplay

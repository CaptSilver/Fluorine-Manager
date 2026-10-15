#include "fomodreplay.h"

#include <QJsonArray>
#include <QJsonValue>

#include <set>
#include <tuple>

namespace
{
using PluginKey = std::tuple<int, int, int>;  // step, group, plugin position in its group

QString foldName(const QString& name)
{
  return name.simplified().toCaseFolded();
}

template <class T>
std::vector<QString> namesOf(const std::vector<T>& items)
{
  std::vector<QString> names;
  names.reserve(items.size());
  for (const T& item : items) {
    names.push_back(item.name);
  }
  return names;
}

// Positions whose name is `wanted`: the exact spellings if there are any, otherwise the
// ones that agree ignoring case and spacing. Several hits mean the name is ambiguous.
std::vector<int> positionsNamed(const std::vector<QString>& names, const QString& wanted)
{
  const int count = static_cast<int>(names.size());
  std::vector<int> hits;
  for (int i = 0; i < count; ++i) {
    if (names[i] == wanted) {
      hits.push_back(i);
    }
  }
  if (!hits.empty()) {
    return hits;
  }
  const QString folded = foldName(wanted);
  for (int i = 0; i < count; ++i) {
    if (foldName(names[i]) == folded) {
      hits.push_back(i);
    }
  }
  return hits;
}

// Splits on '/' and '\', dropping empty and "." parts. False when a ".." part is present.
bool splitPath(const QString& path, QStringList& parts)
{
  parts.clear();
  QString normalized = path;
  normalized.replace(QLatin1Char('\\'), QLatin1Char('/'));
  const QStringList pieces = normalized.split(QLatin1Char('/'), Qt::SkipEmptyParts);
  for (const QString& piece : pieces) {
    if (piece == QLatin1String("..")) {
      return false;
    }
    if (piece != QLatin1String(".")) {
      parts.push_back(piece);
    }
  }
  return true;
}

bool endsWithSeparator(const QString& text)
{
  return text.endsWith(QLatin1Char('/')) || text.endsWith(QLatin1Char('\\'));
}

bool appendRules(const std::vector<FomodFile>& files, int& sequence,
                 std::vector<FileRule>& rules, QString& error)
{
  for (const FomodFile& file : files) {
    QStringList sourceParts;
    QStringList destinationParts;
    if (!splitPath(file.source, sourceParts)) {
      error = QStringLiteral("unsafe source path in FOMOD: '%1'").arg(file.source);
      return false;
    }
    if (sourceParts.isEmpty()) {
      error = QStringLiteral("FOMOD entry with an empty source");
      return false;
    }
    if (!splitPath(file.destination, destinationParts)) {
      error =
          QStringLiteral("unsafe destination path in FOMOD: '%1'").arg(file.destination);
      return false;
    }

    FileRule rule;
    rule.source    = sourceParts.join(QLatin1Char('/'));
    rule.priority  = file.priority;
    rule.sequence  = sequence++;
    rule.isFolder  = file.isFolder;
    const QString destination = destinationParts.join(QLatin1Char('/'));
    if (file.isFolder) {
      rule.destination = destination;
    } else if (!file.hasDestination) {
      rule.destination = rule.source;
    } else if (!destination.isEmpty() && endsWithSeparator(file.destination)) {
      rule.destination = destination + QLatin1Char('/');
    } else {
      rule.destination = destination;
    }
    rules.push_back(rule);
  }
  return true;
}

bool hasChoices(const QJsonArray& groups)
{
  for (const QJsonValue& group : groups) {
    if (!group.toObject().value(QStringLiteral("choices")).toArray().isEmpty()) {
      return true;
    }
  }
  return false;
}

// The plugin named by one recorded pick. A name that occurs more than once in the group
// is told apart by the recorded idx.
std::optional<int> matchPlugin(const FomodGroup& group, const QJsonObject& choice,
                               const QString& where, QStringList& warnings,
                               QString& error)
{
  const QString name = choice.value(QStringLiteral("name")).toString();
  const std::vector<int> hits = positionsNamed(namesOf(group.plugins), name);
  const QJsonValue idxValue   = choice.value(QStringLiteral("idx"));
  const bool hasIdx           = idxValue.isDouble();
  const int idx               = idxValue.toInt(-1);

  if (hits.empty()) {
    error = QStringLiteral("FOMOD choice plugin (%1) '%2' not found in ModuleConfig.xml")
                .arg(where, name);
    return std::nullopt;
  }
  if (hits.size() == 1) {
    if (hasIdx && hits.front() != idx) {
      warnings << QStringLiteral("FOMOD %1: plugin '%2' recorded idx %3 but sits at idx "
                                 "%4; matched by name")
                      .arg(where, name)
                      .arg(idx)
                      .arg(hits.front());
    }
    return hits.front();
  }
  if (hasIdx) {
    for (int hit : hits) {
      if (hit == idx) {
        return hit;
      }
    }
  }
  error = QStringLiteral("FOMOD plugin '%1' (%2) is ambiguous").arg(name, where);
  return std::nullopt;
}

bool recordedPicks(const FomodModel& model, const QJsonObject& choices,
                   QStringList& warnings, QString& error, std::set<PluginKey>& picked)
{
  const std::vector<QString> stepNames = namesOf(model.steps);
  const QJsonArray options = choices.value(QStringLiteral("options")).toArray();

  for (const QJsonValue& optionValue : options) {
    const QJsonObject option = optionValue.toObject();
    const QJsonArray groups  = option.value(QStringLiteral("groups")).toArray();
    // An option nobody picked anything in is not looked up at all.
    if (!hasChoices(groups)) {
      continue;
    }

    const QString stepName = option.value(QStringLiteral("name")).toString();
    const std::vector<int> stepHits = positionsNamed(stepNames, stepName);
    if (stepHits.empty()) {
      error = QStringLiteral("FOMOD choice step '%1' not found in ModuleConfig.xml")
                  .arg(stepName);
      return false;
    }
    if (stepHits.size() > 1) {
      error = QStringLiteral("FOMOD step '%1' is ambiguous").arg(stepName);
      return false;
    }
    const int stepIndex   = stepHits.front();
    const FomodStep& step = model.steps[stepIndex];
    const std::vector<QString> groupNames = namesOf(step.groups);

    for (const QJsonValue& groupValue : groups) {
      const QJsonObject groupChoice = groupValue.toObject();
      const QJsonArray plugins = groupChoice.value(QStringLiteral("choices")).toArray();
      if (plugins.isEmpty()) {
        continue;
      }

      const QString groupName = groupChoice.value(QStringLiteral("name")).toString();
      const std::vector<int> groupHits = positionsNamed(groupNames, groupName);
      if (groupHits.empty()) {
        error = QStringLiteral("FOMOD choice group (step '%1') '%2' not found in "
                               "ModuleConfig.xml")
                    .arg(step.name, groupName);
        return false;
      }
      if (groupHits.size() > 1) {
        error = QStringLiteral("FOMOD group '%1' is ambiguous").arg(groupName);
        return false;
      }
      const int groupIndex    = groupHits.front();
      const FomodGroup& group = step.groups[groupIndex];
      const QString where     = step.name + QStringLiteral(" / ") + group.name;

      for (const QJsonValue& pluginValue : plugins) {
        const std::optional<int> pluginIndex =
            matchPlugin(group, pluginValue.toObject(), where, warnings, error);
        if (!pluginIndex) {
          return false;
        }
        picked.insert(PluginKey(stepIndex, groupIndex, *pluginIndex));
      }
    }
  }
  return true;
}
}  // namespace

namespace FomodReplay
{
std::optional<QString> safeRelative(const QString& path, bool allowEmpty)
{
  QStringList parts;
  if (!splitPath(path, parts)) {
    return std::nullopt;
  }
  if (parts.isEmpty() && !allowEmpty) {
    return std::nullopt;
  }
  return parts.join(QLatin1Char('/'));
}

FomodTriState evaluate(const FomodCondition& condition,
                       const std::map<QString, QString>& flags)
{
  switch (condition.kind) {
  case FomodCondition::Kind::Flag: {
    const auto it = flags.find(condition.name);
    const QString current = it == flags.end() ? QString() : it->second;
    return current == condition.value ? FomodTriState::True : FomodTriState::False;
  }
  case FomodCondition::Kind::File:
  case FomodCondition::Kind::Game:
  case FomodCondition::Kind::Fomm:
    return FomodTriState::Unknown;
  case FomodCondition::Kind::And:
  case FomodCondition::Kind::Or:
    break;
  }

  // A False decides an And and a True decides an Or, even next to an Unknown leaf.
  const bool isAnd = condition.kind == FomodCondition::Kind::And;
  bool sawUnknown  = false;
  for (const FomodCondition& child : condition.children) {
    const FomodTriState result = evaluate(child, flags);
    if (result == FomodTriState::Unknown) {
      sawUnknown = true;
    } else if ((result == FomodTriState::False) == isAnd) {
      return result;
    }
  }
  if (sawUnknown) {
    return FomodTriState::Unknown;
  }
  return isAnd ? FomodTriState::True : FomodTriState::False;
}

FomodSelection select(const FomodModel& model, const QJsonObject& choices,
                      QStringList& warnings, QString& error)
{
  error.clear();

  std::set<PluginKey> picked;
  if (!recordedPicks(model, choices, warnings, error, picked)) {
    return {};
  }

  FomodSelection selection;
  int sequence = 0;
  if (!appendRules(model.requiredInstallFiles, sequence, selection.rules, error)) {
    return {};
  }

  // Document order, not the order the choices were recorded in: it decides which
  // plugin's flag write is the last one.
  std::map<QString, QString> flags;
  const int stepCount = static_cast<int>(model.steps.size());
  for (int s = 0; s < stepCount; ++s) {
    const int groupCount = static_cast<int>(model.steps[s].groups.size());
    for (int g = 0; g < groupCount; ++g) {
      const FomodGroup& group = model.steps[s].groups[g];
      const int pluginCount   = static_cast<int>(group.plugins.size());
      for (int p = 0; p < pluginCount; ++p) {
        if (picked.count(PluginKey(s, g, p)) == 0) {
          continue;
        }
        const FomodPlugin& plugin = group.plugins[p];
        selection.pluginNames << plugin.name;
        if (!appendRules(plugin.files, sequence, selection.rules, error)) {
          return {};
        }
        for (const FomodFlag& flag : plugin.flags) {
          flags[flag.name] = flag.value;
        }
      }
    }
  }

  int patternNumber = 0;
  for (const FomodPattern& pattern : model.conditionalFileInstalls) {
    ++patternNumber;
    switch (evaluate(pattern.condition, flags)) {
    case FomodTriState::Unknown:
      error = QStringLiteral("FOMOD conditional pattern #%1 depends on installed files or "
                             "the game version; install it from Fluorine")
                  .arg(patternNumber);
      return {};
    case FomodTriState::True:
      if (!appendRules(pattern.files, sequence, selection.rules, error)) {
        return {};
      }
      break;
    case FomodTriState::False:
      break;
    }
  }
  return selection;
}
}  // namespace FomodReplay

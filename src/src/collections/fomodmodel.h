#pragma once

#include <QByteArray>
#include <QString>

#include <optional>
#include <vector>

enum class FomodTriState { False, True, Unknown };

struct FomodCondition {
  enum class Kind { Flag, File, Game, Fomm, And, Or };
  Kind kind = Kind::And;
  QString name;  // Flag: flag name; File: file path
  QString value; // Flag: value; File: state; Game/Fomm: version
  std::vector<FomodCondition> children;
};

struct FomodFile {
  QString source;
  QString destination;
  bool hasDestination = false;
  int priority = 0;
  bool isFolder = false;
};

struct FomodFlag {
  QString name;
  QString value;
};

struct FomodPlugin {
  QString name;
  int documentIndex = 0;
  std::vector<FomodFile> files;
  std::vector<FomodFlag> flags;
  QString typeDescriptor; // parsed, not used
};

struct FomodGroup {
  QString name;
  QString type;
  std::vector<FomodPlugin> plugins;
};

struct FomodStep {
  QString name;
  std::vector<FomodGroup> groups;
};

struct FomodPattern {
  FomodCondition condition;
  std::vector<FomodFile> files;
};

struct FomodModel {
  std::vector<FomodFile> requiredInstallFiles;
  std::vector<FomodStep> steps;
  std::vector<FomodPattern> conditionalFileInstalls;
  static std::optional<FomodModel> parse(const QByteArray &xml, QString &error);
};

QString decodeFomodXml(
    const QByteArray &bytes); // UTF-16 BOM, UTF-8 BOM, UTF-8, cp1252, Latin-1
                              // fallback; strips a mismatching declaration

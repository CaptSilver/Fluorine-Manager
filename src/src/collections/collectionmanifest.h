#pragma once

#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QtGlobal>

#include <optional>
#include <vector>

struct CollectionModSource
{
  QString type;
  int modId  = 0;
  int fileId = 0;
  QString md5;          // empty when absent
  qint64 fileSize = 0;  // 0 when absent
  QString logicalFilename;
};

struct CollectionMod
{
  QString name;
  QString version;
  bool optional = false;
  int phase     = 0;
  CollectionModSource source;
  QString choicesType;  // empty when no choices; "fomod" when the choices lack "type"
  // Kept raw: {"type", "options":[{name, groups:[{name, choices:[{name, idx}]}]}]}
  QJsonObject choices;
  // Non-empty: why this mod cannot be installed here. The mod stays in the list
  // so it reaches the results dialog instead of ending the run.
  QString unsupported;
};

struct CollectionInfo
{
  QString name;
  QString domainName;
  QString author;
  QStringList gameVersions;
};

struct CollectionManifest
{
  CollectionInfo info;
  bool recommendNewProfile = false;
  std::vector<CollectionMod> mods;
  // Those of modRules, loadOrder, externalResources and tools that are present
  // and non-empty. Fluorine ignores them; the review dialog says so.
  QStringList unhandledSections;

  bool hasUnhandled() const { return !unhandledSections.isEmpty(); }

  // A document that is not an object, has no info object or no mods array is an
  // error (reported in `error`). Everything else odd about a single mod is
  // recorded on that mod.
  static std::optional<CollectionManifest> parse(const QByteArray& json, QString& error);
};

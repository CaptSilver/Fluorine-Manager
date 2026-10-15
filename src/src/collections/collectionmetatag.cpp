#include "collectionmetatag.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSettings>
#include <QVariant>

#include <algorithm>

namespace
{
QString ownersPath()
{
  return QStringLiteral("Plugins/%1/%2")
      .arg(CollectionMetaTag::PluginName, CollectionMetaTag::OwnersKey);
}

std::set<std::pair<int, int>> installedFilesOf(QSettings& settings)
{
  std::set<std::pair<int, int>> files;
  const int count = settings.beginReadArray(QStringLiteral("installedFiles"));
  for (int i = 0; i < count; ++i) {
    settings.setArrayIndex(i);
    files.insert({settings.value(QStringLiteral("modid")).toInt(),
                  settings.value(QStringLiteral("fileid")).toInt()});
  }
  settings.endArray();
  return files;
}
}  // namespace

namespace CollectionMetaTag
{
QString storedString(const QVariant& value)
{
  if (value.metaType().id() == QMetaType::QStringList) {
    return value.toStringList().join(QLatin1Char(','));
  }
  return value.toString();
}

QString encodeOwners(const std::vector<CollectionOwner>& owners)
{
  QJsonArray array;
  for (const CollectionOwner& owner : owners) {
    array.append(QJsonObject{{QStringLiteral("slug"), owner.slug},
                             {QStringLiteral("revision"), owner.revision},
                             {QStringLiteral("modId"), owner.modId},
                             {QStringLiteral("fileId"), owner.fileId}});
  }
  return QString::fromUtf8(QJsonDocument(array).toJson(QJsonDocument::Compact));
}

std::vector<CollectionOwner> decodeOwners(const QString& encoded)
{
  QJsonParseError parseError;
  const QJsonDocument doc = QJsonDocument::fromJson(encoded.toUtf8(), &parseError);
  if (parseError.error != QJsonParseError::NoError || !doc.isArray()) {
    return {};
  }

  std::vector<CollectionOwner> owners;
  for (const QJsonValue& value : doc.array()) {
    const QJsonObject object = value.toObject();
    CollectionOwner owner;
    owner.slug     = object.value(QStringLiteral("slug")).toString();
    owner.revision = object.value(QStringLiteral("revision")).toInt();
    owner.modId    = object.value(QStringLiteral("modId")).toInt();
    owner.fileId   = object.value(QStringLiteral("fileId")).toInt();
    if (!owner.slug.isEmpty()) {
      owners.push_back(owner);
    }
  }
  return owners;
}

std::vector<CollectionOwner> withOwner(std::vector<CollectionOwner> owners,
                                       const CollectionOwner& owner)
{
  const auto same = std::find_if(owners.begin(), owners.end(), [&](const auto& existing) {
    return existing.slug == owner.slug;
  });
  if (same == owners.end()) {
    owners.push_back(owner);
  } else {
    *same = owner;
  }
  return owners;
}

std::vector<CollectionOwner> readOwners(const QString& metaIniPath)
{
  QSettings settings(metaIniPath, QSettings::IniFormat);
  return decodeOwners(storedString(settings.value(ownersPath())));
}

std::set<std::pair<int, int>> readInstalledFiles(const QString& metaIniPath)
{
  QSettings settings(metaIniPath, QSettings::IniFormat);
  return installedFilesOf(settings);
}

bool write(const QString& metaIniPath, const std::vector<CollectionOwner>& owners,
           int modId, int fileId)
{
  QSettings settings(metaIniPath, QSettings::IniFormat);

  auto files = installedFilesOf(settings);
  files.insert({modId, fileId});

  // remove() takes the size key and every numbered entry with it
  settings.remove(QStringLiteral("installedFiles"));
  settings.beginWriteArray(QStringLiteral("installedFiles"), static_cast<int>(files.size()));
  int index = 0;
  for (const auto& [mod, file] : files) {
    settings.setArrayIndex(index++);
    settings.setValue(QStringLiteral("modid"), mod);
    settings.setValue(QStringLiteral("fileid"), file);
  }
  settings.endArray();

  if (owners.empty()) {
    settings.remove(ownersPath());
  } else {
    settings.setValue(ownersPath(), encodeOwners(owners));
  }

  settings.sync();
  return settings.status() == QSettings::NoError;
}
}  // namespace CollectionMetaTag

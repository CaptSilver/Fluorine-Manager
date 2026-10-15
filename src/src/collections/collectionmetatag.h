#pragma once

#include <QString>
#include <QVariant>

#include <set>
#include <utility>
#include <vector>

// One collection that owns a mod folder. Slugs compare exactly as stored.
struct CollectionOwner
{
  QString slug;
  int revision = 0;
  int modId    = 0;
  int fileId   = 0;

  bool operator==(const CollectionOwner&) const = default;
};

// The tag lives in a mod's meta.ini as [Plugins] NexusCollections\owners, one
// flat key holding a compact JSON array. It has to be flat: ModInfoRegular
// keeps only the childKeys() of each Plugins/<plugin> group and rewrites the
// whole Plugins group on save, so nested keys would be erased the first time the
// user edits the mod. The same file also gets the installedFiles array that
// ModInfoRegular reads (and that the command-line installer wrote).
namespace CollectionMetaTag
{
inline const QString PluginName = QStringLiteral("NexusCollections");
inline const QString OwnersKey  = QStringLiteral("owners");

// QSettings returns a QStringList for an unquoted value containing commas; this
// joins it back so a hand-edited meta.ini value still decodes.
QString storedString(const QVariant& value);

QString encodeOwners(const std::vector<CollectionOwner>& owners);

// Malformed text gives an empty list; an entry without a slug is dropped.
std::vector<CollectionOwner> decodeOwners(const QString& encoded);

// Replaces the entry with the same slug in place, else appends.
std::vector<CollectionOwner> withOwner(std::vector<CollectionOwner> owners,
                                       const CollectionOwner& owner);

// Neither read creates the file; a missing file reads as empty.
std::vector<CollectionOwner> readOwners(const QString& metaIniPath);
std::set<std::pair<int, int>> readInstalledFiles(const QString& metaIniPath);

// Writes `owners` (the key is removed when the list is empty) and makes sure
// (modId, fileId) is in the installedFiles array next to any pairs already
// there. Other keys in the file are kept. False when the file cannot be saved.
bool write(const QString& metaIniPath, const std::vector<CollectionOwner>& owners,
           int modId, int fileId);
}  // namespace CollectionMetaTag

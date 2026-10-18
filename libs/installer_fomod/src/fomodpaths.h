#ifndef FOMODPATHS_H
#define FOMODPATHS_H

#include <QDir>
#include <QString>

namespace FomodPaths
{

// Convert a FOMOD path to the archive's forward-slash form.
inline QString normalizeArchivePath(QString path)
{
  path.replace("\\", "/");
  path = QDir::cleanPath(path);
  return path == "." ? QString() : path;
}

// A <file> destination ending in a separator names a directory to copy into
// ("textures/terrain/tamriel/" keeps the file's own name), while one without
// is the new file name. cleanPath() drops the trailing separator, so without
// this the file was installed *as* "tamriel" and replaced the whole directory.
inline QString normalizeFileDestination(const QString& raw)
{
  QString normalized    = normalizeArchivePath(raw);
  const QString trimmed = raw.trimmed();
  if (!normalized.isEmpty() &&
      (trimmed.endsWith(QLatin1Char('/')) || trimmed.endsWith(QLatin1Char('\\')))) {
    normalized += QLatin1Char('/');
  }
  return normalized;
}

}  // namespace FomodPaths

#endif  // FOMODPATHS_H

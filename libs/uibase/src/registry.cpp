/*
This file is part of Mod Organizer.

Mod Organizer is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Mod Organizer is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Mod Organizer.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <uibase/registry.h>
#include <uibase/log.h>
#include <uibase/report.h>
#include <QApplication>
#include <QFile>
#include <QFileInfo>
#include <QList>
#include <QMessageBox>
#include <QString>
#include <QTextStream>

namespace MOBase
{

// Line-by-line INI writer that preserves the file format.
// Unlike QSettings::IniFormat, this does NOT interpret backslashes as
// line continuations, does NOT URL-encode spaces in key names, and does
// NOT reorder keys. It only modifies the target key=value pair and
// leaves everything else untouched.
static bool writeIniValueDirect(const QString& section, const QString& key,
                                const QString& value, const QString& fileName)
{
  QStringList lines;
  bool fileExists = QFileInfo::exists(fileName);

  if (fileExists) {
    QFile file(fileName);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
      return false;
    }
    QTextStream in(&file);
    while (!in.atEnd()) {
      lines.append(in.readLine());
    }
    file.close();
  }

  // Find the target section and key
  QString sectionHeader = "[" + section + "]";
  int sectionStart = -1;
  int sectionEnd   = lines.size();  // end of file if section is last
  int keyLine      = -1;

  for (int i = 0; i < lines.size(); ++i) {
    QString trimmed = lines[i].trimmed();
    if (trimmed.compare(sectionHeader, Qt::CaseInsensitive) == 0) {
      sectionStart = i;
      // Find end of this section (next section header or EOF)
      for (int j = i + 1; j < lines.size(); ++j) {
        QString t = lines[j].trimmed();
        if (t.startsWith('[') && t.endsWith(']')) {
          sectionEnd = j;
          break;
        }
      }
      break;
    }
  }

  if (sectionStart >= 0) {
    // Section found, look for the key within it
    for (int i = sectionStart + 1; i < sectionEnd; ++i) {
      QString trimmed = lines[i].trimmed();
      // Skip comments and empty lines
      if (trimmed.isEmpty() || trimmed.startsWith(';') || trimmed.startsWith('#')) {
        continue;
      }
      int eqPos = trimmed.indexOf('=');
      if (eqPos > 0) {
        QString existingKey = trimmed.left(eqPos).trimmed();
        if (existingKey.compare(key, Qt::CaseInsensitive) == 0) {
          keyLine = i;
          break;
        }
      }
    }

    if (keyLine >= 0) {
      // Key found, replace the line preserving indentation
      QString original = lines[keyLine];
      int eqPos        = original.indexOf('=');
      // Preserve everything up to and including '='
      lines[keyLine] = original.left(eqPos + 1) + value;
    } else {
      // Key not found in section: append it after the section's last entry
      // (like WritePrivateProfileString) so repeated writes keep their order.
      int insertAt = sectionEnd;
      while (insertAt > sectionStart + 1 && lines[insertAt - 1].trimmed().isEmpty()) {
        --insertAt;
      }
      lines.insert(insertAt, key + "=" + value);
    }
  } else {
    // Section not found, append it
    if (!lines.isEmpty() && !lines.last().trimmed().isEmpty()) {
      lines.append("");  // blank line before new section
    }
    lines.append(sectionHeader);
    lines.append(key + "=" + value);
  }

  // Write back
  QFile outFile(fileName);
  if (!outFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
    return false;
  }
  QTextStream out(&outFile);
  for (int i = 0; i < lines.size(); ++i) {
    out << lines[i];
    if (i < lines.size() - 1) {
      out << '\n';
    }
  }
  // Preserve trailing newline if original had one, or add one
  out << '\n';
  outFile.close();

  return true;
}

bool WriteRegistryValue(const QString& appName, const QString& keyName,
                        const QString& value, const QString& fileName)
{
  if (writeIniValueDirect(appName, keyName, value, fileName)) {
    return true;
  }

  // Write failed, check if the file is read-only
  QFileInfo fileInfo(fileName);

  QMessageBox::StandardButton result =
      MOBase::TaskDialog(qApp->activeModalWidget(),
                         QObject::tr("INI file is read-only"))
          .main(QObject::tr("INI file is read-only"))
          .content(QObject::tr("Mod Organizer is attempting to write to \"%1\" "
                               "which is currently set to read-only.")
                       .arg(fileInfo.fileName()))
          .icon(QMessageBox::Warning)
          .button({QObject::tr("Clear the read-only flag"), QMessageBox::Yes})
          .button({QObject::tr("Allow the write once"),
                   QObject::tr("The file will be set to read-only again."),
                   QMessageBox::Ignore})
          .button({QObject::tr("Skip this file"), QMessageBox::No})
          .remember("clearReadOnly", fileInfo.fileName())
          .exec();

  if (result & (QMessageBox::Yes | QMessageBox::Ignore)) {
    // Make the file writable
    QFile file(fileName);
    file.setPermissions(file.permissions() | QFile::WriteUser | QFile::WriteOwner);

    bool ok = writeIniValueDirect(appName, keyName, value, fileName);

    if (result == QMessageBox::Ignore) {
      // Set back to read-only
      file.setPermissions(file.permissions() & ~(QFile::WriteUser | QFile::WriteOwner));
    }

    return ok;
  }

  return false;
}

bool RemoveRegistryValue(const QString& section, const QString& key,
                         const QString& fileName)
{
  if (!QFileInfo::exists(fileName)) {
    return true;  // nothing to remove
  }

  QFile file(fileName);
  if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
    return false;
  }
  QStringList lines;
  QTextStream in(&file);
  while (!in.atEnd()) {
    lines.append(in.readLine());
  }
  file.close();

  const QString sectionHeader = "[" + section + "]";
  int sectionStart = -1;
  int sectionEnd   = lines.size();

  for (int i = 0; i < lines.size(); ++i) {
    QString trimmed = lines[i].trimmed();
    if (trimmed.compare(sectionHeader, Qt::CaseInsensitive) == 0) {
      sectionStart = i;
      for (int j = i + 1; j < lines.size(); ++j) {
        QString t = lines[j].trimmed();
        if (t.startsWith('[') && t.endsWith(']')) {
          sectionEnd = j;
          break;
        }
      }
      break;
    }
  }

  if (sectionStart < 0) {
    return true;  // section not found, nothing to remove
  }

  // Find and remove the key line
  bool found = false;
  for (int i = sectionStart + 1; i < sectionEnd; ++i) {
    QString trimmed = lines[i].trimmed();
    if (trimmed.isEmpty() || trimmed.startsWith(';') || trimmed.startsWith('#')) {
      continue;
    }
    int eqPos = trimmed.indexOf('=');
    if (eqPos > 0) {
      QString existingKey = trimmed.left(eqPos).trimmed();
      if (existingKey.compare(key, Qt::CaseInsensitive) == 0) {
        lines.removeAt(i);
        found = true;
        break;
      }
    }
  }

  if (!found) {
    return true;  // key not found, nothing to remove
  }

  // Write back
  QFile outFile(fileName);
  if (!outFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
    return false;
  }
  QTextStream out(&outFile);
  for (int i = 0; i < lines.size(); ++i) {
    out << lines[i];
    if (i < lines.size() - 1) {
      out << '\n';
    }
  }
  out << '\n';
  outFile.close();

  return true;
}

bool RemoveRegistrySection(const QString& section, const QString& fileName)
{
  if (!QFileInfo::exists(fileName)) {
    return true;  // nothing to remove
  }

  QFile file(fileName);
  if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
    return false;
  }
  QStringList lines;
  QTextStream in(&file);
  while (!in.atEnd()) {
    lines.append(in.readLine());
  }
  file.close();

  const QString sectionHeader = "[" + section + "]";
  bool inSection              = false;
  bool changed                = false;
  QStringList kept;
  for (const QString& line : lines) {
    const QString trimmed = line.trimmed();
    if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
      inSection = trimmed.compare(sectionHeader, Qt::CaseInsensitive) == 0;
    } else if (inSection && !trimmed.isEmpty() && !trimmed.startsWith(';') &&
               !trimmed.startsWith('#') && trimmed.indexOf('=') > 0) {
      changed = true;
      continue;  // drop key=value entries, keep header, comments and blanks
    }
    kept.append(line);
  }

  if (!changed) {
    return true;
  }

  QFile outFile(fileName);
  if (!outFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
    return false;
  }
  QTextStream out(&outFile);
  for (int i = 0; i < kept.size(); ++i) {
    out << kept[i];
    if (i < kept.size() - 1) {
      out << '\n';
    }
  }
  out << '\n';
  outFile.close();

  return true;
}

#ifdef _WIN32
bool WriteRegistryValue(const wchar_t* appName, const wchar_t* keyName,
                        const wchar_t* value, const wchar_t* fileName)
{
  return WriteRegistryValue(
    QString::fromWCharArray(appName),
    QString::fromWCharArray(keyName),
    QString::fromWCharArray(value),
    QString::fromWCharArray(fileName));
}
#endif

}  // namespace MOBase

#include "filehash.h"

#include <QByteArray>
#include <QCryptographicHash>
#include <QFile>

QString md5OfFile(const QString& path)
{
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly)) {
    return QString();
  }

  QCryptographicHash hash(QCryptographicHash::Md5);
  QByteArray buffer(1 << 20, Qt::Uninitialized);
  while (!file.atEnd()) {
    const qint64 count = file.read(buffer.data(), buffer.size());
    if (count < 0) {
      return QString();
    }
    hash.addData(QByteArrayView(buffer.constData(), count));
  }
  return QString::fromLatin1(hash.result().toHex());
}

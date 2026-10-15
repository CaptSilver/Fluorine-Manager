#include "filehashconcurrent.h"

#include <QFutureWatcher>
#include <QtConcurrent/QtConcurrentRun>

ConcurrentFileHasher::ConcurrentFileHasher(QObject *parent) : QObject(parent) {}

void ConcurrentFileHasher::md5(
    const QString &path, std::function<void(const QString &hexOrEmpty)> done) {
  // Parented to this object so a watcher that never finishes is cleaned up with
  // it. A finished future always delivers `finished`, including when the digest
  // is empty.
  auto *watcher = new QFutureWatcher<QString>(this);
  connect(watcher, &QFutureWatcher<QString>::finished, this,
          [watcher, done = std::move(done)] {
            const QString hex = watcher->result();
            watcher->deleteLater();
            done(hex);
          });
  watcher->setFuture(QtConcurrent::run([path] { return md5OfFile(path); }));
}

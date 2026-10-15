#pragma once

#include "filehash.h"

#include <QObject>

// Runs md5OfFile on the global thread pool and calls back on the thread that
// owns this object, from its event loop. The hasher must outlive the jobs it
// started: destroying it first drops their callbacks.
class ConcurrentFileHasher : public QObject, public FileHasher {
  Q_OBJECT

public:
  explicit ConcurrentFileHasher(QObject *parent = nullptr);

  void md5(const QString &path,
           std::function<void(const QString &hexOrEmpty)> done) override;
};

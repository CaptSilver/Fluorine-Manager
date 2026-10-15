#pragma once

#include <QString>

#include <functional>

// MD5 of a file as lowercase hex, read in chunks. Empty when the file cannot be
// read.
QString md5OfFile(const QString &path);

// Hashes a file somewhere other than the caller's thread. `done` is always
// called exactly once, with the hex digest or an empty string when the file
// cannot be read. Tests substitute an implementation that decides when the
// digest arrives.
class FileHasher {
public:
  virtual ~FileHasher() = default;
  virtual void md5(const QString &path,
                   std::function<void(const QString &hexOrEmpty)> done) = 0;
};

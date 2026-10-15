#pragma once

#include <QString>
#include <QtGlobal>

// What the collection downloader needs from the download manager, as a small interface so
// the queue logic can be tested with fakes. downloadmanagerport.{h,cpp} is the real
// adapter.
//
// DownloadManager::DownloadState (downloadmanager.h:72-85) maps as:
//   Ready  = STATE_READY | STATE_INSTALLED | STATE_UNINSTALLED, or STATE_FETCHINGMODINFO |
//            STATE_FETCHINGFILEINFO | STATE_FETCHINGMODINFO_MD5 | STATE_NOFETCH, and in every
//            case getFilePath(i) exists on disk. The archive is final once a transfer ends
//            (downloadmanager.cpp:3170-3176); what the FETCHING states still wait for is
//            metadata, and an exhausted Nexus account can leave a row there for good.
//   Active = STATE_STARTED | STATE_DOWNLOADING | STATE_CANCELING | STATE_PAUSING, and a
//            FETCHING*/NOFETCH row whose file is not on disk
//            (CANCELING and PAUSING resolve to a terminal state the listener sees, so they
//            are waited on)
//   Paused = STATE_PAUSED
//   Failed = STATE_ERROR | STATE_CANCELED
//   Missing = no row with these ids, or a Ready-state row whose file is gone
enum class PortRowState
{
  Missing,
  Active,
  Paused,
  Failed,
  Ready
};

struct PortRow
{
  int index          = -1;
  PortRowState state = PortRowState::Missing;
  QString path;  // Ready only
  qint64 size = 0;
};

class DownloadPortListener
{
public:
  virtual ~DownloadPortListener() = default;
  virtual void transferStarted(int modId, int fileId)                        = 0;
  virtual void downloadCompleted(int modId, int fileId, const QString& path) = 0;
  virtual void downloadFailed(int modId, int fileId, const QString& reason)  = 0;
};

class DownloadPort
{
public:
  virtual ~DownloadPort() = default;
  virtual void setListener(DownloadPortListener* listener)          = 0;
  virtual PortRow find(int modId, int fileId) const                 = 0;
  virtual void submit(const QString& domain, int modId, int fileId) = 0;
  // A pending or active row with these ids exists now.
  virtual bool appeared(int modId, int fileId) const = 0;
  virtual void resume(int index)                     = 0;
  virtual void remove(int index)                     = 0;  // removeDownload(index, true)
};

class NexusBudgetPort
{
public:
  virtual ~NexusBudgetPort() = default;
  virtual int remainingRequests() const = 0;
};

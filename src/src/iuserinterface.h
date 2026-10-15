#ifndef IUSERINTERFACE_H
#define IUSERINTERFACE_H

#include "modinfodialogfwd.h"
#include <QMainWindow>
#include <QMenu>
#include <QSystemTrayIcon>
#include <delayedfilewriter.h>
#include <ipluginmodpage.h>
#include <iplugintool.h>

class IUserInterface
{
public:
  virtual void registerModPage(MOBase::IPluginModPage* modPage) = 0;

  virtual void installTranslator(const QString& name) = 0;

  virtual bool closeWindow()                  = 0;
  virtual void setWindowEnabled(bool enabled) = 0;

  virtual void displayModInformation(ModInfoPtr modInfo, unsigned int modIndex,
                                     ModInfoTabIDs tabID) = 0;

  virtual void updateBSAList(const QStringList& defaultArchives,
                             const QStringList& activeArchives) = 0;

  virtual MOBase::DelayedFileWriterBase& archivesWriter() = 0;

  virtual QMainWindow* mainWindow() = 0;

  virtual void showNotification(const QString& title, const QString& message,
                                QSystemTrayIcon::MessageIcon icon =
                                    QSystemTrayIcon::MessageIcon::Information) = 0;

  // Selects the profile with exactly this name in the profile box, which runs the same
  // handlers as a click on it. Returns false when no profile has that name or the
  // switch did not happen.
  virtual bool selectProfile(const QString& name) = 0;
};

#endif  // IUSERINTERFACE_H

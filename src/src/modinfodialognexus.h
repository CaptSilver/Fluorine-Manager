#ifndef MODINFODIALOGNEXUS_H
#define MODINFODIALOGNEXUS_H

#include "modinfodialogtab.h"

#ifdef MO2_WEBENGINE
#include <QWebEnginePage>

class QWebEngineView;

// Sends link clicks, form submissions and new-window requests to the system
// browser; the description itself is the only thing that loads in-page.
class NexusDescriptionPage : public QWebEnginePage
{
  Q_OBJECT;

public:
  explicit NexusDescriptionPage(QObject* parent = nullptr, bool popup = false);

protected:
  bool acceptNavigationRequest(const QUrl& url, NavigationType type,
                               bool isMainFrame) override;
  QWebEnginePage* createWindow(WebWindowType type) override;

private:
  // A target="_blank" link gets a throwaway page that only exists to receive
  // the navigation and forward it to the system browser.
  bool m_popup;
};
#endif

class NexusTab : public ModInfoDialogTab
{
  Q_OBJECT;

public:
  NexusTab(ModInfoDialogTabContext cx);

  ~NexusTab() override;

  void clear() override;
  void update() override;
  void firstActivation() override;
  void setMod(ModInfoPtr mod, MOShared::FilesOrigin* origin) override;
  bool usesOriginFiles() const override;

private:
  QMetaObject::Connection m_modConnection;
  bool m_requestStarted{false};
  bool m_loading{false};
#ifdef MO2_WEBENGINE
  QWebEngineView* m_webView{nullptr};
#endif

  void setDescription(const QString& html);

  void cleanup();
  void updateVersionColor();
  void updateWebpage();
  void updateTracking();
  void updateMetadata();

  void refreshData(int modID);
  bool tryRefreshData(int modID);
  void onModChanged();

  void onModIDChanged();
  void onSourceGameChanged();
  void onVersionChanged();
  void onCategoryChanged();

  void onRefreshBrowser();
  void onVisitNexus();
  void onEndorse();
  void onTrack();

  void onCustomURLToggled();
  void onCustomURLChanged();
  void onVisitCustomURL();
};

#endif  // MODINFODIALOGNEXUS_H

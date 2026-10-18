#ifndef UIBASE_BETHESDAPLUGIN_H
#define UIBASE_BETHESDAPLUGIN_H

#include "game_features/gameplugins.h"

#include <cstdint>

namespace MOBase
{

// Shared by the built-in plugin list and the optional BSPlugins panel.
struct BethesdaPluginDisplay
{
  bool light              = false;
  bool medium             = false;
  bool blueprint          = false;
  bool overlay            = false;
  bool respectsExtensions = true;

  static BethesdaPluginDisplay forGame(GamePlugins* game, const QString& gameShortName)
  {
    // Reading and displaying these header flags does not require a game-side
    // loader. Keep display metadata separate from the game's runtime capabilities.
    if (gameShortName.compare("FalloutNV", Qt::CaseInsensitive) == 0 ||
        gameShortName.compare("TTW", Qt::CaseInsensitive) == 0)
      return {true, true, false, true, false};
    if (!game)
      return {};
    return {game->lightPluginsAreSupported(), game->mediumPluginsAreSupported(),
            game->blueprintPluginsAreSupported(), false, true};
  }

  bool operator==(const BethesdaPluginDisplay&) const = default;
};

struct BethesdaPluginFlags
{
  bool master    = false;
  bool light     = false;
  bool medium    = false;
  bool blueprint = false;
  bool overlay   = false;

  static BethesdaPluginFlags fromHeader(std::uint32_t flags,
                                        const BethesdaPluginDisplay& support)
  {
    BethesdaPluginFlags result;
    result.master  = (flags & 0x1) != 0;
    result.overlay = support.overlay && (flags & 0x200) != 0;
    // FNV and Starfield use 0x100 for light; Skyrim/FO4 use 0x200.
    // An overlay takes precedence over the other plugin-space flags.
    if (!result.overlay) {
      const auto lightFlag = support.medium || support.overlay ? 0x100 : 0x200;
      result.light         = support.light && (flags & lightFlag) != 0;
      result.medium        = support.medium && (flags & 0x400) != 0;
      result.blueprint     = support.blueprint && (flags & 0x800) != 0;
    }
    return result;
  }
};

inline bool isBethesdaLight(bool lightFlag, bool lightExtension,
                            bool respectsExtensions, bool overlay)
{
  return !overlay && (lightFlag || (respectsExtensions && lightExtension));
}

// Index assignment is independent of load-order priority. Overlays still have a
// priority, but do not advance any of the regular, medium or light counters.
class BethesdaPluginIndexes
{
public:
  QString next(bool enabled, bool overlay, bool medium, bool light)
  {
    if (!enabled)
      return {};
    if (overlay)
      return QStringLiteral("XX");
    if (medium) {
      const int index = m_Medium++;
      return QStringLiteral("%1:%2")
          .arg(0xFD + index / 256, 2, 16, QChar('0'))
          .arg(index % 256, 2, 16, QChar('0'))
          .toUpper();
    }
    if (light) {
      const int index = m_Light++;
      return QStringLiteral("%1:%2")
          .arg(0xFE + index / 4096, 2, 16, QChar('0'))
          .arg(index % 4096, 3, 16, QChar('0'))
          .toUpper();
    }
    return QStringLiteral("%1").arg(m_Regular++, 2, 16, QChar('0')).toUpper();
  }

private:
  int m_Regular = 0;
  int m_Medium  = 0;
  int m_Light   = 0;
};

}  // namespace MOBase

#endif

#include "ui.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "board.h"
#include "font.h"

#if __has_include("config.h")
#include "config.h"
#else
#error "Copy firmware/include/config.example.h to firmware/include/config.h before building."
#endif

namespace {

constexpr int kWidth = 320;
constexpr int kHeight = 170;
constexpr int kPad = 14;
constexpr int kRight = 306;
constexpr int kStatusY = 8;
constexpr int kDotCx = 20;
constexpr int kDotCy = 17;
constexpr int kLabel1Y = 36;
constexpr int kBar1Y = 56;
constexpr int kBarH = 12;
constexpr int kSub1Y = 72;
constexpr int kLabel2Y = 92;
constexpr int kBar2Y = 112;
constexpr int kSub2Y = 128;
constexpr int kFootY = 148;

DeskDisplay tft;
LGFX_Sprite canvas(&tft);
bool spriteReady = false;
bool flashPending = false;
bool sawRunning = false;

uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return static_cast<uint16_t>(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

const uint16_t kBg = rgb(11, 18, 32);
const uint16_t kOrange = rgb(245, 165, 36);
const uint16_t kGreen = rgb(61, 220, 106);
const uint16_t kAmber = rgb(230, 178, 60);
const uint16_t kRed = rgb(255, 93, 93);
const uint16_t kTrack = rgb(44, 53, 72);
const uint16_t kText = rgb(230, 234, 242);
const uint16_t kDim = rgb(139, 149, 168);
const uint16_t kClock = rgb(154, 163, 181);
const uint16_t kWhite = rgb(255, 255, 255);
const uint16_t kBlue = rgb(76, 154, 255);

uint16_t statusColor(const char* name) {
  if (name && strcmp(name, "green") == 0) return kGreen;
  if (name && strcmp(name, "red") == 0) return kRed;
  if (name && strcmp(name, "orange") == 0) return kOrange;
  return kClock;
}

uint16_t barColor(float percent) {
  if (percent >= 85.0f) return kRed;
  if (percent >= 55.0f) return kAmber;
  return kGreen;
}

long unixNow() {
  if (!gState.everSynced) return 0;
  return gState.unixBase + static_cast<long>((millis() - gState.syncMillis) / 1000UL);
}

void formatClock(char* out, size_t size) {
  if (!gState.everSynced) {
    strlcpy(out, "--:--", size);
    return;
  }
  time_t shifted = static_cast<time_t>(unixNow() + gState.tzOffset);
  struct tm parts {};
  gmtime_r(&shifted, &parts);
  snprintf(out, size, "%02d:%02d", parts.tm_hour, parts.tm_min);
}

int shownElapsed() {
  if (gState.running && gState.turnStartedAt > 0 && gState.everSynced) {
    const long now = unixNow();
    if (now >= gState.turnStartedAt) return static_cast<int>(now - gState.turnStartedAt);
  }
  return gState.elapsedSeconds < 0 ? 0 : gState.elapsedSeconds;
}

void formatElapsed(char* out, size_t size, int seconds) {
  if (seconds < 0) seconds = 0;
  const int hours = seconds / 3600;
  const int minutes = (seconds % 3600) / 60;
  const int secs = seconds % 60;
  if (hours > 0) snprintf(out, size, "%d小时%d分", hours, minutes);
  else if (minutes > 0) snprintf(out, size, "%d分%d秒", minutes, secs);
  else snprintf(out, size, "%d秒", secs);
}

void formatAgo(char* out, size_t size) {
  const uint32_t seconds = (millis() - gState.syncMillis) / 1000UL;
  if (seconds < 45) {
    strlcpy(out, "刚刚", size);
    return;
  }
  if (seconds < 3600) {
    snprintf(out, size, "%u 分钟前", static_cast<unsigned>(seconds / 60));
    return;
  }
  if (seconds < 86400) {
    snprintf(out, size, "%u 小时前", static_cast<unsigned>(seconds / 3600));
    return;
  }
  snprintf(out, size, "%u 天前", static_cast<unsigned>(seconds / 86400));
}

void composeFooter(char* out, size_t size) {
  if (gState.link == LINK_WIFI) {
    strlcpy(out, "请检查无线网络", size);
    return;
  }
  if (gState.link == LINK_SEARCH) {
    strlcpy(out, "正在查找电脑", size);
    return;
  }
  if ((gState.link == LINK_DOWN || gState.link == LINK_STALE) && gState.everSynced) {
    char ago[24];
    formatAgo(ago, sizeof(ago));
    if (gState.project[0]) snprintf(out, size, "%s · 上次更新 %s", gState.project, ago);
    else snprintf(out, size, "上次更新 %s", ago);
    return;
  }
  if (gState.link == LINK_DOWN) {
    strlcpy(out, "请检查电脑上的服务", size);
    return;
  }
  if (!gState.hasSession) {
    strlcpy(out, "还没有会话", size);
    return;
  }
  char elapsed[24];
  formatElapsed(elapsed, sizeof(elapsed), shownElapsed());
  const char* project = gState.project[0] ? gState.project : "Codex";
  snprintf(out, size, "%s · 本轮已运行 %s", project, elapsed);
}

void percentText(char* out, size_t size, float value) {
  if (value < 0) {
    strlcpy(out, "--", size);
    return;
  }
  int shown = static_cast<int>(value + 0.5f);
  if (shown < 0) shown = 0;
  if (shown > 999) shown = 999;
  snprintf(out, size, "%d%%", shown);
}

template <typename Gfx>
void drawBar(Gfx& gfx, int y, float percent) {
  const int width = kRight - kPad;
  gfx.fillRoundRect(kPad, y, width, kBarH, kBarH / 2, kTrack);
  if (percent <= 0) return;
  float clamped = percent;
  if (clamped > 100.0f) clamped = 100.0f;
  int fill = static_cast<int>((width * clamped) / 100.0f + 0.5f);
  if (fill < 2) fill = 2;
  if (fill > width) fill = width;
  const int radius = fill < kBarH ? fill / 2 : kBarH / 2;
  gfx.fillRoundRect(kPad, y, fill, kBarH, radius, barColor(percent));
}

template <typename Gfx>
void paintTo(Gfx& gfx) {
  gfx.fillRect(0, 0, kWidth, kHeight, kBg);

  const char* label = gState.label;
  uint16_t accent = statusColor(gState.color);
  if (gState.link == LINK_WIFI) {
    label = "未连接无线网络";
    accent = kRed;
  } else if (gState.link == LINK_SEARCH) {
    label = "正在查找电脑";
    accent = kOrange;
  } else if (gState.link == LINK_DOWN && !gState.everSynced) {
    label = "无法连接电脑";
    accent = kRed;
  } else if (gState.link == LINK_DOWN || gState.link == LINK_STALE) {
    label = "连接中断";
    accent = kRed;
  }

  gfx.fillCircle(kDotCx, kDotCy, 4, accent);
  drawTextFit(gfx, FONT_UI, 30, kStatusY, label, accent, kRight - 58, kHeight);
  char clock[8];
  formatClock(clock, sizeof(clock));
  const int clockWidth = textWidth(FONT_UI, clock);
  drawText(gfx, FONT_UI, kRight - clockWidth, kStatusY, clock, kClock, kWidth, kHeight);

  const bool showMeters = gState.link == LINK_OK || gState.everSynced;
  const float weekly = showMeters ? gState.weekly : -1;
  const float context = showMeters ? gState.context : -1;
  const char* weeklySub = showMeters && gState.weeklySub[0] ? gState.weeklySub : "暂无数据";
  const char* contextSub = showMeters && gState.contextSub[0] ? gState.contextSub : "暂无数据";

  drawText(gfx, FONT_UI, kPad, kLabel1Y, "本周额度", kText, kWidth, kHeight);
  char pct[8];
  percentText(pct, sizeof(pct), weekly);
  const int pctY = kLabel1Y + (FONT_UI.lineHeight - FONT_NUM.lineHeight) / 2;
  drawText(gfx, FONT_NUM, kRight - textWidth(FONT_NUM, pct), pctY, pct, kWhite, kWidth, kHeight);
  drawBar(gfx, kBar1Y, weekly);
  drawTextFit(gfx, FONT_SUB, kPad, kSub1Y, weeklySub, kDim, kRight, kHeight);

  drawText(gfx, FONT_UI, kPad, kLabel2Y, "上下文", kText, kWidth, kHeight);
  percentText(pct, sizeof(pct), context);
  drawText(gfx, FONT_NUM, kRight - textWidth(FONT_NUM, pct), kLabel2Y + (FONT_UI.lineHeight - FONT_NUM.lineHeight) / 2, pct, kWhite, kWidth, kHeight);
  drawBar(gfx, kBar2Y, context);
  drawTextFit(gfx, FONT_SUB, kPad, kSub2Y, contextSub, kDim, kRight, kHeight);

  char footer[96];
  composeFooter(footer, sizeof(footer));
  drawTextFit(gfx, FONT_UI, kPad, kFootY, footer, kBlue, kRight, kHeight);
}

}  // namespace

DeskState gState;

void uiBegin() {
  tft.beginDesk(DISPLAY_ROTATION, BACKLIGHT);
  if (tft.width() != kWidth || tft.height() != kHeight) {
    tft.setRotation(DISPLAY_ROTATION);
  }
  canvas.setPsram(true);
  canvas.setColorDepth(16);
  spriteReady = canvas.createSprite(kWidth, kHeight) != nullptr;
  if (!spriteReady) {
    canvas.setPsram(false);
    spriteReady = canvas.createSprite(kWidth, kHeight) != nullptr;
  }
  uiPaint(false);
}

void uiNoteComplete(const char* previousStatus) {
  if (previousStatus && strcmp(previousStatus, "running") == 0) {
    sawRunning = true;
  }
  if (sawRunning && gState.link == LINK_OK && strcmp(gState.status, "complete") == 0) {
    flashPending = true;
    sawRunning = false;
  }
}

void uiPaint(bool) {
  if (flashPending) {
    flashPending = false;
    tft.fillScreen(kWhite);
    delay(90);
    tft.fillScreen(kBg);
    delay(40);
  }
  if (spriteReady) {
    paintTo(canvas);
    canvas.pushSprite(0, 0);
  } else {
    paintTo(tft);
  }
}

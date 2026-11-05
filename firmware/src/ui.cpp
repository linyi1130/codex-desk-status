#include "ui.h"

#include <driver/gpio.h>
#include <esp_sleep.h>

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
// FONT_UI ink reaches 20px below the draw origin and FONT_SUB reaches 16px.
// Bars start 24px under the label origin so they clear that ink and the window title.
constexpr int kStatusY = 2;
constexpr int kDotCx = 20;
constexpr int kDotCy = 14;
constexpr int kLabel1Y = 24;
constexpr int kBar1Y = 48;
constexpr int kBarH = 4;
constexpr int kSub1Y = 56;
constexpr int kLabel2Y = 74;
constexpr int kBar2Y = 98;
constexpr int kSub2Y = 106;
constexpr int kSummaryY = 124;
constexpr int kSummaryStep = 14;
constexpr int kFootY = 150;

DeskDisplay tft;
LGFX_Sprite canvas(&tft);
bool spriteReady = false;
bool flashPending = false;
bool sawRunning = false;
int paintedMinute = -2;

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

int clockMinute() {
  if (!gState.everSynced) return -1;
  time_t shifted = static_cast<time_t>(unixNow() + gState.tzOffset);
  struct tm parts {};
  gmtime_r(&shifted, &parts);
  return parts.tm_hour * 60 + parts.tm_min;
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

uint16_t batteryColor() {
  if (gState.batteryKind != 2) {
    return kClock;
  }
  if (gState.batteryPercent <= 15) {
    return kRed;
  }
  if (gState.batteryPercent <= 40) {
    return kAmber;
  }
  return kGreen;
}

template <typename Gfx>
void drawBatteryIcon(Gfx& gfx, int x, int y, uint16_t color) {
  constexpr int bodyW = 16;
  constexpr int bodyH = 9;
  gfx.drawRoundRect(x, y, bodyW, bodyH, 1, color);
  gfx.fillRect(x + bodyW, y + 2, 2, bodyH - 4, color);
  if (gState.batteryKind != 2 || gState.batteryPercent <= 0) {
    return;
  }
  int fill = (bodyW - 4) * gState.batteryPercent / 100;
  if (fill < 1) {
    fill = 1;
  }
  gfx.fillRect(x + 2, y + 2, fill, bodyH - 4, color);
}

template <typename Gfx>
void drawSummary(Gfx& gfx, const char* text) {
  const int width = kRight - kPad;
  if (textWidth(FONT_SUB, text) <= width) {
    drawText(gfx, FONT_SUB, kPad, kSummaryY, text, kDim, kRight, kHeight);
    return;
  }
  const char* cursor = text;
  const char* split = text;
  int used = 0;
  uint16_t cp = 0;
  while (*cursor) {
    nextCodepoint(cursor, cp);
    const int advance = glyphAdvance(FONT_SUB, cp);
    if (used + advance > width) {
      break;
    }
    used += advance;
    split = cursor;
  }
  if (split == text) {
    drawTextFit(gfx, FONT_SUB, kPad, kSummaryY, text, kDim, kRight, kHeight);
    return;
  }
  char line[160];
  const size_t count = static_cast<size_t>(split - text);
  if (count >= sizeof(line)) {
    drawTextFit(gfx, FONT_SUB, kPad, kSummaryY, text, kDim, kRight, kHeight);
    return;
  }
  memcpy(line, text, count);
  line[count] = 0;
  drawText(gfx, FONT_SUB, kPad, kSummaryY, line, kDim, kRight, kHeight);
  drawTextFit(gfx, FONT_SUB, kPad, kSummaryY + kSummaryStep, split, kDim, kRight, kHeight);
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

  char clock[8];
  formatClock(clock, sizeof(clock));
  const int clockWidth = textWidth(FONT_UI, clock);
  const int clockX = kRight - clockWidth;
  const char* batteryText = gState.batteryText[0] ? gState.batteryText : "--";
  const int batteryTextWidth = textWidth(FONT_SUB, batteryText);
  constexpr int kIconW = 18;
  const int batteryX = clockX - 8 - kIconW - 2 - batteryTextWidth;
  const uint16_t batColor = batteryColor();

  gfx.fillCircle(kDotCx, kDotCy, 4, accent);
  drawTextFit(gfx, FONT_UI, 30, kStatusY, label, accent, batteryX - 6, kHeight);
  drawBatteryIcon(gfx, batteryX, kStatusY + 4, batColor);
  drawText(gfx, FONT_SUB, batteryX + kIconW + 2, kStatusY + 2, batteryText, batColor, kWidth, kHeight);
  drawText(gfx, FONT_UI, clockX, kStatusY, clock, kClock, kWidth, kHeight);

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
  const int pctX = kRight - textWidth(FONT_NUM, pct);
  if (showMeters && gState.windowName[0]) {
    const int nameX = kPad + textWidth(FONT_UI, "上下文") + 6;
    drawTextFit(gfx, FONT_SUB, nameX, kLabel2Y + 3, gState.windowName, kDim, pctX - 6, kHeight);
  }
  drawText(gfx, FONT_NUM, pctX, kLabel2Y + (FONT_UI.lineHeight - FONT_NUM.lineHeight) / 2, pct, kWhite, kWidth, kHeight);
  drawBar(gfx, kBar2Y, context);
  drawTextFit(gfx, FONT_SUB, kPad, kSub2Y, contextSub, kDim, kRight, kHeight);

  if (showMeters && gState.summary[0]) {
    drawSummary(gfx, gState.summary);
  }

  char footer[96];
  composeFooter(footer, sizeof(footer));
  int footerMax = kRight;
  if (gState.link == LINK_OK && gState.pageCount > 1) {
    char badge[8];
    snprintf(badge, sizeof(badge), "%d/%d", gState.pageIndex, gState.pageCount);
    const int badgeWidth = textWidth(FONT_SUB, badge);
    const int badgeX = kRight - badgeWidth;
    drawText(gfx, FONT_SUB, badgeX, kFootY + 2, badge, kClock, kWidth, kHeight);
    footerMax = badgeX - 6;
  }
  drawTextFit(gfx, FONT_UI, kPad, kFootY, footer, kBlue, footerMax, kHeight);
}

}  // namespace

DeskState gState;

void uiBegin() {
  // Deep sleep latches GPIO15 and the backlight. A normal boot is a no-op.
  gpio_deep_sleep_hold_dis();
  gpio_hold_dis(static_cast<gpio_num_t>(PIN_POWER_ON));
  gpio_hold_dis(static_cast<gpio_num_t>(PIN_BACKLIGHT));
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

void uiShutdown() {
  tft.setBrightness(0);
  tft.sleep();
  ledcDetachPin(PIN_BACKLIGHT);
  pinMode(PIN_BACKLIGHT, OUTPUT);
  digitalWrite(PIN_BACKLIGHT, LOW);
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, LOW);
  gpio_set_level(static_cast<gpio_num_t>(PIN_BACKLIGHT), 0);
  gpio_set_level(static_cast<gpio_num_t>(PIN_POWER_ON), 0);
  gpio_hold_en(static_cast<gpio_num_t>(PIN_BACKLIGHT));
  gpio_hold_en(static_cast<gpio_num_t>(PIN_POWER_ON));
  gpio_deep_sleep_hold_en();
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
  paintedMinute = clockMinute();
}

bool uiMinuteChanged() {
  return clockMinute() != paintedMinute;
}

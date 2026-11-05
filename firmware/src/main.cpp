#include <Arduino.h>
#include <WiFi.h>

#include <driver/rtc_io.h>
#include <esp_sleep.h>

#include <stdio.h>
#include <string.h>

#include "battery.h"
#include "board.h"
#include "net.h"
#include "ui.h"

#if __has_include("config.h")
#include "config.h"
#else
#error "Copy firmware/include/config.example.h to firmware/include/config.h before building."
#endif

namespace {

struct DemoFrame {
  LinkState link;
  const char* label;
  const char* color;
  const char* status;
  float weekly;
  const char* weeklySub;
  float context;
  const char* contextSub;
  const char* project;
  int elapsed;
  bool running;
  bool hasSession;
};

// Same scenes the host fixtures produce, plus the offline screens.
const DemoFrame kFrames[] = {
    {LINK_OK, "执行中 · 跑命令", "orange", "running", 16.0f, "约 6 天后重置 · Pro", 73.47f, "349k / 475k tokens",
     "NiceShot", 252, true, true},
    {LINK_OK, "执行中 · 思考中", "orange", "running", 16.0f, "约 6 天后重置 · Pro", 48.0f, "228k / 475k tokens",
     "NiceShot", 18, true, true},
    {LINK_OK, "执行中 · 编辑文件", "orange", "running", 22.0f, "约 6 天后重置 · Pro", 61.0f, "290k / 475k tokens",
     "NiceShot", 75, true, true},
    {LINK_OK, "已完成", "green", "complete", 12.0f, "约 6 天后重置 · Plus", 2.53f, "12k / 475k tokens", "NiceShot",
     90, false, true},
    {LINK_OK, "已中断", "red", "interrupted", 20.0f, "约 6 天后重置 · Pro", 4.23f, "20.1k / 475k tokens", "NiceShot",
     70, false, true},
    {LINK_OK, "额度将尽 · 思考中", "red", "running", 96.0f, "约 6 天后重置 · Pro", 95.16f, "452k / 475k tokens",
     "NiceShot", 66, true, true},
    {LINK_STALE, "执行中 · 跑命令", "orange", "running", 16.0f, "约 6 天后重置 · Pro", 73.47f, "349k / 475k tokens",
     "NiceShot", 252, false, true},
    {LINK_WIFI, "未连接无线网络", "red", "idle", -1, "暂无数据", -1, "暂无数据", "", 0, false, false},
};

uint8_t demoIndex = 0;
uint32_t demoStamp = 0;

struct SleepKey {
  int pin;
  bool seenRelease;
  uint32_t downAt;
};

SleepKey sleepKeys[] = {
    {PIN_BUTTON_BOOT, false, 0},
    {PIN_BUTTON_USER, false, 0},
};

bool anyKeyDown() {
  for (const SleepKey& key : sleepKeys) {
    if (digitalRead(key.pin) == LOW) return true;
  }
  return false;
}

bool consumeSleepPress() {
  const uint32_t now = millis();
  bool ready = false;
  for (SleepKey& key : sleepKeys) {
    const bool down = digitalRead(key.pin) == LOW;
    if (!key.seenRelease) {
      if (!down) key.seenRelease = true;
      continue;
    }
    if (!down) {
      if (key.downAt != 0 && now - key.downAt >= 40) ready = true;
      key.downAt = 0;
      continue;
    }
    if (key.downAt == 0) key.downAt = now;
  }
  // Sleep only after the press is released, and only when both pins are high,
  // so ext1 ANY_LOW does not wake in the same gesture.
  return ready && !anyKeyDown();
}

void armEitherButtonWake() {
  const gpio_num_t pins[] = {GPIO_NUM_0, GPIO_NUM_14};
  for (gpio_num_t pin : pins) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
  }
  const uint64_t mask = (1ULL << GPIO_NUM_0) | (1ULL << GPIO_NUM_14);
  // ESP32-S3 ext1: ANY_LOW wakes if either RTC pin goes low. ext0 can watch only one pin.
  esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
}

void enterDeepSleep() {
  Serial.println("deep sleep");
  Serial.flush();
  uiShutdown();
  if (!DEMO_MODE) {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
  }
  armEitherButtonWake();
  esp_deep_sleep_start();
}

void applyBattery(const BatteryState& reading) {
  gState.batteryKind = reading.kind;
  gState.batteryPercent = reading.percent;
  if (reading.kind == BAT_USB) {
    strlcpy(gState.batteryText, "USB", sizeof(gState.batteryText));
  } else if (reading.kind == BAT_LEVEL && reading.percent >= 0) {
    snprintf(gState.batteryText, sizeof(gState.batteryText), "%d%%", reading.percent);
  } else {
    gState.batteryKind = BAT_NONE;
    gState.batteryPercent = -1;
    strlcpy(gState.batteryText, "无电", sizeof(gState.batteryText));
  }
}

void showDemo(uint8_t index) {
  const DemoFrame& frame = kFrames[index];
  char previous[16];
  strlcpy(previous, gState.status, sizeof(previous));
  gState.link = frame.link;
  strlcpy(gState.label, frame.label, sizeof(gState.label));
  strlcpy(gState.color, frame.color, sizeof(gState.color));
  strlcpy(gState.status, frame.status, sizeof(gState.status));
  gState.weekly = frame.weekly;
  strlcpy(gState.weeklySub, frame.weeklySub, sizeof(gState.weeklySub));
  gState.context = frame.context;
  strlcpy(gState.contextSub, frame.contextSub, sizeof(gState.contextSub));
  strlcpy(gState.project, frame.project, sizeof(gState.project));
  gState.elapsedSeconds = frame.elapsed;
  gState.running = frame.running;
  gState.hasSession = frame.hasSession;
  gState.unixBase = 1793838432;
  gState.tzOffset = 8 * 3600;
  gState.syncMillis = millis();
  gState.everSynced = frame.link != LINK_WIFI;
  gState.turnStartedAt = frame.running ? gState.unixBase - frame.elapsed : 0;
  gState.pageIndex = (index == 0) ? 2 : 1;
  gState.pageCount = (index == 0) ? 4 : 1;
  const char* summary = "";
  const char* windowName = "请查桌面状态";
  if (index == 0) {
    summary = "请把桌面状态改一下";
  } else if (index == 1 || index == 2) {
    summary = "请把桌面状态改一下";
  } else if (index == 3) {
    summary = "请查一下本周额度";
  }
  strlcpy(gState.summary, summary, sizeof(gState.summary));
  strlcpy(gState.windowName, windowName, sizeof(gState.windowName));
  if (frame.link == LINK_WIFI) {
    applyBattery(batteryMake(BAT_USB, -1, 5000));
  } else if (frame.link == LINK_STALE) {
    applyBattery(batteryMake(BAT_NONE, -1, 0));
  } else if (index == 4) {
    applyBattery(batteryMake(BAT_LEVEL, 12, 3480));
  } else {
    applyBattery(batteryMake(BAT_LEVEL, 76, 3950));
  }
  uiNoteComplete(previous);
  uiPaint(false);
  demoStamp = millis();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(50);
  pinMode(PIN_BUTTON_BOOT, INPUT_PULLUP);
  pinMode(PIN_BUTTON_USER, INPUT_PULLUP);
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1) {
    Serial.println("wake");
  }
  batteryBegin();
  if (!DEMO_MODE) {
    applyBattery(batteryRead());
  }
  uiBegin();
  Serial.printf("屏幕 %d x %d\n", 320, 170);
  if (DEMO_MODE) {
    Serial.println("demo");
    showDemo(0);
    return;
  }
  netBegin();
}

void loop() {
  if (consumeSleepPress()) {
    enterDeepSleep();
  }
  if (DEMO_MODE) {
    if (millis() - demoStamp > 4000) {
      demoIndex = static_cast<uint8_t>((demoIndex + 1) % (sizeof(kFrames) / sizeof(kFrames[0])));
      showDemo(demoIndex);
    }
    delay(20);
    return;
  }
  const bool fetched = netLoop(false);
  static uint32_t lastBattery = 0;
  const uint32_t now = millis();
  bool batteryChanged = false;
  if (lastBattery == 0) {
    lastBattery = now;
  } else if (now - lastBattery >= 10000) {
    const int previousKind = gState.batteryKind;
    const int previousPercent = gState.batteryPercent;
    char previousText[8];
    strlcpy(previousText, gState.batteryText, sizeof(previousText));
    applyBattery(batteryRead());
    lastBattery = now;
    batteryChanged = previousKind != gState.batteryKind || previousPercent != gState.batteryPercent ||
                     strcmp(previousText, gState.batteryText) != 0;
  }
  if (fetched || batteryChanged || uiMinuteChanged()) {
    uiPaint(false);
  }
  // The radio stays idle here. Sample both case buttons often enough for a short press.
  for (uint32_t waited = 0; waited < 250; waited += 20) {
    if (consumeSleepPress()) enterDeepSleep();
    delay(20);
  }
}

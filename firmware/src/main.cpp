#include <Arduino.h>

#include <stdio.h>
#include <string.h>

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
bool buttonDown = false;

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
  uiNoteComplete(previous);
  uiPaint(false);
  demoStamp = millis();
}

bool userPressed() {
  const bool down = digitalRead(PIN_BUTTON_USER) == LOW;
  const bool pressed = down && !buttonDown;
  buttonDown = down;
  return pressed;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(50);
  pinMode(PIN_BUTTON_USER, INPUT_PULLUP);
  uiBegin();
  Serial.printf("屏幕 %d x %d\n", 320, 170);
  if (DEMO_MODE) {
    Serial.println("演示模式：侧键切换画面");
    showDemo(0);
    return;
  }
  netBegin();
}

void loop() {
  const bool pressed = userPressed();
  if (DEMO_MODE) {
    if (pressed || millis() - demoStamp > 4000) {
      demoIndex = static_cast<uint8_t>((demoIndex + 1) % (sizeof(kFrames) / sizeof(kFrames[0])));
      showDemo(demoIndex);
    }
    delay(20);
    return;
  }
  netLoop(pressed);
  static uint32_t lastDraw = 0;
  if (millis() - lastDraw >= 1000) {
    uiPaint(false);
    lastDraw = millis();
  }
  delay(20);
}

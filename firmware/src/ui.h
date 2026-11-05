#pragma once

#include <Arduino.h>

enum LinkState : uint8_t {
  LINK_WIFI = 0,
  LINK_SEARCH,
  LINK_DOWN,
  LINK_STALE,
  LINK_OK,
};

struct DeskState {
  LinkState link = LINK_WIFI;
  char label[64] = "正在启动";
  char color[12] = "gray";
  char status[16] = "idle";
  float weekly = -1;
  char weeklySub[48] = "暂无数据";
  float context = -1;
  char contextSub[48] = "暂无数据";
  char project[48] = "";
  int elapsedSeconds = 0;
  bool running = false;
  long turnStartedAt = 0;
  long unixBase = 0;
  long tzOffset = 0;
  uint32_t syncMillis = 0;
  bool everSynced = false;
  bool hasSession = false;
  // 0 = no cell, 1 = USB/charger voltage, 2 = a real pack percentage.
  int batteryKind = 0;
  int batteryPercent = -1;
  char batteryText[8] = "";
  int pageIndex = 1;
  int pageCount = 1;
};

extern DeskState gState;

void uiBegin();
void uiPaint(bool flashFirst);
void uiNoteComplete(const char* previousStatus);

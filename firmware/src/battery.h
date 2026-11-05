#pragma once

#include <stdint.h>

// Classic T-Display-S3 (FCC ID 2ASYE-T-DISPLAYS3) battery sense.
// GPIO4 reads a 100k/100k divider, so the pack voltage is twice the ADC
// voltage. LilyGO's GetBatteryVoltage example treats anything above 4300 mV
// as the TP4056/TP4065 charger rather than a cell: USB with no battery, or
// USB overriding the reading. That must not be shown as a made-up percent.

enum BatteryKind : int8_t {
  BAT_NONE = 0,
  BAT_USB = 1,
  BAT_LEVEL = 2,
};

struct BatteryState {
  BatteryKind kind = BAT_NONE;
  int percent = -1;
  int millivolts = 0;
};

void batteryBegin();
BatteryState batteryRead();

inline BatteryState batteryMake(BatteryKind kind, int percent, int millivolts) {
  BatteryState state;
  state.kind = kind;
  state.percent = percent;
  state.millivolts = millivolts;
  return state;
}

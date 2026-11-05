#include "battery.h"

#include <Arduino.h>

#include "board.h"

namespace {

constexpr int kSamples = 8;
// Charger voltage on the divider. A real 1S cell stays at or below 4.2V.
constexpr int kUsbMillivolts = 4300;
// Open divider sits near 0. A depleted cell is still around 3.0-3.3V.
constexpr int kAbsentMillivolts = 2500;

int percentFromMillivolts(int millivolts) {
  struct Step {
    int mv;
    int pct;
  };
  // Rough 1S LiPo curve, not a straight line from empty to full.
  static constexpr Step kCurve[] = {
      {3300, 0}, {3500, 10}, {3700, 30}, {3800, 50}, {3900, 70}, {4000, 85}, {4100, 95}, {4200, 100},
  };
  if (millivolts <= kCurve[0].mv) {
    return 0;
  }
  constexpr int last = static_cast<int>(sizeof(kCurve) / sizeof(kCurve[0])) - 1;
  if (millivolts >= kCurve[last].mv) {
    return 100;
  }
  for (int i = 1; i <= last; ++i) {
    if (millivolts > kCurve[i].mv) {
      continue;
    }
    const int span = kCurve[i].mv - kCurve[i - 1].mv;
    const int rise = kCurve[i].pct - kCurve[i - 1].pct;
    if (span <= 0) {
      return kCurve[i].pct;
    }
    return kCurve[i - 1].pct + (millivolts - kCurve[i - 1].mv) * rise / span;
  }
  return 100;
}

}  // namespace

void batteryBegin() {
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_BAT_VOLT, ADC_11db);
}

BatteryState batteryRead() {
  BatteryState state;
  uint32_t sum = 0;
  for (int i = 0; i < kSamples; ++i) {
    sum += analogReadMilliVolts(PIN_BAT_VOLT);
  }
  // Divider is one-half, same conversion as LilyGO GetBatteryVoltage.
  state.millivolts = static_cast<int>((sum / kSamples) * 2);
  if (state.millivolts > kUsbMillivolts) {
    state.kind = BAT_USB;
    return state;
  }
  if (state.millivolts < kAbsentMillivolts) {
    state.kind = BAT_NONE;
    return state;
  }
  state.kind = BAT_LEVEL;
  state.percent = percentFromMillivolts(state.millivolts);
  return state;
}

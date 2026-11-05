#pragma once

// Classic LilyGO T-Display-S3, the board in the back-cover photo
// (MODEL:T-Display-S3, FCC ID 2ASYE-T-DISPLAYS3). ST7789, 170x320,
// 8-bit parallel. This is not the AMOLED or Pro variant.
//
// Pins match LilyGO examples/factory/pin_config.h and TFT_eSPI
// User_Setups/Setup206_LilyGo_T_Display_S3.h:
//   BL 38, RST 5, CS 6, DC 7, WR 8, RD 9,
//   D0-D7 = 39, 40, 41, 42, 45, 46, 47, 48,
//   power enable GPIO15, BOOT GPIO0, user button GPIO14.
// The GRAM offset of 35 is the working LovyanGFX setup for this panel
// (panel is 170 wide inside a 240-wide ST7789).
//
// If red and blue are swapped, set cfg.rgb_order = true.
// If the image is a negative, set cfg.invert = false.

#include <LovyanGFX.hpp>

static constexpr int PIN_POWER_ON = 15;
static constexpr int PIN_BUTTON_USER = 14;

class DeskDisplay : public lgfx::LGFX_Device {
 public:
  DeskDisplay() {
    {
      auto cfg = _bus.config();
      cfg.freq_write = 20000000;
      cfg.pin_wr = 8;
      cfg.pin_rd = 9;
      cfg.pin_rs = 7;
      cfg.pin_d0 = 39;
      cfg.pin_d1 = 40;
      cfg.pin_d2 = 41;
      cfg.pin_d3 = 42;
      cfg.pin_d4 = 45;
      cfg.pin_d5 = 46;
      cfg.pin_d6 = 47;
      cfg.pin_d7 = 48;
      _bus.config(cfg);
      _panel.setBus(&_bus);
    }
    {
      auto cfg = _panel.config();
      cfg.pin_cs = 6;
      cfg.pin_rst = 5;
      cfg.pin_busy = -1;
      cfg.memory_width = 240;
      cfg.memory_height = 320;
      cfg.panel_width = 170;
      cfg.panel_height = 320;
      cfg.offset_x = 35;
      cfg.offset_y = 0;
      cfg.offset_rotation = 0;
      cfg.dummy_read_pixel = 8;
      cfg.dummy_read_bits = 1;
      cfg.readable = false;
      cfg.invert = true;
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      cfg.bus_shared = false;
      _panel.config(cfg);
    }
    {
      auto cfg = _light.config();
      cfg.pin_bl = 38;
      cfg.invert = false;
      cfg.freq = 44100;
      cfg.pwm_channel = 0;
      _light.config(cfg);
      _panel.setLight(&_light);
    }
    setPanel(&_panel);
  }

  void beginDesk(uint8_t rotation, uint8_t brightness) {
    pinMode(PIN_POWER_ON, OUTPUT);
    digitalWrite(PIN_POWER_ON, HIGH);
    delay(10);
    init();
    setRotation(rotation);
    setBrightness(brightness);
    fillScreen(0x0000);
  }

 private:
  lgfx::Panel_ST7789 _panel;
  lgfx::Bus_Parallel8 _bus;
  lgfx::Light_PWM _light;
};

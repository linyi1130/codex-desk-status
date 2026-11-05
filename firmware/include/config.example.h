#pragma once

// Copy this file to firmware/include/config.h and edit it.
// config.h is gitignored. Do not commit your SSID or password.

#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-password"

// Hostname the Mac sidecar advertises with dns-sd. No ".local" suffix.
#define MDNS_HOST "codex-status"

// Port used when the board discovers the Mac over mDNS.
// Must match CODEX_STATUS_PORT on the Mac (default 8787).
#define SIDECAR_PORT 8787

// Used only when mDNS does not resolve. Include the /status path.
#define SIDECAR_URL "http://192.168.1.50:8787/status"

// Main chatting window is still running. The board does not rotate helpers.
#define STATUS_POLL_MS 12000
// complete / interrupted / idle / offline / stale. The screen stays on.
#define STATUS_POLL_IDLE_MS 30000
#define WIFI_TIMEOUT_MS 15000

// 1 or 3. If the picture is upside down, switch this.
#define DISPLAY_ROTATION 1

// 0-255. 150 is readable on a desk. Raise this if the panel looks dim.
#define BACKLIGHT 150

// 1 cycles sample screens with no Wi-Fi, so you can check the panel
// before the Mac sidecar is running. Set back to 0 for live data.
#define DEMO_MODE 0

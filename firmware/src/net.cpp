#include "net.h"

#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

#include "ui.h"

#if __has_include("config.h")
#include "config.h"
#else
#error "Copy firmware/include/config.example.h to firmware/include/config.h before building."
#endif

namespace {

// The desk does not poll. If the event stream drops, wait this long, then
// try again, and fetch /status once so the screen is not stuck.
constexpr uint32_t kReconnectMs = 20000;
constexpr uint32_t kReconnectMaxMs = 60000;
constexpr uint32_t kWifiRetryMs = 30000;
constexpr size_t kAccumMax = 3072;

char sidecarHost[64] = "";
uint16_t sidecarPort = SIDECAR_PORT;
char sidecarUrl[160] = SIDECAR_URL;
WiFiClient stream;
bool streamLive = false;
bool headersDone = false;
bool urlFromMdns = false;
bool mdnsReady = false;
uint32_t lastAttempt = 0;
uint32_t reconnectMs = kReconnectMs;
char accum[kAccumMax];
size_t accumLen = 0;

void enableModemSleep() {
  // WIFI_PS_MIN_MODEM is the same mode as WiFi.setSleep(true): beacon
  // wakeups only, association kept. WIFI_PS_MAX_MODEM is not used.
  WiFi.setSleep(WIFI_PS_MIN_MODEM);
}

void copyText(char* dest, size_t size, const char* value) {
  if (!value) value = "";
  strlcpy(dest, value, size);
}

void markLost() {
  gState.link = gState.everSynced ? LINK_STALE : LINK_DOWN;
}

void rememberHost(const char* host, uint16_t port) {
  strlcpy(sidecarHost, host, sizeof(sidecarHost));
  sidecarPort = port == 0 ? SIDECAR_PORT : port;
  snprintf(sidecarUrl, sizeof(sidecarUrl), "http://%s:%u/status", sidecarHost, static_cast<unsigned>(sidecarPort));
}

bool parseFallbackUrl() {
  const char* text = SIDECAR_URL;
  if (strncmp(text, "http://", 7) == 0) text += 7;
  const char* slash = strchr(text, '/');
  const char* colon = strchr(text, ':');
  char host[64];
  uint16_t port = 80;
  if (colon && (slash == nullptr || colon < slash)) {
    size_t length = static_cast<size_t>(colon - text);
    if (length >= sizeof(host)) length = sizeof(host) - 1;
    memcpy(host, text, length);
    host[length] = '\0';
    port = static_cast<uint16_t>(atoi(colon + 1));
  } else {
    size_t length = slash ? static_cast<size_t>(slash - text) : strlen(text);
    if (length >= sizeof(host)) length = sizeof(host) - 1;
    memcpy(host, text, length);
    host[length] = '\0';
  }
  if (!host[0]) return false;
  rememberHost(host, port);
  return true;
}

bool discover() {
  if (!mdnsReady) {
    mdnsReady = MDNS.begin("codex-desk");
    if (!mdnsReady) {
      Serial.println("mDNS 启动失败，改用备用地址");
      urlFromMdns = false;
      return parseFallbackUrl();
    }
  }
  IPAddress host = MDNS.queryHost(MDNS_HOST, 2000);
  if (host != IPAddress(0, 0, 0, 0)) {
    rememberHost(host.toString().c_str(), SIDECAR_PORT);
    urlFromMdns = true;
    Serial.printf("mDNS %s -> %s\n", MDNS_HOST, sidecarHost);
    return true;
  }
  const int found = MDNS.queryService("codexstatus", "tcp");
  if (found > 0) {
    rememberHost(MDNS.IP(0).toString().c_str(), static_cast<uint16_t>(MDNS.port(0)));
    urlFromMdns = true;
    Serial.printf("mDNS service -> %s:%u\n", sidecarHost, static_cast<unsigned>(sidecarPort));
    return true;
  }
  Serial.printf("mDNS 未找到 %s.local，使用备用地址\n", MDNS_HOST);
  urlFromMdns = false;
  return parseFallbackUrl();
}

void applyJson(const String& body) {
  JsonDocument doc;
  if (deserializeJson(doc, body)) {
    Serial.println("状态 JSON 解析失败");
    markLost();
    return;
  }
  char previous[16];
  strlcpy(previous, gState.status, sizeof(previous));
  copyText(gState.label, sizeof(gState.label), doc["label"] | "等待中");
  copyText(gState.color, sizeof(gState.color), doc["color"] | "green");
  copyText(gState.status, sizeof(gState.status), doc["status"] | "idle");
  copyText(gState.project, sizeof(gState.project), doc["project_name"] | "");

  const char* reset = doc["weekly_reset_text"] | "";
  const char* plan = doc["plan_name"] | "";
  if (reset[0] && plan[0]) snprintf(gState.weeklySub, sizeof(gState.weeklySub), "%s · %s", reset, plan);
  else if (reset[0]) copyText(gState.weeklySub, sizeof(gState.weeklySub), reset);
  else if (plan[0]) copyText(gState.weeklySub, sizeof(gState.weeklySub), plan);
  else copyText(gState.weeklySub, sizeof(gState.weeklySub), "暂无数据");

  const char* contextText = doc["context_text"] | "";
  copyText(gState.contextSub, sizeof(gState.contextSub), contextText[0] ? contextText : "暂无数据");

  gState.weekly = doc["weekly_used_percent"].isNull() ? -1.0f : doc["weekly_used_percent"].as<float>();
  const long used = doc["context_used_tokens"].isNull() ? -1 : doc["context_used_tokens"].as<long>();
  const long limit = doc["context_limit_tokens"].isNull() ? -1 : doc["context_limit_tokens"].as<long>();
  if (used >= 0 && limit > 0) gState.context = static_cast<float>(used) * 100.0f / static_cast<float>(limit);
  else if (!doc["context_used_percent"].isNull()) gState.context = doc["context_used_percent"].as<float>();
  else gState.context = -1.0f;

  gState.running = strcmp(gState.status, "running") == 0;
  gState.elapsedSeconds = doc["turn_elapsed_seconds"] | 0;
  gState.turnStartedAt = doc["turn_started_at"].isNull() ? 0 : doc["turn_started_at"].as<long>();
  gState.unixBase = doc["unix"] | 0;
  gState.tzOffset = doc["tz_offset_seconds"] | 0;
  gState.syncMillis = millis();
  gState.everSynced = gState.unixBase > 0;
  gState.hasSession = doc["has_session"] | false;
  int pageIndex = doc["page_index"] | 1;
  int pageCount = doc["page_count"] | 1;
  if (pageIndex < 1) pageIndex = 1;
  if (pageCount < 1) pageCount = 1;
  if (pageIndex > pageCount) pageIndex = pageCount;
  gState.pageIndex = pageIndex;
  gState.pageCount = pageCount;
  const char* summary = doc["summary"] | "";
  copyText(gState.summary, sizeof(gState.summary), summary);
  copyText(gState.windowName, sizeof(gState.windowName), doc["window_name"] | "");
  gState.link = LINK_OK;
  uiNoteComplete(previous);
  Serial.printf("状态 %s  周额度 %.0f%%  上下文 %.0f%%\n", gState.label, gState.weekly, gState.context);
}

bool poll() {
  if (WiFi.status() != WL_CONNECTED || !sidecarHost[0]) {
    gState.link = LINK_WIFI;
    return false;
  }
  HTTPClient http;
  http.setTimeout(4000);
  http.setConnectTimeout(3000);
  if (!http.begin(sidecarUrl)) {
    markLost();
    return false;
  }
  const int code = http.GET();
  if (code == 200) {
    applyJson(http.getString());
    http.end();
    return true;
  }
  Serial.printf("GET %s -> %d\n", sidecarUrl, code);
  http.end();
  markLost();
  return false;
}

void dropStream() {
  streamLive = false;
  headersDone = false;
  accumLen = 0;
  stream.stop();
}

bool connectStream() {
  dropStream();
  if (!sidecarHost[0]) return false;
  stream.setTimeout(3000);
  if (!stream.connect(sidecarHost, sidecarPort, 3000)) {
    Serial.printf("推送连接失败 %s:%u\n", sidecarHost, static_cast<unsigned>(sidecarPort));
    return false;
  }
  stream.setNoDelay(true);
  stream.print("GET /events HTTP/1.1\r\nHost: ");
  stream.print(sidecarHost);
  stream.print("\r\nAccept: text/event-stream\r\nConnection: keep-alive\r\n\r\n");
  streamLive = true;
  enableModemSleep();
  Serial.printf("推送 %s:%u/events\n", sidecarHost, static_cast<unsigned>(sidecarPort));
  return true;
}

bool consumeEvents() {
  bool applied = false;
  while (true) {
    if (!headersDone) {
      char* end = strstr(accum, "\r\n\r\n");
      if (!end) return applied;
      *end = '\0';
      const bool ok = strstr(accum, " 200 ") != nullptr || strncmp(accum, "HTTP/1.1 200", 12) == 0 ||
                      strncmp(accum, "HTTP/1.0 200", 12) == 0;
      const size_t used = static_cast<size_t>(end + 4 - accum);
      const size_t rest = accumLen - used;
      memmove(accum, end + 4, rest);
      accumLen = rest;
      accum[accumLen] = '\0';
      headersDone = true;
      if (!ok) {
        Serial.println("推送握手失败");
        dropStream();
        return applied;
      }
    }
    char* gap = strstr(accum, "\n\n");
    if (!gap) return applied;
    *gap = '\0';
    char* line = accum;
    while (line && *line) {
      char* nl = strchr(line, '\n');
      if (nl) *nl = '\0';
      if (*line == '\r') {
        // blank
      } else if (*line == ':') {
        // keepalive comment; do not redraw
      } else if (strncmp(line, "data:", 5) == 0) {
        const char* json = line + 5;
        if (*json == ' ') json++;
        applyJson(String(json));
        applied = true;
      }
      line = nl ? nl + 1 : nullptr;
    }
    const size_t used = static_cast<size_t>(gap + 2 - accum);
    const size_t rest = accumLen - used;
    memmove(accum, gap + 2, rest);
    accumLen = rest;
    accum[accumLen] = '\0';
  }
}

bool pumpStream() {
  if (!streamLive) return false;
  while (stream.available() > 0 && accumLen + 1 < kAccumMax) {
    accum[accumLen++] = static_cast<char>(stream.read());
    accum[accumLen] = '\0';
  }
  if (accumLen + 1 >= kAccumMax) {
    Serial.println("推送缓冲溢出");
    dropStream();
    markLost();
    return false;
  }
  const bool applied = consumeEvents();
  if (streamLive && !stream.connected() && stream.available() == 0) {
    Serial.println("推送断开");
    dropStream();
    markLost();
  }
  return applied;
}

bool openOrFetch(bool allowFetch) {
  if (!urlFromMdns && !sidecarHost[0]) discover();
  if (connectStream()) {
    reconnectMs = kReconnectMs;
    return false;
  }
  if (urlFromMdns) {
    urlFromMdns = false;
    parseFallbackUrl();
    if (connectStream()) {
      reconnectMs = kReconnectMs;
      return false;
    }
  }
  reconnectMs = reconnectMs * 2;
  if (reconnectMs > kReconnectMaxMs) reconnectMs = kReconnectMaxMs;
  if (!allowFetch) return false;
  return poll();
}

}  // namespace

void netBegin() {
  Serial.printf("连接无线网络 %s\n", WIFI_SSID);
  gState.link = LINK_WIFI;
  copyText(gState.label, sizeof(gState.label), "正在连接无线网络");
  copyText(gState.color, sizeof(gState.color), "orange");
  uiPaint(false);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("codex-desk");
  enableModemSleep();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("无线网络未连接");
    gState.link = LINK_WIFI;
    lastAttempt = millis();
    uiPaint(false);
    return;
  }
  enableModemSleep();
  Serial.printf("已连接 %s\n", WiFi.localIP().toString().c_str());
  gState.link = LINK_SEARCH;
  copyText(gState.label, sizeof(gState.label), "正在查找电脑");
  uiPaint(false);
  discover();
  openOrFetch(true);
  lastAttempt = millis();
  const uint32_t until = millis() + 2500;
  while (streamLive && millis() < until) {
    if (pumpStream()) break;
    delay(20);
  }
  uiPaint(false);
}

bool netLoop(bool force) {
  const uint32_t now = millis();
  static bool modemSleepOn = false;
  if (WiFi.status() != WL_CONNECTED) {
    modemSleepOn = false;
    dropStream();
    const bool changed = gState.link != LINK_WIFI;
    gState.link = LINK_WIFI;
    if (now - lastAttempt > kWifiRetryMs) {
      Serial.println("重试无线网络");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      lastAttempt = now;
      return true;
    }
    return changed;
  }
  if (!modemSleepOn) {
    enableModemSleep();
    modemSleepOn = true;
  }
  const LinkState before = gState.link;
  bool changed = false;
  if (streamLive) {
    changed = pumpStream();
  } else if (force || now - lastAttempt >= reconnectMs) {
    lastAttempt = now;
    if (!sidecarHost[0]) discover();
    changed = openOrFetch(true);
  }
  // markLost() flips gState.link without painting; surface that change here so
  // the "连接中断" screen appears immediately instead of on the next minute tick.
  return changed || gState.link != before;
}

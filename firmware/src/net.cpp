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

char sidecarUrl[160] = SIDECAR_URL;
uint32_t lastPoll = 0;
uint32_t lastDiscover = 0;
bool urlFromMdns = false;
bool mdnsReady = false;

void copyText(char* dest, size_t size, const char* value) {
  if (!value) value = "";
  strlcpy(dest, value, size);
}

void markLost() {
  gState.link = gState.everSynced ? LINK_STALE : LINK_DOWN;
}

bool discover() {
  if (!mdnsReady) {
    mdnsReady = MDNS.begin("codex-desk");
    if (!mdnsReady) {
      Serial.println("mDNS 启动失败，改用备用地址");
      return false;
    }
  }
  IPAddress host = MDNS.queryHost(MDNS_HOST, 2000);
  if (host != IPAddress(0, 0, 0, 0)) {
    snprintf(sidecarUrl, sizeof(sidecarUrl), "http://%s:%d/status", host.toString().c_str(), SIDECAR_PORT);
    urlFromMdns = true;
    Serial.printf("mDNS %s -> %s\n", MDNS_HOST, sidecarUrl);
    return true;
  }
  const int found = MDNS.queryService("codexstatus", "tcp");
  if (found > 0) {
    snprintf(sidecarUrl, sizeof(sidecarUrl), "http://%s:%u/status", MDNS.IP(0).toString().c_str(),
             static_cast<unsigned>(MDNS.port(0)));
    urlFromMdns = true;
    Serial.printf("mDNS service -> %s\n", sidecarUrl);
    return true;
  }
  Serial.printf("mDNS 未找到 %s.local，使用备用地址\n", MDNS_HOST);
  strlcpy(sidecarUrl, SIDECAR_URL, sizeof(sidecarUrl));
  urlFromMdns = false;
  return false;
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
  gState.link = LINK_OK;
  uiNoteComplete(previous);
  Serial.printf("状态 %s  周额度 %.0f%%  上下文 %.0f%%\n", gState.label, gState.weekly, gState.context);
}

bool poll() {
  if (WiFi.status() != WL_CONNECTED) {
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

}  // namespace

void netBegin() {
  Serial.printf("连接无线网络 %s\n", WIFI_SSID);
  gState.link = LINK_WIFI;
  copyText(gState.label, sizeof(gState.label), "正在连接无线网络");
  copyText(gState.color, sizeof(gState.color), "orange");
  uiPaint(false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setHostname("codex-desk");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("无线网络未连接");
    gState.link = LINK_WIFI;
    uiPaint(false);
    return;
  }
  Serial.printf("已连接 %s\n", WiFi.localIP().toString().c_str());
  gState.link = LINK_SEARCH;
  copyText(gState.label, sizeof(gState.label), "正在查找电脑");
  uiPaint(false);
  discover();
  poll();
  lastPoll = millis();
  lastDiscover = lastPoll;
  uiPaint(false);
}

void netLoop(bool force) {
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    gState.link = LINK_WIFI;
    if (now - lastPoll > 8000) {
      Serial.println("重试无线网络");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      lastPoll = now;
    }
    return;
  }
  if (!urlFromMdns && (force || now - lastDiscover > 30000)) {
    discover();
    lastDiscover = now;
  }
  if (force || now - lastPoll >= STATUS_POLL_MS) {
    const bool ok = poll();
    lastPoll = now;
    if (!ok && urlFromMdns) {
      urlFromMdns = false;
    }
  }
}

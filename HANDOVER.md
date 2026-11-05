# 交接文档

> 本次会话时间：2026-11-06
> 提交：`09dbe25`「Fix sleep panel power, disconnection refresh, and host races」
> 状态：已推送到 GitHub（`origin/main`）

## 一、项目概述

Codex 书桌屏：用 LilyGO T-Display-S3（ESP32-S3 + ST7789 8-bit 并行 + LovyanGFX）做一块常亮小屏，通过 Mac 上的边车程序（`host/codex_status.py`）经 SSE 推送 Codex 会话状态显示在屏幕上。按键（GPIO0 / GPIO14）触发深度休眠熄屏，再按唤醒。

## 二、本次完成的工作

共修复 4 个问题，都已提交并验证：

| # | 文件 | 问题 | 性质 |
|---|------|------|------|
| 1 | `host/codex_status.py` | `Service.publish()` 的读-算-写未加锁，poller 线程与 hook POST 线程并发竞态 | 并发 |
| 2 | `firmware/src/net.cpp` | 断连后 `gState.link` 变化未返回给渲染，导致「连接中断」画面不立即刷新 | 逻辑 |
| 3 | `host/parse_session.py` | `WEEKLY_MINUTES` 阈值语义错位（Codex 只发 5h/一周两档窗口） | 语义 |
| 4 | `firmware/src/ui.cpp` | 深度休眠时面板电源 GPIO15 未被真正锁存，第二次熄屏失效 | 硬件/底层 |

## 三、每个修复的根因与改动

### 1. host 并发竞态（codex_status.py）

`publish()` 原先分两段加锁：先锁内取 `hook`/`tz`，解锁后在锁外调用 `engine.payload()`，再重新加锁写回 `self.payload` 并 `gate.consider()`。而 `engine` / `SessionFollower` 的缓存并非线程安全，poller 线程和 hook 的 HTTP handler 线程会同时进入，导致数据竞争。

**改动**：把「读 hook/tz → 计算 payload → 写回 self.payload → gate 判值」整段挪进同一个 `with self.lock` 内，并统一使用同一次 `time.time()`。

### 2. 断连不刷新（net.cpp）

`netLoop()` 内 `markLost()` 会翻转 `gState.link` 但不触发重绘，而 `netLoop()` 的返回值没有把「link 前后不一致」这个变化暴露给外层渲染循环，导致断连画面要等到下一个分钟 tick 才出现。

**改动**：进入时记录 `const LinkState before = gState.link`，返回 `changed || gState.link != before`，让 link 变化立即被上层感知并重绘。

### 3. 周额度阈值语义（parse_session.py）

`WEEKLY_MINUTES = 24 * 60`（一天）语义名不副实：Codex 只产出约 5 小时（300 分钟）和一周（10080，偶发 10079）两档窗口，归类时“一天”这个中间值无法正确把两档分开。

**改动**：重命名为 `DAY_MINUTES` 并加注释，明确其作为「区分 5h 挡与一周挡」的阈值。归类逻辑不变。

### 4. 深度休眠熄屏（ui.cpp）—— 本次核心修复

**根因**：ESP32-S3 上 `GPIO0–20` 是 RTC GPIO，`gpio_hold_en()` 走的是数字 pad hold，其掩码 `GPIO_HOLD_MASK[0..20]` 全为 0（已反汇编 `libsoc.a` 确认）。因此旧代码里 `gpio_hold_en(GPIO15)` 一直是空操作，面板电源 GPIO15 在深度休眠期间从未被锁存、处于浮空。冷启动后第一次能靠寄生电容/板载下拉维持在低电平而熄屏；唤醒后再休眠时浮空回高，面板重新上电，屏幕就“不息屏”。

**改动**：
- `uiShutdown()`：背光 GPIO38（非 RTC 脚）仍走数字 hold + `gpio_deep_sleep_hold_en()`；面板电源 GPIO15 改用 RTC IO 驱动并锁存：
  `rtc_gpio_init → rtc_gpio_set_direction(OUTPUT_ONLY) → rtc_gpio_set_level(0) → rtc_gpio_hold_en`。
- `uiBegin()`：唤醒后先 `rtc_gpio_hold_dis(GPIO15)` + `rtc_gpio_deinit(GPIO15)`，把 GPIO15 交回数字 IO MUX，再由 LovyanGFX 上电点亮。
- 同步清理了 `main.cpp` 里该修复过程的临时诊断打印与 `delay(3000)`。

**验证**：串口日志确认每次按键都真实进入深度休眠且能正常唤醒（无崩溃）；用户目视确认「第 1 下黑屏 → 第 2 下亮屏 → 第 3 下黑屏」全部正常。

## 四、交接状态

- ✅ 4 个修复已提交（commit `09dbe25`）。
- ✅ 已推送到 GitHub：https://github.com/linyi1130/codex-desk-status
- ⚠️ `firmware/platformio.ini` 的 `upload_speed = 115200` 是本地改动，**有意未提交**（板子用 115200 才能稳定烧录）。

## 五、关键约束（后续严格遵守）

1. **不要提交** `host/.env`、`firmware/include/config.h`（含 WiFi 密码 / 局域网 IP，已被 `.gitignore` 覆盖，切勿 `git add -f`）。
2. **不要提交** `firmware/platformio.ini` 的 `upload_speed` 本地修改。
3. **不要** `git push --force` 到 `main`。
4. **不要合并 PR #1**（相关流程未走完）。
5. 烧录命令（板子深度休眠时 USB 会断开，烧录前需先插拔 USB 冷启动）：
   ```
   /Users/linyi/.codex-desk-pio-venv/bin/pio run -t upload --upload-port /dev/cu.usbmodem21101
   ```
   （`cwd = firmware/`，串口监听需带自动重连逻辑）

## 六、遗留 / 待办

- `console` 里可能残留 `/tmp/codex_*.py`、`/tmp/codex_cap*.log` 等临时抓取脚本，非仓库内容，可清理。
- 边车 host 程序需保持运行（此前已重启加载 host 侧修复）。
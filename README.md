# BB-8 Web Console

Drive a Sphero BB-8 directly over BLE from Python, with a local web control panel. No discontinued official app, no Sphero Edu required.

[中文](#中文) · [English](#english)

---

<a id="中文"></a>

## 中文

### 这是什么

`bb8_web.py` 是一个单文件网页控制台，只监听 `127.0.0.1:8765`；`bb8_direct.py` 是可独立运行的命令行版本。两者共用同一套 BLE 连接层。

功能：

- 八方向驾驶，速度和每段时长可调，点一次滚一段后自动停
- 主灯 RGB、蓝色尾灯亮度
- 走正方形、自主巡逻（随机换向 + 加速度计碰撞检测）
- 紧急停止：立刻置中断标志、清空排队指令、停车
- 电源状态：状态档位、电压、累计充电次数
- 充电时间预估：用历史电压做线性回归求 V/h，外推到满电
- 充电指示灯：充电时主灯暗绿慢闪，亮度可调
- 连接状态与实时日志

### 背景

- Sphero 自 2021-10-01 起把老款机器人的配套 App 从 App Store / Google Play 下架，官方也不再支持这些 legacy 产品。BB-8 原来的巡逻、全息消息、语音指令、星战音效等玩法随之消失。
- 官方说明 BB-8 仍可用 Sphero Edu 编程，但那是课堂编程工具，只有基础驾驶和 LED，没有角色功能。
- BB-8 本体**没有扬声器**，所有音效都是手机/平板 App 播放的；同代的 R2-D2 / R2-Q5 才内置喇叭。因此本项目不提供音效，也没有复制任何有版权的星战原声。

### 快速开始

仅支持 macOS（依赖 CoreBluetooth / pyobjc），Python 3.9+：

```bash
python3 -m venv .venv
.venv/bin/pip install bleak spherov2
.venv/bin/python bb8_web.py
# 打开 http://127.0.0.1:8765
```

命令行版：

```bash
.venv/bin/python bb8_direct.py            # 正方形
.venv/bin/python bb8_direct.py --patrol   # 自主巡逻
```

首次连接前，先让系统或手机与 BB-8 配对/连接过一次，并把 BB-8 唤醒。程序会自动发现外设，不需要填任何 UUID。若自动发现不稳定，可用 `BB8_UUID` 指定 macOS 分配的外设 UUID：

```bash
BB8_UUID=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX .venv/bin/python bb8_web.py
```

`BB8_HISTORY_FILE` 可指定电压历史文件路径（默认写入系统临时目录）。

### 文件

| 文件 | 作用 |
| --- | --- |
| `bb8_direct.py` | BLE 连接层 + 命令行程序（正方形 / 巡逻） |
| `bb8_web.py` | HTTP 服务 + 内嵌 HTML/CSS/JS 的单文件网页 |
| `README.md` | 本文档（中英双语） |

### HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 控制页面 |
| GET | `/api/status` | 连接状态、当前模式、电源状态、充电预估、日志 |
| POST | `/api/reconnect` | 重新连接 |
| POST | `/api/stop` | 紧急停止 |
| POST | `/api/led` | `{"r":255,"g":140,"b":0}` 设置主灯 |
| POST | `/api/back_led` | `{"brightness":255}` 蓝色尾灯亮度 |
| POST | `/api/roll` | `{"heading":0,"speed":50,"duration":0.6}` |
| POST | `/api/square` | 走正方形 |
| POST | `/api/patrol` | `{"seconds":20,"speed":50}` 自主巡逻 |
| POST | `/api/power` | 立即刷新电源状态 |
| POST | `/api/charge_led` | `{"on":true,"level":40}` 充电指示灯开关与亮度 |

所有 POST 立即返回并把指令排入队列，由单个 worker 线程串行执行。

### 关键技术点（踩坑记录）

1. **已连接的 BB-8 不再广播，而 bleak 1.x 在 macOS 上连接前必须重新扫描到广播包。** 解决：用 CoreBluetooth 的 `retrieveConnectedPeripheralsWithServices_` / `retrievePeripheralsWithIdentifiers_` 复用系统缓存的 `CBPeripheral`，把它塞进 `BLEDevice.details` 直接交给 `BleakClient`，绕过扫描。自动发现失败时再退回主动广播扫描。

2. **CoreBluetooth 要在主线程预热。** 在后台线程冷启动会出现缓存查询为空、退化成十几秒的扫描；所以服务启动时先在主线程查一次并缓存结果。

3. **空闲后 droid 会睡。** 不发 `wake()` 时第一条指令会等 10 秒超时；连上后、以及每次长空闲后都要先唤醒。

4. **所有 BLE 指令必须串行。** `spherov2` 的 `_execute` 按命令 ID 等待应答，并发写会串台。这里用单个 worker 线程 + 队列，连电量轮询和指示灯闪烁也排队执行。

5. **STOP 不能排队等。** 它立刻置 `threading.Event`、清空队列、单独投一条停车指令；正在跑的正方形 / 巡逻在可打断的 `nap()` 检查点退出，并在 `finally` 里停车。

6. **主灯没有亮度参数。** `set_main_led(r, g, b)` 只有 RGB，"调暗"靠按比例降低通道值（本项目充电指示用 `g=40/255`）。BB-8 也没有 `strobe` 命令，闪烁靠定时切换亮/灭实现。

7. **电源 API 不对称。** `get_power_state()` 可用，返回状态档位、电压、累计充电次数；`get_charger_state()` 在 BB-8 上返回非法枚举值，不能用。

8. **BB-8 不提供电量百分比。** 只能按电压粗估（2S 锂电 6.0–8.4V）。充电时长用最近 30 分钟的电压做最小二乘线性回归，线性外推到 8.4V；锂电进入恒压（CV）阶段后升压变慢，所以这是粗略估计，实际通常更久。电压样本持久化到文件，重启不丢。

9. **碰撞巡逻是"撞了再换向"。** BB-8 没有测距 / 避障传感器，`configure_collision_detection()` 加异步碰撞回调是唯一手段，这也正是原版 App 巡逻模式的做法。回调里只置 Event，不直接下发指令，避免和主循环抢应答队列。

### 安全

- 服务只绑 `127.0.0.1`，不要改成 `0.0.0.0`，也不要直接暴露到公网。
- 巡逻和正方形会真的滚动，且 BB-8 看不出桌沿和楼梯 —— 放在空旷、平坦的地面。
- 页面顶部有常驻 STOP 按钮；调试时把速度和时长调小。

### 脱敏说明

仓库不含任何设备 UUID / MAC 地址、设备序列号、主机名、内网 IP、系统用户名、`system_profiler` / `ioreg` 原始输出，也不含运行时采集的数据。示例里的设备名统一写作 `BB-XXXX`，UUID 写作占位符。代码中唯一的 UUID 常量 `22bb746f-…` 是 Sphero 公开的 GATT 服务 UUID，属于协议规范而非设备标识。

### 限制

- 仅 macOS（CoreBluetooth / pyobjc）；Linux / Windows 需要换成别的连接层。
- 依赖非官方 MIT 库 `spherov2`，它不实现 BB-8 的星战音效、头部动画等角色功能。
- BB-8 是 2015 年的产品，电池老化很常见；官方标称续航约 1 小时。

### 参考

- Sphero legacy products: https://sphero.com/pages/legacy-products
- Sphero Edu programming wiki: https://sites.google.com/sphero.com/sphero-edu-javascript-wiki/
- spherov2 文档: https://spherov2.readthedocs.io/
- spherov2 on PyPI: https://pypi.org/project/spherov2/
- Sphero Edu 音效说明: https://help.sphero.com/sphero-support/using-sounds-in-sphero-edu-programs
- 第三方把玩向 App: https://legacybotcontroller.com/

### License

MIT

---

<a id="english"></a>

## English

### What this is

`bb8_web.py` is a single-file web control panel bound to `127.0.0.1:8765`; `bb8_direct.py` is a standalone CLI. Both share the same BLE connection layer.

Features:

- Eight-direction driving with adjustable speed and per-leg duration; one click rolls for a short stretch then stops
- Main RGB LED and blue tail-light brightness
- Square path and autonomous patrol (random headings + accelerometer collision detection)
- Emergency STOP: sets an interrupt flag immediately, drains the queue, and stops the motors
- Power state: state level, voltage, cumulative charge count
- Charge-time estimate: linear regression over historical voltage to get V/h, extrapolated to full charge
- Charge indicator: dim green slow blink while charging, with adjustable brightness
- Connection status and a live log

### Background

- Since 2021-10-01 Sphero removed the companion apps for its legacy robots from the App Store and Google Play, and no longer supports these products. BB-8's original Patrol, holographic messages, voice commands and Star Wars sound effects disappeared along with them.
- Sphero states that BB-8 can still be programmed with Sphero Edu, but that is a classroom coding tool: basic driving and LEDs only, none of the character features.
- **BB-8 has no speaker.** All sounds were played by the phone/tablet app; the R2-D2 / R2-Q5 from the same generation have built-in speakers. This project therefore ships no sound effects and does not reproduce any copyrighted Star Wars audio.

### Quick start

macOS only (CoreBluetooth / pyobjc), Python 3.9+:

```bash
python3 -m venv .venv
.venv/bin/pip install bleak spherov2
.venv/bin/python bb8_web.py
# open http://127.0.0.1:8765
```

CLI:

```bash
.venv/bin/python bb8_direct.py            # square
.venv/bin/python bb8_direct.py --patrol   # autonomous patrol
```

Before the first connection, pair/connect the BB-8 once from the system or a phone and wake it up. The program discovers the peripheral automatically — no UUID needs to be supplied. If discovery is unreliable, set `BB8_UUID` to the peripheral UUID assigned by macOS:

```bash
BB8_UUID=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX .venv/bin/python bb8_web.py
```

`BB8_HISTORY_FILE` overrides the voltage-history path (defaults to the system temp directory).

### Files

| File | Purpose |
| --- | --- |
| `bb8_direct.py` | BLE connection layer + CLI (square / patrol) |
| `bb8_web.py` | HTTP server + single-file page with embedded HTML/CSS/JS |
| `README.md` | This document (bilingual) |

### HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Control page |
| GET | `/api/status` | Connection, mode, power state, charge estimate, log |
| POST | `/api/reconnect` | Reconnect |
| POST | `/api/stop` | Emergency stop |
| POST | `/api/led` | `{"r":255,"g":140,"b":0}` set main LED |
| POST | `/api/back_led` | `{"brightness":255}` blue tail light |
| POST | `/api/roll` | `{"heading":0,"speed":50,"duration":0.6}` |
| POST | `/api/square` | Drive a square |
| POST | `/api/patrol` | `{"seconds":20,"speed":50}` autonomous patrol |
| POST | `/api/power` | Refresh power state now |
| POST | `/api/charge_led` | `{"on":true,"level":40}` charge indicator on/off and brightness |

Every POST returns immediately and enqueues the command; a single worker thread executes them serially.

### Key technical notes (lessons learned)

1. **A connected BB-8 stops advertising, but bleak 1.x on macOS must see an advertisement before it can connect.** Fix: reuse the cached `CBPeripheral` from CoreBluetooth via `retrieveConnectedPeripheralsWithServices_` / `retrievePeripheralsWithIdentifiers_`, inject it into `BLEDevice.details`, and hand it straight to `BleakClient` to bypass scanning. Fall back to an active advertisement scan if discovery fails.

2. **Warm up CoreBluetooth on the main thread.** A cold start on a background thread returns empty cache lookups and degrades into a ~12-second scan, so the server resolves and caches the peripheral once on the main thread at startup.

3. **The droid sleeps when idle.** Without `wake()`, the first command waits 10 seconds and times out. Wake it after connecting and after long idle periods.

4. **All BLE commands must be serialized.** `spherov2`'s `_execute` waits for a response by command ID; concurrent writes cross-talk. A single worker thread plus a queue is used here — even power polling and the blinking indicator go through that queue.

5. **STOP must not wait in the queue.** It sets a `threading.Event` immediately, drains pending commands, and enqueues one stop command. Running square/patrol loops exit at interruptible `nap()` checkpoints and stop in `finally`.

6. **The main LED has no brightness parameter.** `set_main_led(r, g, b)` is RGB only, so "dim" means scaling the channel values down (this project uses `g=40/255`). BB-8 also has no `strobe` command; blinking is implemented by toggling on/off on a timer.

7. **The power API is asymmetric.** `get_power_state()` works and returns state level, voltage and charge count; `get_charger_state()` returns an invalid enum value on BB-8 and must not be used.

8. **BB-8 does not report a battery percentage.** It can only be estimated from voltage (2S Li-ion/LiPo, 6.0–8.4 V). Charge time uses least-squares regression over the last 30 minutes of voltage samples, extrapolated linearly to 8.4 V. Once the cell enters the constant-voltage (CV) phase the voltage rises more slowly, so this is a rough estimate and the real time is usually longer. Voltage samples are persisted to a file and survive restarts.

9. **Collision patrol means "bump, then turn".** BB-8 has no ranging or obstacle-avoidance sensors; `configure_collision_detection()` plus the async collision callback is the only mechanism — exactly how the original app's Patrol mode worked. The callback only sets an Event; it never issues a command directly, so it cannot race the main loop for responses.

### Safety

- The server binds to `127.0.0.1` only. Do not change it to `0.0.0.0` or expose it to the internet.
- Patrol and square paths make the droid physically roll, and BB-8 cannot see table edges or stairs. Use an open, flat floor.
- A permanent STOP button sits at the top of the page; keep speed and duration low while debugging.

### Sanitization

This repository contains no device UUIDs or MAC addresses, serial numbers, hostnames, private IPs, system usernames, raw `system_profiler` / `ioreg` dumps, or runtime telemetry. Examples use `BB-XXXX` for the device name and placeholders for UUIDs. The only UUID constant in the code, `22bb746f-…`, is Sphero's public GATT service UUID from the protocol spec, not a device identifier.

### Limitations

- macOS only (CoreBluetooth / pyobjc); Linux / Windows need a different transport layer.
- Depends on the unofficial MIT-licensed `spherov2` library, which does not implement BB-8's Star Wars sounds, head animations or other character features.
- BB-8 dates from 2015, so degraded batteries are common; rated runtime is about one hour.

### References

- Sphero legacy products: https://sphero.com/pages/legacy-products
- Sphero Edu programming wiki: https://sites.google.com/sphero.com/sphero-edu-javascript-wiki/
- spherov2 docs: https://spherov2.readthedocs.io/
- spherov2 on PyPI: https://pypi.org/project/spherov2/
- Sphero Edu sound notes: https://help.sphero.com/sphero-support/using-sounds-in-sphero-edu-programs
- Third-party play-oriented app: https://legacybotcontroller.com/

### License

MIT

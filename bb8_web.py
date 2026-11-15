#!/usr/bin/env python3
"""BB-8 本地控制网页（只监听 127.0.0.1，不对外暴露）。

依赖: pip install bleak spherov2
运行: python bb8_web.py
打开: http://127.0.0.1:8765

可选环境变量:
  BB8_UUID          外设 UUID（默认自动发现）
  BB8_HISTORY_FILE  电压历史文件路径（默认系统临时目录）

所有 BLE 指令由单个 worker 线程串行执行；STOP 会立刻置中断标志、
清空排队指令并停车，正在跑的正方形/巡逻会在下个检查点退出。
"""
import json
import os
import queue
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bb8_direct as bb8

HOST, PORT = "127.0.0.1", 8765

# 2S 锂电满电电压，与前端百分比估算的上限保持一致
FULL_VOLTAGE = 8.4
RATE_WINDOW = 1800      # 只用最近 30 分钟的样本做回归
MIN_SAMPLES = 8         # 少于 8 个样本不给预估
MIN_SPAN = 120          # 采样跨度至少 2 分钟
HISTORY_FILE = (os.environ.get("BB8_HISTORY_FILE") or
                os.path.join(tempfile.gettempdir(), "bb8_charge_history.json"))
BLINK_PERIOD = 1.0       # 充电指示灯亮/灭各 1 秒
LED_OVERRIDE_SEC = 15    # 手动设色后临时覆盖充电闪烁的时长


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Droid:
    def __init__(self):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.toy = None
        self.connected = False
        self.mode = "idle"
        self.power = None
        self._hist_lock = threading.RLock()
        self.charge_samples = deque(maxlen=1200)
        self._charge_id = None
        self._load_history()
        self.charge_led_on = True
        self.charge_led_level = 40       # 绿色通道值，远低于 255 = 调暗
        self.last_led = None             # 最近一次写入主灯的 RGB，便于观测闪烁
        self._led_override_until = 0
        self._was_charging = False
        self.log = deque(maxlen=50)
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._power_ticker, daemon=True).start()
        threading.Thread(target=self._charge_led_ticker, daemon=True).start()

    def _charge_led_ticker(self):
        """相位变化时才投一条刷新命令，BLE 写入仍由 worker 串行执行。"""
        last = None
        while True:
            time.sleep(0.25)
            if not self.connected or not self.charge_led_on:
                last = None
                continue
            if not self.power or self.power.get("state") != "CHARGING":
                last = None
                continue
            if time.time() < self._led_override_until:
                continue
            phase = int(time.time() / BLINK_PERIOD) % 2
            if phase == last:
                continue
            last = phase
            if self.q.qsize() < 2:
                self.q.put(("charge_led", {}))

    def _load_history(self):
        with self._hist_lock:
            try:
                with open(HISTORY_FILE) as f:
                    d = json.load(f)
                self._charge_id = d.get("charge_id")
                self.charge_samples = deque(
                    ((float(t), float(v)) for t, v in d.get("samples", [])), maxlen=1200)
            except Exception:
                self._charge_id = None
                self.charge_samples = deque(maxlen=1200)

    def _save_history(self):
        with self._hist_lock:
            try:
                with open(HISTORY_FILE, "w") as f:
                    json.dump({"charge_id": self._charge_id,
                               "samples": list(self.charge_samples)}, f)
            except Exception:
                pass

    def _record_charge(self, p):
        """记录本轮充电的电压样本，供线性回归估算充电速率。"""
        with self._hist_lock:
            if p.get("state") != "CHARGING":
                if self.charge_samples:
                    self.charge_samples.clear()
                    self._charge_id = None
                    self._save_history()
                return
            cid = p.get("charges")
            if self._charge_id is None:
                self._charge_id = cid
            elif cid != self._charge_id:
                self.charge_samples.clear()  # 累计充电次数变了 = 新的一轮充电
                self._charge_id = cid
            v = p.get("voltage")
            if v is None:
                return
            now = time.time()
            if self.charge_samples:
                t0, v0 = self.charge_samples[-1]
                # 电压分辨率 0.01V，5s 一次太密；相同值至少隔 30s 再记
                if v == v0 and now - t0 < 30:
                    return
            self.charge_samples.append((now, v))
            self._save_history()

    def _charge_estimate(self):
        """按最近窗口的电压上升斜率，线性外推到满电所需时间。"""
        if not self.power or self.power.get("state") != "CHARGING":
            return {"ok": False, "reason": "未在充电"}

        now = time.time()
        with self._hist_lock:
            pts = [(t, v) for t, v in self.charge_samples if now - t <= RATE_WINDOW]
        if len(pts) < MIN_SAMPLES:
            return {"ok": False, "reason": "样本积累中",
                    "samples": len(pts), "need": MIN_SAMPLES}
        span = pts[-1][0] - pts[0][0]
        if span < MIN_SPAN:
            return {"ok": False, "reason": "采样时长不足",
                    "samples": len(pts), "span": round(span)}

        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        if den <= 0:
            return {"ok": False, "reason": "采样时长不足"}

        slope = sum((t - mt) * (v - mv) for t, v in pts) / den  # V/s
        rate = slope * 3600                                       # V/h
        v_now = pts[-1][1]
        remaining = FULL_VOLTAGE - v_now
        out = {"ok": True, "rate": round(rate, 3), "voltage": v_now,
               "samples": n, "window_min": round(span / 60, 1),
               "target": FULL_VOLTAGE}

        if remaining <= 0.02:
            out["full"] = True
            out["eta"] = "已接近满电"
            return out
        if rate <= 0.005:
            return {"ok": False, "reason": "电压未稳定上升（可能已充满或读数波动）",
                    "rate": round(rate, 3), "samples": n}

        eta = remaining / slope
        out["eta_sec"] = int(eta)
        h, m = int(eta // 3600), int((eta % 3600) // 60)
        out["eta"] = f"{h} 小时 {m} 分" if h else f"{m} 分"
        out["eta_at"] = time.strftime("%H:%M", time.localtime(now + eta))
        return out

    def _power_ticker(self):
        """每 5s 排一次电源查询，走同一个 worker 队列以串行化 BLE 指令。"""
        time.sleep(2)
        while True:
            time.sleep(5)
            if self.connected and self.q.qsize() < 2:
                self.q.put(("power", {}))

    def _note(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _connect(self):
        name, uuid = bb8.cached_sphero_uuid()
        toy = bb8.BB8(bb8.DeviceInfo(uuid, name), bb8.CachedBleakAdapter)
        toy.__enter__()
        # 空闲后 droid 会睡，不唤醒的话第一条指令会等 10s 超时
        toy.wake()
        time.sleep(0.3)
        self.toy = toy
        self.connected = True
        self._note(f"已连接 {name}")
        self.q.put(("power", {}))  # 连上立刻取一次电源状态
        return toy

    def _disconnect(self):
        if self.toy is not None:
            try:
                self.toy.__exit__(None, None, None)
            except Exception:
                pass
        self.toy = None
        self.connected = False

    def _ensure(self):
        if self.toy is None:
            return self._connect()
        return self.toy

    def _drain(self):
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
            except queue.Empty:
                break

    def status(self):
        return {
            "ok": True,
            "connected": self.connected,
            "mode": self.mode,
            "stopping": self.stop_evt.is_set(),
            "queued": self.q.qsize(),
            "power": self.power,
            "charge": self._charge_estimate(),
            "charge_led": {"on": self.charge_led_on, "level": self.charge_led_level},
            "last_led": self.last_led,
            "log": list(self.log),
        }

    def submit(self, path, data):
        name = {
            "/api/reconnect": "reconnect",
            "/api/led": "led",
            "/api/back_led": "back_led",
            "/api/roll": "roll",
            "/api/square": "square",
            "/api/patrol": "patrol",
            "/api/power": "power",
        }.get(path.split("?")[0])

        if path.split("?")[0] == "/api/stop":
            self.stop_evt.set()
            self._drain()
            self.q.put(("stop", {}))
            self._note("STOP")
            return {"ok": True, "queued": 1}
        if path.split("?")[0] == "/api/charge_led":
            self.charge_led_on = bool(data.get("on", True))
            if "level" in data:
                self.charge_led_level = clamp(int(data["level"]), 0, 120)
            self.q.put(("charge_led", {"force": True}))
            self._note(f"充电指示灯 {'开' if self.charge_led_on else '关'} 亮度 {self.charge_led_level}")
            return {"ok": True, "on": self.charge_led_on,
                    "level": self.charge_led_level}
        if name is None:
            return {"ok": False, "error": f"unknown endpoint {path}"}

        self.q.put((name, data))
        return {"ok": True, "queued": self.q.qsize()}

    def _worker(self):
        while True:
            name, data = self.q.get()
            try:
                with self.lock:
                    self._run(name, data)
            except Exception as e:
                self._note(f"错误 {type(e).__name__}: {e}")
                self._disconnect()
                self.mode = "idle"
            finally:
                self.q.task_done()

    def _run(self, name, data):
        if name == "stop":
            if self.toy is not None:
                try:
                    self.toy.wake()
                    self.toy.roll(0, 0, bb8.RollModes.STOP, bb8.ReverseFlags.OFF)
                except Exception:
                    pass
            self.mode = "idle"
            self.stop_evt.clear()
            self._note("已停车")
            return

        # 电源查询：不碰 stop_evt，异常自己吞掉，避免超时被误判成断线
        if name == "power":
            if self.toy is None:
                return
            try:
                self.toy.wake()
                p = self.toy.get_power_state()
                self.power = {
                    "state": p.state.name,
                    "voltage": round(p.voltage, 2),
                    "charges": p.number_of_charges,
                    "since_charge": p.time_since_last_charge,
                    "ts": time.strftime("%H:%M:%S"),
                }
                self._record_charge(self.power)
                charging = self.power.get("state") == "CHARGING"
                if self._was_charging and not charging and self.charge_led_on:
                    self.toy.set_main_led(0, 0, 0)  # 拔下充电座时熄灭指示灯
                    self.last_led = (0, 0, 0)
                self._was_charging = charging
            except Exception as e:
                self.power = {"error": f"{type(e).__name__}: {e}",
                              "ts": time.strftime("%H:%M:%S")}
            return

        # 充电指示灯：同样不碰 stop_evt，异常自己吞掉
        if name == "charge_led":
            if self.toy is None:
                return
            if not data.get("force") and time.time() < self._led_override_until:
                return
            try:
                self.toy.wake()
                charging = bool(self.power and self.power.get("state") == "CHARGING")
                phase = int(time.time() / BLINK_PERIOD) % 2 == 0
                on = charging and self.charge_led_on and phase
                self.last_led = (0, self.charge_led_level if on else 0, 0)
                self.toy.set_main_led(*self.last_led)
            except Exception:
                pass
            return

        self.stop_evt.clear()
        if name == "reconnect":
            self._disconnect()
            self._ensure()
            return

        t = self._ensure()
        t.wake()  # 长空闲后再操作也要先唤醒
        time.sleep(0.1)
        if name == "led":
            r, g, b = (clamp(int(data.get(k, 0)), 0, 255) for k in ("r", "g", "b"))
            t.set_main_led(r, g, b)
            self.last_led = (r, g, b)
            self._led_override_until = time.time() + LED_OVERRIDE_SEC
            self._note(f"主灯 RGB({r},{g},{b})（临时覆盖充电灯 {LED_OVERRIDE_SEC}s）")
        elif name == "back_led":
            v = clamp(int(data.get("brightness", 255)), 0, 255)
            t.set_back_led_brightness(v)
            self._note(f"尾灯亮度 {v}")
        elif name == "roll":
            heading = clamp(int(data.get("heading", 0)), 0, 359)
            speed = clamp(int(data.get("speed", 50)), 0, 255)
            duration = clamp(float(data.get("duration", 0.6)), 0.1, 5.0)
            self.mode = "roll"
            self._note(f"滚动 heading={heading} speed={speed} {duration}s")
            t.roll(speed, heading, bb8.RollModes.GO, bb8.ReverseFlags.OFF)
            bb8.nap(duration, self.stop_evt)
            t.roll(0, heading, bb8.RollModes.STOP, bb8.ReverseFlags.OFF)
            self.mode = "idle"
        elif name == "square":
            self.mode = "square"
            self._note("开始正方形")
            bb8.roll_square(t, stop_event=self.stop_evt)
            self.mode = "idle"
            self._note("正方形结束")
        elif name == "patrol":
            seconds = clamp(float(data.get("seconds", 20)), 1, 300)
            speed = clamp(int(data.get("speed", 50)), 0, 255)
            self.mode = "patrol"
            self._note(f"开始巡逻 {seconds}s @ {speed}")
            bb8.patrol(t, seconds=seconds, speed=speed, stop_event=self.stop_evt)
            self.mode = "idle"
            self._note("巡逻结束")


droid = Droid()

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BB-8 控制台</title>
<style>
:root{
  --bg:#f4f2ec; --card:#ffffff; --line:#e6e1d6; --txt:#25272c;
  --muted:#8b8b88; --accent:#f5821f; --accent-soft:#fff1e2;
  --accent-dark:#c9640c; --red:#d9383a; --shell:#fbfaf6; --lens:#171a1f;
}
*{box-sizing:border-box}
body{
  margin:0;color:var(--txt);padding:22px 20px 40px;
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
  background:
    radial-gradient(circle at 10% -5%, rgba(245,130,31,.13), transparent 42%),
    radial-gradient(circle at 95% 4%, rgba(245,130,31,.08), transparent 36%),
    var(--bg);
}
.wrap{max-width:820px;margin:0 auto}
.sub{color:var(--muted);font-size:12px}

/* ---- 顶部 BB-8 头像（纯 CSS） ---- */
.hero{display:flex;align-items:center;gap:20px;margin:0 0 20px;padding:4px 6px}
.hero h1{font-size:25px;margin:0;letter-spacing:-.02em}
.hero .sub{margin-top:3px}
.bb8{width:92px;flex:0 0 92px;display:flex;flex-direction:column;align-items:center}
.bb8-head{
  position:relative;width:74px;height:40px;margin-bottom:-7px;z-index:2;overflow:hidden;
  background:linear-gradient(160deg,#ffffff,#eae6dc);
  border:2px solid #ddd8cc;border-bottom:3px solid #d3cec2;
  border-radius:74px 74px 0 0;
  box-shadow:inset -9px -5px 14px rgba(0,0,0,.07);
}
.bb8-head::before{
  content:"";position:absolute;left:0;right:0;top:52%;height:5px;
  background:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12);
}
.bb8-eye{
  position:absolute;right:11px;top:8px;width:26px;height:26px;border-radius:50%;
  background:radial-gradient(circle at 34% 28%,#5b6672,var(--lens) 62%);
  border:3px solid #efede6;box-shadow:0 0 0 1px #c7c2b6,inset 0 0 5px #000;
}
.bb8-eye i{position:absolute;left:5px;top:4px;width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.75)}
.bb8-pip{position:absolute;left:13px;top:17px;width:7px;height:7px;border-radius:50%;background:#b9b4a8;border:1px solid #9c978b}
.bb8-body{
  position:relative;width:84px;height:60px;border-radius:50%;
  background:radial-gradient(circle at 32% 26%,#ffffff,#e6e1d6);
  border:2px solid #ded9cd;
  box-shadow:inset -12px -10px 18px rgba(0,0,0,.09),0 3px 8px rgba(80,60,30,.08);
  display:flex;align-items:center;justify-content:center;gap:9px;
}
.bb8-panel{
  width:20px;height:20px;border-radius:50%;
  background:radial-gradient(circle at 35% 30%,#ffa64d,var(--accent) 65%,var(--accent-dark));
  box-shadow:0 0 0 3px #e9e5db,0 0 0 4px #cfc9bb;
}
.bb8-panel.sm{width:9px;height:9px;box-shadow:0 0 0 2px #e9e5db,0 0 0 3px #cfc9bb}

/* ---- 卡片 / 控件 ---- */
.card{
  position:relative;background:var(--card);border:1px solid var(--line);
  border-top:3px solid var(--accent);border-radius:18px;
  padding:18px;margin-bottom:14px;
  box-shadow:0 3px 14px rgba(80,65,40,.06);
}
.card.stopcard{border-top-color:var(--red)}
h2{font-size:11px;letter-spacing:.12em;color:var(--accent-dark);text-transform:uppercase;margin:0 0 13px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{
  background:#fff;color:var(--txt);border:1px solid var(--line);border-radius:10px;
  padding:9px 15px;cursor:pointer;font-size:14px;
  box-shadow:0 1px 2px rgba(80,65,40,.05);transition:.15s;
}
button:hover{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-dark)}
button:active{transform:translateY(1px)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
button.primary:hover{background:var(--accent-dark);border-color:var(--accent-dark);color:#fff}
button.stop{
  background:var(--red);border-color:var(--red);color:#fff;font-weight:700;
  font-size:17px;padding:15px;width:100%;border-radius:14px;
  box-shadow:0 3px 10px rgba(217,56,58,.28);
}
button.stop:hover{background:#bf2c2e;border-color:#bf2c2e;color:#fff}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#c9c5bb}
.dot.on{background:var(--accent);box-shadow:0 0 0 3px rgba(245,130,31,.2)}
.pad{
  display:grid;grid-template-columns:repeat(3,56px);grid-template-rows:repeat(3,56px);
  gap:8px;justify-content:center;margin:14px 0 6px;
}
.pad button{
  padding:0;border-radius:50%;font-size:19px;color:var(--accent-dark);
  font-weight:600;box-shadow:0 2px 5px rgba(80,65,40,.08);
}
.pad button:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.pad button.center{background:var(--txt);color:#fff;border-color:var(--txt);font-size:12px}
.pad button.center:hover{background:var(--red);border-color:var(--red);color:#fff}
label{font-size:12px;color:var(--muted);display:block;margin-bottom:5px}
input[type=range]{width:100%;accent-color:var(--accent)}
.field{min-width:180px;flex:1}
input[type=color]{width:48px;height:36px;border:1px solid var(--line);border-radius:10px;background:#fff;padding:3px;cursor:pointer}
.sw{width:30px;height:30px;border-radius:50%;border:2px solid #fff;padding:0;box-shadow:0 0 0 1px var(--line)}
.sw:hover{border-color:var(--accent)}
pre{
  background:#faf8f3;border:1px solid var(--line);border-radius:12px;padding:11px;
  height:140px;overflow:auto;font-size:11px;color:#6f7075;margin:0;
}
.hint{color:var(--muted);font-size:11px;margin-top:9px}
.bar{height:11px;background:#efebe2;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:11px}
.fill{height:100%;width:0;background:linear-gradient(90deg,#ffb25e,var(--accent));transition:width .4s,background .4s;border-radius:8px}
.charge{margin-top:11px;padding:9px 12px;border-radius:10px;background:var(--accent-soft);border:1px solid #f7dcbc;font-size:12px;color:var(--accent-dark);font-weight:500}
.switch{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--txt);cursor:pointer;white-space:nowrap}
.switch input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="bb8" aria-hidden="true">
      <div class="bb8-head">
        <span class="bb8-eye"><i></i></span><span class="bb8-pip"></span>
      </div>
      <div class="bb8-body"><span class="bb8-panel"></span><span class="bb8-panel sm"></span></div>
    </div>
    <div>
      <h1>BB-8 控制台</h1>
      <div class="sub">本机 BLE 直连 · 不经过 Sphero Edu App</div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="badge"><span id="dot" class="dot"></span><span id="state">连接中…</span></div>
      <button onclick="post('/api/reconnect')">重新连接</button>
    </div>
    <div class="row" style="margin-top:12px;justify-content:space-between">
      <div class="badge" id="powerText">电源: 读取中…</div>
      <button onclick="post('/api/power')">刷新电量</button>
    </div>
    <div class="bar"><div id="barFill" class="fill"></div></div>
    <div class="charge" id="chargeText">充电预估：等待数据…</div>
    <div class="hint" id="powerHint">BB-8 不提供官方电量百分比；百分比按电压估算（2S 6.0–8.4V），充电时仅供参考。充电时长为线性外推，锂电池进入恒压阶段后升压会变慢，实际通常更久。</div>
  </div>

  <div class="card stopcard"><button class="stop" onclick="post('/api/stop')">紧急停止 / STOP</button></div>

  <div class="card">
    <h2>驾驶</h2>
    <div class="row">
      <div class="field"><label>速度 <span id="spdv">50</span> / 255</label><input id="spd" type="range" min="10" max="120" value="50"></div>
      <div class="field"><label>每段时长 <span id="durv">0.6</span> s</label><input id="dur" type="range" min="0.2" max="2" step="0.1" value="0.6"></div>
    </div>
    <div class="pad">
      <button data-h="315">↖</button><button data-h="0">↑</button><button data-h="45">↗</button>
      <button data-h="270">←</button><button class="center" onclick="post('/api/stop')">停</button><button data-h="90">→</button>
      <button data-h="225">↙</button><button data-h="180">↓</button><button data-h="135">↘</button>
    </div>
    <div class="hint">点一次 = 朝该方向滚一段后自动停。heading 以蓝色尾灯朝向自己为 0°。</div>
  </div>

  <div class="card">
    <h2>灯光</h2>
    <div class="row">
      <input id="color" type="color" value="#ff8c00">
      <button class="primary" onclick="setColor()">设置主灯</button>
      <button class="sw" style="background:#ff8c00" onclick="pick('#ff8c00')"></button>
      <button class="sw" style="background:#ff0000" onclick="pick('#ff0000')"></button>
      <button class="sw" style="background:#00ff00" onclick="pick('#00ff00')"></button>
      <button class="sw" style="background:#0000ff" onclick="pick('#0000ff')"></button>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field"><label>蓝色尾灯亮度 <span id="blv">255</span></label><input id="bl" type="range" min="0" max="255" value="255"></div>
      <button onclick="setBack()">应用</button>
    </div>
    <div class="row" style="margin-top:14px;padding-top:13px;border-top:1px solid var(--line)">
      <label class="switch"><input type="checkbox" id="clOn" checked onchange="setChargeLed()"> 充电时暗绿慢闪</label>
      <div class="field"><label>指示灯亮度 <span id="clv">40</span> / 120</label><input id="cl" type="range" min="0" max="120" value="40" onchange="setChargeLed()"></div>
    </div>
    <div class="hint">仅在检测到充电时生效，亮/灭各 1 秒；手动设色会临时覆盖 15 秒。</div>
  </div>

  <div class="card">
    <h2>动作</h2>
    <div class="row">
      <button onclick="post('/api/square')">走正方形</button>
      <button onclick="startPatrol()">开始巡逻</button>
      <button onclick="post('/api/stop')">停止巡逻</button>
    </div>
    <div class="row" style="margin-top:10px">
      <div class="field"><label>巡逻时长 <span id="ptv">20</span> s</label><input id="pt" type="range" min="5" max="60" step="5" value="20"></div>
    </div>
    <div class="hint">巡逻是随机换向 + 碰撞检测，务必放在空旷、无桌沿 / 楼梯的地面。</div>
  </div>

  <div class="card"><h2>日志</h2><pre id="log"></pre></div>
</div>
<script>
const $ = id => document.getElementById(id);
function bind(id, out){const e=$(id);const u=()=>$(out).textContent=e.value;e.addEventListener('input',u);u();}
bind('spd','spdv'); bind('dur','durv'); bind('bl','blv'); bind('pt','ptv'); bind('cl','clv');
let clSynced=false;
async function post(p,b={}){try{return await (await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}catch(e){return {ok:false,error:String(e)};}}
function pick(hex){$('color').value=hex;setColor();}
function hex2rgb(h){return {r:parseInt(h.slice(1,3),16),g:parseInt(h.slice(3,5),16),b:parseInt(h.slice(5,7),16)};}
function setColor(){post('/api/led',hex2rgb($('color').value));}
function setBack(){post('/api/back_led',{brightness:+$('bl').value});}
function setChargeLed(){post('/api/charge_led',{on:$('clOn').checked,level:+$('cl').value});}
function startPatrol(){post('/api/patrol',{seconds:+$('pt').value,speed:+$('spd').value});}
document.querySelectorAll('[data-h]').forEach(b=>b.addEventListener('click',()=>post('/api/roll',{heading:+b.dataset.h,speed:+$('spd').value,duration:+$('dur').value})));
const STATE_LABEL={CHARGING:'充电中',OK:'电量正常',LOW:'电量低',CRITICAL:'电量极低',UNKNOWN:'未知'};
function renderPower(p){
  if(!p){$('powerText').textContent='电源: 读取中…';return;}
  if(p.error){$('powerText').textContent='电源: 读取失败 ('+p.error+')';$('barFill').style.width='0';return;}
  const label=STATE_LABEL[p.state]||p.state;
  // 2S 锂电典型区间 6.0–8.4V，仅作粗略估算
  const pct=Math.max(0,Math.min(100,Math.round((p.voltage-6.0)/2.4*100)));
  $('powerText').textContent='电源: '+label+' · '+p.voltage.toFixed(2)+' V · 约 '+pct+'% · 已充电 '+p.charges+' 次 ('+p.ts+')';
  const f=$('barFill');
  f.style.width=pct+'%';
  f.style.background=p.state==='CHARGING'?'#4a90d9':(pct<25?'#d9383a':(pct<50?'#f5821f':'#3fa76a'));
}
function renderCharge(c){
  const el=$('chargeText');
  if(!c){el.textContent='充电预估：等待数据…';return;}
  if(c.full){el.textContent='充电预估：'+c.voltage.toFixed(2)+' V，已接近满电 ('+c.target.toFixed(1)+' V)';return;}
  if(!c.ok){
    const extra=c.samples!==undefined?(' · '+c.samples+'/'+(c.need||'?')+' 样本'):'';
    el.textContent='充电预估：'+c.reason+extra+' — 连续采样几分钟后给出';
    return;
  }
  el.textContent='充电预估：+'+c.rate.toFixed(2)+' V/h · 还差 '+(c.target-c.voltage).toFixed(2)+' V'
    +' · 约 '+c.eta+'（'+c.eta_at+' 充满）'
    +' · 最近 '+c.window_min+' 分钟 / '+c.samples+' 样本';
}
async function tick(){
  try{
    const s=await (await fetch('/api/status')).json();
    $('dot').className='dot'+(s.connected?' on':'');
    $('state').textContent=(s.connected?'已连接':'未连接')+' · '+s.mode+(s.stopping?' · 停止中':'');
    renderPower(s.power);
    renderCharge(s.charge);
    if(s.charge_led && !clSynced){
      $('clOn').checked=s.charge_led.on; $('cl').value=s.charge_led.level;
      $('clv').textContent=s.charge_led.level; clSynced=true;
    }
    $('log').textContent=(s.log||[]).join('\n');
    $('log').scrollTop=$('log').scrollHeight;
  }catch(e){}
}
setInterval(tick,1000); tick();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, json.dumps(droid.status(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        result = droid.submit(self.path, data)
        self._send(200, json.dumps(result, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    # 在主线程预热 CoreBluetooth，把 UUID 缓存下来；worker 线程直接用缓存，
    # 否则 CB 在后台线程冷启动会退化成十几秒的扫描。
    try:
        name, uuid = bb8.cached_sphero_uuid()
        print(f"[init] {name}  {uuid}")
    except Exception as e:
        print(f"[init] 预热失败（仍可稍后重试）: {e}")

    droid.submit("/api/reconnect", {})
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BB-8 控制台: http://{HOST}:{PORT}  (Ctrl-C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        droid.submit("/api/stop", {})
        server.server_close()


if __name__ == "__main__":
    main()

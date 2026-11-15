#!/usr/bin/env python3
"""macOS 上用代码直连已配对的 Sphero BB-8（不经过 Sphero Edu App）。

依赖: pip install bleak spherov2
运行:
  python bb8_direct.py             # 默认走正方形
  python bb8_direct.py --patrol    # 自主巡逻
可选环境变量:
  BB8_UUID   macOS 分配的外设 UUID；留空则自动发现（推荐）
注意: 脚本会让 BB-8 真的滚动，先把它放在空旷、平坦、无桌沿、无楼梯的地面。
"""
import argparse
import asyncio
import os
import random
import threading
import time

import bleak
from bleak.backends.device import BLEDevice
from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
from CoreBluetooth import CBCentralManager, CBUUID
from Foundation import NSDate, NSRunLoop, NSUUID

from spherov2.commands.sphero import CollisionDetectionMethods, ReverseFlags, RollModes
from spherov2.toy.bb8 import BB8

SPHERO_SERVICE = "22bb746f-2ba0-7554-2d6f-726568705327"  # Sphero 公开 GATT 服务 UUID

# 不硬编码任何机器专属 UUID。留空时完全靠 CoreBluetooth 自动发现；
# 只有在自动发现不稳定时，才用 BB8_UUID 环境变量覆盖。
DEFAULT_UUID = os.environ.get("BB8_UUID", "").strip() or None
_UUID_CACHE = None  # (name, uuid)，首次发现后缓存，避免每条指令都重新查

# 正方形路径：每条边的速度(0-255)、时长(秒)，以及拐角停顿(秒)
SQUARE_SPEED = 50
SQUARE_LEG = 0.5
CORNER_PAUSE = 0.15

# 自主巡逻：运行时长(秒)、速度(0-255)、每段随机方向与碰撞等待的区间(秒)
PATROL_SECONDS = 30
PATROL_SPEED = 60
PATROL_LEG_MIN = 1.0
PATROL_LEG_MAX = 2.5


class CachedBleakAdapter:
    """接口与 spherov2.adapter.bleak_adapter.BleakAdapter 相同。

    bleak 1.x 在 macOS 上连接前必须重新扫描到广播包；BB-8 已被系统连接时
    不再广播，所以用 CoreBluetooth 复用系统缓存的 CBPeripheral 绕过这一步。
    """

    def __init__(self, address):
        self.__loop = asyncio.new_event_loop()
        self.__lock = threading.Lock()
        self.__thread = threading.Thread(target=self.__loop.run_forever, daemon=True)
        self.__thread.start()
        try:
            self.__client = self.__exec(self.__make_client(address))
        except Exception:
            self.close(False)
            raise

    def __exec(self, coro):
        with self.__lock:
            return asyncio.run_coroutine_threadsafe(coro, self.__loop).result()

    async def __make_client(self, address):
        delegate = CentralManagerDelegate.alloc().init()  # 必须建在本 event loop 内
        mgr = delegate.central_manager
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))

        peripherals = []
        try:
            uid = NSUUID.alloc().initWithUUIDString_(address)
            peripherals = mgr.retrievePeripheralsWithIdentifiers_([uid])
        except Exception:
            pass
        if not peripherals:
            peripherals = mgr.retrieveConnectedPeripheralsWithServices_(
                [CBUUID.UUIDWithString_(SPHERO_SERVICE)]
            )
        if not peripherals:
            # 缓存里没有该外设（例如重新配对后 UUID 变了），退回 bleak 正常扫描连接
            client = bleak.BleakClient(address, timeout=15.0)
            await client.connect()
            print("[ble] connected via scan")
            return client

        p = peripherals[0]
        print(f"[ble] {p.name()}  uuid={p.identifier().UUIDString()}")
        dev = BLEDevice(p.identifier().UUIDString(), p.name(), (p, delegate))
        client = bleak.BleakClient(dev, timeout=15.0)
        await client.connect()
        print("[ble] connected")
        return client

    def set_callback(self, uuid, cb):
        self.__exec(self.__client.start_notify(uuid, cb))

    def write(self, uuid, data):
        self.__exec(self.__client.write_gatt_char(uuid, data, True))

    def close(self, disconnect=True):
        if disconnect:
            self.__exec(self.__client.disconnect())
        with self.__lock:
            self.__loop.call_soon_threadsafe(self.__loop.stop)
            self.__thread.join()
        self.__loop.close()


class DeviceInfo:
    """Toy.__init__ 只读取 .address 和 .name。"""

    def __init__(self, address, name):
        self.address, self.name = address, name


def scan_sphero_uuid():
    """缓存彻底失效时，主动扫广播包拿 UUID（CoreBluetooth 冷启动首次常为空）。"""
    async def _scan():
        for _ in range(2):
            found = await bleak.BleakScanner.discover(timeout=6.0, return_adv=True)
            for addr, (dev, adv) in found.items():
                name = adv.local_name or dev.name or ""
                if name.startswith(("BB-", "GB-")):
                    return name, addr
        return None, None

    return asyncio.run(_scan())


def cached_sphero_uuid():
    """取系统缓存里的 Sphero 外设；冷启动/缓存缺失时退回主动扫描。

    CoreBluetooth 在主线程表现最稳定，调用方应在启动时（主线程）预热一次。
    """
    global _UUID_CACHE
    if _UUID_CACHE is not None:
        return _UUID_CACHE

    mgr = CBCentralManager.alloc().initWithDelegate_queue_(None, None)
    svc = [CBUUID.UUIDWithString_(SPHERO_SERVICE)]
    uid = ([NSUUID.alloc().initWithUUIDString_(DEFAULT_UUID)]
           if DEFAULT_UUID else None)

    for _ in range(3):
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))
        ps = mgr.retrieveConnectedPeripheralsWithServices_(svc)
        if not ps and uid:
            ps = mgr.retrievePeripheralsWithIdentifiers_(uid)
        if ps:
            _UUID_CACHE = (ps[0].name(), ps[0].identifier().UUIDString())
            return _UUID_CACHE

    name, uuid = scan_sphero_uuid()
    if uuid:
        _UUID_CACHE = (name, uuid)
        return _UUID_CACHE
    raise RuntimeError("找不到 BB-8：先唤醒它，或用 BB8_UUID 指定外设 UUID")


def nap(seconds, stop_event=None):
    """可被 stop_event 打断的 sleep；完整睡满返回 True，被中断返回 False。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event is not None and stop_event.is_set():
            return False
        time.sleep(min(0.05, end - time.monotonic()))
    return not (stop_event is not None and stop_event.is_set())


def roll_square(t, stop_event=None):
    """走一个正方形：heading 依次 0/90/180/270，每个拐角先停稳再转向。

    heading 是以 aim（蓝色尾灯朝向自己）为基准的绝对方向，所以四段合起来
    会回到起点附近，而不是越走越偏。
    """
    try:
        for heading in (0, 90, 180, 270):
            if stop_event is not None and stop_event.is_set():
                break
            t.roll(SQUARE_SPEED, heading, RollModes.GO, ReverseFlags.OFF)
            print(f"[sphero] square leg heading={heading} speed={SQUARE_SPEED} {SQUARE_LEG}s")
            if not nap(SQUARE_LEG, stop_event):
                break
            t.roll(0, heading, RollModes.STOP, ReverseFlags.OFF)
            if not nap(CORNER_PAUSE, stop_event):
                break
    finally:
        t.roll(0, 0, RollModes.STOP, ReverseFlags.OFF)
        print("[sphero] stopped")


def patrol(t, seconds=PATROL_SECONDS, speed=PATROL_SPEED, stop_event=None,
           on_collision=None):
    """自主巡逻：随机换向，撞到东西就立刻换方向（复刻原版 App 的做法）。

    BB-8 没有避障传感器，靠加速度计的碰撞事件感知障碍；配置参数沿用
    spherov2 ToyUtil 给 V1 机器人的推荐值。回调里只置 Event，不直接发
    roll 指令，避免和主循环抢同一个应答队列。
    """
    hit = threading.Event()

    def on_collision(c):  # c: CollisionDetected(加速度/轴/速度/时间)
        hit.set()

    t.configure_collision_detection(
        CollisionDetectionMethods.DEFAULT, 90, 130, 90, 130, 1
    )
    t.add_collision_detected_notify_listener(on_collision)
    try:
        end = time.monotonic() + seconds
        stopped = lambda: stop_event is not None and stop_event.is_set()
        while time.monotonic() < end and not stopped():
            heading = random.randrange(0, 360, 15)
            t.roll(speed, heading, RollModes.GO, ReverseFlags.OFF)
            print(f"[patrol] heading={heading} speed={speed}")
            hit.clear()
            leg_end = time.monotonic() + random.uniform(PATROL_LEG_MIN, PATROL_LEG_MAX)
            collided = False
            while time.monotonic() < leg_end and not stopped():
                if hit.wait(min(0.1, max(0, leg_end - time.monotonic()))):
                    collided = True
                    break
            if stopped():
                break
            if collided:
                print("[patrol] collision -> turn")
                if on_collision is not None:
                    on_collision()
    finally:
        t.remove_collision_detected_notify_listener(on_collision)
        t.roll(0, 0, RollModes.STOP, ReverseFlags.OFF)
        print("[patrol] stopped")


def main():
    parser = argparse.ArgumentParser(description="直连 BB-8：默认走正方形，--patrol 走自主巡逻")
    parser.add_argument("--patrol", action="store_true", help="开启自主巡逻（碰撞检测 + 随机换向）")
    args = parser.parse_args()

    name, uuid = cached_sphero_uuid()
    print(f"[scan] {name}  {uuid}")

    toy = BB8(DeviceInfo(uuid, name), CachedBleakAdapter)
    with toy as t:
        print("[sphero] handshake ok")
        for r, g, b in (
            (255, 140, 0),   # BB-8 橙
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 140, 0),
        ):
            t.set_main_led(r, g, b)
            print(f"[sphero] main LED = ({r},{g},{b})")
            time.sleep(0.8)
        t.set_back_led_brightness(255)  # 蓝色瞄准尾灯
        print("[sphero] LED done")

        if args.patrol:
            patrol(t)
        else:
            roll_square(t)
        print("[sphero] done")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""全局鼠标区域触发：点击指定屏幕区域 -> 触发录制开始/停止。

实现：pynput 全局鼠标监听，不依赖焦点窗口。
用户在其他软件（如 fiberphotometry）里点击其"开始"按钮所在区域，
本模块检测到点击坐标落入触发矩形，即发出回调。

注意：需要管理员权限才能全局监听（Windows）。普通权限下只能监听
当前会话内的事件，一般够用。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

# 调试日志（写到 videorec_trigger.log，方便排查触发不工作的问题）
_log = logging.getLogger("trigger")
if not _log.handlers:
    import sys
    from pathlib import Path
    if getattr(sys, "frozen", False):
        _log_path = Path(sys.executable).parent / "videorec_trigger.log"
    else:
        _log_path = Path(__file__).parent.parent / "videorec_trigger.log"
    _h = logging.FileHandler(str(_log_path), encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.DEBUG)

try:
    from pynput import mouse
    HAVE_PYNPUT = True
    _log.info("pynput 加载成功")
except ImportError as e:
    HAVE_PYNPUT = False
    _log.warning("pynput 加载失败: %s", e)


@dataclass
class TriggerRegion:
    """屏幕上的触发区域（以主显示器坐标，左上角原点）。"""

    x: int = 0
    y: int = 0
    w: int = 100
    h: int = 100
    enabled: bool = True

    def contains(self, px: int, py: int) -> bool:
        return self.enabled and (self.x <= px <= self.x + self.w
                                 and self.y <= py <= self.y + self.h)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "TriggerRegion":
        return cls(x=d.get("x", 0), y=d.get("y", 0), w=d.get("w", 100),
                   h=d.get("h", 100), enabled=d.get("enabled", True))


class MouseTrigger:
    """区域点击触发管理器。

    用法：
        trigger = MouseTrigger(region)
        trigger.on_click = lambda: print("开始录制")
        trigger.start()
        ...
        trigger.stop()
    """

    def __init__(self, region: Optional[TriggerRegion] = None):
        self.region = region or TriggerRegion()
        self.on_click: Optional[Callable[[], None]] = None
        self._listener = None
        self._lock = threading.Lock()
        self._last_fire = 0.0
        self._cooldown = 0.3  # 秒，防止一次点击触发多次（原 1.0s 太长，用户点停止会被忽略）

    def start(self) -> bool:
        if not HAVE_PYNPUT:
            _log.warning("start() 失败: pynput 不可用")
            return False
        if self._listener is not None:
            _log.debug("start() 跳过: 监听器已在运行")
            return True
        try:
            self._listener = mouse.Listener(on_click=self._on_click)
            self._listener.daemon = True
            self._listener.start()
            _log.info("start() 成功: 监听区域 (%d,%d) %dx%d",
                      self.region.x, self.region.y, self.region.w, self.region.h)
            return True
        except Exception as e:
            _log.error("start() 异常: %r", e)
            self._listener = None
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
                _log.info("stop() 成功")
            except Exception as e:
                _log.warning("stop() 异常: %r", e)
            self._listener = None

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed:
            return
        if button != mouse.Button.left:
            return
        import time
        now = time.monotonic()
        with self._lock:
            in_region = self.region.contains(x, y)
            if not in_region:
                return
            if now - self._last_fire < self._cooldown:
                _log.debug("点击 (%d,%d) 在区域内但被 cooldown 阻止 (%.2fs < %.2fs)",
                           x, y, now - self._last_fire, self._cooldown)
                return
            self._last_fire = now
        _log.info("触发! 点击 (%d,%d) 在区域 (%d,%d) %dx%d 内",
                  x, y, self.region.x, self.region.y, self.region.w, self.region.h)
        if self.on_click is not None:
            # 在线程中执行回调，避免阻塞监听器
            threading.Thread(target=self.on_click, daemon=True).start()

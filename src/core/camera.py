# -*- coding: utf-8 -*-
"""摄像头枚举、参数控制与参数快照。

UVC 免驱摄像头通过 OpenCV VideoCapture 直接访问。
注意：不同摄像头固件支持的 CAP_PROP 集合不同，set 失败时静默跳过并记录。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import cv2
import numpy as np


# OpenCV 属性名 -> 人类可读名（设置面板用）
PROP_NAMES = {
    cv2.CAP_PROP_BRIGHTNESS: "brightness",
    cv2.CAP_PROP_CONTRAST: "contrast",
    cv2.CAP_PROP_SATURATION: "saturation",
    cv2.CAP_PROP_HUE: "hue",
    cv2.CAP_PROP_GAIN: "gain",
    cv2.CAP_PROP_EXPOSURE: "exposure",
    cv2.CAP_PROP_WHITE_BALANCE_BLUE_U: "white_balance_u",
    cv2.CAP_PROP_FOCUS: "focus",
    cv2.CAP_PROP_SHARPNESS: "sharpness",
}

# 设置面板中可调的属性（值域因摄像头而异，用 -1 表示自动）
ADJUSTABLE_PROPS = [
    (cv2.CAP_PROP_BRIGHTNESS, "亮度", 0, 255),
    (cv2.CAP_PROP_CONTRAST, "对比度", 0, 255),
    (cv2.CAP_PROP_SATURATION, "饱和度", 0, 255),
    (cv2.CAP_PROP_GAIN, "增益", 0, 255),
    (cv2.CAP_PROP_EXPOSURE, "曝光", -7, 7),
    (cv2.CAP_PROP_WHITE_BALANCE_BLUE_U, "白平衡U", 0, 4095),
]


@dataclass
class CameraSnapshot:
    """摄像头参数快照：录制/参考时自动保存，用于后续对比。"""

    index: int = 0
    backend: str = ""
    width: int = 640
    height: int = 480
    fps: float = 30.0
    fourcc: str = "MJPG"
    params: Dict[str, float] = field(default_factory=dict)  # prop_id -> value
    taken_at: float = 0.0  # time.time()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["params"] = {str(k): v for k, v in self.params.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CameraSnapshot":
        d = dict(d)
        d["params"] = {int(k): v for k, v in d.get("params", {}).items()}
        return cls(**d)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "CameraSnapshot":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


class CameraError(Exception):
    pass


class Camera:
    """单个 USB 摄像头封装。"""

    def __init__(self, index: int = 0):
        self.index = index
        self.cap: Optional[cv2.VideoCapture] = None

    # ---------- 打开/关闭 ----------

    def open(self, width: int = 640, height: int = 480, fps: float = 30.0,
             fourcc: str = "MJPG") -> None:
        """打开摄像头。优先尝试 MJPG 以获得高帧率，失败则退回默认。

        ⚠️ 关键顺序（踩坑经验）：必须【先设分辨率/帧率，最后设 MJPG】。
        若先设 MJPG 再设分辨率，Windows DirectShow 会用分辨率设置把
        fourcc 覆盖回 YUY2 未压缩格式 → 4 路 1080p 未压缩 = 500MB/s 爆
        USB2.0 带宽 → read() 阻塞 1s/帧 → 预览冻结。
        实测：正确顺序下 4 路 1920x1080@30 MJPG = 29.4fps 全部跑满。

        ⚠️ 后端选择（Win7 黑屏坑）：OpenCV 4.8 在 Windows 上默认优先
        MSMF（Media Foundation）后端，但 MSMF 在 Win7 SP1 上对多数
        UVC 摄像头兼容性差：isOpened() 返回 True 但 read() 一直失败
        → 预览全黑且无报错。因此先强制试 DirectShow (CAP_DSHOW)，
        失败再回退默认（MSMF/ANY），保证 Win7 与 Win10 都能出画面。
        """
        # 后端候选：Win7 首选 DSHOW，其次默认；每个候选都做真实 read 验证
        # （isOpened() 为 True 不代表能出帧，必须试读一帧）
        backends = [cv2.CAP_DSHOW]  # 700 = DirectShow
        if sys.platform.startswith("win"):
            backends.append(cv2.CAP_ANY)  # 0 = 默认（MSMF 等）
        self.cap = None
        last_err = None
        for b in backends:
            try:
                cap = cv2.VideoCapture(self.index, b)
            except Exception as e:
                last_err = e
                continue
            if not cap.isOpened():
                cap.release()
                continue
            # 试读验证：DSHOW 打开后首帧可能要几百 ms（摄像头初始化中），
            # 只试 1 帧就 fallback 会误判 → Win7 上回退 MSMF 极易黑屏。
            # 每个后端最多试 5 次、每次间隔 200ms，仍无帧才换下一个后端。
            ok = False
            for _ in range(5):
                ok, _ = cap.read()
                if ok:
                    break
                time.sleep(0.2)
            if ok:
                self.cap = cap
                break
            cap.release()
        if self.cap is None:
            raise CameraError(
                f"无法打开摄像头 #{self.index}（后端 DSHOW/MSMF 均失败"
                + (f": {last_err}" if last_err else "") + "）")

        # 格式/分辨率设置（v10.14.6 双 set 策略）：
        # 现场 Win7 实测（16:34 log）：单次 set FOURCC=MJPG 后读回仍 YUY2
        # ——部分 DSHOW 驱动对 fourcc set 只响应一次，或会被随后的分辨率
        # 设置覆盖。双 set（前 + 后）提高固件协商成功率。
        # ⚠️ MJPG 是保住高分辨率的关键：YUY2 800x600@30 = 28.8MB/s 必爆
        # USB2.0，MJPG 800x600@30 ≈ 2-5MB/s，双路高分辨率完全可行。
        try:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        except Exception:
            pass
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        try:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        except Exception:
            pass

        # 读回实际 fourcc：若不支持 MJPG（仍为 YUY2 等未压缩格式），
        # 且请求分辨率带宽超阈值，自动降级到 320x240@30。
        # ⚠️ 阈值取 15MB/s 而非 30：多路摄像头共享同一 USB 控制器，
        # 单路 640x480@30 YUY2 = 18.4MB/s 看似安全，两路合计 37MB/s
        # 已超 USB2.0 实际可用 ~30MB/s → 实测"READY 后一路断流"（Win7
        # 现场 15:31 log：cam#0 READY 后持续无帧）。按多路共享预算降级。
        try:
            fcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
            fcc_s = "".join([chr((fcc >> 8 * i) & 0xFF) for i in range(4)])
            aw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ah = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            uncompressed = fcc_s.upper() in ("YUYV", "YUV2", "UYVY", "YUY2", "")
            mbps = estimate_raw_bandwidth_mbps(aw or width, ah or height, fps, fourcc=fcc_s)
            if uncompressed and mbps > 15.0:
                # 读回 YUY2 且带宽超预算。⚠️ v10.14.8：fourcc 读回不可
                # 全信——老板确认该批摄像头【出厂默认 MJPG】，部分 DSHOW
                # 驱动读回的是解码/协商状态而非 USB 实际传输格式
                # （读回 YUY2 但实际 MJPG 传输的情况真实存在）。
                # 因此先试读 3 帧验证流是否真的正常：
                #   - 流正常 → 保持请求分辨率（读回假 YUY2，实际压缩
                #     传输，双路高分辨率毫无压力）——这才是老板要的
                #   - 流异常 → 进入降级组合表（真 YUY2 爆带宽才会挂流）
                _stream_ok = True
                for _ in range(3):
                    _ok, _ = self.cap.read()
                    if not _ok:
                        _stream_ok = False
                        break
                if _stream_ok:
                    try:
                        base = os.path.dirname(os.path.abspath(sys.executable))
                        path = os.path.join(base, "videorec_error.log")
                        with open(path, "a", encoding="utf-8") as f:
                            f.write("[%s] cam#%d fourcc 读回 %s 但流正常，"
                                    "保持 %dx%d（读回可能不可靠，实际可能为 MJPG）\n" % (
                                        time.strftime("%H:%M:%S"), self.index,
                                        fcc_s.strip() or "?",
                                        aw or width, ah or height))
                    except Exception:
                        pass
                else:
                    # 流异常（真 YUY2 爆带宽才会挂流）→ 降级组合表：
                    # ⚠️ 多候选 + 读回验证：部分老驱动不认 320x240（set
                    # 静默忽略），必须逐个尝试并确认读回变化。
                    # ⚠️ v10.14.6 调整（老板反馈：降 320x240 没意义，要保
                    # 分辨率）：候选顺序【优先保分辨率】640x480@30 →
                    # 640x480@15 → 640x480@10 → 320x240@30 → 320x240@15；
                    # 每档都双 set MJPG（部分固件特定分辨率下才接受）；
                    # 停档 = MJPG 成功 或 带宽≤15MB/s + 读回匹配 + 出帧。
                    _downgraded = False
                    for _w, _h, _f in ((640, 480, 30), (640, 480, 15),
                                       (640, 480, 10), (320, 240, 30),
                                       (320, 240, 15)):
                        try:
                            try:
                                self.cap.set(cv2.CAP_PROP_FOURCC,
                                             cv2.VideoWriter_fourcc(*fourcc))
                            except Exception:
                                pass
                            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, _w)
                            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _h)
                            self.cap.set(cv2.CAP_PROP_FPS, _f)
                            try:
                                self.cap.set(cv2.CAP_PROP_FOURCC,
                                             cv2.VideoWriter_fourcc(*fourcc))
                            except Exception:
                                pass
                            rw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                            rh = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                            rf = float(self.cap.get(cv2.CAP_PROP_FPS) or 0)
                            rfc = int(self.cap.get(cv2.CAP_PROP_FOURCC) or 0)
                            rfc_s = "".join(
                                [chr((rfc >> 8 * i) & 0xFF) for i in range(4)])
                            rmbps = estimate_raw_bandwidth_mbps(
                                rw, rh, _f, fourcc=rfc_s)
                            if rw == _w and rh == _h and \
                                    (rf == 0 or abs(rf - _f) <= 1.5) and \
                                    (rfc_s.strip().upper() == "MJPG" or rmbps <= 15.0):
                                ok1, _ = self.cap.read()
                                if ok1:
                                    _downgraded = True
                                    break  # 读回确认生效且能出帧
                        except Exception:
                            continue
                    if not _downgraded:
                        try:
                            base = os.path.dirname(os.path.abspath(sys.executable))
                            path = os.path.join(base, "videorec_error.log")
                            with open(path, "a", encoding="utf-8") as f:
                                f.write("[%s] cam#%d 带宽超限但降级全部失败"
                                        "（当前 %dx%d %s，%.1fMB/s > 15MB/s 预算）"
                                        "——请拔插该摄像头 USB 或换低分辨率摄像头\n" % (
                                            time.strftime("%H:%M:%S"), self.index,
                                            aw or width, ah or height, fcc_s.strip() or "?",
                                            mbps))
                        except Exception:
                            pass
        except Exception:
            pass

        # 关掉自动曝光/自动白平衡（否则手动参数会被覆盖，画面"调了没反应"）。
        # Windows DirectShow 约定：0.25=自动 / 0.75=手动（Linux V4L2 是 1/0）。
        # ⚠️ 旧代码用 set(AUTO_EXPOSURE, 1)：1 在 Windows 上不是有效值，
        # 驱动会忽略 → 自动曝光保持开启 → 手动调曝光/亮度全被自动覆盖。
        # 部分摄像头需要"先开再关"才能生效，此处直接 set 0.75 并读回验证，
        # 不认 0.75 的固件保持原样（尽力而为）。
        for prop, manual_val in ((cv2.CAP_PROP_AUTO_EXPOSURE, 0.75),
                                 (cv2.CAP_PROP_AUTO_WB, 0.75)):
            try:
                self.cap.set(prop, manual_val)
                time.sleep(0.05)
            except Exception:
                pass

        self._warmup()

    def _warmup(self, n: int = 5) -> None:
        """丢前几帧，等自动增益/曝光稳定。"""
        for _ in range(n):
            self.cap.read()

    def ensure_stream_config(self, width: int, height: int, fps: float = 30.0,
                             fourcc: str = "MJPG") -> tuple:
        """确保摄像头流配置为期望值，返回 (ok, 实际宽, 实际高, 实际fps)。

        ⚠️ 为什么需要：DirectShow 下 set_prop（亮度/曝光等）可能触发
        摄像头流重启，重启后 FOURCC/分辨率协商会丢失（回退 YUY2 未压缩
        或最低档分辨率）→ 800x600 YUY2 ≈ 28.8MB/s 直接爆 USB2.0 带宽
        → 录制掉帧一半、另一路被迫降分辨率。录制开始前调用本方法
        把流恢复到已验证的配置（顺序同 open()：分辨率/帧率先，MJPG 最后）。
        """
        if self.cap is None:
            return False, width, height, fps
        try:
            # 先读回当前配置：已匹配就直接通过，不强制 set。
            # ⚠️ set(CAP_PROP_FOURCC) 会触发摄像头流重启，固件响应慢时
            # 阻塞数秒；多路串行启动时后启动的摄像头被拖慢 ~2.5s
            # （实测 Cam2 比 Cam1 晚开始，用户误判为丢帧）。
            cw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ch = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cfps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0)
            cfcc = int(self.cap.get(cv2.CAP_PROP_FOURCC) or 0)
            cfcc_s = "".join(chr((cfcc >> 8 * i) & 0xFF) for i in range(4)).strip().upper()
            # fourcc 读回在部分 DSHOW 摄像头返回 0/乱码——此时不当作不匹配，
            # 只要分辨率一致就信任当前流（分辨率才是掉帧的决定因素）
            if cw == width and ch == height and (cfcc_s == fourcc.upper() or not cfcc_s or cfcc == 0):
                return True, cw, ch, cfps
            # 确实不一致才重设（顺序同 open()：分辨率/帧率先，MJPG 最后）
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_FPS, fps)
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            time.sleep(0.1)  # 给固件一点协商时间
            aw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ah = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            afps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0)
            ok = (aw == width and ah == height)
            return ok, aw, ah, afps
        except Exception:
            return False, width, height, fps

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    # ---------- 读取 ----------

    def read(self) -> Optional[np.ndarray]:
        """读一帧 BGR。失败返回 None。"""
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        return frame if ok else None

    # ---------- 参数 ----------

    def set_prop(self, prop_id: int, value: float) -> bool:
        if self.cap is None:
            return False
        try:
            return bool(self.cap.set(prop_id, value))
        except Exception:
            return False

    def get_prop(self, prop_id: int) -> Optional[float]:
        if self.cap is None:
            return None
        try:
            v = self.cap.get(prop_id)
            return float(v) if np.isfinite(v) else None
        except Exception:
            return None

    def apply_snapshot(self, snap: CameraSnapshot) -> None:
        """把参数快照套用到当前摄像头（用于复现第一天条件）。"""
        if self.cap is None:
            return
        for k, v in snap.params.items():
            try:
                self.cap.set(int(k), v)
            except Exception:
                pass

    def take_snapshot(self) -> CameraSnapshot:
        """采集当前全部可读参数。"""
        snap = CameraSnapshot(
            index=self.index,
            width=int(self.get_prop(cv2.CAP_PROP_FRAME_WIDTH) or 640),
            height=int(self.get_prop(cv2.CAP_PROP_FRAME_HEIGHT) or 480),
            fps=float(self.get_prop(cv2.CAP_PROP_FPS) or 30.0),
            fourcc="",
            taken_at=time.time(),
        )
        for prop_id in PROP_NAMES:
            v = self.get_prop(prop_id)
            if v is not None:
                snap.params[prop_id] = v
        return snap

    def list_resolutions(self) -> List[tuple]:
        """尝试常见分辨率组合，返回摄像头支持列表。"""
        if self.cap is None:
            return []
        supported = []
        for w, h in [(640, 480), (800, 600), (960, 720), (1280, 720), (1280, 960),
                     (1920, 1080), (2560, 1440)]:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            rw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            rh = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if (rw, rh) not in supported:
                supported.append((rw, rh))
        return supported


def list_cameras(max_index: int = 8) -> List[int]:
    """枚举可用的摄像头索引。

    ⚠️ v10.12 修复（Win7 根因）：优先用 DirectShow COM 枚举（纯枚举，
    不实际 open）——OpenCV cap_dshow 在【同一进程内】打开第 2 个同型号
    （同 VID/PID）摄像头必失败（Win7 血泪结论），若在这里用 open+read
    探测，后面的同型号摄像头会被漏掉 → 多进程方案根本没有上场机会
    （症状：Cam N 显示"未连接"、只有 1 个子进程）。DirectShow 枚举
    顺序与 OpenCV CAP_DSHOW 索引完全一致（同一系统枚举器）。
    COM 枚举失败时回退旧的 open+read 探测。
    """
    try:
        from core import dshow_enum
        devs = dshow_enum.enum_dshow_video_devices()
        if devs:
            return list(range(len(devs)))
    except Exception:
        pass
    # 回退：open+read 探测（COM 枚举不可用时）
    available = []
    for i in range(max_index):
        found = False
        for b in (cv2.CAP_DSHOW, cv2.CAP_ANY):
            try:
                cap = cv2.VideoCapture(i, b)
            except Exception:
                continue
            if cap.isOpened():
                # 试读一帧确认真能出画面（防 MSMF 假阳性）
                ok, _ = cap.read()
                if ok:
                    available.append(i)
                cap.release()
                found = True
                break
            cap.release()
    return available


def camera_device_names() -> Dict[int, str]:
    """获取摄像头索引 -> 设备名（用于型号识别/同型号分组）。

    ⚠️ 关键修复 (v10.11): 旧实现用 WMI 枚举顺序直接当 OpenCV 索引，
    但 WMI 顺序 ≠ DirectShow 顺序（实测：WMI 把内置摄像头排在中间，
    DirectShow 把 WL22A 排第一），导致 pick_cameras_by_model 型号分组
    全部错位、"默认 4 路同型号"失效。

    正确做法（本实现）：
    1. dshow_enum 用 COM ICreateDevEnum 枚举 DirectShow 视频设备，
       顺序与 OpenCV CAP_DSHOW 索引完全一致（同一系统枚举器）；
    2. WMI 拿完整设备清单 (DeviceID -> Name)；
    3. 按 VID/PID (+实例号) 把 DirectShow 路径匹配到 WMI 设备名。

    失败时返回空 dict（调用方 fallback 到 f"Camera #{i}"）。
    """
    import subprocess
    try:
        from .dshow_enum import enum_dshow_video_devices
        dshow_devs = enum_dshow_video_devices()
    except Exception:
        dshow_devs = []
    if not dshow_devs:
        return {}

    # WMI: DeviceID -> Name（DeviceID 含 VID/PID/实例号，可作唯一键）
    scripts = [
        (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
            "Select-Object Name, DeviceID | ConvertTo-Json -Compress -Depth 3"
        ),
        (
            "Get-WmiObject Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
            "ForEach-Object { $_.Name + '|' + $_.DeviceID }"
        ),
    ]
    wmi_names = {}   # DeviceID.lower() -> Name
    for ps in scripts:
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=20)
            if r.returncode != 0 or not r.stdout.strip():
                continue
            if "ConvertTo-Json" in ps:
                import json
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                for d in data:
                    did = (d.get("DeviceID") or "").lower()
                    name = d.get("Name")
                    if did and name:
                        wmi_names[did] = str(name)
            else:
                for line in r.stdout.splitlines():
                    if "|" not in line:
                        continue
                    name, did = line.strip().split("|", 1)
                    name = name.strip().strip('"')
                    did = did.strip().lower()
                    if name and did:
                        wmi_names[did] = str(name)
            if wmi_names:
                break
        except Exception:
            continue

    # DirectShow DevicePath -> 归一化匹配键
    def norm_key(path: str) -> str:
        """@device:pnp:\\?\\usb#vid_1d6c&pid_0103&mi_00#6&1b1b8a3a&0&0000#{...}
        -> usb#vid_1d6c&pid_0103&mi_00#6&1b1b8a3a&0&0000"""
        p = path.lower()
        if "@device:pnp:" in p:
            p = p.split("@device:pnp:")[1]
        if "#{65e8773d" in p:
            p = p.split("#{65e8773d")[0]
        p = p.replace("\\\\?\\", "").replace("\\?", "")
        return p.strip("#\\")

    # 匹配: 直接拿 DeviceID 的 VID/PID+实例段
    def match_wmi(dev_path: str):
        nk = norm_key(dev_path)  # usb#vid_..&pid_..&mi_..&instance..
        for did, name in wmi_names.items():
            # WMI: usb\vid_1d6c&pid_0103&mi_00\6&1b1b8a3a&0&0000
            dnorm = did.replace("\\", "#")
            if nk == dnorm:
                return name
        # 宽松回退: 仅 VID/PID 前缀（同型号多实例时名字应相同）
        import re
        m = re.search(r'vid_([0-9a-f]+)&pid_([0-9a-f]+)', nk)
        if m:
            vp = f'vid_{m.group(1)}&pid_{m.group(2)}'
            for did, name in wmi_names.items():
                if vp in did:
                    return name
        return ""

    names: Dict[int, str] = {}
    for i, (_, path) in enumerate(dshow_devs):
        if path:
            names[i] = match_wmi(path) or f"Camera #{i}"
    return names


def pick_cameras_by_model(available: List[int], limit: int = 4) -> List[int]:
    """从可用摄像头中挑选 limit 个，优先同一型号。

    规则：
    1. 按设备名（型号）分组；
    2. 优先选数量最多的型号（凑满 limit 个）；
    3. 不足时按索引顺序补其他型号。
    """
    if len(available) <= limit:
        return list(available)
    names = camera_device_names()
    # 型号 -> 索引列表（保持索引升序）
    groups: Dict[str, List[int]] = {}
    for i in available:
        name = names.get(i, f"Camera #{i}")
        groups.setdefault(name, []).append(i)
    # 优先数量多的型号（数量相同按最小索引）
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), min(kv[1])))
    picked: List[int] = []
    for name, idxs in ordered:
        if len(picked) >= limit:
            break
        picked.extend(idxs[: limit - len(picked)])
    return picked[:limit]


def estimate_raw_bandwidth_mbps(width: int, height: int, fps: float,
                                fourcc: str = "MJPG") -> float:
    """粗略估计 USB 带宽需求（MB/s）。

    - MJPG：摄像头内部压缩，按 ~1 byte/像素估算（画面复杂时更高）
    - YUYV/YUV2：未压缩，固定 2 bytes/像素
    USB3.0 实际可用约 300-400 MB/s；USB2.0 约 30-40 MB/s。
    多摄像头共用同一 USB 控制器时，总带宽是所有摄像头的和。
    """
    bpp = 2.0 if fourcc.upper() in ("YUYV", "YUV2", "UYVY") else 1.0
    return width * height * fps * bpp / 1e6


# =====================================================================
# 多进程采集代理（Win7 多路同型号摄像头的唯一可靠方案）
# =====================================================================
# 背景：OpenCV cap_dshow 后端在同一进程内无法同时打开两个同型号
# （同 VID/PID）USB 摄像头（A/B/C/H/I 实验全部"第一成功第二失败"）；
# MSMF 后端在 Win7 上打开成功但永远读不到帧。唯一被证实可行的
# 路线（J1/J2 多进程实验）：每路摄像头一个独立子进程，进程隔离
# 绕过 OpenCV 后端 bug。
#
# WorkerCamera 保持与 Camera 完全相同的对外接口（open/read/close/
# get_prop/set_prop/ensure_stream_config/take_snapshot/apply_snapshot/
# list_resolutions/cap/index），因此 main_window.py / recorder.py /
# settings_panel.py 无需改动即可切换。
#
# 实现：
#   - 主进程创建命名 mmap（头部 256B + 双缓冲帧区）
#   - 启动子进程（core.capture_worker.worker_main，通过 main.py 的
#     --capture-worker 分支，PyInstaller frozen 时重启 exe 自身）
#   - 子进程真正持有 VideoCapture：read 帧写共享内存（双缓冲交替 +
#     frame_id 发布，无读写冲突），参数命令走 stdin/stdout 行协议
#   - 打开失败子进程自动重试 3 次；多路错峰启动由 start_delay 控制
# =====================================================================

import mmap as _mmap
import struct as _struct
import subprocess as _subprocess

# 与 capture_worker.py 保持一致的共享内存布局常量
_SHM_HEADER_SIZE = 256
_SHM_MAGIC = 0x56435231
_ST_STARTING = 0
_ST_READY = 1
_ST_ERROR = 2
_ST_CLOSING = 3
_HDR = _struct.Struct("<iiqiiif")   # magic, status, frame_id, buffer_idx, w, h, fps
# v10.14.9: 主进程心跳（offset 32，int32 秒级时间戳）。worker 据此判断
# 主进程是否存活（主进程死 → 心跳停更 → worker 自杀释放摄像头）。
# err_msg 相应后移到 offset 36（char[220]）。
_HEARTBEAT_OFF = 32
_ERR_MSG_OFF = 36

# 命令区（共享内存内，windowed exe 无 stdin/stdout，参数控制走这里）
# ⚠️ 顺序约定：写方先写 text/len，最后写 seq（seq 是发布标志）
_CMD_SEQ_OFF = 256      # int32 主进程写，递增
_CMD_LEN_OFF = 260      # int32
_CMD_TEXT_OFF = 264     # char[512]
_CMD_TEXT_LEN = 512
_RESP_SEQ_OFF = 776     # int32 子进程写（= 处理的 cmd_seq）
_RESP_LEN_OFF = 780     # int32
_RESP_TEXT_OFF = 784    # char[1024]
_RESP_TEXT_LEN = 1024
_BUF_OFF = 1808         # 双缓冲帧区起始（256+命令区）

# OpenCV 属性常量（避免子进程侧额外 import cv2 的开销）
_CAP_FRAME_WIDTH = 3
_CAP_FRAME_HEIGHT = 4
_CAP_FPS = 5


class _CapProxy:
    """settings_panel 直接访问 camera.cap.set/get 时的代理（走 IPC）。"""

    def __init__(self, cam: "WorkerCamera"):
        self._cam = cam

    def set(self, prop_id, value) -> bool:
        return self._cam.set_prop(int(prop_id), float(value))

    def get(self, prop_id):
        v = self._cam.get_prop(int(prop_id))
        return float(v) if v is not None else 0.0


class WorkerCamera:
    """多进程采集代理：接口与 Camera 完全一致，内部走子进程 + 共享内存。

    用法与 Camera 相同：
        cam = WorkerCamera(index, start_delay=slot * 2.0)
        cam.open(width=800, height=600, fps=30.0)
        frame = cam.read()
        cam.close()
    """

    def __init__(self, index: int = 0, start_delay: float = 0.0):
        self.index = index
        self.start_delay = start_delay   # 错峰启动秒数（多路时按槽位递增）
        self.cap = _CapProxy(self)       # settings_panel 兼容
        self._proc: Optional[_subprocess.Popen] = None
        self._shm: Optional[_mmap.mmap] = None
        self._hdr = None                 # header 字节视图
        self._w = 0
        self._h = 0
        self._fps = 0.0
        self._frame_bytes = 0
        self._last_frame_id = -1
        self._last_buf_idx = -1
        self._buf_view = None            # (buf0, buf1) numpy 视图
        self._opened = False

    # ---------- 诊断日志（写 exe 旁 videorec_error.log，便于 Win7 现场排查） ----------

    def _log_diag(self, msg: str) -> None:
        try:
            base = os.path.dirname(os.path.abspath(sys.executable))
            path = os.path.join(base, "videorec_error.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] cam#%d %s\n" % (time.strftime("%H:%M:%S"),
                                              self.index, msg))
        except Exception:
            pass

    # ---------- 打开/关闭 ----------

    def start(self, width: int = 640, height: int = 480, fps: float = 30.0,
              fourcc: str = "MJPG") -> None:
        """启动采集子进程并【立即返回】（不等待就绪）。

        ⚠️ Win7 血泪结论（diag2 v1.5 K/L 实验）：同型号摄像头必须
        【几乎同时启动】才能都打开成功——"一个进程稳定持有后另一个
        再打开"会持续失败（重试 6 次 x 3s 也失败）。因此多路场景
        必须先对所有路调用 start()（同时启动），再统一 wait_ready()。
        """
        if self._opened:
            return
        self._req = (width, height, fps, fourcc)
        frame_bytes = width * height * 3
        shm_size = _BUF_OFF + 2 * frame_bytes
        shm_name = "videorec_cam%d_%d_%d" % (self.index, os.getpid(),
                                             int(time.time() * 1000))
        # 主进程创建共享内存（tagname 命名，Windows 跨进程可见）
        try:
            shm = _mmap.mmap(-1, shm_size, tagname=shm_name)
        except Exception as e:
            self._log_diag("创建共享内存失败: %r" % e)
            raise CameraError(f"创建共享内存失败: {e}")
        self._shm = shm
        self._hdr = shm  # mmap 本身（buffer 协议），不能切片（切片是 bytes 快照）

        # 启动子进程：frozen 时重启 exe 自身，否则 python main.py
        # ⚠️ windowed (console=False) 打包下 stdin/stdout 无效，命令通道
        # 走共享内存命令区（见 _cmd_sync），这里不依赖任何管道。
        cmd = self._worker_cmd(shm_name, width, height, fps)
        self._log_diag("启动子进程: %r" % (cmd,))
        try:
            self._proc = _subprocess.Popen(cmd)
        except Exception as e:
            self._log_diag("启动采集子进程失败: %r" % e)
            try:
                shm.close()
            except Exception:
                pass
            self._shm = None
            raise CameraError(f"启动采集子进程失败: {e}")

    def wait_ready(self, timeout: float = 120.0):
        """等待子进程就绪（start() 之后调用）。返回实际 (w, h, fps)。

        超时上限：错峰 delay + 打开重试 6*3s + warmup（设备争抢时单次
        open 可能阻塞 10s+，实测需要 ~80-100s，故默认 120s）。
        """
        if self._opened:
            return self._w, self._h, self._fps
        width, height, fps, fourcc = self._req
        deadline = time.time() + timeout + self.start_delay
        while time.time() < deadline:
            st = self._read_status()
            if st == _ST_READY:
                break
            if st == _ST_ERROR:
                err = self._read_err()
                self._log_diag("子进程 ST_ERROR: %s" % err)
                self._cleanup_proc()
                raise CameraError(f"采集子进程错误: {err}")
            if self._proc.poll() is not None:
                # 子进程提前退出
                rc = self._proc.returncode
                self._log_diag("子进程异常退出 rc=%s, status=%d" % (rc, st))
                self._cleanup_proc()
                raise CameraError(f"采集子进程异常退出 rc={rc}")
            time.sleep(0.1)
        else:
            alive = self._proc.poll() is None
            st = self._read_status()
            self._log_diag("等待子进程就绪超时: 子进程存活=%s, status=%d"
                           % (alive, st))
            self._cleanup_proc()
            raise CameraError("等待采集子进程就绪超时")

        # 读回实际分辨率
        magic, status, fid, bidx, aw, ah, afps = _HDR.unpack_from(self._shm, 0)
        self._w, self._h = aw, ah
        self._fps = afps
        self._frame_bytes = aw * ah * 3
        import numpy as _np

        def _view(off):
            return _np.frombuffer(self._shm, dtype=_np.uint8,
                                  count=self._frame_bytes,
                                  offset=off).reshape(ah, aw, 3)

        self._buf_view = (_view(_BUF_OFF),
                          _view(_BUF_OFF + self._frame_bytes))
        self._last_frame_id = -1
        self._opened = True
        # 记录子进程实际流参数（录制 fallback 用：ENSURE 失败时
        # 用这个值建 writer，避免与实际帧尺寸不匹配）
        self.actual = (aw, ah, afps)

        # 与 Camera.open 对齐：先分辨率/帧率、后 MJPG 的协商顺序
        # ⚠️ 5s 上限：这是 best-effort（异常已吞），主线程调用时
        # 不能让它阻塞 UI 太久（Win7 流重启可能 >10s）
        try:
            self._cmd_sync("ENSURE %d %d %.4f %s" % (width, height, fps, fourcc),
                           timeout=5.0)
        except Exception:
            pass
        return self._w, self._h, self._fps

    def open(self, width: int = 640, height: int = 480, fps: float = 30.0,
             fourcc: str = "MJPG"):
        """兼容接口：start + wait_ready（单路场景用）。"""
        self.start(width=width, height=height, fps=fps, fourcc=fourcc)
        return self.wait_ready()

    def _worker_cmd(self, shm_name, width, height, fps):
        args = ["--capture-worker", str(self.index), shm_name,
                str(width), str(height), "%.4f" % fps,
                "%.2f" % self.start_delay]
        if getattr(sys, "frozen", False):
            return [sys.executable] + args
        return [sys.executable, os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "main.py"))] + args

    def _read_status(self) -> int:
        try:
            # v10.14.9 心跳：主进程每次轮询都更新 heartbeat，
            # worker 监控线程据此判断主进程存活（防孤儿残留）
            _struct.pack_into("<i", self._shm, _HEARTBEAT_OFF,
                              int(time.time()))
            return _HDR.unpack_from(self._shm, 0)[1]
        except Exception:
            return _ST_STARTING

    def _read_err(self) -> str:
        try:
            raw = bytes(self._shm[_ERR_MSG_OFF:256])
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        except Exception:
            return "(未知错误)"

    def _cleanup_proc(self):
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                    try:
                        self._proc.wait(timeout=5)
                    except Exception:
                        pass
            except Exception:
                pass
            self._proc = None
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
        self._opened = False

    def close(self) -> None:
        """停止采集子进程，释放共享内存。幂等。"""
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    # 通过共享内存命令区发 QUIT（windowed 下无 stdin）
                    self._cmd_sync("QUIT", timeout=2.0)
                    try:
                        self._proc.wait(timeout=5)
                    except Exception:
                        pass
                if self._proc.poll() is None:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            except Exception:
                pass
        self._cleanup_proc()

    # ---------- 读取 ----------

    def read(self):
        """读最新一帧（阻塞等待新帧，行为与 Camera.read 一致）。

        返回 BGR numpy 数组（副本），子进程无新帧时最多等 3 秒
        （防子进程挂掉导致 UI 卡死），超时返回 None。
        """
        if not self._opened or self._shm is None:
            return None
        # v10.14.9 心跳：READY 后由 tick 的 read() 持续更新
        try:
            _struct.pack_into("<i", self._shm, _HEARTBEAT_OFF,
                              int(time.time()))
        except Exception:
            pass
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                magic, status, fid, bidx, aw, ah, afps = _HDR.unpack_from(self._shm, 0)
            except Exception:
                return None
            if status != _ST_READY:
                return None
            if fid != self._last_frame_id:
                self._last_frame_id = fid
                self._last_buf_idx = bidx
                try:
                    frame = self._buf_view[bidx].copy()
                except Exception:
                    return None
                return frame
            time.sleep(0.002)
        return None

    # ---------- 参数（走共享内存命令区） ----------

    def _cmd_sync(self, line: str, timeout: float = 10.0) -> str:
        """发命令并等待响应（共享内存命令区）。

        ⚠️ windowed (console=False) 打包下 stdin/stdout 无效，命令通道
        必须走共享内存：主进程写 cmd 区（先 text 后 seq），子进程轮询
        cmd_seq，处理后写 resp 区（先 text 后 seq）。超时/失败返回 ""。
        """
        if not self._opened or self._shm is None:
            return ""
        try:
            seq = _struct.unpack_from("<i", self._shm, _CMD_SEQ_OFF)[0] + 1
            data = line.encode("utf-8", "replace")[:_CMD_TEXT_LEN]
            # ⚠️ 发布顺序：先写 text/len，最后写 seq（seq 是发布标志，
            # 子进程读到新 seq 时 text 必已完整，不会读到旧命令残留）
            _struct.pack_into("<i", self._shm, _CMD_LEN_OFF, len(data))
            self._shm[_CMD_TEXT_OFF:_CMD_TEXT_OFF + len(data)] = data
            _struct.pack_into("<i", self._shm, _CMD_SEQ_OFF, seq)
            # 清响应标记（防止子进程未处理时读到旧响应）
            _struct.pack_into("<i", self._shm, _RESP_SEQ_OFF, 0)
            deadline = time.time() + timeout
            while time.time() < deadline:
                rseq = _struct.unpack_from("<i", self._shm, _RESP_SEQ_OFF)[0]
                if rseq == seq:
                    rlen = _struct.unpack_from("<i", self._shm, _RESP_LEN_OFF)[0]
                    rlen = max(0, min(rlen, _RESP_TEXT_LEN))
                    raw = bytes(self._shm[_RESP_TEXT_OFF:_RESP_TEXT_OFF + rlen])
                    return raw.decode("utf-8", "replace").strip()
                time.sleep(0.005)
            return ""
        except Exception:
            return ""

    def set_prop(self, prop_id: int, value: float) -> bool:
        resp = self._cmd_sync("SET %d %.6f" % (int(prop_id), float(value)))
        return resp == "OK"

    def get_prop(self, prop_id: int) -> Optional[float]:
        resp = self._cmd_sync("GET %d" % int(prop_id))
        if resp.startswith("V "):
            try:
                return float(resp[2:])
            except Exception:
                return None
        return None

    def ensure_stream_config(self, width: int, height: int, fps: float = 30.0,
                             fourcc: str = "MJPG", timeout: float = 10.0) -> tuple:
        resp = self._cmd_sync("ENSURE %d %d %.4f %s"
                              % (int(width), int(height), float(fps), fourcc),
                              timeout=timeout)
        parts = resp.split()
        if parts and parts[0] == "OK" and len(parts) >= 4:
            try:
                return True, int(parts[1]), int(parts[2]), float(parts[3])
            except Exception:
                pass
        if parts and parts[0] == "FAIL" and len(parts) >= 4:
            try:
                return False, int(parts[1]), int(parts[2]), float(parts[3])
            except Exception:
                pass
        return False, int(width), int(height), float(fps)

    def take_snapshot(self) -> "CameraSnapshot":
        import json as _json
        resp = self._cmd_sync("SNAP", timeout=15.0)
        if resp.startswith("SNAP "):
            try:
                return CameraSnapshot.from_dict(_json.loads(resp[5:]))
            except Exception:
                pass
        # 兜底：逐属性查询
        snap = CameraSnapshot(index=self.index, taken_at=time.time())
        for prop_id in PROP_NAMES:
            v = self.get_prop(prop_id)
            if v is not None:
                snap.params[prop_id] = v
        snap.width = int(self.get_prop(_CAP_FRAME_WIDTH) or 640)
        snap.height = int(self.get_prop(_CAP_FRAME_HEIGHT) or 480)
        snap.fps = float(self.get_prop(_CAP_FPS) or 30.0)
        return snap

    def apply_snapshot(self, snap: "CameraSnapshot") -> None:
        import json as _json
        d = snap.to_dict()
        # params 的 key 是 str，子进程 from_dict 会转回 int
        self._cmd_sync("APPLY " + _json.dumps(d), timeout=20.0)

    def list_resolutions(self) -> List[tuple]:
        import json as _json
        resp = self._cmd_sync("RES", timeout=15.0)
        if resp.startswith("RES "):
            try:
                data = _json.loads(resp[4:])
                return [(int(a), int(b)) for a, b in data]
            except Exception:
                pass
        return []

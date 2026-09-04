# -*- coding: utf-8 -*-
"""录制线程：独立线程读帧、写视频、记录帧时间戳、统计丢帧。"""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from qt_compat import QThread, pyqtSignal


class Recorder:
    """录制器（在独立线程中运行，不阻塞 UI）。

    输出：
      - <name>.avi          视频（MJPG）
      - <name>.timestamps.csv  每帧系统时间戳（用于跨设备同步）
      - <name>.notes.csv    录制中按快捷键/按钮添加的行为备注（帧号 + 时间 + 内容）
      - <name>.meta.json    参数快照 + 统计
    """

    def __init__(self, camera, out_dir: str | Path, name: str,
                 fps: float = 30.0, fourcc: str = "MJPG", meta_extra: dict = None):
        self.camera = camera
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.fps = fps
        self.fourcc = fourcc
        self.meta_extra = dict(meta_extra or {})  # 附加 meta 字段（如 cam/animal）

        self._writer: Optional[cv2.VideoWriter] = None
        self._ts_file = None
        self._ts_writer = None
        self._running = False
        self._stopped = False
        self._stop_lock = threading.Lock()   # stop() 线程安全（主线程/录制线程可能并发调）
        self._frame_count = 0
        self._drop_count = 0
        self._last_read_time = 0.0
        self._start_time = 0.0
        self._notes = []   # list[(frame_idx, elapsed_s, content)]

    # ---------- 生命周期 ----------

    def start(self, width: int, height: int) -> None:
        """启动录制。

        width/height = 实际流分辨率（调用方从 ensure_stream_config 读回）。
        ⚠️ 不要在这里同步 camera.read() 取尺寸：DSHOW 首帧/流重启可能
        阻塞数秒，而多路是串行启动的——实测第二路摄像头因此比第一路
        晚开始 ~2.5s（用户误判为丢帧/时长不一致）。用读回分辨率建
        writer，读帧交给录制线程在后台进行。
        """
        if self._running:
            return
        h, w = int(height), int(width)
        self._w, self._h = w, h  # 实际写入尺寸（丢帧警告/元数据用）

        video_path = self.out_dir / f"{self.name}.avi"
        # 先探针写一个临时文件：区分"目录/磁盘不可写" vs "编码器问题"。
        # ⚠️ Win7 现场：VideoWriter 创建失败但 mkdir 成功（E:\ 项目目录
        # 建得出，文件却建不了）→ 光看"磁盘不可写？"无法定位，必须分层报。
        probe = self.out_dir / "_vrec_probe.tmp"
        try:
            with open(probe, "wb") as f:
                f.write(b"probe")
            probe.unlink()
        except OSError as _pe:
            raise RuntimeError(
                f"无法创建视频文件：目录不可写 {self.out_dir} "
                f"（{_pe.strerror or _pe}）")
        self._writer = cv2.VideoWriter(
            str(video_path),
            cv2.VideoWriter_fourcc(*self.fourcc),
            self.fps, (w, h),
        )
        self._writer_fallback = False  # 是否回退到兼容写入器（UI 提示用）
        if self._writer is None or not self._writer.isOpened():
            self._writer = None
            # ⚠️ Win7 现场：VideoWriter 依赖 ffmpeg dll，该 dll 在 Win7
            # 加载失败时 VideoWriter 必然打不开。不再报错——自动回退到
            # 手写 AVI 写入器（内置 JPEG 编码，不依赖 videoio/ffmpeg）。
            try:
                from core.avi_writer import AviMjpgWriter
                self._writer = AviMjpgWriter(
                    video_path, w, h, self.fps, quality=90)
                self._writer_fallback = True
            except Exception as _fe:
                raise RuntimeError(
                    f"无法创建视频文件（目录可写，但 VideoWriter 与兼容"
                    f"写入器均失败）：{video_path}（{_fe}）") from _fe
        self._ts_file = open(self.out_dir / f"{self.name}.timestamps.csv",
                             "w", newline="", encoding="utf-8")
        self._ts_writer = csv.writer(self._ts_file)
        self._ts_writer.writerow(["frame_idx", "timestamp_s", "monotonic_s"])

        self._running = True
        self._frame_count = 0
        self._drop_count = 0
        self._last_read_time = time.monotonic()
        self._start_time = time.time()

    def step(self) -> Optional[np.ndarray]:
        """读一帧并写入。返回该帧（供预览），失败返回 None。"""
        if not self._running:
            return None
        frame = self.camera.read()
        now = time.monotonic()
        # 丢帧检测：若距上一帧超过 2 个帧周期，记为丢帧
        # ⚠️ fps<=0 防御：Win7 DSHOW 读回 FPS 可能为 0，除零会崩录制线程
        _fps = self.fps if self.fps and self.fps > 0 else 30.0
        if self._frame_count > 0 and (now - self._last_read_time) > 2.2 / _fps:
            self._drop_count += 1
        self._last_read_time = now

        if frame is None:
            self._drop_count += 1
            return None

        try:
            self._writer.write(frame)
            self._ts_writer.writerow([self._frame_count, time.time(), now])
        except Exception as e:
            # 写盘失败（磁盘满/IO/文件已关闭）：抛出让线程安全收尾，
            # 不能静默吞掉——否则 AVI 尾部缺帧、meta/timestamps 写坏。
            raise RuntimeError(f"写盘失败: {e}") from e
        self._frame_count += 1
        return frame

    # ---------- 行为备注 ----------

    def add_note(self, content: str) -> int:
        """记录一条备注：当前帧号 + 录制经过时间。返回备注序号（1 起）。"""
        self._notes.append((self._frame_count, round(self.elapsed_s, 3), content))
        return len(self._notes)

    @property
    def notes(self) -> list:
        return list(self._notes)

    def stop(self) -> dict:
        """停止录制，返回统计信息。线程安全、幂等（可被主线程和录制线程并发调用）。"""
        with self._stop_lock:
            if self._stopped:
                return {}
            self._stopped = True
            self._running = False
            if self._writer is not None:
                try:
                    self._writer.release()
                except Exception:
                    pass
                self._writer = None
            if self._ts_file is not None:
                try:
                    self._ts_file.close()
                except Exception:
                    pass
                self._ts_file = None

            duration = time.time() - self._start_time
            stats = {
                "frames": self._frame_count,
                "drops": self._drop_count,
                "duration_s": round(duration, 2),
                "actual_fps": round(self._frame_count / duration, 2) if duration > 0 else 0,
                "width": getattr(self, "_w", 0),   # 实际写入分辨率（不是请求值）
                "height": getattr(self, "_h", 0),
                "video": str(self.out_dir / f"{self.name}.avi"),
                "timestamps": str(self.out_dir / f"{self.name}.timestamps.csv"),
                "notes": len(self._notes),
                "writer_fallback": bool(getattr(self, "_writer_fallback", False)),
            }
            # 写备注文件（UTF-8 BOM，Excel 直接打开不乱码）
            if self._notes:
                try:
                    notes_path = self.out_dir / f"{self.name}.notes.csv"
                    with open(notes_path, "w", newline="", encoding="utf-8-sig") as f:
                        w = csv.writer(f)
                        w.writerow(["note_idx", "frame", "time_s", "content"])
                        for i, (fr, ts, content) in enumerate(self._notes, 1):
                            w.writerow([i, fr, ts, content])
                    stats["notes_file"] = str(notes_path)
                except Exception:
                    pass
            # 写 meta.json（写盘失败时不能崩——否则连统计信息都丢）
            import json
            meta = {"name": self.name, "stats": stats}
            if self.meta_extra:
                meta.update(self.meta_extra)
            try:
                with open(self.out_dir / f"{self.name}.meta.json", "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return stats

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def elapsed_s(self) -> float:
        """录制已进行的秒数。"""
        if not self._start_time:
            return 0.0
        return time.time() - self._start_time


class RecorderThread(QThread):
    """录制线程：独占摄像头读帧 + 写盘，与 UI 完全解耦。

    - 循环以摄像头帧率自然节流（read() 阻塞等待新帧）
    - 每帧通过 frame_ready 信号发给 UI 做预览（UI 不再自己读摄像头）
    - 掉帧统计在线程内完成，UI 随时可查
    """

    frame_ready = pyqtSignal(object)   # 最新帧（供预览）
    stopped = pyqtSignal(dict)         # 停止后发出统计信息

    def __init__(self, recorder: Recorder, parent=None):
        super().__init__(parent)
        self.recorder = recorder
        self._running = False
        self._latest_frame: Optional[np.ndarray] = None
        self._error: Optional[str] = None   # 录制中写盘异常信息（磁盘满/IO错误）
        self._stats: Optional[dict] = None  # 线程内异常收尾时保存的统计

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        """最新一帧（UI 预览用）。"""
        return self._latest_frame

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        """录制线程异常信息（正常为 None）。"""
        return self._error

    def run(self) -> None:
        self._running = True
        try:
            while self._running:
                frame = self.recorder.step()
                if frame is None:
                    # 读帧失败：短暂让出 CPU，避免忙等
                    self.msleep(2)
                    continue
                self._latest_frame = frame
                self.frame_ready.emit(frame)
        except Exception as e:
            # ⚠️ 写盘异常（磁盘满/IO 错误）：不能静默崩溃——否则 AVI 尾部
            # 缺帧、meta/timestamps 写坏（用户踩过：Cam2 在 ~117s 处文件
            # 损坏，头声明 3532 帧实际只有 3495）。记录错误 + 安全收尾，
            # 让 AVI 头/统计尽量正确。
            self._error = str(e)
            try:
                self._stats = self.recorder.stop()
            except Exception:
                pass
        finally:
            self._running = False

    def stop_and_wait(self, timeout_ms: int = 5000) -> dict:
        """请求停止并等待线程退出，返回统计。

        ⚠️ 必须先确保线程真正退出再调 recorder.stop()：若 wait 超时
        （DSHOW read 偶尔阻塞数秒）就强停，recorder.stop() 会与仍在
        写帧的录制线程并发操作文件 → AVI 尾部缺帧、timestamps 混入
        NUL、meta 写坏（12:36 自动停止时 Cam2 中招，Cam1 侥幸）。
        因此超时后继续分片等待（最多 ~15s），宁可慢不可坏文件。
        """
        self._running = False
        waited = 0
        while not self.wait(1000) and waited < 15000:
            waited += 1000
        stats = self.recorder.stop()
        if not stats and self._stats:
            stats = self._stats   # 线程内已异常收尾过，取它保存的统计
        return stats

    @property
    def frame_count(self) -> int:
        return self.recorder.frame_count

    @property
    def elapsed_s(self) -> float:
        return self.recorder.elapsed_s

    def add_note(self, content: str) -> int:
        """打备注（线程安全：只追加 list，由 GIL 保护）。"""
        return self.recorder.add_note(content)

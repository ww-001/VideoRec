# -*- coding: utf-8 -*-
"""采集子进程（多进程架构的 worker 侧）。

为什么需要多进程（Win7 血泪结论）：
  OpenCV cap_dshow 后端在【同一进程内】无法同时打开两个同型号
  （同 VID/PID）USB 摄像头——第一个成功、第二个必失败（与顺序、
  间隔、是否 read 无关，已用 A/B/C/H/I 对照实验验证）。MSMF 后端
  在 Win7 上打开成功但永远读不到帧。这是 OpenCV 后端 bug，不是
  硬件/驱动问题（多进程实验 J1/J2 证实：独立进程各开一路全部成功）。

本模块 = 每路摄像头一个独立子进程：
  1. 打开摄像头（复用 Camera 类的完整逻辑：DSHOW 优先、MJPG 协商
     顺序、带宽降级、自动曝光关闭），打开失败自动重试 3 次
  2. 持续 read 并把帧写入命名 mmap 共享内存（双缓冲 + frame_id，
     与主进程零冲突）
  3. 通过 stdin/stdout 行协议响应主进程的参数控制命令
     （SET/GET/ENSURE/SNAP/APPLY/RES/QUIT）

共享内存布局（头部 256 字节，struct 小端）：
  offset 0   int32  magic   = 0x56435231 ('VCR1')
  offset 4   int32  status  = 0 starting / 1 ready / 2 error / 3 closing
  offset 8   int64  frame_id（递增，主进程据此判断新帧）
  offset 16  int32  buffer_idx（0/1，当前有效帧所在缓冲区）
  offset 20  int32  width
  offset 24  int32  height
  offset 28  float  fps
  offset 32  char[224] err_msg（status=2 时的错误信息，utf-8）
  offset 256  buffer0（width*height*3 字节 BGR）
  offset 256 + w*h*3  buffer1
"""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import sys
import threading
import time

import numpy as np

# ---------- 共享内存布局常量（与 camera.py WorkerCamera 保持一致） ----------
SHM_HEADER_SIZE = 256
SHM_MAGIC = 0x56435231
ST_STARTING = 0
ST_READY = 1
ST_ERROR = 2
ST_CLOSING = 3

# 命令区（windowed exe 无 stdin/stdout，参数控制走共享内存）
CMD_SEQ_OFF = 256      # int32 主进程写，递增（发布标志：先写 text 再写 seq）
CMD_LEN_OFF = 260      # int32
CMD_TEXT_OFF = 264     # char[512]
CMD_TEXT_LEN = 512
RESP_SEQ_OFF = 776     # int32 子进程写（= 处理的 cmd_seq）
RESP_LEN_OFF = 780     # int32
RESP_TEXT_OFF = 784    # char[1024]
RESP_TEXT_LEN = 1024
BUF_OFF = 1808         # 双缓冲帧区起始

_HDR = struct.Struct("<iiqiiif")   # magic, status, frame_id, buffer_idx, w, h, fps


def _open_camera_with_retry(index: int, width: int, height: int, fps: float,
                            max_retries: int = 6, retry_interval: float = 3.0):
    """打开摄像头，失败重试 max_retries 次。返回 Camera 实例或抛异常。

    ⚠️ Win7 实测（diag2 v1.5 K/L 实验）：同型号摄像头打开需要
    重试 4-5 次才成功（K 实验 A 用 5 次、L 实验 #1 用 4 次）——
    之前 3 次 x 2s 会在"差一点成功"时放弃。故默认 6 次 x 3s。
    """
    from core.camera import Camera
    last_err = None
    for attempt in range(1, max_retries + 1):
        cam = Camera(index)
        try:
            cam.open(width=width, height=height, fps=fps)
            return cam
        except Exception as e:
            last_err = e
            try:
                cam.close()
            except Exception:
                pass
            if attempt < max_retries:
                time.sleep(retry_interval)
    raise CameraOpenError(f"摄像头 #{index} 打开失败（重试 {max_retries} 次）: {last_err}")


class CameraOpenError(Exception):
    pass


def worker_main(argv):
    """子进程入口：--capture-worker <index> <shm_name> <w> <h> <fps> <delay>"""
    try:
        index = int(argv[0])
        shm_name = argv[1]
        width = int(argv[2])
        height = int(argv[3])
        fps = float(argv[4])
        delay = float(argv[5]) if len(argv) > 5 else 0.0
    except Exception as e:
        _die(f"参数解析失败: {e} | argv={argv}")
        return

    # 错峰启动：等待主进程安排的延迟（避免多路同时抢设备初始化）
    if delay > 0:
        time.sleep(delay)

    # ⚠️ v10.14.9 父进程监控：主进程异常退出（任务管理器强杀/崩溃）时，
    # worker 会变孤儿残留并继续持有摄像头 → 后续所有打开全失败
    # （16:52 事故链：主进程 READY 后异常 → worker 泄漏 → 17:07
    # 三路 DSHOW/MSMF 全败）。方案：共享内存心跳——主进程每 500ms
    # 更新 offset 32 的 heartbeat（int32 秒级时间戳），本线程每 2s
    # 检查一次，心跳停更超过 15s = 主进程已死 → 自杀释放摄像头。
    # （不用 OpenProcess/PID：PID 复用会误判；句柄等待在部分权限
    # 环境下不生效——实测两个方案都残留 worker，心跳最可靠）
    #
    # ⚠️ v10.14.10 修复：线程必须在 shm attach 成功之后启动，且通过
    # args 传入 shm。v10.14.9 把线程放在 attach 之前，闭包引用未赋值
    # 的 shm → 首次检查 NameError → os._exit(3) 误自杀
    # （08:13 本机复现：cam#1 启动后 ~3s 退出 rc=3）。

    # attach 主进程创建的共享内存（必须先 attach，再启动监控线程）
    try:
        shm = mmap.mmap(-1, BUF_OFF + 2 * width * height * 3,
                        tagname=shm_name)
    except Exception as e:
        _die(f"共享内存 attach 失败: {e}")
        os._exit(3)  # 显式退出码，主进程能看到失败而非静默 return
        return

    # v10.14.11：心跳看门狗已禁用（v10.14.9 的 rc=3 误杀元凶：
    # 线程在 shm attach 完成前启动 → NameError 误自杀；且主进程 UI
    # 阻塞时心跳停更 >15s 误杀正常 worker）。防孤儿改由 main.py
    # 启动时清理残留 --capture-worker 进程（只杀父进程已死的真孤儿）。
    # def _watch_parent(shm_obj):
    #     while True:
    #         time.sleep(2.0)
    #         try:
    #             hb = _struct.unpack_from("<i", shm_obj, 32)[0]
    #         except Exception:
    #             os._exit(3)
    #         if hb > 0 and time.time() - hb > 15:
    #             os._exit(3)
    #
    # threading.Thread(target=_watch_parent, args=(shm,),
    #                  daemon=True).start()

    try:
        cam = _open_camera_with_retry(index, width, height, fps)
    except Exception as e:
        msg = str(e).encode("utf-8", "replace")[:220]
        struct.pack_into("<iiqiiif", shm, 0, SHM_MAGIC, ST_ERROR, 0, 0, 0, 0, 0)
        shm[36:36 + len(msg)] = msg
        shm[36 + len(msg):256] = b"\x00" * (256 - 36 - len(msg))
        try:
            shm.close()
        except Exception:
            pass
        _die(str(e))
        return

    # 读回实际分辨率（摄像头可能拒绝请求值）
    try:
        aw = int(cam.get_prop(3) or width)      # CAP_PROP_FRAME_WIDTH=3
        ah = int(cam.get_prop(4) or height)     # CAP_PROP_FRAME_HEIGHT=4
        afps = float(cam.get_prop(5) or fps)    # CAP_PROP_FPS=5
    except Exception:
        aw, ah, afps = width, height, fps
    frame_bytes = aw * ah * 3
    if BUF_OFF + 2 * frame_bytes > shm.size():
        # 实际分辨率比请求大（摄像头回退到更高档）→ 共享内存不够
        struct.pack_into("<iiqiiif", shm, 0, SHM_MAGIC, ST_ERROR, 0, 0, aw, ah, afps)
        msg = f"实际分辨率 {aw}x{ah} 超出共享内存容量".encode("utf-8", "replace")[:220]
        shm[36:36 + len(msg)] = msg
        try:
            cam.close()
            shm.close()
        except Exception:
            pass
        _die(f"实际分辨率 {aw}x{ah} 超出共享内存容量")
        return

    buf0 = np_view(shm, BUF_OFF, aw, ah)
    buf1 = np_view(shm, BUF_OFF + frame_bytes, aw, ah)
    buffers = (buf0, buf1)

    # 发布 ready
    struct.pack_into("<iiqiiif", shm, 0,
                     SHM_MAGIC, ST_READY, 0, 0, aw, ah, afps)

    # 命令处理放独立线程：ENSURE（set FOURCC 触发流重启）会阻塞数秒，
    # 若与采集耦合在同一循环，read() 阻塞期间命令永远无法响应
    # （3 路同时流重启时实测全部超时）。ENSURE 期间暂停采集，
    # 避免 set() 与 read() 并发访问 VideoCapture。
    stop_event = threading.Event()
    paused = threading.Event()
    cmd_thread = threading.Thread(target=_cmd_loop,
                                  args=(cam, shm, stop_event, paused),
                                  daemon=True)
    cmd_thread.start()

    # 采集主循环：read → 写双缓冲 → 更新 header
    frame_id = 0
    no_frame_since = None   # 持续无帧计时起点（None=当前有帧）
    no_frame_logged = False
    try:
        while not stop_event.is_set():
            if paused.is_set():
                # 命令线程正在改流配置（ENSURE），暂停读帧
                time.sleep(0.02)
                continue
            frame = cam.read()
            if frame is None:
                # ⚠️ 持续无帧检测：READY 后 read 长期失败 = Win7 常见
                # 黑屏模式（isOpened True 但流已断）。必须留痕——
                # 之前无限空转、无任何日志，现场只能看到"黑屏+无报错"。
                now = time.time()
                if no_frame_since is None:
                    no_frame_since = now
                elif now - no_frame_since > 5.0 and not no_frame_logged:
                    no_frame_logged = True
                    try:
                        base = os.path.dirname(os.path.abspath(sys.executable))
                        path = os.path.join(base, "capture_worker.log")
                        with open(path, "a", encoding="utf-8") as f:
                            f.write("\n[%s] cam#%d READY 后持续无帧 >5s"
                                    "（流已断/驱动问题）\n" % (
                                        time.strftime("%H:%M:%S"), index))
                    except Exception:
                        pass
                time.sleep(0.005)
                continue
            no_frame_since = None
            # 尺寸防御：个别驱动偶发返回错误尺寸帧，跳过
            if frame.shape[0] != ah or frame.shape[1] != aw:
                continue
            buf = buffers[frame_id % 2]
            np.copyto(buf, frame)
            struct.pack_into("<iiqiiif", shm, 0,
                             SHM_MAGIC, ST_READY, frame_id, frame_id % 2, aw, ah, afps)
            frame_id += 1
    except Exception:
        import traceback as _tb
        try:
            base = os.path.dirname(os.path.abspath(sys.executable))
            path = os.path.join(base, "capture_worker.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n[%s] worker crash:\n" % time.strftime("%H:%M:%S"))
                _tb.print_exc(file=f)
        except Exception:
            pass
    finally:
        struct.pack_into("<iiqiiif", shm, 0,
                         SHM_MAGIC, ST_CLOSING, frame_id, frame_id % 2, aw, ah, afps)
        try:
            cam.close()
        except Exception:
            pass
        try:
            shm.close()
        except Exception:
            pass


def np_view(shm, offset, w, h):
    return np.frombuffer(shm, dtype=np.uint8, count=w * h * 3,
                         offset=offset).reshape(h, w, 3)


def _cmd_loop(cam, shm, stop_event, paused):
    """命令线程：轮询共享内存命令区（~10ms）。

    ⚠️ 发布顺序约定：主进程先写 text/len 再写 seq（seq 是发布标志）；
    这里先读 seq 再读 text 即可拿到完整命令。响应同理：先写
    text/len 再写 seq。
    """
    last_cmd_seq = 0
    try:
        while not stop_event.is_set():
            try:
                cseq = struct.unpack_from("<i", shm, CMD_SEQ_OFF)[0]
            except Exception:
                cseq = last_cmd_seq
            if cseq != last_cmd_seq:
                last_cmd_seq = cseq
                clen = struct.unpack_from("<i", shm, CMD_LEN_OFF)[0]
                clen = max(0, min(clen, CMD_TEXT_LEN))
                cmd_text = bytes(shm[CMD_TEXT_OFF:CMD_TEXT_OFF + clen]).decode(
                    "utf-8", "replace").strip()
                parts = cmd_text.split()
                cmd = parts[0].upper() if parts else ""
                if cmd == "QUIT":
                    stop_event.set()
                    return
                # ENSURE 触发流重启会阻塞数秒：暂停采集避免并发访问 cap
                if cmd == "ENSURE":
                    paused.set()
                    try:
                        resp = _handle_cmd(cam, cmd_text)
                    finally:
                        paused.clear()
                else:
                    resp = _handle_cmd(cam, cmd_text)
                rdata = resp.encode("utf-8", "replace")[:RESP_TEXT_LEN]
                struct.pack_into("<i", shm, RESP_LEN_OFF, len(rdata))
                shm[RESP_TEXT_OFF:RESP_TEXT_OFF + len(rdata)] = rdata
                struct.pack_into("<i", shm, RESP_SEQ_OFF, cseq)
            time.sleep(0.01)
    except Exception:
        pass


def _handle_cmd(cam, cmd_text):
    """执行命令，返回响应文本（QUIT 返回 "QUIT"）。"""
    import json
    parts = cmd_text.split()
    cmd = parts[0].upper() if parts else ""
    try:
        if cmd == "QUIT":
            return "QUIT"
        elif cmd == "SET" and len(parts) >= 3:
            return "OK" if cam.set_prop(int(parts[1]), float(parts[2])) else "FAIL"
        elif cmd == "GET" and len(parts) >= 2:
            v = cam.get_prop(int(parts[1]))
            return ("V %.6f" % v) if v is not None else "NA"
        elif cmd == "ENSURE" and len(parts) >= 5:
            ok, aw, ah, afps = cam.ensure_stream_config(
                int(parts[1]), int(parts[2]), float(parts[3]), parts[4])
            return ("OK %d %d %.4f" % (aw, ah, afps)) if ok else \
                   ("FAIL %d %d %.4f" % (aw, ah, afps))
        elif cmd == "SNAP":
            return "SNAP " + json.dumps(cam.take_snapshot().to_dict())
        elif cmd == "APPLY":
            from core.camera import CameraSnapshot
            cam.apply_snapshot(CameraSnapshot.from_dict(
                json.loads(" ".join(parts[1:]))))
            return "OK"
        elif cmd == "RES":
            return "RES " + json.dumps(
                [[int(a), int(b)] for a, b in cam.list_resolutions()])
        else:
            return "ERR unknown cmd"
    except Exception as e:
        return "ERR %r" % e


def _die(msg):
    """致命错误：写 stderr + exe 旁 capture_worker.log（Win7 现场可查）。"""
    try:
        sys.stderr.write("[capture-worker] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        base = os.path.dirname(os.path.abspath(sys.executable))
        path = os.path.join(base, "capture_worker.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


if __name__ == "__main__":
    worker_main(sys.argv[1:])

# -*- coding: utf-8 -*-
"""
深度版录制器 — Intel RealSense D415 双流录制（独立脚本，不动 VideoRec 主程序）

功能：
  - 同时录制 RGB 视频 + 深度（16bit PNG 序列），硬件同步、SDK 对齐
  - RGB 可直接进 MovAl 现有管线（YOLO 关键点 -> 行为分析）
  - 深度留作高度/站立/蜷缩/叠压等指标（以后加分析模块）

用法：
  python depth_recorder.py --outdir D:\data\session1
  python depth_recorder.py --outdir D:\data\session1 --duration 600 --fps 30
  python depth_recorder.py --outdir D:\data\session1 --laser 120 --filters on

输出（--outdir 下）：
  RGB.avi              彩色视频（MJPG，OpenCV 可直接读）
  depth/000000.png ... 深度 PNG 序列（16bit 单通道，单位毫米，0=无数据）
  timestamps.csv       每帧时间戳（硬件时钟，RGB/深度同源同步）
  meta.json            参数记录（分辨率/帧率/激光功率/滤波器设置）

停止：按 Esc（预览窗口）或 Ctrl+C。
依赖：pip install pyrealsense2 opencv-python numpy
注意：必须插 USB 3.0 口；D415 顶视架高建议 40cm+（站立时深度不掉出 0.3m 下限）。
"""

import argparse
import csv
import json
import signal
import sys
import time
from pathlib import Path

import cv2
import numpy as np

try:
    import pyrealsense2 as rs
except ImportError:
    print("[错误] 缺少 pyrealsense2，请先执行: pip install pyrealsense2")
    sys.exit(1)

STOP = False


def _signal_handler(sig, frame):
    global STOP
    STOP = True


def main():
    global STOP
    parser = argparse.ArgumentParser(description="RealSense D415 双流录制器")
    parser.add_argument("--outdir", type=str, required=True, help="输出目录")
    parser.add_argument("--duration", type=float, default=0, help="录制时长（秒），0=手动停止")
    parser.add_argument("--fps", type=int, default=30, choices=[15, 30, 60], help="帧率（默认30）")
    parser.add_argument("--width", type=int, default=1280, help="RGB/深度宽度（默认1280）")
    parser.add_argument("--height", type=int, default=720, help="RGB/深度高度（默认720）")
    parser.add_argument("--laser", type=int, default=150,
                        help="IR 激光功率 0-360（默认150；35-45cm 近距建议 100-200，降低箱壁反射）")
    parser.add_argument("--filters", choices=["on", "off"], default="on",
                        help="深度滤波器（空间+时间+空洞填充），默认 on")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)

    outdir = Path(args.outdir)
    depth_dir = outdir / "depth"
    outdir.mkdir(parents=True, exist_ok=True)
    depth_dir.mkdir(parents=True, exist_ok=True)

    # ── 初始化 RealSense pipeline ──────────────────────────────
    try:
        ctx = rs.context()
        if len(ctx.devices) == 0:
            print("[错误] 未检测到 RealSense 设备。请检查：USB 3.0 口 / 驱动 / 是否被其他软件占用")
            sys.exit(1)
        dev = ctx.devices[0]
        print(f"[设备] {dev.get_info(rs.camera_info.name)}  SN: {dev.get_info(rs.camera_info.serial_number)}")

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_stream(rs.stream.color, args.width, args.height, rs.format.bgr8, args.fps)
        config.enable_stream(rs.stream.depth, args.width, args.height, rs.format.z16, args.fps)

        # 对齐：深度图注册到彩色图坐标系（同尺寸同视野）
        align = rs.align(rs.stream.color)

        profile = pipeline.start(config)

        # 激光功率（近距降功率 -> 减少透明箱壁反射鬼影）
        depth_sensor = profile.get_device().first_depth_sensor()
        if depth_sensor.supports(rs.option.laser_power):
            depth_sensor.set_option(rs.option.laser_power, args.laser)
            print(f"[激光功率] {depth_sensor.get_option(rs.option.laser_power)}")

        # 深度滤波器（按推荐顺序：空间 -> 时间 -> 空洞填充）
        spatial = rs.spatial_filter()
        temporal = rs.temporal_filter()
        hole = rs.hole_filling_filter()

        # 预热（丢弃前 30 帧自动曝光收敛）
        print("[预热] 等待自动曝光收敛...")
        for _ in range(30):
            pipeline.wait_for_frames()
    except Exception as e:
        print(f"[错误] 初始化失败: {e}")
        sys.exit(1)

    # ── 输出文件 ────────────────────────────────────────────────
    rgb_path = outdir / "RGB.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(rgb_path), fourcc, args.fps, (args.width, args.height))
    if not writer.isOpened():
        print("[错误] RGB.avi 写入器打开失败（磁盘空间/权限？）")
        pipeline.stop()
        sys.exit(1)

    ts_path = outdir / "timestamps.csv"
    ts_file = open(ts_path, "w", newline="", encoding="utf-8")
    ts_writer = csv.writer(ts_file)
    ts_writer.writerow(["frame_idx", "rgb_ts_ms", "depth_ts_ms"])

    meta = {
        "device": "RealSense D415",
        "rgb": f"{args.width}x{args.height}@{args.fps} MJPG RGB.avi",
        "depth": f"{args.width}x{args.height}@{args.fps} 16bit PNG 序列 depth/ (单位mm)",
        "laser_power": args.laser,
        "filters": args.filters,
        "aligned": True,
        "note": "depth 0 值=无数据；与 RGB 帧号一一对应",
    }
    (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 录制主循环 ──────────────────────────────────────────────
    print(f"[开始录制] 输出: {outdir}   (Esc/Ctrl+C 停止, duration={args.duration}s)")
    t0 = time.time()
    frame_idx = 0
    try:
        while not STOP:
            frames = pipeline.wait_for_frames()
            aligned_frames = align.process(frames)
            color = aligned_frames.get_color_frame()
            depth = aligned_frames.get_depth_frame()
            if not color or not depth:
                continue

            rgb = np.asanyarray(color.get_data())
            d16 = np.asanyarray(depth.get_data())

            if args.filters == "on":
                d16 = spatial.process(depth)
                d16 = temporal.process(d16)
                d16 = hole.process(d16)
                d16 = np.asanyarray(d16.get_data())

            writer.write(rgb)
            cv2.imwrite(str(depth_dir / f"{frame_idx:06d}.png"), d16)
            ts_writer.writerow([frame_idx, color.get_timestamp(), depth.get_timestamp()])
            frame_idx += 1

            # 预览（RGB + 深度伪彩）
            preview = np.hstack([
                cv2.resize(rgb, (640, 360)),
                cv2.applyColorMap(cv2.convertScaleAbs(d16, alpha=0.08), cv2.COLORMAP_JET),
            ])
            cv2.imshow("D415 RGB | Depth", preview)
            if cv2.waitKey(1) & 0xFF == 27:  # Esc
                break

            if args.duration > 0 and (time.time() - t0) >= args.duration:
                break

            if frame_idx % 150 == 0:
                el = time.time() - t0
                print(f"  {frame_idx} 帧, {el:.0f}s, 实际 {frame_idx/max(el,0.1):.1f} fps")

    except Exception as e:
        print(f"[错误] 录制中断: {e}")
    finally:
        writer.release()
        ts_file.close()
        pipeline.stop()
        cv2.destroyAllWindows()
        el = time.time() - t0
        print(f"[完成] {frame_idx} 帧 / {el:.0f}s")
        print(f"  RGB : {rgb_path}")
        print(f"  深度: {depth_dir}  ({frame_idx} 张 PNG)")
        print(f"  时间戳: {ts_path}")


if __name__ == "__main__":
    main()

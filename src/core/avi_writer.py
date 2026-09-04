# -*- coding: utf-8 -*-
"""手写 AVI (MJPG) 写入器 —— 不依赖 OpenCV videoio/ffmpeg。

背景（Win7 现场）：cv2.VideoWriter 依赖 opencv_videoio_ffmpeg*.dll；
该 dll 在 Win7 上可能因运行库依赖加载失败 → VideoWriter 无后端 →
写 .avi 全部失败（"无法创建视频文件"）。MSMF 后端在 Win7 上又受限。

本模块只依赖：
  - cv2.imencode('.jpg', ...)  —— OpenCV imgcodecs 内置 libjpeg，
    与 videoio 完全无关，任何能读帧的 OpenCV 都能用
  - Python 内置 open/struct   —— 零依赖

输出为标准 RIFF AVI（MJPG 压缩，'00dc' 帧 + idx1 索引），
与 ffmpeg 写出的文件同格式，主流播放器可直接播放。
"""
from __future__ import annotations

import struct
import time

import cv2
import numpy as np

_FOURCC_MJPG = 0x47504A4D  # bytes 'M','J','P','G' 的 little-endian int
_AVIF_HASINDEX = 0x00000010
_KEYFRAME = 0x00000010


class AviMjpgWriter:
    """增量写 AVI MJPG。write() 逐帧追加，release() 收尾写索引。"""

    def __init__(self, path, width, height, fps, quality=90):
        self.path = str(path)
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.quality = int(quality)
        self._f = open(self.path, "wb")
        self._frames = 0
        self._movi_data_start = 0   # 第一帧 '00dc' 的偏移（相对文件头）
        self._idx = []              # (ckid, flags, offset, size)
        self._write_headers()
        self._closed = False

    # ---------- 头 ----------
    def _write_headers(self):
        w, h, fps = self.width, self.height, self.fps
        micro = int(round(1000000.0 / fps)) if fps > 0 else 33333
        rate = int(round(fps)) if fps > 0 else 30
        f = self._f

        # 占位 RIFF 头（总大小最后回填）
        f.write(b"RIFF" + struct.pack("<I", 0) + b"AVI ")

        # ---- LIST hdrl ----
        hdrl_body = b""
        # avih: 56 字节
        avih = struct.pack(
            "<IIIIIIIIII",
            micro,          # dwMicroSecPerFrame
            0,              # dwMaxBytesPerSec
            0,              # dwPaddingGranularity
            _AVIF_HASINDEX,  # dwFlags
            0,              # dwTotalFrames（回填）
            0,              # dwInitialFrames
            1,              # dwStreams
            0,              # dwSuggestedBufferSize
            w,              # dwWidth
            h,              # dwHeight
        ) + struct.pack("<IIII", 0, 0, 0, 0)  # dwReserved[4]
        hdrl_body += b"avih" + struct.pack("<I", len(avih)) + avih

        # ---- LIST strl ----
        strl_body = b""
        # strh: 56 字节
        strh = struct.pack(
            "<4s4sIHHIIIIIIII",
            b"vids", b"MJPG",
            0,              # dwFlags
            0, 0,           # wPriority, wLanguage
            0,              # dwInitialFrames
            1,              # dwScale
            rate,           # dwRate
            0,              # dwStart
            0,              # dwLength（回填）
            0,              # dwSuggestedBufferSize
            0xFFFFFFFF,     # dwQuality (-1)
            0,              # dwSampleSize
        ) + struct.pack("<4I", 0, 0, w, h)  # rcFrame
        strl_body += b"strh" + struct.pack("<I", len(strh)) + strh
        # strf: BITMAPINFOHEADER 40 字节
        strf = struct.pack(
            "<IiiHHIIiiII",
            40,             # biSize
            w,              # biWidth
            h,              # biHeight
            1,              # biPlanes
            24,             # biBitCount
            _FOURCC_MJPG,   # biCompression 'MJPG'
            w * h * 3,      # biSizeImage
            0, 0,           # biXPelsPerMeter, biYPelsPerMeter
            0, 0,           # biClrUsed, biClrImportant
        )
        strl_body += b"strf" + struct.pack("<I", len(strf)) + strf
        hdrl_body += (b"LIST" + struct.pack("<I", 4 + len(strl_body))
                      + b"strl" + strl_body)

        f.write(b"LIST" + struct.pack("<I", 4 + len(hdrl_body))
                + b"hdrl" + hdrl_body)

        # ---- LIST movi（占位，第一帧写入时回填起点） ----
        self._movi_list_pos = f.tell()
        f.write(b"LIST" + struct.pack("<I", 0) + b"movi")
        self._movi_data_start = f.tell()

    # ---------- 写帧 ----------
    def write(self, bgr_frame):
        if self._closed:
            raise RuntimeError("AviMjpgWriter 已 release")
        ok, jpg = cv2.imencode(
            ".jpg", bgr_frame,
            [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        data = jpg.tobytes()
        offset = self._f.tell() - self._movi_data_start
        self._f.write(b"00dc" + struct.pack("<I", len(data)) + data)
        if len(data) % 2 == 1:          # RIFF 2 字节对齐
            self._f.write(b"\x00")
        self._idx.append((offset, len(data)))
        self._frames += 1

    # ---------- 收尾 ----------
    def release(self):
        if self._closed:
            return
        self._closed = True
        f = self._f
        n = self._frames

        # idx1 索引
        idx_body = b""
        for offset, size in self._idx:
            idx_body += struct.pack(
                "<4sIII", b"00dc", _KEYFRAME, offset, size)
        f.write(b"idx1" + struct.pack("<I", len(idx_body)) + idx_body)

        # 回填各尺寸
        file_size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", file_size - 8))       # RIFF size
        f.seek(self._movi_list_pos + 4)
        movi_size = file_size - self._movi_list_pos - 8
        f.write(struct.pack("<I", 4 + movi_size))       # LIST movi size
        f.seek(0)
        f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()

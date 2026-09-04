# -*- coding: utf-8 -*-
"""参考帧管理 + Ghost overlay + ORB 自动对齐。

核心概念：
  - 参考帧 (reference frame)：第一天录制时保存的一帧，作为后续对齐基准
  - Ghost overlay：预览时半透明叠加参考帧，肉眼对齐位置/角度/缩放
  - ORB 自动对齐：特征匹配计算当前帧 -> 参考帧的单应变换，给出位移/缩放建议
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np


@dataclass
class AlignmentResult:
    """当前帧相对参考帧的对齐信息。"""

    ok: bool = False
    dx: float = 0.0      # 水平位移（像素，正=参考需右移/当前偏左）
    dy: float = 0.0      # 垂直位移
    scale: float = 1.0   # 缩放比（>1 当前画面更大）
    angle: float = 0.0   # 旋转角度（度）
    n_matches: int = 0
    n_inliers: int = 0
    confidence: float = 0.0   # 0-1 对齐置信度（基于匹配质量 + inlier 比例）
    homography: Optional[np.ndarray] = None

    def describe(self) -> str:
        if not self.ok:
            return "无法自动对齐（特征点不足或匹配质量低）"
        parts = []
        # 始终报告位移量，不再用 2px 阈值掩盖真实偏差
        if abs(self.dx) > 1 or abs(self.dy) > 1:
            parts.append(f"位移 ({self.dx:+.0f}, {self.dy:+.0f})px")
        else:
            parts.append("位移 ≈0px")
        if abs(self.scale - 1.0) > 0.02:
            parts.append(f"缩放 {self.scale:.2f}x")
        if abs(self.angle) > 1:
            parts.append(f"旋转 {self.angle:+.1f}°")
        if self.confidence < 0.5:
            return "、".join(parts) + "（⚠ 匹配质量低，虚影仅供参考，请手动对齐）"
        if abs(self.dx) <= 2 and abs(self.dy) <= 2 and abs(self.scale - 1.0) <= 0.02 and abs(self.angle) <= 1:
            return "位置基本重合 ✅"
        return "、".join(parts) + "（调整相机位置）"


class ReferenceManager:
    """管理一个 project 的参考帧和参数快照。

    4 路版：每路一个实例（slot 区分文件名），互不干扰。
    slot=None 时为旧版单路行为（reference.png）。
    """

    def __init__(self, project_dir: str | Path, slot: int = None):
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        if slot is None:
            self.ref_path = self.project_dir / "reference.png"
            self.snapshot_path = self.project_dir / "reference_camera.json"
        else:
            self.ref_path = self.project_dir / f"reference_cam{slot + 1}.png"
            self.snapshot_path = self.project_dir / f"reference_cam{slot + 1}_camera.json"
        self._ref_frame: Optional[np.ndarray] = None
        self._ref_metrics = None

        # ORB 特征器（单例，避免重复创建）
        self._orb = cv2.ORB_create(nfeatures=1000)
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self._ref_keypoints = None
        self._ref_descriptors = None

        # ORB 关注区域（帧坐标 x,y,w,h；None = 全图）。用于把特征点限制在
        # 装置/背景等静态区域，避开小鼠活动造成的干扰匹配。
        self._roi: Optional[Tuple[int, int, int, int]] = None
        self._indexed_roi: Optional[Tuple[int, int, int, int]] = None

    # ---------- 参考帧读写 ----------

    def has_reference(self) -> bool:
        return self.ref_path.exists()

    def load_reference(self) -> Optional[np.ndarray]:
        if self.ref_path.exists():
            self._ref_frame = cv2.imread(str(self.ref_path))
            if self._ref_frame is not None:
                self._index_reference()
                self._indexed_roi = self._roi
        return self._ref_frame

    def save_reference(self, frame: np.ndarray, camera_snapshot=None) -> None:
        """保存参考帧（+ 可选摄像头参数快照）。"""
        cv2.imwrite(str(self.ref_path), frame)
        self._ref_frame = frame.copy()
        self._index_reference()
        self._indexed_roi = self._roi
        if camera_snapshot is not None:
            camera_snapshot.save(str(self.snapshot_path))

    # ---------- ORB 关注区域 ----------

    def set_roi(self, x: int, y: int, w: int, h: int):
        """限定 ORB 特征检测区域（帧坐标）。用于避开小鼠等动态干扰。"""
        self._roi = (int(x), int(y), int(w), int(h))
        self._ensure_indexed()
        return self._roi

    def clear_roi(self):
        """取消关注区域，回到全图检测。"""
        self._roi = None
        self._ensure_indexed()

    def get_roi(self) -> Optional[Tuple[int, int, int, int]]:
        return self._roi

    def _mask_from_roi(self, shape_hw) -> Optional[np.ndarray]:
        """根据 ROI 生成检测掩码（全图返回 None）。"""
        if self._roi is None:
            return None
        h, w = shape_hw
        x, y, rw, rh = self._roi
        mask = np.zeros((h, w), dtype=np.uint8)
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + rw, w), min(y + rh, h)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 255
        return mask

    def _ensure_indexed(self):
        """ROI 变化时重建参考帧特征索引。"""
        if (self._ref_frame is not None
                and self._indexed_roi != self._roi):
            self._index_reference()
            self._indexed_roi = self._roi

    def _index_reference(self):
        """预计算参考帧的 ORB 特征（按当前 ROI），加速后续对齐。"""
        if self._ref_frame is None:
            self._ref_keypoints = None
            self._ref_descriptors = None
            return
        gray = cv2.cvtColor(self._ref_frame, cv2.COLOR_BGR2GRAY)
        mask = self._mask_from_roi(gray.shape)
        self._ref_keypoints, self._ref_descriptors = self._orb.detectAndCompute(gray, mask)

    # ---------- Ghost overlay ----------

    def ghost_overlay(self, frame: np.ndarray, alpha: float = 0.4) -> np.ndarray:
        """把参考帧半透明叠加到当前帧（虚影），尺寸自适应。"""
        if self._ref_frame is None:
            return frame
        h, w = frame.shape[:2]
        ref = cv2.resize(self._ref_frame, (w, h))
        return cv2.addWeighted(frame, 1 - alpha, ref, alpha, 0)

    # ---------- ORB 自动对齐 ----------

    def align(self, frame: np.ndarray) -> AlignmentResult:
        """计算当前帧相对参考帧的位移/缩放/旋转。

        质量把关（2026-08-03 修复误报"匹配完好"）：
          1. 绝对距离阈值：过差的匹配直接丢弃
          2. Lowe ratio test：区分独特征 vs 歧义特征
          3. inlier 比例检查：RANSAC 一致集过小视为不可靠
          4. 置信度 = f(匹配距离, inlier 比例)，低置信不报"重合"
        """
        res = AlignmentResult()
        if self._ref_frame is None or self._ref_descriptors is None:
            return res
        self._ensure_indexed()          # ROI 变了就重建参考特征
        if self._ref_descriptors is None:
            return res
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = self._mask_from_roi(gray.shape)
        kp, desc = self._orb.detectAndCompute(gray, mask)
        if desc is None or len(kp) < 10:
            return res

        # 1) 距离阈值：ORB 用 HAMMING 距离，经验上 < 64 才算可靠匹配
        #    同时保留最好的 120 个，防止画面变化后全是噪声匹配
        matches = self._bf.match(self._ref_descriptors, desc)
        matches = [m for m in matches if m.distance < 64]
        matches = sorted(matches, key=lambda m: m.distance)[:120]
        if len(matches) < 10:
            return res

        src_pts = np.float32([self._ref_keypoints[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        if H is None or mask is None:
            return res
        inliers = int(mask.sum())
        # 2) inlier 比例：< 30% 的匹配支持同一变换 -> 结果不可信
        inlier_ratio = inliers / len(matches)
        if inliers < 10 or inlier_ratio < 0.3:
            return res

        # 3) 置信度：平均距离越小、inlier 比例越高 -> 越可信
        mean_dist = float(np.mean([m.distance for m in matches]))
        conf_dist = max(0.0, 1.0 - mean_dist / 64.0)      # 距离项 0-1
        conf_ratio = min(1.0, inlier_ratio / 0.6)          # 比例项 0-1
        confidence = round(0.5 * conf_dist + 0.5 * conf_ratio, 3)

        # 从单应矩阵提取平移/缩放/旋转（近似，忽略透视）
        # H = [a b tx; c d ty; ...]，取左上 2x2
        a, b, c, d = H[0, 0], H[0, 1], H[1, 0], H[1, 1]
        scale = float(np.sqrt(abs(a * d - b * c)))
        angle = float(np.degrees(np.arctan2(c, a)))
        res.ok = True
        res.dx = float(H[0, 2])
        res.dy = float(H[1, 2])
        res.scale = round(scale, 3)
        res.angle = round(angle, 2)
        res.n_matches = len(matches)
        res.n_inliers = inliers
        res.confidence = confidence
        res.homography = H
        return res

    # ---------- 对齐预览（把当前帧变换到参考视角） ----------

    def warp_to_reference(self, frame: np.ndarray, H: np.ndarray) -> np.ndarray:
        """用单应矩阵把当前帧变换到参考帧视角（叠加对比用）。"""
        h, w = self._ref_frame.shape[:2]
        return cv2.warpPerspective(frame, H, (w, h))

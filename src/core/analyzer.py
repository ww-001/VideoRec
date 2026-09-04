# -*- coding: utf-8 -*-
"""画面一致性分析：亮度/对比度/直方图/模糊度/过曝欠曝。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class FrameMetrics:
    """单帧画面指标。"""

    mean_brightness: float = 0.0      # 0-255 平均亮度
    contrast: float = 0.0             # 灰度标准差（对比度）
    overexposed: float = 0.0          # 过曝像素比例 0-1（>250）
    underexposed: float = 0.0         # 欠曝像素比例 0-1（<5）
    blur: float = 0.0                 # Laplacian 方差（越小越模糊）
    hist: np.ndarray = field(default_factory=lambda: np.zeros(256, dtype=np.float64))
    n_pixels: int = 0

    def to_dict(self) -> dict:
        return {
            "mean_brightness": round(self.mean_brightness, 2),
            "contrast": round(self.contrast, 2),
            "overexposed": round(self.overexposed, 4),
            "underexposed": round(self.underexposed, 4),
            "blur": round(self.blur, 2),
        }


@dataclass
class ComparisonResult:
    """当前帧 vs 参考帧的一致性对比结果。"""

    brightness_diff: float = 0.0      # 绝对亮度差
    brightness_diff_pct: float = 0.0  # 相对参考的百分比
    contrast_diff: float = 0.0        # 对比度差
    hist_corr: float = 1.0            # 直方图相关性 (0-1)
    verdict: str = "OK"               # OK / 注意 / 调整
    suggestions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "brightness_diff": round(self.brightness_diff, 2),
            "brightness_diff_pct": round(self.brightness_diff_pct, 1),
            "contrast_diff": round(self.contrast_diff, 2),
            "hist_corr": round(self.hist_corr, 3),
            "verdict": self.verdict,
            "suggestions": self.suggestions,
        }


def compute_metrics(frame: np.ndarray) -> FrameMetrics:
    """计算单帧指标（降采样加速：画质指标在缩放图上算，不影响相对比较）。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 降采样到最长边 480，Laplacian/直方图计算量降到 ~1/10
    h, w = gray.shape[:2]
    if max(h, w) > 480:
        scale = 480.0 / max(h, w)
        gray_small = cv2.resize(gray, (int(w * scale), int(h * scale)),
                                interpolation=cv2.INTER_AREA)
    else:
        gray_small = gray
    m = FrameMetrics(
        mean_brightness=float(gray.mean()),
        contrast=float(gray.std()),
        overexposed=float((gray > 250).mean()),
        underexposed=float((gray < 5).mean()),
        blur=float(cv2.Laplacian(gray_small, cv2.CV_64F).var()),
        n_pixels=int(gray.size),
    )
    m.hist = cv2.calcHist([gray_small], [0], None, [256], [0, 256]).flatten().astype(np.float64)
    if m.hist.sum() > 0:
        m.hist /= m.hist.sum()
    return m


def compare_frames(current: FrameMetrics, ref: FrameMetrics,
                   brightness_threshold_pct: float = 10.0,
                   hist_corr_threshold: float = 0.9) -> ComparisonResult:
    """对比当前帧与参考帧，给出结论和建议。

    thresholds:
      brightness_diff_pct < 10%  -> OK
      10-20%                    -> 注意
      >20%                      -> 调整
    """
    res = ComparisonResult()
    if ref.n_pixels == 0:
        res.verdict = "无参考"
        res.suggestions = ["尚未设置参考帧"]
        return res

    res.brightness_diff = current.mean_brightness - ref.mean_brightness
    res.brightness_diff_pct = (abs(res.brightness_diff) / max(ref.mean_brightness, 1)) * 100
    res.contrast_diff = current.contrast - ref.contrast
    # 直方图相关性（Pearson）
    a = current.hist - current.hist.mean()
    b = ref.hist - ref.hist.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    res.hist_corr = float((a * b).sum() / denom) if denom > 0 else 1.0

    # 判定
    if res.brightness_diff_pct < brightness_threshold_pct and res.hist_corr > hist_corr_threshold:
        res.verdict = "OK"
        res.suggestions = ["条件与参考一致，可以录制"]
    else:
        if res.brightness_diff > 0:
            res.suggestions.append(f"当前偏亮 {res.brightness_diff_pct:.0f}%，建议降低曝光或减弱补光")
        elif res.brightness_diff < 0:
            res.suggestions.append(f"当前偏暗 {res.brightness_diff_pct:.0f}%，建议增加曝光或补光")
        if res.contrast_diff < -10:
            res.suggestions.append("对比度偏低，检查对焦或光源方向")
        elif res.contrast_diff > 10:
            res.suggestions.append("对比度偏高，可能光照过强")
        if current.overexposed > 0.01:
            res.suggestions.append(f"过曝像素 {current.overexposed * 100:.1f}%，注意高光区域")
        if current.underexposed > 0.01:
            res.suggestions.append(f"欠曝像素 {current.underexposed * 100:.1f}%，暗部细节丢失")
        if current.blur < 50:
            res.suggestions.append("画面偏模糊（对焦？）")
        if res.hist_corr < hist_corr_threshold:
            res.suggestions.append("直方图分布差异大，光照条件可能已改变")
        res.verdict = "注意" if res.brightness_diff_pct < 20 else "调整"
    return res

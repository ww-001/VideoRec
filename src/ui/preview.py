# -*- coding: utf-8 -*-
"""预览控件：显示摄像头画面，支持虚影叠加、网格、自动对齐提示、ORB 关注区域框选。"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import cv2
from qt_compat import Qt, pyqtSignal, QImage, QPainter, QPen, QColor, QWidget, mouse_pos


class PreviewWidget(QWidget):
    """OpenCV 画面预览 + 参考虚影叠加 + 网格 + ORB 关注区域。

    属性：
        ghost_alpha: 虚影透明度 (0-1)，0 不显示
        show_grid:   是否显示网格
        roi_frame_rect: 已确认的 ORB 关注区域（帧坐标）或 None
    """

    roi_selected = pyqtSignal(int, int, int, int)   # 帧坐标 (x, y, w, h)
    clicked = pyqtSignal(int)                       # 点击选中某路 (slot)

    def __init__(self, slot: int = 0, parent=None):
        super().__init__(parent)
        self.slot = slot
        self._frame: np.ndarray = None       # 原始 BGR 帧
        self._display: np.ndarray = None     # 叠加虚影后的显示帧
        self.ghost_alpha = 0.0
        self.show_grid = False
        self.alignment_text = ""
        self.label = f"Cam {slot + 1}"
        self.selected = False
        self.recording = False
        self.setMinimumSize(480, 360)

        # ORB 关注区域
        self.roi_mode = False                # 框选模式开关
        self.roi_frame_rect: Optional[Tuple[int, int, int, int]] = None
        self._roi_drag_rect: Optional[Tuple[int, int, int, int]] = None  # 拖画中（控件坐标）
        self._drag_start = None
        self._view_offset = (0, 0)           # 画面在控件内的偏移（paint 时更新）
        self._view_scale = 1.0               # 帧 -> 控件 缩放比
        self.setMouseTracking(True)

    def set_roi_mode(self, on: bool):
        """进入/退出框选模式。"""
        self.roi_mode = on
        self._drag_start = None
        self._roi_drag_rect = None
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.update()

    def clear_roi(self):
        """清除已确认的关注区域并退出框选。"""
        self.roi_frame_rect = None
        self.set_roi_mode(False)

    # ---------- 状态指示 ----------

    def set_label(self, text: str):
        self.label = text
        self.update()

    def set_selected(self, on: bool):
        self.selected = on
        self.update()

    def set_recording(self, on: bool):
        self.recording = on
        self.update()

    # ---------- 鼠标框选 ----------

    def mousePressEvent(self, event) -> None:
        if self.roi_mode and event.button() == Qt.LeftButton:
            self._drag_start = mouse_pos(event)
            self._roi_drag_rect = (self._drag_start.x(), self._drag_start.y(), 0, 0)
            self.update()
            return
        if self.roi_mode and event.button() == Qt.RightButton:
            self.set_roi_mode(False)   # 右键取消框选
            return
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.slot)   # 非框选模式点击 = 选中该路
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.roi_mode and self._drag_start is not None:
            p = mouse_pos(event)
            x0, y0 = self._drag_start.x(), self._drag_start.y()
            x = min(x0, p.x()); y = min(y0, p.y())
            self._roi_drag_rect = (x, y, abs(p.x() - x0), abs(p.y() - y0))
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.roi_mode and self._drag_start is not None and event.button() == Qt.LeftButton:
            r = self._roi_drag_rect
            self._drag_start = None
            self._roi_drag_rect = None
            if r is not None and r[2] > 10 and r[3] > 10:
                ox, oy = self._view_offset
                s = self._view_scale
                fx = int(round((r[0] - ox) / s))
                fy = int(round((r[1] - oy) / s))
                fw = int(round(r[2] / s))
                fh = int(round(r[3] / s))
                self.roi_frame_rect = (fx, fy, fw, fh)
                self.roi_selected.emit(fx, fy, fw, fh)
            self.update()
            return
        super().mouseReleaseEvent(event)

    def update_frame(self, frame: np.ndarray, display: np.ndarray = None,
                     alignment_text: str = "") -> None:
        """更新画面。display 为叠加后的帧；若为 None 则直接显示原帧。"""
        self._frame = frame
        self._display = display if display is not None else frame
        self.alignment_text = alignment_text
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))

        if self._display is None:
            painter.setPen(QPen(QColor(180, 180, 180)))
            painter.drawText(self.rect(), Qt.AlignCenter, "无画面")
            # 路名标签
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(10, 20, self.label)
            # 选中高亮
            if self.selected:
                pen = QPen(QColor(255, 165, 0), 3)
                painter.setPen(pen)
                painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
            return

        h, w = self._display.shape[:2]
        # BGR -> RGB888 再显示：Format_BGR888 直显在部分 Windows
        # 显卡驱动/旧系统（Win7）上有渲染异常（用户实测整格红屏），
        # RGB888 是 PyQt 显示 OpenCV 帧的标准做法，全平台安全。
        # 800x600 转换 ~1ms，预览帧率下开销可忽略。
        rgb = cv2.cvtColor(self._display, cv2.COLOR_BGR2RGB)
        img = QImage(rgb.data, w, h, 3 * w,
                     QImage.Format_RGB888).copy()
        # 缩放适应控件，保持宽高比
        # FastTransformation：Win7 老机器上 SmoothTransformation 太慢
        # （4 路 30fps 预览，每帧都做双线性插值 → CPU 吃紧）
        scaled = img.scaled(self.size(), Qt.KeepAspectRatio,
                            Qt.FastTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        self._view_offset = (x, y)
        self._view_scale = scaled.width() / w if w else 1.0

        # 网格
        if self.show_grid:
            pen = QPen(QColor(255, 255, 255, 80))
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            for i in range(1, 4):
                gx = x + scaled.width() * i / 4
                painter.drawLine(int(gx), y, int(gx), y + scaled.height())
            for i in range(1, 4):
                gy = y + scaled.height() * i / 4
                painter.drawLine(x, int(gy), x + scaled.width(), int(gy))

        # ORB 关注区域
        if self.roi_frame_rect is not None:
            fx, fy, fw, fh = self.roi_frame_rect
            s = self._view_scale
            rx = x + fx * s; ry = y + fy * s
            rw = fw * s; rh = fh * s
            pen = QPen(QColor(255, 200, 0, 255), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(int(rx), int(ry), int(rw), int(rh))
            painter.setPen(QPen(QColor(255, 220, 80)))
            painter.drawText(int(rx) + 6, int(ry) - 8, "ORB 关注区域")
        if self._roi_drag_rect is not None:
            dx, dy, dw, dh = self._roi_drag_rect
            pen = QPen(QColor(255, 200, 0, 255), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.drawRect(dx, dy, dw, dh)
        if self.roi_mode:
            painter.setPen(QPen(QColor(255, 200, 0)))
            painter.drawText(10, 50, "拖画框选 ORB 关注区域（右键取消）")

        # 对齐提示
        if self.alignment_text:
            painter.setPen(QPen(QColor(0, 255, 0)))
            painter.drawText(10, 25, self.alignment_text)

        # 路名标签（右上角）
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(10, 20, self.label)

        # 录制红点
        if self.recording:
            painter.setBrush(QColor(231, 76, 60))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(self.width() - 22, 10, 12, 12)
            # ⚠️ 关键：恢复无填充 brush！否则下面的 drawRect（选中边框）
            # 会用当前 brush 填充整个格子 → 录制中选中路"整格红屏"
            # （Qt drawRect 是填充+描边，不是只描边）
            painter.setBrush(Qt.NoBrush)

        # 选中高亮边框
        if self.selected:
            pen = QPen(QColor(255, 165, 0), 3)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)   # 双保险：只描边，绝不填充
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

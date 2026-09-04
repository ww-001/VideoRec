# -*- coding: utf-8 -*-
"""全屏框选覆盖层：拖画矩形选择屏幕触发区域。

用法：
    overlay = RegionSelectOverlay()
    overlay.region_selected.connect(on_selected)   # (x, y, w, h) 屏幕坐标
    overlay.show_fullscreen()
"""
from __future__ import annotations

from qt_compat import (Qt, pyqtSignal, QRect, QColor, QPainter, QPen,
                       QApplication, QWidget, mouse_global_pos)


class RegionSelectOverlay(QWidget):
    """半透明全屏覆盖层，用户拖拽画框选择区域。"""

    region_selected = pyqtSignal(int, int, int, int)  # x, y, w, h（屏幕坐标）
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self._start = None   # 拖拽起点（全局坐标）
        self._current = None # 当前鼠标位置
        self._selecting = False

    def show_fullscreen(self) -> None:
        """铺满主屏开始框选。"""
        screen = QApplication.primaryScreen()
        geo = screen.geometry()
        self.setGeometry(geo)
        self._start = None
        self._current = None
        self._selecting = False
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------- 鼠标事件 ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = mouse_global_pos(event)
            self._current = self._start
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._current = mouse_global_pos(event)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self._current = mouse_global_pos(event)
            self.update()
            if self._start is not None:
                rect = QRect(self._start, self._current).normalized()
                if rect.width() >= 10 and rect.height() >= 10:
                    self.hide()
                    self.region_selected.emit(
                        rect.x(), rect.y(), rect.width(), rect.height())
                    return
            # 太小视为误操作，留在框选模式
            self._start = None
            self._current = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.cancelled.emit()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # 半透明遮罩
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        if self._start is not None and self._current is not None:
            rect = QRect(self._start, self._current).normalized()
            # 挖空选中区域（画遮罩外部的四个矩形，更直观看到目标软件）
            mask = QColor(0, 0, 0, 100)
            painter.fillRect(self.rect().left(), self.rect().top(),
                             self.rect().width(), rect.top() - self.rect().top(), mask)
            painter.fillRect(self.rect().left(), rect.bottom(),
                             self.rect().width(), self.rect().bottom() - rect.bottom(), mask)
            painter.fillRect(self.rect().left(), rect.top(),
                             rect.left() - self.rect().left(), rect.height(), mask)
            painter.fillRect(rect.right(), rect.top(),
                             self.rect().right() - rect.right(), rect.height(), mask)

            # 选区边框 + 半透明填充
            pen = QPen(QColor(0, 200, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect)
            painter.fillRect(rect, QColor(0, 200, 255, 30))

            # 尺寸提示
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(rect.left() + 6, max(rect.top() - 8, 14),
                             f"{rect.width()} × {rect.height()}")

        # 顶部提示
        painter.setPen(QPen(QColor(255, 255, 255, 220)))
        painter.drawText(20, 30,
                         "拖拽框选触发区域（点击该区域即开始/停止录制）  |  Esc 取消")

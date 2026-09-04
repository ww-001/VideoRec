# -*- coding: utf-8 -*-
"""触发区域可视化覆盖层：在屏幕上显示半透明触发框。

特性：
  - 点击穿透（WindowTransparentForInput）：不拦截任何鼠标事件，
    框内点击照常到达 fiberphotometry 等其它软件
  - 置顶显示：VideoRec 最小化/在后台时框依然可见
  - flash()：触发命中时闪一下，作为"触发提示"反馈
"""
from __future__ import annotations

from qt_compat import Qt, QTimer, QColor, QPainter, QPen, QWidget


class TriggerOverlay(QWidget):
    """半透明触发区框（点击穿透 + 置顶）。"""

    BORDER = 3
    FILL = QColor(231, 76, 60, 36)       # 常态：红 14% 填充
    BORDER_COLOR = QColor(231, 76, 60, 220)
    FLASH_COLOR = QColor(241, 196, 15, 255)   # 触发闪黄
    REC_FILL = QColor(46, 204, 113, 42)       # 录制中：绿填充
    REC_BORDER = QColor(46, 204, 113, 240)    # 录制中：绿边框

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput  # 关键：点击穿透
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._flash_step = 0
        self._recording = False
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(110)
        self._flash_timer.timeout.connect(self._flash_tick)
        self.setRegion(100, 100, 200, 200)

    def setRecording(self, recording: bool):
        """录制状态：True = 绿框"录制中"，False = 红虚线"触发区"。"""
        self._recording = recording
        self.update()

    # ---------- 区域 ----------

    def setRegion(self, x: int, y: int, w: int, h: int):
        """移动到屏幕指定区域（屏幕坐标）。"""
        self.setGeometry(x - self.BORDER, y - self.BORDER,
                         w + self.BORDER * 2, h + self.BORDER * 2)

    def showRegion(self, x: int, y: int, w: int, h: int):
        self.setRegion(x, y, w, h)
        self.show()
        self.raise_()

    # ---------- 触发提示 ----------

    def flash(self):
        """触发命中反馈：边框闪黄 3 次渐隐。"""
        self._flash_step = 1
        self._flash_timer.start()
        self.update()

    def _flash_tick(self):
        self._flash_step += 1
        if self._flash_step > 3:
            self._flash_step = 0
            self._flash_timer.stop()
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(self.BORDER, self.BORDER,
                                    -self.BORDER, -self.BORDER)

        flashing = self._flash_step > 0
        recording = self._recording and not flashing
        # 填充
        fill = self.FILL
        if flashing:
            alpha = 36 + self._flash_step * 40
            fill = QColor(241, 196, 15, min(alpha, 150))
        elif recording:
            fill = self.REC_FILL
        painter.fillRect(rect, fill)

        # 边框（闪烁时加粗变黄；录制中为绿色实线；常态红色虚线）
        if flashing:
            pen = QPen(self.FLASH_COLOR, self.BORDER + 2, Qt.SolidLine)
        elif recording:
            pen = QPen(self.REC_BORDER, self.BORDER + 1, Qt.SolidLine)
        else:
            pen = QPen(self.BORDER_COLOR, self.BORDER, Qt.DashLine)
        painter.setPen(pen)
        painter.drawRect(rect)

        # 四角小方块（暗示可框选区域）
        if not flashing and not recording:
            painter.setPen(QPen(self.BORDER_COLOR, 1))
            painter.setBrush(self.BORDER_COLOR)
            c = self.BORDER + 4
            for cx, cy in ((c, c), (rect.right() - c, c),
                           (c, rect.bottom() - c), (rect.right() - c, rect.bottom() - c)):
                painter.drawRect(cx - 3, cy - 3, 6, 6)

        # 标签
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        if flashing:
            text = "⚡ 触发！"
        elif recording:
            text = "● 录制中（再点停止）"
        else:
            text = "触发区"
        painter.drawText(rect.adjusted(8, 6, -8, -6),
                         Qt.AlignTop | Qt.AlignLeft, text)

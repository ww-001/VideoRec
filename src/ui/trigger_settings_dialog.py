# -*- coding: utf-8 -*-
"""触发区域设置对话框：区域坐标/拖画框选/延时/半透明框，独立窗口。"""
from __future__ import annotations

from qt_compat import (QCheckBox, QDialog, QFormLayout, QGridLayout,
                       QPushButton, QSpinBox, QVBoxLayout)


class TriggerSettingsDialog(QDialog):
    """触发区域设置（独立窗口，非模态）。

    主窗口把本对话框的控件引用为 self.trig_*，原逻辑无需改动。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("触发区域设置")
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)

        # 区域坐标（2x2 紧凑排布）
        grid = QGridLayout()
        self.trig_x = QSpinBox(); self.trig_x.setRange(0, 8000); self.trig_x.setValue(600)
        self.trig_y = QSpinBox(); self.trig_y.setRange(0, 8000); self.trig_y.setValue(400)
        self.trig_w = QSpinBox(); self.trig_w.setRange(10, 4000); self.trig_w.setValue(200)
        self.trig_h = QSpinBox(); self.trig_h.setRange(10, 4000); self.trig_h.setValue(200)
        grid.addWidget(self._mk_label("X"), 0, 0)
        grid.addWidget(self.trig_x, 0, 1)
        grid.addWidget(self._mk_label("Y"), 0, 2)
        grid.addWidget(self.trig_y, 0, 3)
        grid.addWidget(self._mk_label("宽"), 1, 0)
        grid.addWidget(self.trig_w, 1, 1)
        grid.addWidget(self._mk_label("高"), 1, 2)
        grid.addWidget(self.trig_h, 1, 3)
        lay.addLayout(grid)

        # 拖画框选 + 延时
        form = QFormLayout()
        self.select_region_btn = QPushButton("🎯 拖画框选区域（全屏）")
        self.trig_delay = QSpinBox(); self.trig_delay.setRange(0, 5000)
        self.trig_delay.setValue(0); self.trig_delay.setSuffix(" ms")
        self.trig_delay.setToolTip("点击触发区域后延迟多久开始录制（0 = 立即）")
        form.addRow(self.select_region_btn)
        form.addRow("触发延时", self.trig_delay)
        lay.addLayout(form)

        # 半透明框开关
        self.trig_show_overlay = QCheckBox("显示触发区（半透明框，点击穿透）")
        self.trig_show_overlay.setChecked(True)
        lay.addWidget(self.trig_show_overlay)

        # 删除触发区（回调由主窗口提供）
        self.delete_region_btn = QPushButton("🗑 删除触发区")
        self.delete_region_btn.setStyleSheet("color:#e74c3c;")
        self.delete_region_btn.setToolTip("清除触发区域并关闭触发（录制不受影响）")
        self.delete_region_btn.clicked.connect(self._on_delete_clicked)
        lay.addWidget(self.delete_region_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)

        self.on_delete_region = None  # callable()，主窗口注入

    def _on_delete_clicked(self):
        if self.on_delete_region is not None:
            self.on_delete_region()

    @staticmethod
    def _mk_label(text: str):
        from qt_compat import QLabel
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#888;")
        return lbl

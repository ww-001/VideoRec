# -*- coding: utf-8 -*-
"""画面参数对话框：独立窗口调节摄像头参数，支持保存/加载。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from qt_compat import (QDialog, QHBoxLayout, QLabel, QMessageBox,
                       QPushButton, QVBoxLayout)

from .settings_panel import SettingsPanel


class SettingsDialog(QDialog):
    """承载 SettingsPanel 的独立窗口。

    非模态：边看预览边调参数。主窗口通过 .panel 访问面板。
    支持把参数存到项目目录（有项目时）或程序目录（全局默认）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("画面参数")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        self.panel = SettingsPanel()
        lay.addWidget(self.panel, 1)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("💾 保存参数")
        load_btn = QPushButton("📂 加载参数")
        save_btn.clicked.connect(self._save_params)
        load_btn.clicked.connect(self._load_params)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(load_btn)
        lay.addLayout(btn_row)

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color:#888;")
        self.path_label.setWordWrap(True)
        lay.addWidget(self.path_label)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)

    # ---------- 参数文件位置 ----------

    def params_path(self) -> Path:
        """有项目 → 项目目录/camera_params.json；否则 → 程序目录（exe 旁/源码根）。"""
        win = self.parent()
        if win is not None and getattr(win, "project_dir", None):
            return Path(win.project_dir) / "camera_params.json"
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).resolve().parent.parent
        return base / "camera_params.json"

    # ---------- 保存 / 加载 ----------

    def _save_params(self):
        p = self.params_path()
        try:
            p.write_text(
                json.dumps(self.panel.get_params(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            self.path_label.setText(f"✅ 已保存: {p.name}")
            self.path_label.setToolTip(str(p))
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _load_params(self):
        p = self.params_path()
        if not p.exists():
            QMessageBox.information(self, "加载参数", f"未找到参数文件：\n{p}")
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            changed = self.panel.set_params(data)
            self.path_label.setText(f"✅ 已加载: {p.name}")
            self.path_label.setToolTip(str(p))
            win = self.parent()
            if changed and win is not None and hasattr(win, "_reopen_camera_for_settings"):
                win._reopen_camera_for_settings()
        except Exception as e:
            QMessageBox.warning(self, "加载失败", str(e))

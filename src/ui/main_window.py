# -*- coding: utf-8 -*-
"""主窗口：预览 + 虚影对齐 + 量化对比 + 录制控制 + 触发设置。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from qt_compat import (Qt, QTimer, QColor, QKeySequence, QShortcut,
                       QCheckBox, QComboBox, QDialog, QFileDialog,
                       QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                       QLabel, QLineEdit, QMainWindow, QMessageBox,
                       QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter,
                       QVBoxLayout, QWidget, DialogCode, qt_exec, pyqtSignal)

from core.analyzer import compute_metrics, compare_frames
from core.camera import (Camera, WorkerCamera, estimate_raw_bandwidth_mbps,
                         list_cameras, pick_cameras_by_model,
                         camera_device_names)
from core.project import ProjectManager
from core.recorder import Recorder, RecorderThread
from core.reference import AlignmentResult, ReferenceManager
from core.trigger import MouseTrigger, TriggerRegion
from ui.preview import PreviewWidget
from ui.project_dialog import NewProjectDialog
from ui.region_select import RegionSelectOverlay
from ui.trigger_overlay import TriggerOverlay
from ui.settings_dialog import SettingsDialog
from ui.trigger_settings_dialog import TriggerSettingsDialog


def _default_projects_root() -> str:
    """默认项目目录：打包后跟随 exe，开发时跟随源码目录。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent / "projects")
    return str(Path(__file__).resolve().parent.parent / "projects")


class MainWindow(QMainWindow):
    """VideoRec 主窗口。"""

    # v10.14.11: 触发跨线程信号。pynput 的 on_click 回调在监听线程执行，
    # 不能直接调 QTimer.singleShot（无事件循环的线程里 timer 永不触发，
    # 这就是"触发区域设置没作用"的根因）。emit 线程安全，queued
    # connection 自动把槽调度回主线程。
    _trigger_clicked = pyqtSignal()

    def __init__(self, projects_root: str = None):
        super().__init__()
        if projects_root is None:
            projects_root = _default_projects_root()
        self.setWindowTitle("VideoRec 4路版 - 行为学录制条件控制")
        self.resize(1440, 900)

        self.projects = ProjectManager(projects_root)
        self.project = None
        self.project_dir = None
        self.cameras: list = []
        self._pending_cams: list = []      # 异步打开中: (slot, idx, cam, deadline)
        self._camera_poll_timer = None     # 500ms 轮询待就绪摄像头
        self._cam_refresh: dict = {}       # 本次刷新上下文（分辨率/检测总数）          # 每路一个 Camera（None = 未连接）
        # v10.14.9: 无帧检测状态在 __init__ 初始化（原来在 tick 里惰性
        # 初始化，_poll_pending_cams 先于首个 tick 执行时 AttributeError
        # → READY 后异常 → worker 泄漏占摄像头 → 后续所有打开失败。
        # Win7 现场 17:07 事故根因。）
        self._last_frame_time: dict = {}
        self._no_frame_warned: dict = {}
        self._no_frame_retried: dict = {}
        self._slot_req: dict = {}
        self.current_idx = 0             # 当前选中路
        self.ref_mgrs: dict = {}         # slot -> ReferenceManager（每路独立参考帧）
        self.recorder = None
        self.recorder_thread = None
        self._recorders: list = []       # 多路录制器
        self._recorder_threads: list = []
        self._recorder_slot_map: dict = {}   # slot -> RecorderThread（录制预览按槽位推送）
        self.trigger = MouseTrigger()
        # v10.14.11: 跨线程回调必须走 Qt 信号（见类属性注释）。点击区域后
        # pynput 线程 emit → queued connection → 主线程执行 _on_trigger_click。
        self.trigger.on_click = self._trigger_clicked.emit
        self._trigger_clicked.connect(self._on_trigger_click)
        self._trigger_overlay: TriggerOverlay | None = None
        self._recording = False
        # ORB 对齐节流状态（每 5 tick ≈ 150ms 算一次，避免拖垮预览帧率）
        self._align_counter = 0
        self._last_align = AlignmentResult()
        # 量化分析节流（每 10 tick ≈ 300ms 算一次，Win7 老机器扛不住每帧都算）
        self._metrics_counter = 0
        # 录制中掉帧预警（一次录制只弹一次）
        self._drop_warned = False
        # 录制计时（时长显示 / 到时自动停止）
        self._rec_start_time = 0.0
        self._rec_duration_limit = 0
        self._tick_count = 0
        self._auto_stop_reason = None
        # 每路动物信息（弹窗标记，持久化到录制目录 animal_info.json）
        self._animal_map: dict = {}
        # 录制备注
        self.note_edits: list = []
        self.note_btns: list = []
        self._note_layout = None

        self._build_ui()

        # 画面参数对话框（非模态，边看预览边调）
        self._settings_dialog = SettingsDialog(self)
        self.settings_panel = self._settings_dialog.panel
        self.settings_panel.on_res_fps_changed = self._reopen_camera_for_settings
        self.settings_panel.on_auto_restored = self._on_auto_restored

        # 触发区域设置对话框（非模态）；主窗口持控件引用，原逻辑不变
        self._trig_dialog = TriggerSettingsDialog(self)
        self.trig_x = self._trig_dialog.trig_x
        self.trig_y = self._trig_dialog.trig_y
        self.trig_w = self._trig_dialog.trig_w
        self.trig_h = self._trig_dialog.trig_h
        self.trig_delay = self._trig_dialog.trig_delay
        self.trig_show_overlay = self._trig_dialog.trig_show_overlay
        self._trig_dialog.select_region_btn.clicked.connect(self._select_trigger_region)
        for sb in (self.trig_x, self.trig_y, self.trig_w, self.trig_h):
            sb.valueChanged.connect(lambda _: self._sync_trigger_overlay())
        self.trig_show_overlay.toggled.connect(self._sync_trigger_overlay)

        # 预览定时器（30ms ≈ 33fps 刷新）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        # v10.14.11：Win7 卡顿优化 —— 待机 150ms(~7fps)，录像时 _start_recording 里降 200ms(5fps)。
        # 原 30ms(33fps) 在 Win7 老 CPU 上 4 路预览把主进程榨干（录像质量不受影响：
        # 录像由 worker 独立写盘，预览只是显示）。
        self.timer.start(150)

        self._refresh_camera_list()
        self._refresh_recent_projects()

    # ================= 当前路快捷引用 =================

    @property
    def camera(self):
        """当前选中路的 Camera（None = 未连接）。"""
        if self.current_idx < len(self.cameras):
            return self.cameras[self.current_idx]
        return None

    @property
    def preview(self):
        """当前选中路的预览控件。"""
        return self.previews[self.current_idx]

    def _set_current(self, slot: int):
        """切换当前选中路（点击预览触发）。

        幂等：slot == current_idx 时也刷新高亮——启动时 current_idx 默认 0
        （_refresh_camera_list 会调 _set_current(0)），若直接 return 则
        Cam1 永远没有选中边框，直到点过其他路再切回才出现（用户踩过）。
        """
        if slot < 0 or slot >= len(self.previews):
            return
        if slot == self.current_idx:
            self.previews[slot].set_selected(True)
            return
        self.current_idx = slot
        for i, pw in enumerate(self.previews):
            pw.set_selected(i == slot)
        # 参数面板绑定到新选中的路
        if self.camera is not None:
            self.settings_panel.set_camera(self.camera, slot=slot)
        self.status_label.setText(f"已切换到 Cam {slot + 1}"
                                  + (f"（#{self.camera.index}）" if self.camera else "（未连接）"))

    # ================= UI 构建 =================

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # ---- 帮助菜单 ----
        mb = self.menuBar()
        help_menu = mb.addMenu("帮助(&H)")
        help_menu.addAction("关于", self._show_about)

        # ---- 左侧：四路预览（2×2）+ 分析 ----
        left = QVBoxLayout()
        self.previews: list = []
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(4):
            pw = PreviewWidget(slot=i)
            pw.roi_selected.connect(self._on_roi_selected)
            pw.clicked.connect(self._set_current)
            self.previews.append(pw)
            grid.addWidget(pw, i // 2, i % 2)
        left.addLayout(grid, 1)

        # 分析面板（4 路预览占满左侧高度后会遮挡 → 移到右侧滚动区）
        self.analysis_box = QGroupBox("一致性分析（位置 + 光照）")
        ab = QVBoxLayout(self.analysis_box)
        self.metrics_label = QLabel("当前帧指标：--")
        self.compare_label = QLabel("对比参考：--")
        self.align_label = QLabel("位置：--")
        self.verdict_label = QLabel("")
        self.verdict_label.setStyleSheet("font-size:14pt; font-weight:bold;")
        ab.addWidget(self.metrics_label)
        ab.addWidget(self.compare_label)
        ab.addWidget(self.align_label)
        ab.addWidget(self.verdict_label)
        left.addWidget(self._build_control_bar())
        left.addStretch(0)

        # ---- 右侧：项目 + 参考帧 + 触发 + 备注 + 画面参数 + 一致性分析（可滚动，防挤压）----
        right = QVBoxLayout()
        right.addWidget(self._build_project_box())
        right.addWidget(self._build_ghost_box())
        right.addWidget(self._build_trigger_box())
        right.addWidget(self._build_notes_box())
        self.settings_btn = QPushButton("🎛 画面参数...")
        self.settings_btn.setToolTip("打开独立窗口调节亮度/对比度/曝光/分辨率/帧率")
        self.settings_btn.clicked.connect(self._open_settings_dialog)
        right.addWidget(self.settings_btn)
        right.addWidget(self._build_animal_box())
        right.addWidget(self.analysis_box)
        right.addStretch(1)

        splitter = QSplitter(Qt.Horizontal)
        left_w = QWidget()
        left_w.setLayout(left)
        right_w = QWidget()
        right_w.setLayout(right)
        # 右栏放进滚动区：窗口矮/全屏时参数不再被挤压
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right_w)
        right_scroll.setMinimumWidth(360)
        splitter.addWidget(left_w)
        splitter.addWidget(right_scroll)
        splitter.setSizes([850, 430])
        root.addWidget(splitter)
        self.setMinimumSize(1024, 620)

    def _show_about(self):
        """关于弹窗：软件信息（名称/型号/版本/功能）+ 底部 WWT 小字标识。"""
        from qt_compat import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        dlg = QDialog(self)
        dlg.setWindowTitle("关于")
        dlg.setFixedWidth(560)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(28, 28, 28, 22)
        lay.setSpacing(10)

        # 名称
        lbl_title = QLabel("VideoRec 4路版 - 行为学录制条件控制")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("font-size: 18px; color: #1B2A4A; font-weight: bold;")
        lay.addWidget(lbl_title)

        # 型号 + 版本
        lbl_model = QLabel("型号：VR4 系列   |   版本：v10.14.11")
        lbl_model.setAlignment(Qt.AlignCenter)
        lbl_model.setStyleSheet("font-size: 13px; color: #2E86DE;")
        lay.addWidget(lbl_model)

        # 分隔线
        line1 = QLabel("─" * 46)
        line1.setAlignment(Qt.AlignCenter)
        line1.setStyleSheet("color: #C9D6E8;")
        lay.addWidget(line1)

        # 功能介绍
        lbl_func = QLabel(
            "主要功能：\n"
            "· 4 路摄像头同步录制（AVI + 时间戳）\n"
            "· 触发录制与 TTL 同步（按键 / 区域触发）\n"
            "· 参考对齐与背景差分分析\n"
            "· 动物信息写入、项目管理与回放")
        lbl_func.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_func.setStyleSheet("font-size: 13px; color: #3A4A63; line-height: 1.6;")
        lay.addWidget(lbl_func)

        # 分隔线
        line2 = QLabel("─" * 46)
        line2.setAlignment(Qt.AlignCenter)
        line2.setStyleSheet("color: #C9D6E8;")
        lay.addWidget(line2)

        # 底部 WWT 小字标识
        lbl_lab = QLabel("WWT Lab · Wired, We Think · 星河为络，思接苍穹")
        lbl_lab.setAlignment(Qt.AlignCenter)
        lbl_lab.setStyleSheet("font-size: 11px; color: #78849B;")
        lay.addWidget(lbl_lab)

        btn_ok = QPushButton("确定")
        btn_ok.setFixedWidth(120)
        btn_ok.clicked.connect(dlg.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_ok)
        row.addStretch(1)
        lay.addLayout(row)
        from qt_compat import exec_dialog
        exec_dialog(dlg)

    def _build_animal_box(self) -> QWidget:
        """动物信息常驻面板：每路一个输入框，录制时自动写入 meta.json。

        不弹窗不打断录制；某路空缺时录制开始给黄色警告（不阻止）。
        """
        box = QGroupBox("动物信息（录制时写入 meta.json）")
        form = QFormLayout(box)
        self.animal_edits: list = []
        for slot in range(4):
            edit = QLineEdit()
            edit.setPlaceholderText(f"如 M{slot + 1}-Ctrl-01 / 20260804")
            edit.textChanged.connect(lambda txt, s=slot: self._on_animal_edited(s, txt))
            self.animal_edits.append(edit)
            form.addRow(f"Cam {slot + 1}:", edit)
        return box

    def _on_animal_edited(self, slot: int, txt: str):
        self._animal_map[slot] = txt.strip()
        self._save_animal_info()

    def _save_animal_info(self):
        """持久化动物信息到当前项目录制目录（跨会话记忆）。"""
        if self.project is None:
            return
        try:
            import json as _json
            out_dir = self.projects.recordings_dir(self.project)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "animal_info.json").write_text(
                _json.dumps(self._animal_map, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    def _load_animal_info(self):
        """从项目录制目录加载动物信息并回填面板（blockSignals 防回写）。"""
        self._animal_map = {}
        if self.project is not None:
            try:
                import json as _json
                info_file = self.projects.recordings_dir(self.project) / "animal_info.json"
                if info_file.exists():
                    self._animal_map = {int(k): v for k, v in
                                        _json.loads(info_file.read_text(encoding="utf-8")).items()}
            except Exception:
                self._animal_map = {}
        for slot in range(4):
            edit = self.animal_edits[slot]
            edit.blockSignals(True)
            edit.setText(self._animal_map.get(slot, ""))
            edit.blockSignals(False)

    def _build_project_box(self) -> QWidget:
        box = QGroupBox("项目 (Project)")
        form = QFormLayout(box)
        self.project_name_edit = QLineEdit()
        self.project_name_edit.setPlaceholderText("如 social_interaction_2026")
        self.project_name_edit.setReadOnly(True)
        self.new_project_btn = QPushButton("新建项目")
        self.new_project_btn.clicked.connect(self._create_project)
        self.load_project_btn = QPushButton("打开项目...")
        self.load_project_btn.clicked.connect(self._load_project)
        self.recent_combo = QComboBox()
        self.recent_combo.setPlaceholderText("最近项目...")
        self.recent_combo.activated.connect(self._load_recent_project)
        # 录像保存位置（只读显示，防止录到别处找不到）
        self.rec_path_label = QLabel("--")
        self.rec_path_label.setWordWrap(True)
        self.rec_path_label.setStyleSheet("color:#7f8c8d; font-size:9pt;")
        form.addRow("项目名", self.project_name_edit)
        form.addRow(self.new_project_btn, self.load_project_btn)
        form.addRow("最近", self.recent_combo)
        form.addRow("录像目录", self.rec_path_label)
        return box

    def _refresh_rec_path_label(self):
        """刷新录像目录显示；路径在本程序之外时红色警告。"""
        if self.project is None:
            self.rec_path_label.setText("--")
            self.rec_path_label.setStyleSheet("color:#7f8c8d; font-size:9pt;")
            return
        rec_dir = self.projects.recordings_dir(self.project)
        try:
            app_root = Path(__file__).resolve().parent.parent  # D:\video_rec4
            outside = not str(rec_dir.resolve()).lower().startswith(str(app_root.resolve()).lower())
        except Exception:
            outside = False
        if outside:
            self.rec_path_label.setText(f"⚠ {rec_dir}\n（在当前程序目录之外，请检查！）")
            self.rec_path_label.setStyleSheet("color:#e74c3c; font-weight:bold; font-size:9pt;")
        else:
            self.rec_path_label.setText(str(rec_dir))
            self.rec_path_label.setStyleSheet("color:#7f8c8d; font-size:9pt;")

    def _recent_file_path(self) -> Path:
        """全局最近项目记录文件（exe 同目录 / 源码根目录），跨目录记忆。"""
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent.parent
        return base / "recent_projects.json"

    def _remember_recent_project(self, path: str):
        """记录最近打开/创建的项目（最多 8 条，最近的在最前）。"""
        try:
            p = self._recent_file_path()
            items = []
            if p.exists():
                items = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
            items = [str(path)] + [i for i in items if i != str(path)]
            p.write_text(json.dumps(items[:8], ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception:
            pass

    def _refresh_recent_projects(self):
        """从全局最近项目文件加载列表（不限项目根目录，跨盘可用）。

        旧版只扫 exe 同目录 projects/ —— 用户项目建在 E 盘时列表永远
        是空的（"最近项目没显示、下拉拉不动"）。改为记忆实际路径。
        """
        self.recent_combo.clear()
        items = []
        try:
            p = self._recent_file_path()
            if p.exists():
                items = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        valid = 0
        for path in items:
            if path and Path(path).joinpath("project.json").exists():
                self.recent_combo.addItem(Path(path).name, str(path))
                valid += 1
        if valid == 0:
            self.recent_combo.addItem("（暂无最近项目，请新建或打开）", None)
            try:
                self.recent_combo.model().item(0).setEnabled(False)
            except Exception:
                pass

    def _load_recent_project(self, index: int):
        if index < 0:
            return
        path = self.recent_combo.itemData(index)
        if not path:
            return
        proj = self.projects.load(path)
        if proj is None:
            QMessageBox.warning(self, "提示", "该目录不是有效项目")
            return
        self._apply_loaded_project(proj, path)

    def _build_ghost_box(self) -> QWidget:
        box = QGroupBox("参考帧与虚影")
        form = QFormLayout(box)
        self.set_ref_btn = QPushButton("📌 存当前帧为参考")
        self.set_ref_btn.clicked.connect(self._save_reference)
        self.load_ref_btn = QPushButton("加载参考帧")
        self.load_ref_btn.clicked.connect(self._load_reference)
        self.ghost_slider = QSlider(Qt.Horizontal)
        self.ghost_slider.setRange(0, 100)
        self.ghost_slider.setValue(40)
        self.ghost_slider.valueChanged.connect(self._update_ghost)
        self.grid_check = QCheckBox("显示网格")
        self.grid_check.toggled.connect(self._set_grid_all)
        self.auto_align_check = QCheckBox("ORB 自动对齐")
        self.auto_align_check.setChecked(True)
        self.roi_btn = QPushButton("🎯 ORB 关注区域")
        self.roi_btn.setCheckable(True)
        self.roi_btn.setToolTip("拖画框选装置/背景区域，ORB 只在该区域找特征点，避开小鼠活动干扰")
        self.roi_btn.toggled.connect(self._toggle_roi_mode)
        self.clear_roi_btn = QPushButton("清除")
        self.clear_roi_btn.clicked.connect(self._clear_roi)
        form.addRow(self.set_ref_btn, self.load_ref_btn)
        form.addRow("虚影透明度", self.ghost_slider)
        form.addRow(self.grid_check, self.auto_align_check)
        form.addRow(self.roi_btn, self.clear_roi_btn)
        return box

    def _set_grid_all(self, v: bool):
        """网格开关应用到全部四路预览。"""
        for pw in self.previews:
            pw.show_grid = v

    def _toggle_roi_mode(self, on: bool):
        self.preview.set_roi_mode(on)
        self.status_label.setText("在预览画面拖画框选 ORB 关注区域（右键取消）" if on
                                  else "ORB 关注区域框选已取消")

    def _on_roi_selected(self, x: int, y: int, w: int, h: int):
        """预览框选完成 -> 应用到当前路的参考管理器。"""
        self.roi_btn.setChecked(False)  # 退出框选模式
        mgr = self._ref_mgr()
        if mgr is not None:
            mgr.set_roi(x, y, w, h)
        self.status_label.setText(f"ORB 关注区域已设定: ({x},{y}) {w}x{h}（只在该区域找特征点）")

    def _clear_roi(self):
        self.roi_btn.setChecked(False)
        self.preview.clear_roi()
        mgr = self._ref_mgr()
        if mgr is not None:
            mgr.clear_roi()
        self.status_label.setText("ORB 关注区域已清除（恢复全图特征点）")

    def _build_trigger_box(self) -> QWidget:
        box = QGroupBox("鼠标区域触发（联动录制）")
        form = QFormLayout(box)
        self.trig_enable = QCheckBox("启用")
        self.trig_enable.toggled.connect(self._toggle_trigger)
        self.trig_info = QLabel("未启用")
        self.trig_info.setWordWrap(True)
        self.trig_settings_btn = QPushButton("🎯 触发区域设置...")
        self.trig_settings_btn.setToolTip("打开独立窗口：区域坐标/拖画框选/延时/半透明框")
        self.trig_settings_btn.clicked.connect(self._open_trigger_settings)
        form.addRow(self.trig_enable, self.trig_info)
        form.addRow(self.trig_settings_btn)
        return box

    def _open_settings_dialog(self):
        """打开画面参数独立窗口（非模态）。"""
        if self.camera is not None:
            self.settings_panel.set_camera(self.camera)
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _open_trigger_settings(self):
        """打开触发区域设置独立窗口（非模态）。"""
        self._trig_dialog.on_delete_region = self._delete_trigger_region
        self._trig_dialog.show()
        self._trig_dialog.raise_()
        self._trig_dialog.activateWindow()

    def _delete_trigger_region(self):
        """删除触发区：清除区域、隐藏半透明框、关闭触发。"""
        if self.project is not None:
            self.project.trigger = None
            self.project.trigger_delay_ms = 0
            self.projects.save(self.project)
        if self.trig_enable.isChecked():
            self.trig_enable.setChecked(False)
            self._toggle_trigger(False)
        self.trig_show_overlay.setChecked(False)
        self._sync_trigger_overlay(force_show=False)
        self.status_label.setText("触发区已删除（点击触发已关闭）")

    # ================= 录制备注 =================

    def _build_notes_box(self) -> QWidget:
        """预定义备注内容 + F1-F9 快捷键，录制中点按钮/按快捷键打点。"""
        box = QGroupBox("录制备注（F1-F9 快捷键打点）")
        v = QVBoxLayout(box)
        self._note_layout = QGridLayout()
        self._note_layout.setHorizontalSpacing(8)
        self._note_layout.setVerticalSpacing(4)
        v.addLayout(self._note_layout)
        for text in ("嗅探", "理毛", "爬跨", "攻击", "其他"):
            self._note_add_row(text)
        row = QHBoxLayout()
        add_btn = QPushButton("+ 添加")
        del_btn = QPushButton("- 删除")
        add_btn.clicked.connect(lambda: self._note_add_row(""))
        del_btn.clicked.connect(self._note_del_row)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        row.addStretch(1)
        v.addLayout(row)
        hint = QLabel("录制中按 F1-F9 或点按钮添加备注；停止后生成 notes.csv")
        hint.setStyleSheet("color:#888;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        # 快捷键（F1-F9，按行号对应）
        for i in range(9):
            sc = QShortcut(QKeySequence(f"F{i + 1}"), self)
            sc.activated.connect(lambda i=i: self._add_note(i))
        return box

    def _note_add_row(self, text: str):
        """新增一行备注：F键按钮 + 内容输入框，一列两个。"""
        idx = len(self.note_edits)
        row, col = idx // 2, (idx % 2) * 2
        btn = QPushButton(f"F{idx + 1}")
        edit = QLineEdit(text)
        edit.setPlaceholderText("备注内容")
        self._note_layout.addWidget(btn, row, col)
        self._note_layout.addWidget(edit, row, col + 1)
        self.note_edits.append(edit)
        self.note_btns.append(btn)
        btn.clicked.connect(lambda _, i=idx: self._add_note(i))

    def _note_del_row(self):
        """删除最后一行（至少保留一行）。"""
        if len(self.note_edits) <= 1:
            return
        idx = len(self.note_edits) - 1
        row, col = idx // 2, (idx % 2) * 2
        edit = self.note_edits.pop()
        btn = self.note_btns.pop()
        self._note_layout.removeWidget(btn); btn.deleteLater()
        self._note_layout.removeWidget(edit); edit.deleteLater()

    def _add_note(self, i: int):
        """录制中在当前位置打一个备注（打到当前选中路）。"""
        rt = self._recorder_threads[self.current_idx] if self.current_idx < len(self._recorder_threads) else None
        if rt is None or not rt.is_recording:
            self.status_label.setText(f"Cam {self.current_idx + 1} 未在录制，备注不生效")
            return
        if i >= len(self.note_edits):
            return
        content = self.note_edits[i].text().strip()
        if not content:
            self.status_label.setText(f"F{i + 1} 备注内容为空，请先填写")
            return
        idx = rt.add_note(content)
        self.status_label.setText(
            f"✓ 备注{idx} (Cam {self.current_idx + 1}): {content} @ {rt.elapsed_s:.1f}s 帧{rt.frame_count}")

    def _sync_trigger_overlay(self, force_show: bool = False):
        """同步/显示/隐藏触发区半透明框（纯视觉，不影响任何点击）。"""
        want_show = force_show or self.trig_show_overlay.isChecked()
        if not want_show:
            if self._trigger_overlay is not None:
                self._trigger_overlay.hide()
            return
        if self._trigger_overlay is None:
            self._trigger_overlay = TriggerOverlay()  # 顶层窗口，不随主窗隐藏
        self._trigger_overlay.showRegion(
            self.trig_x.value(), self.trig_y.value(),
            self.trig_w.value(), self.trig_h.value())

    def _select_trigger_region(self):
        """全屏拖画选择触发区域。"""
        self._region_overlay = RegionSelectOverlay(self)
        self._region_overlay.region_selected.connect(self._on_region_selected)
        self._region_overlay.cancelled.connect(
            lambda: self.status_label.setText("框选已取消"))
        self._region_overlay.show_fullscreen()

    def _on_region_selected(self, x: int, y: int, w: int, h: int):
        self.trig_x.setValue(x)
        self.trig_y.setValue(y)
        self.trig_w.setValue(w)
        self.trig_h.setValue(h)
        self._save_trigger_to_project()
        self.status_label.setText(f"触发区域已设定: ({x},{y}) {w}x{h}")
        self._sync_trigger_overlay(force_show=True)
        if self.trig_enable.isChecked():
            self._toggle_trigger(True)  # 刷新监听区域

    def _build_control_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        self.cam_combo = None
        self.record_btn = QPushButton("● 开始录制")
        self.record_btn.setStyleSheet("background:#e74c3c; color:white; font-weight:bold; padding:6px 16px;")
        self.record_btn.clicked.connect(self._toggle_record)

        # ---- 定时录制 + 录制时长（0 = 不限）----
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 3600)
        self.delay_spin.setValue(0)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setToolTip("定时录制：点击【⏱ 定时录制】后延迟多少秒自动开始（0 = 立即）")
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 7200)
        self.duration_spin.setValue(0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setToolTip("录制时长：录满该秒数自动停止（0 = 不限时）")
        self.sched_btn = QPushButton("定时录制")
        self.sched_btn.setToolTip("延迟 N 秒后自动开始录制；倒计时中再点一次 = 取消")
        self.sched_btn.clicked.connect(self._on_schedule_record)
        self._sched_timer = None   # 定时倒计时 QTimer

        self.status_label = QLabel("未连接摄像头")
        h.addWidget(self.record_btn)
        h.addWidget(QLabel("延迟"))
        h.addWidget(self.delay_spin)
        h.addWidget(QLabel("时长"))
        h.addWidget(self.duration_spin)
        h.addWidget(self.sched_btn)
        h.addWidget(self.status_label, 1)
        return bar

    # ================= 摄像头 =================

    def _refresh_camera_list(self):
        """检测可用摄像头并自动打开（最多 4 路，依次填满槽位）。

        超过 4 个摄像头时：优先选择同型号数量最多的 4 个
        （如 4 个同型号 USB CAMERA + 1 个其他型号 → 自动选 4 个同型号）。
        """
        try:
            idxs = list_cameras()
        except Exception:
            idxs = []
        if not idxs:
            self.status_label.setText("未检测到摄像头")
            return
        if len(idxs) > 4:
            picked = pick_cameras_by_model(idxs, limit=4)
            dropped = [i for i in idxs if i not in picked]
            names = camera_device_names()
            picked_txt = ", ".join(
                f"#{i}({names.get(i, '?')})" for i in picked)
            dropped_txt = ", ".join(
                f"#{i}({names.get(i, '?')})" for i in dropped)
            self.status_label.setText(
                f"检测到 {len(idxs)} 个摄像头，已优先选择同型号 4 路: {picked_txt}"
                f" | 未使用: {dropped_txt}")
        else:
            picked = idxs

        # 分辨率：默认 640x480（或项目/设置里保存的分辨率）。
        # ⚠️ v10.14.7：默认档从 800x600 改为 640x480——800x600 非标准
        # 分辨率，部分摄像头固件在该档只提供 YUY2 未压缩（MJPG 档挂在
        # 标准分辨率上），双路 800x600 YUY2 = 57.6MB/s 必爆 USB2.0
        # （Win7 现场 16:34 log 实证）。640x480 MJPG ≈ 3-6MB/s/路，
        # 双路毫无压力。用户仍可在设置面板主动选 800x600，协商失败
        # 时 Camera.open() 会自动落回 640x480 MJPG。
        width, height = 640, 480
        try:
            sel = self.settings_panel.selected_resolution()
            if sel:
                w, h = sel
                if w and h:
                    width, height = int(w), int(h)
        except Exception:
            pass

        # ⚠️ Win7 多路同型号摄像头：必须【同时启动】所有采集子进程
        # （J2 式，start_delay=0），再统一等待就绪——"一个进程稳定持有
        # 后另一个再打开"会持续失败（diag2 v1.5 K/L 实验：K 的"错峰"
        # 成功其实是 A 还在重试中 B 就打开了 = 同时竞争；VideoRec 原先
        # 的错峰 6 次重试也失败）。各子进程内部自带 6 次 x 3s 重试。
        pending = []
        for slot, idx in enumerate(picked[:4]):
            while len(self.cameras) <= slot:
                self.cameras.append(None)
            try:
                cam = WorkerCamera(idx, start_delay=0.0)
                cam.start(width=width, height=height, fps=30.0)
                pending.append((slot, idx, cam))
                # v10.14.6: 记录请求分辨率（无帧自动重启时复用）
                self._slot_req[slot] = (width, height, 30.0)
                self.previews[slot].set_label(
                    f"Cam {slot + 1} (#{idx}) 打开中…（可能需要 1-2 分钟）")
            except Exception as e:
                self.cameras[slot] = None
                self.previews[slot].set_label(f"Cam {slot + 1} (#{idx}) 启动失败: {e}")

        # ⚠️ 非阻塞等待：主窗口立即显示，就绪推进交给 500ms 轮询定时器。
        # 背景：Win7 实机 USB 摄像头打开可能耗时 30-100s+，同步等待会让
        # 界面冻结数分钟——用户误以为卡死而重复双击启动（实测日志里
        # 10:03/10:04/10:05 三个不同主进程就是被"卡死"观感逼出来的）。
        self._cam_refresh = {"width": width, "height": height, "fps": 30.0,
                             "total": len(idxs)}
        self._pending_cams = []
        deadline = time.time() + 180.0
        for slot, idx, cam in pending:
            self._pending_cams.append((slot, idx, cam, deadline))
        if self._pending_cams:
            if self._camera_poll_timer is None:
                self._camera_poll_timer = QTimer(self)
                self._camera_poll_timer.timeout.connect(self._poll_pending_cams)
            self._camera_poll_timer.start(500)
            self.status_label.setText(
                f"正在打开摄像头…（{len(self._pending_cams)} 路，"
                f"设备就绪可能需要 1-2 分钟）")
        else:
            self._finish_camera_refresh()

    def _log_diag(self, msg: str) -> None:
        """诊断日志（写 exe 旁 videorec_error.log，Win7 现场可查）。

        ⚠️ 打开失败/超时/进程退出必须落盘——之前只改界面标签，
        用户贴回来的 log 里只有"启动子进程"行，失败原因全丢了。
        """
        try:
            base = os.path.dirname(os.path.abspath(sys.executable))
            path = os.path.join(base, "videorec_error.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    def _poll_pending_cams(self):
        """轮询待就绪摄像头（主线程，每 500ms）：就绪→收尾；报错/超时→标记失败。

        不阻塞 UI：每个 pending 只做非阻塞检查；wait_ready(timeout=0.5)
        仅在状态已 READY 时才会返回（立即），ST_ERROR 时立即抛错。
        """
        if not self._pending_cams:
            self._finish_camera_refresh()
            return
        now = time.time()
        done = set()
        for slot, idx, cam, deadline in self._pending_cams:
            try:
                st = cam._read_status()
            except Exception:
                st = None
            if st == 1:  # ST_READY
                try:
                    cam.wait_ready(timeout=0.5)
                    # v10.14.5/6: 自动重启的槽位用其请求分辨率参数收尾
                    # （否则 UI 会显示"请求 800x600 回退 320x240"，误导）
                    _auto = self._no_frame_retried.get(slot, False)
                    if _auto:
                        _w, _h, _f = self._slot_req.get(slot, (640, 480, 30.0))
                    else:
                        _w = self._cam_refresh.get("width", 800)
                        _h = self._cam_refresh.get("height", 600)
                        _f = self._cam_refresh.get("fps", 30.0)
                    self._finalize_camera_slot(cam, idx, slot, _w, _h, _f)
                except Exception as e:
                    self.cameras[slot] = None
                    self.previews[slot].set_label(f"Cam {slot + 1} (#{idx}) 打开失败: {e}")
                    self._log_diag("cam#%d 打开失败(READY后): %s" % (idx, e))
                    # ⚠️ v10.14.9: 必须杀 worker——否则子进程残留持有
                    # 摄像头，后续所有打开全失败（16:52 AttributeError
                    # → worker 泄漏 → 17:07 三路 DSHOW/MSMF 全败 事故）
                    try:
                        cam._cleanup_proc()
                    except Exception:
                        pass
                done.add(slot)
            elif st == 2:  # ST_ERROR
                try:
                    cam.wait_ready(timeout=0.5)
                except Exception as e:
                    self.cameras[slot] = None
                    tip = ("——请拔插该摄像头 USB 后重试"
                           if self._no_frame_retried.get(slot) else "")
                    self.previews[slot].set_label(
                        f"Cam {slot + 1} (#{idx}) 打开失败: {e}{tip}")
                    self._log_diag("cam#%d ST_ERROR: %s" % (idx, e))
                done.add(slot)
            elif cam._proc is not None and cam._proc.poll() is not None:
                rc = cam._proc.returncode
                self.cameras[slot] = None
                self.previews[slot].set_label(f"Cam {slot + 1} (#{idx}) 采集进程退出")
                self._log_diag("cam#%d 采集进程退出 rc=%s" % (idx, rc))
                done.add(slot)
            elif now > deadline:
                self.cameras[slot] = None
                self.previews[slot].set_label(f"Cam {slot + 1} (#{idx}) 打开超时")
                self._log_diag("cam#%d 打开超时(180s)" % idx)
                # 用 _cleanup_proc（直接 kill）而不是 close()——close 会先
                # 发 QUIT 等 5s，子进程若卡在 open 里 QUIT 无人处理，
                # 会拖住主线程（轮询定时器里不能阻塞）
                try:
                    cam._cleanup_proc()
                except Exception:
                    pass
                done.add(slot)
        if done:
            self._pending_cams = [p for p in self._pending_cams if p[0] not in done]
        if not self._pending_cams:
            self._finish_camera_refresh()
            return
        n_ok = sum(1 for c in self.cameras if c is not None)
        # 进度显示：已等待秒数（deadline = 开始时间 + 180s），避免用户
        # 误以为卡死而重复双击（Win7 现场反复出现的恶性循环）
        elapsed = max(0, int(now - (deadline - 180.0)))
        self.status_label.setText(
            f"正在打开摄像头… 已就绪 {n_ok} 路，等待 {len(self._pending_cams)} 路"
            f"（已等待 {elapsed}s / 上限 180s，请耐心等待）")

    def _finish_camera_refresh(self):
        """所有摄像头就绪/失败后收尾：停止轮询、选中槽位、更新状态栏。"""
        if self._camera_poll_timer is not None:
            self._camera_poll_timer.stop()
        self._pending_cams = []
        self._set_current(0)
        n_ok = sum(1 for c in self.cameras if c is not None)
        total = self._cam_refresh.get("total", n_ok)
        self.status_label.setText(
            f"已连接 {n_ok} 路摄像头（共检测到 {total} 个，最多开 4 路）")

    def _finalize_camera_slot(self, cam, index: int, slot: int,
                              width: int = 640, height: int = 480,
                              fps: float = 30.0):
        """打开成功后的收尾：标签、参数面板绑定、带宽提示。返回 (aw, ah, afps)。"""
        self.cameras[slot] = cam
        self.previews[slot].set_label(f"Cam {slot + 1} (#{index})")
        if slot == self.current_idx:
            # 只把当前路的参数面板绑定到当前路（避免多路串参数）
            self.settings_panel.set_camera(cam, slot=slot)
            self._auto_restore_camera_props()

        # 回读实际参数，检查硬件是否接受请求
        aw = int(cam.get_prop(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        ah = int(cam.get_prop(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        afps = float(cam.get_prop(cv2.CAP_PROP_FPS) or 0)
        mbps = estimate_raw_bandwidth_mbps(aw, ah, afps)
        bw_hint = f"；估算带宽 {mbps:.0f} MB/s" if mbps > 0 else ""
        if (aw, ah) != (width, height) or abs(afps - fps) > 1:
            # 多路场景不弹窗打断，状态栏警示即可（用户主动改分辨率时
            # 由 _reopen_camera_for_settings 根据返回值弹窗）
            self.status_label.setText(
                f"Cam {slot + 1} (#{index}): 实际 {aw}x{ah}@{afps:.0f}fps"
                f"（请求 {width}x{height}@{fps:.0f}，超出硬件能力已回退）{bw_hint}")
            self.settings_panel.set_res_fps_blocking(aw, ah, afps)
        else:
            self.previews[slot].set_label(f"Cam {slot + 1} (#{index}) {aw}x{ah}@{afps:.0f}fps")
            self.status_label.setText(
                f"Cam {slot + 1} (#{index}) 已连接 {aw}x{ah}@{afps:.0f}fps{bw_hint}")
        return aw, ah, afps

    def _open_camera_slot(self, index: int, slot: int, width: int = 800,
                          height: int = 600, fps: float = 30.0):
        """打开一路摄像头到指定槽位（单路/重开场景）。返回实际 (w, h, fps)，失败返回 None。"""
        while len(self.cameras) <= slot:
            self.cameras.append(None)
        # 关键：先关闭该槽位旧摄像头，否则同一设备被两个 VideoCapture
        # 同时占用，新实例 set 分辨率会失败（回读 0x0）
        old = self.cameras[slot]
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
            self.cameras[slot] = None
            # DSHOW 设备释放不是即时的：立刻重开同一设备经常失败
            # （试读无帧 → fallback MSMF → Win7 黑屏）。给驱动一点
            # 时间释放，再开新实例。
            time.sleep(0.4)
        # ⚠️ 多进程采集（Win7 多路同型号摄像头的唯一可靠方案）：
        # OpenCV cap_dshow 同进程内第二个同型号摄像头必失败（已用
        # A/B/C/H/I 实验验证）；每路一个独立采集子进程（进程隔离）。
        # 单路场景：start + wait_ready。仅启动阶段异常（shm/Popen）
        # 回退本地 Camera；摄像头打开失败不回退（Win7 同进程必败，
        # 回退只会掩盖真因，失败原因已在 videorec_error.log）。
        try:
            cam = WorkerCamera(index, start_delay=slot * 2.0)
            cam.start(width=width, height=height, fps=fps)
            self._slot_req[slot] = (width, height, fps)  # v10.14.6
        except Exception as e:
            try:
                cam = Camera(index)
                cam.open(width=width, height=height, fps=fps)
                self._slot_req[slot] = (width, height, fps)
            except Exception as e2:
                self.cameras[slot] = None
                self.previews[slot].set_label(f"Cam {slot + 1} (失败)")
                self.status_label.setText(f"摄像头 #{index} 打开失败: {e2}")
                return None
        try:
            cam.wait_ready()
        except Exception as e:
            self.cameras[slot] = None
            self.previews[slot].set_label(f"Cam {slot + 1} (失败)")
            self.status_label.setText(f"摄像头 #{index} 打开失败: {e}")
            return None
        try:
            return self._finalize_camera_slot(cam, index, slot, width, height, fps)
        except Exception as e:
            self.cameras[slot] = None
            self.previews[slot].set_label(f"Cam {slot + 1} (失败)")
            self.status_label.setText(f"摄像头 #{index} 打开失败: {e}")
            return None

    def _open_camera(self, index: int, width: int = 640, height: int = 480,
                     fps: float = 30.0):
        """打开/重开当前选中路（设置面板改分辨率/帧率时调用）。"""
        if self.current_idx >= len(self.previews):
            return
        return self._open_camera_slot(index, self.current_idx, width, height, fps)

    def _reopen_camera_for_settings(self):
        """分辨率/帧率变化 -> 用新参数重开当前路摄像头（录制中禁止）。

        若摄像头不接受请求参数（set 静默失败、悄悄回退成别的分辨率），
        弹窗明确警告——这正是之前"设了 720p 实际输出 1080p 导致带宽
        雪崩丢帧"事故的诱因，状态栏小字容易被忽略。
        """
        if self.camera is None:
            return
        if self._recording:
            QMessageBox.warning(self, "提示", "录制中不能修改分辨率/帧率，请先停止录制")
            return
        w, h = self.settings_panel.selected_resolution()
        fps = self.settings_panel.selected_fps()
        actual = self._open_camera(self.camera.index, width=w, height=h, fps=fps)
        if actual:
            aw, ah, afps = actual
            if (aw, ah) != (w, h) or abs(afps - fps) > 1:
                QMessageBox.warning(
                    self, "分辨率未生效",
                    f"Cam {self.current_idx + 1}（#{self.camera.index}）\n"
                    f"请求：{w}x{h} @ {fps:.0f} fps\n"
                    f"实际：{aw}x{ah} @ {afps:.0f} fps\n\n"
                    "该摄像头不支持此分辨率/帧率，已自动回退到实际档位。\n"
                    "注意：若实际分辨率高于预期（如 1080p），多路录制会爆带宽导致丢帧，"
                    "建议在列表中选择更低的档位。")

    def _on_auto_restored(self, ok: bool, detail: str):
        """恢复自动的结果反馈到状态栏。"""
        slot = self.current_idx + 1
        if ok:
            self.status_label.setText(f"✓ Cam {slot}: {detail}")
        else:
            self.status_label.setText(f"⚠ Cam {slot}: {detail}（可手动拖曝光滑杆）")

    def _auto_restore_camera_props(self):
        """摄像头连接后自动恢复已保存的画面参数（仅滑杆项，不重开摄像头）。

        优先项目参数文件，其次程序目录全局参数。
        """
        candidates = []
        if getattr(self, "project_dir", None):
            candidates.append(Path(self.project_dir) / "camera_params.json")
        candidates.append(self._settings_dialog.params_path())
        seen = set()
        for p in candidates:
            p = Path(p)
            if str(p) in seen or not p.exists():
                continue
            seen.add(str(p))
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.settings_panel.apply_props(data.get("props", {}))
            except Exception:
                pass

    # ================= 项目 =================

    def _create_project(self):
        """弹出新建项目对话框（实验信息/装置/路径）。"""
        dlg = NewProjectDialog(str(self.projects.root_dir), self)
        if qt_exec(dlg) != DialogCode.Accepted:
            return
        data = dlg.result_data()
        cur = self.camera
        self.project = self.projects.create(
            data["name"],
            experiment_info=data["experiment_info"],
            apparatus=data["apparatus"],
            notes=data["notes"],
            project_dir=data["project_dir"],
            recordings_dir=data["video_dir"],
            camera_index=cur.index if cur else 0,
        )
        self.project_dir = Path(data["project_dir"])
        self.ref_mgrs = {}  # 新项目：清空旧路的参考帧管理器（懒重建）
        self.project_name_edit.setText(data["name"])
        self._load_animal_info()
        self._refresh_rec_path_label()
        self._apply_project_settings()
        self.status_label.setText(
            f"项目已创建: {data['name']} | 录像 → {data['video_dir']}")
        self._remember_recent_project(str(self.project_dir))
        self._refresh_recent_projects()

    def _load_project(self):
        dlg = QFileDialog.getExistingDirectory(self, "选择项目目录", str(self.projects.root_dir))
        if not dlg:
            return
        proj = self.projects.load(dlg)
        if proj is None:
            QMessageBox.warning(self, "提示", "该目录不是有效项目")
            return
        self._apply_loaded_project(proj, dlg)

    def _apply_loaded_project(self, proj, dir_path: str):
        """应用已加载的项目（打开/最近项目共用）。"""
        self.project = proj
        self.project_dir = Path(dir_path)
        self.ref_mgrs = {}  # 加载项目：重建参考帧管理器（懒创建）
        self.project_name_edit.setText(proj.name)
        self._load_animal_info()
        self._refresh_rec_path_label()
        self._apply_project_settings()
        # 加载当前路的参考帧（若已保存）
        mgr = self._ref_mgr()
        if mgr is not None:
            mgr.load_reference()
        self.preview.ghost_alpha = self.ghost_slider.value() / 100.0
        self.status_label.setText(f"项目已加载: {proj.name}")
        self._update_ghost()
        self._remember_recent_project(str(dir_path))
        self._refresh_recent_projects()

    def _apply_project_settings(self):
        """把 project 配置应用到界面。"""
        p = self.project
        if p is None:
            return
        self.trig_x.setValue(p.trigger.x if p.trigger else 600)
        self.trig_y.setValue(p.trigger.y if p.trigger else 400)
        self.trig_w.setValue(p.trigger.w if p.trigger else 200)
        self.trig_h.setValue(p.trigger.h if p.trigger else 200)
        self.trig_delay.setValue(int(getattr(p, "trigger_delay_ms", 0) or 0))
        # 触发区域保存
        self._save_trigger_to_project()
        # 显示触发区半透明框
        self._sync_trigger_overlay(force_show=True)

    def _save_trigger_to_project(self):
        if self.project is None:
            return
        self.project.trigger = TriggerRegion(
            x=self.trig_x.value(), y=self.trig_y.value(),
            w=self.trig_w.value(), h=self.trig_h.value(),
            enabled=self.trig_enable.isChecked(),
        )
        self.project.trigger_delay_ms = self.trig_delay.value()
        self.projects.save(self.project)

    # ================= 参考帧 =================

    def _ref_mgr(self, slot: int = None):
        """获取指定路（默认当前路）的参考帧管理器，懒创建。

        每路独立参考帧：slot -> ReferenceManager，文件按槽位区分
        （reference_cam1.png ... reference_cam4.png），互不干扰。
        """
        if self.project_dir is None:
            return None
        s = self.current_idx if slot is None else slot
        if s not in self.ref_mgrs:
            self.ref_mgrs[s] = ReferenceManager(self.project_dir, slot=s)
        return self.ref_mgrs[s]

    def _save_reference(self):
        if self.camera is None or self.project is None:
            QMessageBox.warning(self, "提示", "需要先连接摄像头并创建/打开项目")
            return
        frame = self.camera.read()
        if frame is None:
            QMessageBox.warning(self, "提示", "读取画面失败")
            return
        snap = self.camera.take_snapshot()
        mgr = self._ref_mgr()
        if mgr is None:
            QMessageBox.warning(self, "提示", "需要先创建/打开项目")
            return
        mgr.save_reference(frame, snap)
        self.preview.ghost_alpha = self.ghost_slider.value() / 100.0
        self.status_label.setText(
            f"✅ 参考帧已保存（Cam {self.current_idx + 1}，含摄像头参数快照）")

    def _load_reference(self):
        if self.project is None:
            QMessageBox.warning(self, "提示", "请先创建/打开项目")
            return
        mgr = self._ref_mgr()
        if mgr is None or mgr.load_reference() is None:
            QMessageBox.warning(self, "提示", f"Cam {self.current_idx + 1} 没有参考帧")
            return
        self.preview.ghost_alpha = self.ghost_slider.value() / 100.0
        self.status_label.setText(f"参考帧已加载（Cam {self.current_idx + 1}）")

    def _update_ghost(self):
        self.preview.ghost_alpha = self.ghost_slider.value() / 100.0

    # ================= 触发 =================

    def _toggle_trigger(self, enabled: bool):
        if enabled:
            self._save_trigger_to_project()
            # 从 UI 控件同步触发区域（即使没有 project 也能用）
            self.trigger.region = TriggerRegion(
                x=self.trig_x.value(), y=self.trig_y.value(),
                w=self.trig_w.value(), h=self.trig_h.value(),
                enabled=True,
            )
            ok = self.trigger.start()
            delay = self.trig_delay.value()
            delay_txt = f"，延迟 {delay}ms 后开始录制" if delay else "，立即开始录制"
            self.trig_info.setText(
                f"✅ 监听中：点击屏幕 ({self.trig_x.value()},{self.trig_y.value()}) "
                f"{self.trig_w.value()}x{self.trig_h.value()} 区域{delay_txt}" if ok
                else "❌ 触发启动失败（pynput 不可用或无权限）")
            if not ok:
                self.trig_enable.setChecked(False)
        else:
            self.trigger.stop()
            self.trig_info.setText("未启用")

    def _on_trigger_click(self):
        """鼠标触发回调（在其他线程）。通过 QTimer 回主线程。"""
        # 用 QTimer.singleShot 确保在 GUI 线程执行
        from qt_compat import QTimer as T
        delay = self.trig_delay.value() if self.project is None else \
            int(getattr(self.project, "trigger_delay_ms", 0) or 0)
        T.singleShot(delay, lambda: self._toggle_record(from_trigger=True))
        # 触发提示：半透明框闪黄（立即反馈，不等延时）
        T.singleShot(0, self._flash_trigger_overlay)

    def _flash_trigger_overlay(self):
        if self._trigger_overlay is not None:
            self._trigger_overlay.flash()

    # ================= 录制 =================

    def _on_schedule_record(self):
        """定时录制：延迟 N 秒后自动开始；倒计时中再点 = 取消。"""
        if self._recording:
            QMessageBox.information(self, "提示", "正在录制中，不能定时（先停止录制）")
            return
        if self._sched_timer is not None:
            self._sched_timer.stop()
            self._sched_timer = None
            self.sched_btn.setText("定时录制")
            self.status_label.setText("已取消定时录制")
            return
        if self.project is None:
            QMessageBox.warning(self, "提示", "请先创建/打开项目")
            return
        if not any(self.cameras):
            if getattr(self, "_pending_cams", None):
                QMessageBox.warning(
                    self, "提示",
                    "摄像头正在打开中（可能需要 1-2 分钟）…\n\n"
                    "请等待界面出现画面后再点击录制。\n"
                    "（状态栏会显示打开进度）")
            else:
                QMessageBox.warning(self, "提示", "摄像头未连接")
            return
        delay = self.delay_spin.value()
        if delay <= 0:
            self._start_recording()
            return
        self.sched_btn.setText("取消定时")
        self.status_label.setText(
            f"[定时] {delay} 秒后自动开始录制...（再点【取消定时】可取消）")
        self._sched_timer = QTimer(self)
        self._sched_timer.setSingleShot(True)
        self._sched_timer.timeout.connect(self._sched_fire)
        self._sched_timer.start(delay * 1000)

    def _sched_fire(self):
        """定时到点：开始录制（若此时正在录则忽略）。"""
        self._sched_timer = None
        self.sched_btn.setText("定时录制")
        if self._recording:
            return
        self._start_recording()

    def _toggle_record(self, from_trigger: bool = False):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording(from_trigger=from_trigger)

    def _start_recording(self, from_trigger: bool = False):
        if not any(self.cameras):
            if getattr(self, "_pending_cams", None):
                QMessageBox.warning(
                    self, "提示",
                    "摄像头正在打开中（可能需要 1-2 分钟）…\n\n"
                    "请等待界面出现画面后再点击录制。\n"
                    "（状态栏会显示打开进度）")
            else:
                QMessageBox.warning(self, "提示", "摄像头未连接")
            return
        # 清除未决的定时倒计时（手动开始 = 取消定时）
        if self._sched_timer is not None:
            self._sched_timer.stop()
            self._sched_timer = None
            self.sched_btn.setText("定时录制")
        if self.project is None:
            QMessageBox.warning(self, "提示", "请先创建/打开项目")
            return
        out_dir = self.projects.recordings_dir(self.project)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 磁盘空间预检：录制是持续写盘，空间不足会导致文件损坏
        # （AVI 尾部缺帧 + meta/timestamps 写坏，用户踩过坑）----
        import shutil
        try:
            free_b = shutil.disk_usage(out_dir).free
            if free_b < 200 * 1024 * 1024:
                QMessageBox.warning(
                    self, "磁盘空间不足",
                    f"录制目录所在盘剩余空间仅 {free_b / 1024 / 1024:.0f} MB，\n"
                    f"不足以安全录制（建议保留 ≥200MB）。\n"
                    f"请清理磁盘后重试。")
                return
        except Exception:
            pass

        # ---- 动物信息：取面板当前值（常驻面板，不弹窗不打断）----
        for slot in range(4):
            self._animal_map[slot] = self.animal_edits[slot].text().strip()
        slots = [i for i, c in enumerate(self.cameras) if c is not None]
        missing = [s + 1 for s in slots if not self._animal_map.get(s)]
        animals = dict(self._animal_map)

        # ---- 开始录制：<项目名>_CamN_<时间戳>，编号 = 槽位号，与预览标签一一对应 ----
        stamp = time.strftime("%Y%m%d_%H%M%S")
        proj_name = (self.project.name or "rec").strip()
        fps = float(self.settings_panel.fps_combo.currentText())
        # ⚠️ fps<=0 防御（Win7 DSHOW 读回 0 曾导致 2.2/fps 除零崩溃）
        if fps <= 0:
            fps = 30.0
        # 期望分辨率：设置面板当前选择（可能已被摄像头实际值同步，见 _load_from_camera）
        exp_w, exp_h = self.settings_panel.selected_resolution()
        self._recorders = []
        self._recorder_threads = []
        self._recorder_slot_map = {}   # slot -> RecorderThread（录制预览按槽位推送）
        ok_count = 0
        stream_warns = []   # 流配置未恢复的提示
        # ⚠️ 各路并行启动：ensure_stream_config 的 set(FOURCC) 可能触发
        # 流重启阻塞数秒（固件响应慢）。串行启动会让后面的路跟着晚开始
        # ——实测 2 路时 Cam2 比 Cam1 晚 ~2.5s（用户误判为丢帧），4 路
        # 会更严重。并行后总启动时间 = 最慢一路，而不是各路之和。
        import threading as _th
        _launch_results = {}
        _launch_lock = _th.Lock()

        def _launch_one(slot, cam, name):
            try:
                # 录制前恢复流配置（MJPG+分辨率+帧率）——调参可能把
                # FOURCC/分辨率协商搞丢（回退 YUY2 未压缩 → 带宽爆 → 掉帧）。
                # ⚠️ 非致命：Win7 上 ENSURE（set FOURCC 触发流重启）可能
                # 阻塞超时——配置恢复失败就用子进程实际流参数继续录，
                # 绝不让"锦上添花"的配置恢复阻塞/放弃录制。
                ok_cfg, aw, ah, afps = False, 0, 0, 0
                try:
                    ok_cfg, aw, ah, afps = cam.ensure_stream_config(
                        exp_w, exp_h, fps, timeout=25.0)
                except Exception:
                    ok_cfg, aw, ah, afps = False, 0, 0, 0
                if not ok_cfg or not (aw and ah):
                    aw, ah, afps = getattr(cam, "actual", (0, 0, 0))
                    if not (aw and ah):
                        aw, ah, afps = exp_w, exp_h, fps
                rec = Recorder(cam, out_dir, name, fps=fps,
                               meta_extra={"cam": slot + 1,
                                           "animal": animals.get(slot, "")})
                # 用 ensure 读回的实际分辨率建 writer（不要同步 read()——
                # DSHOW 首帧可能阻塞数秒，会拖慢启动）
                rec.start(aw, ah)
                rt = RecorderThread(rec)
                with _launch_lock:
                    _launch_results[slot] = (rec, rt, ok_cfg, aw, ah, afps, None)
            except Exception as e:
                with _launch_lock:
                    _launch_results[slot] = (None, None, False, 0, 0, 0, str(e))

        _slots = [s for s, c in enumerate(self.cameras) if c is not None]
        _names = {s: f"{proj_name}_Cam{s + 1}_{stamp}" for s in _slots}
        _workers = [_th.Thread(target=_launch_one, args=(s, self.cameras[s], _names[s]),
                               daemon=True) for s in _slots]
        for _t in _workers:
            _t.start()
        for _t in _workers:
            _t.join()

        for slot in _slots:
            rec, rt, ok_cfg, aw, ah, afps, err = _launch_results[slot]
            if err is not None:
                self.status_label.setText(f"Cam {slot + 1} 录制失败: {err}")
                continue
            if not ok_cfg:
                stream_warns.append(
                    f"Cam{slot + 1} 请求 {exp_w}x{exp_h}@{fps:.0f}，"
                    f"实际 {aw}x{ah}@{afps:.0f}")
            # 录制线程每帧直接推送到对应槽位预览（双路径：
            # 信号直推 + _tick 轮询 latest_frame 兜底）。
            # ⚠️ 不能靠 enumerate(_recorder_threads) 推断槽位——
            # cameras 有空洞时下标会错位，非选中路画面停更。
            rt.frame_ready.connect(
                lambda f, s=slot: self.previews[s].update_frame(f, f, ""))
            rt.start()
            self._recorders.append(rec)
            self._recorder_threads.append(rt)
            self._recorder_slot_map[slot] = rt
            self.previews[slot].set_recording(True)
            ok_count += 1
        if ok_count == 0:
            self._recorders = []
            self._recorder_threads = []
            # 合并每路失败原因到弹窗（之前只写状态栏，现场看不到）
            _errs = []
            for _s in _slots:
                _r = _launch_results.get(_s)
                if _r and _r[6]:
                    _errs.append(f"Cam {_s + 1}: {_r[6]}")
            _detail = "\n".join(_errs) if _errs else "（无失败详情）"
            QMessageBox.warning(self, "录制失败",
                                "所有摄像头均无法录制：\n" + _detail)
            return
        self._recording = True
        self._drop_warned = False
        self._rec_start_time = time.time()
        self._rec_duration_limit = self.duration_spin.value()  # 秒，0 = 不限
        self._tick_count = 0
        # v10.14.11：录像中预览降到 200ms(5fps)。录像由 worker 独立写盘，
        # 预览降帧只省 CPU 不影响录像质量（Win7 老 CPU 关键优化）。
        self.timer.start(200)
        self.record_btn.setText("■ 停止录制")
        self.record_btn.setStyleSheet("background:#2ecc71; color:white; font-weight:bold; padding:6px 16px;")
        warn_txt = f" | ⚠ Cam {', '.join(map(str, missing))} 动物信息未填" if missing else ""
        if stream_warns:
            warn_txt += " | ⚠ " + "; ".join(stream_warns)
        if self._rec_duration_limit > 0:
            self.status_label.setText(
                f"● 录制中 {ok_count} 路 | 时长上限 {self._rec_duration_limit}s{warn_txt}")
        else:
            self.status_label.setText(
                f"录制中... {ok_count} 路 | {proj_name}_Cam1~{ok_count}_{stamp}{warn_txt}")
        if missing:
            self.status_label.setStyleSheet("color:#e67e22; font-weight:bold;")
            from qt_compat import QTimer as _T
            _T.singleShot(6000, lambda: self.status_label.setStyleSheet(""))
        if self._trigger_overlay is not None:
            self._trigger_overlay.setRecording(True)
        # 录制中禁用参数面板：set_prop 可能干扰录制线程读帧/触发流重启
        # （FOURCC 丢失回退 YUY2 → 带宽爆 → 掉帧）。要调参请先停止录制。
        self.settings_panel.setEnabled(False)

    def _stop_recording(self):
        if not self._recorders and not self._recorder_threads:
            return
        total_frames = 0
        total_drops = 0
        total_dur = 0.0
        notes_total = 0
        all_stats = []
        write_errors = []   # 录制中写盘异常（磁盘满/IO 错误）
        for slot, rt in enumerate(self._recorder_threads):
            if rt is not None:
                stats = rt.stop_and_wait()
                if rt.error:
                    write_errors.append(f"Cam{slot + 1}: {rt.error}")
                if stats:
                    all_stats.append(stats)
                    total_frames += stats.get("frames", 0)
                    total_drops += stats.get("drops", 0)
                    total_dur = max(total_dur, stats.get("duration_s", 0))
                    notes_total += stats.get("notes", 0)
        self._recorders = []
        self._recorder_threads = []
        self._recorder_slot_map = {}
        for pw in self.previews:
            pw.set_recording(False)
        self._recording = False
        self.record_btn.setText("● 开始录制")
        self.record_btn.setStyleSheet("background:#e74c3c; color:white; font-weight:bold; padding:6px 16px;")
        self.settings_panel.setEnabled(True)   # 恢复参数面板
        # v10.14.11：停止后恢复待机预览帧率（触发区继续检测，可再次触发）
        self.timer.start(150)
        if self._trigger_overlay is not None:
            self._trigger_overlay.setRecording(False)
        if all_stats:
            # 回显每路 animal（编号对应关系一目了然）
            animal_txt = " | ".join(
                f"Cam{k + 1}: {v}" for k, v in sorted(self._animal_map.items())
                if v and k + 1 <= len(all_stats))
            msg = (f"录制完成: {len(all_stats)} 路 | 共 {total_frames} 帧, {total_dur:.1f}s, "
                   f"总丢帧 {total_drops}, 备注 {notes_total} 条")
            # 兼容写入器回退提示（Win7 ffmpeg 不可用时自动启用）
            if any(s.get("writer_fallback") for s in all_stats):
                msg += ("\n（兼容写入模式：系统编码器不可用，已用内置写入器，"
                        "文件为标准 MJPG AVI，可正常播放）")
            if animal_txt:
                msg += f"\n{animal_txt}"
            # 写盘异常（磁盘满等）单独警示——这是文件损坏的元凶
            if write_errors:
                msg += "\n\n⚠ 写盘异常（文件可能不完整）：\n" + "\n".join(write_errors)
            self.status_label.setText(msg)
            # 自动停止（定时时长到点）时保留原因
            if getattr(self, "_auto_stop_reason", None):
                self.status_label.setText(f"[自动停止] {self._auto_stop_reason} | {msg}")
                self._auto_stop_reason = None
            if total_drops > total_frames * 0.01 and total_frames > 0:
                # 每路实际分辨率（recorder 统计的是真实写入尺寸，不是请求值）
                res_lines = []
                for k, s in enumerate(all_stats):
                    w = s.get("width", 0)
                    h = s.get("height", 0)
                    res = f"{w}x{h}" if w and h else "?"
                    res_lines.append(f"  Cam{k + 1}: {res}（{s.get('frames', 0)} 帧, "
                                     f"丢 {s.get('drops', 0)}, 实际 "
                                     f"{s.get('actual_fps', 0):.1f} fps）")
                QMessageBox.warning(self, "丢帧警告",
                                    f"总丢帧 {total_drops} 帧 ({total_drops / max(total_frames, 1) * 100:.1f}%)，"
                                    "建议降低分辨率或帧率\n\n"
                                    "各路实际分辨率：\n" + "\n".join(res_lines))

    # ================= 主循环 =================

    def _tick(self):
        """定时器：各路读帧 -> 虚影叠加 -> 分析 -> 显示。"""
        if not any(self.cameras):
            return

        # 录制中：预览直接用各路录制线程的最新帧，UI 不再读摄像头（避免抢帧）。
        # 按槽位映射表更新（不用 enumerate 推断槽位，防 cameras 空洞错位）
        if self._recording and self._recorder_threads:
            for slot, rt in self._recorder_slot_map.items():
                if rt is not None:
                    frame = rt.latest_frame
                    if frame is not None:
                        self.previews[slot].update_frame(frame, frame, "")

            # ---- 录制时长：显示 + 到时自动停止 ----
            self._tick_count += 1
            elapsed = time.time() - self._rec_start_time
            limit = getattr(self, "_rec_duration_limit", 0)
            if limit > 0 and elapsed >= limit:
                self._auto_stop_reason = f"已录满设定时长（{limit}s）"
                self._stop_recording()
                return
            if self._tick_count % 5 == 0:   # 200ms×5 ≈ 每 1s 刷新一次
                if limit > 0:
                    remain = max(0, int(limit - elapsed))
                    dur_txt = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d} / {int(limit // 60):02d}:{int(limit % 60):02d}（剩 {remain}s）"
                else:
                    dur_txt = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
                self.status_label.setText(f"● 录制中 {dur_txt}")
            # 提前掉帧检测（取第一路）：录制 ~2s 后检查一次实际帧率，远低于目标立即警示
            rt0 = self._recorder_threads[0] if self._recorder_threads else None
            if (not self._drop_warned and rt0 is not None and rt0.elapsed_s > 2.0
                    and rt0.frame_count > 0):
                target = float(self.settings_panel.fps_combo.currentText())
                actual = rt0.frame_count / rt0.elapsed_s
                if actual < target * 0.8:
                    self._drop_warned = True
                    w = getattr(rt0.recorder, "_w", 0)
                    h = getattr(rt0.recorder, "_h", 0)
                    res = f"{w}x{h}" if w and h else "?"
                    self.status_label.setText(f"⚠ 掉帧中: 实际 {actual:.1f} fps / 目标 {target:.0f} fps")
                    # 录制中不弹模态框：模态框会阻塞事件循环 ~2s，
                    # worker 积压反而加剧掉帧；且在部分 Qt 环境下
                    # 调用被替换的 QMessageBox.warning 会触发 fail-fast 崩溃。
                    # 警告信息常驻状态栏，停止录制后可查看。
            self.compare_label.setText("录制中：比对已暂停")
            self.verdict_label.setText("")
            self.align_label.setStyleSheet("")
            self.align_label.setText("位置：--")
            return

        # 非录制：各路独立读帧显示；分析/参考帧/ORB 只对当前选中路
        # （无帧检测状态已在 __init__ 初始化，v10.14.9）
        now = time.time()
        for slot, cam in enumerate(self.cameras):
            if cam is None:
                continue
            frame = cam.read()
            if frame is None:
                # ⚠️ 持续无帧：worker 可能"READY 但流已断"（Win7 黑屏）。
                # 之前无限跳过、无任何提示/日志；现在 5s 无帧 → 标签+落盘。
                if now - self._last_frame_time.get(slot, 0) > 5.0 \
                        and not self._no_frame_warned.get(slot):
                    self._no_frame_warned[slot] = True
                    self._log_diag(
                        "cam#%d READY 后持续无帧(>5s)，预览黑屏" % slot)
                    self.previews[slot].set_label(
                        f"Cam {slot + 1} (#{slot}) 已连接但无画面"
                        f"（驱动/带宽问题，详见 videorec_error.log）")
                    # v10.14.5/6 自动恢复：杀旧子进程释放设备 → 以【原请求
                    # 分辨率】异步重启一次。v10.14.6 起不再硬降 320x240：
                    # 重开走完整协商（MJPG 双 set + 带宽适配），固件支持
                    # MJPG 则保持高分辨率（这才是老板要的），只有固件确实
                    # 只认 YUY2 且带宽超限时才由 open() 内部降档。
                    if not self._no_frame_retried.get(slot):
                        self._no_frame_retried[slot] = True
                        self._log_diag(
                            "cam#%d 尝试自动重启（MJPG 协商 + 带宽适配）" % slot)
                        try:
                            old = self.cameras[slot]
                            if old is not None:
                                try:
                                    old._cleanup_proc()
                                except Exception:
                                    pass
                                self.cameras[slot] = None
                            idx = getattr(old, "index", slot)
                            _req_w, _req_h, _req_f = self._slot_req.get(
                                slot, (640, 480, 30.0))
                            cam = WorkerCamera(idx, start_delay=0.0)
                            cam.start(width=_req_w, height=_req_h, fps=_req_f)
                            self._pending_cams.append(
                                (slot, idx, cam, time.time() + 180.0))
                            self.previews[slot].set_label(
                                f"Cam {slot + 1} (#{idx}) 无画面，"
                                f"自动重启 {_req_w}x{_req_h}…")
                            if self._camera_poll_timer is None:
                                self._camera_poll_timer = QTimer(self)
                                self._camera_poll_timer.timeout.connect(
                                    self._poll_pending_cams)
                            self._camera_poll_timer.start(500)
                        except Exception as e:
                            self._log_diag("cam#%d 自动重启失败: %r" % (slot, e))
                            self.previews[slot].set_label(
                                f"Cam {slot + 1} (#{slot}) 无画面，自动重启失败"
                                f"——请拔插该摄像头 USB")
                    else:
                        self.previews[slot].set_label(
                            f"Cam {slot + 1} (#{slot}) 无画面（已尝试降级重启）"
                            f"——请拔插该摄像头 USB 后重试")
                continue
            self._last_frame_time[slot] = now
            self._no_frame_warned[slot] = False
            if slot != self.current_idx:
                self.previews[slot].update_frame(frame, frame, "")
                continue

            display = frame
            align_text = ""

            # 参考帧叠加（每路独立：取当前路的 manager）
            mgr = self._ref_mgr(slot)
            if mgr is not None and mgr.has_reference():
                if mgr._ref_frame is None:
                    mgr.load_reference()

                # ORB 自动对齐（节流：每 5 tick ≈ 150ms 算一次；只查位置，不查光照）
                if self.auto_align_check.isChecked() and mgr._ref_frame is not None:
                    self._align_counter += 1
                    if self._align_counter % 5 == 1:
                        self._last_align = mgr.align(frame)
                    res = self._last_align
                    if res.ok:
                        align_text = "位置: " + res.describe()
                        # 低置信度用橙色提示，避免"谎报完好"
                        if res.confidence < 0.5:
                            self.align_label.setStyleSheet("color:#f39c12;")
                        else:
                            self.align_label.setStyleSheet("color:#2ecc71;")
                        # 用单应把当前帧变换到参考视角再叠加，虚影更准
                        warped = mgr.warp_to_reference(frame, res.homography)
                        display = mgr.ghost_overlay(warped, self.preview.ghost_alpha)
                    else:
                        self.align_label.setStyleSheet("color:#e74c3c;")
                        display = mgr.ghost_overlay(frame, self.preview.ghost_alpha)
                else:
                    self.align_label.setStyleSheet("color:#999;")
                    display = mgr.ghost_overlay(frame, self.preview.ghost_alpha)

                # 量化分析（独立维度：光照/画质，与位置无关）
                # 节流：每 10 tick ≈ 300ms 算一次，Win7 老机器扛不住每帧都算
                self._metrics_counter += 1
                if self._metrics_counter % 10 == 1:
                    try:
                        cur = compute_metrics(frame)
                        self.metrics_label.setText(
                            f"亮度 {cur.mean_brightness:.0f} | 对比度 {cur.contrast:.0f} | "
                            f"过曝 {cur.overexposed * 100:.1f}% | 欠曝 {cur.underexposed * 100:.1f}% | "
                            f"模糊度 {cur.blur:.0f}")
                        from core.analyzer import FrameMetrics
                        ref_metrics = mgr._ref_metrics
                        if ref_metrics is None:
                            ref_metrics = compute_metrics(mgr._ref_frame)
                            mgr._ref_metrics = ref_metrics
                        cmp = compare_frames(cur, ref_metrics,
                                             brightness_threshold_pct=(
                                                 self.project.brightness_threshold_pct
                                                 if self.project is not None else 10.0))
                        self.compare_label.setText(
                            f"亮度差 {cmp.brightness_diff:+.0f} ({cmp.brightness_diff_pct:.0f}%) | "
                            f"对比度差 {cmp.contrast_diff:+.0f} | 直方图相关 {cmp.hist_corr:.2f}")
                        color = {"OK": "#2ecc71", "注意": "#f39c12", "调整": "#e74c3c"}.get(cmp.verdict, "#999")
                        self.verdict_label.setText(f"光照判定: {cmp.verdict}")
                        self.verdict_label.setStyleSheet(f"font-size:14pt; font-weight:bold; color:{color};")
                        self.verdict_label.setToolTip(
                            "光照判定只看亮度/对比度/直方图（画质维度），与相机位置无关。\n\n"
                            + "\n".join(cmp.suggestions))
                    except Exception:
                        pass
            else:
                self.metrics_label.setText("当前帧指标：--")
                self.compare_label.setText("对比参考：--")
                self.verdict_label.setText("")
                self.verdict_label.setToolTip("")

            if align_text:
                self.align_label.setText(align_text)
            else:
                self.align_label.setStyleSheet("")
                self.align_label.setText("位置：--")
            self.previews[slot].update_frame(frame, display, align_text)

    # ================= 关闭 =================

    def closeEvent(self, event):
        self.timer.stop()
        self.trigger.stop()
        if self._recording:
            for rt in self._recorder_threads:
                if rt is not None:
                    rt.stop_and_wait()
            self._recorder_threads = []
            self._recorder_slot_map = {}
        for cam in self.cameras:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
        # ⚠️ v10.14.9: 待就绪/失败的 pending worker 也要清理——它们不在
        # self.cameras 里，不杀会残留占摄像头（16:52 事故泄漏源）
        for _slot, _idx, cam, _dl in self._pending_cams:
            try:
                cam._cleanup_proc()
            except Exception:
                pass
        self._pending_cams = []
        super().closeEvent(event)

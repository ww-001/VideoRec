# -*- coding: utf-8 -*-
"""摄像头参数面板：亮度/对比度/曝光等滑杆 + 分辨率/帧率选择。"""
from __future__ import annotations

import cv2
from qt_compat import (Qt, QTimer, QComboBox, QFormLayout, QGroupBox,
                       QHBoxLayout, QLabel, QPushButton, QSlider,
                       QVBoxLayout, QWidget, QMessageBox)

from core.camera import ADJUSTABLE_PROPS


class SettingsPanel(QWidget):
    """摄像头参数调节面板。

    通过回调 on_prop_changed(prop_id, value) 通知外部应用参数。
    """

    def __init__(self, camera=None, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.on_prop_changed = None   # callable(prop_id, value)
        self.on_res_fps_changed = None  # callable() 分辨率/帧率变化（由外部重开摄像头）
        self.on_auto_restored = None  # callable(ok: bool, detail: str) 恢复自动结果
        self._sliders = {}
        self._debounce: dict = {}  # prop_id -> QTimer（拖动停止后才下发 UVC）
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 当前调节哪路（4 路版：点击预览切换，此处实时显示）
        self.current_label = QLabel("当前调节：--")
        self.current_label.setStyleSheet("font-weight:bold; color:#2980b9;")
        layout.addWidget(self.current_label)

        # 分辨率/帧率
        res_group = QGroupBox("画幅与帧率")
        res_form = QFormLayout(res_group)
        self.res_combo = QComboBox()
        self.res_combo.addItems(["320x240", "640x480", "800x600", "960x720",
                                 "1280x720", "1280x960", "1920x1080"])
        # 默认 800x600@30：MJPG 下带宽占用 ~1MB/s/路，4 路也毫无压力
        # （实测 4 路 1920x1080@30 MJPG 都能跑满 29.4fps），清晰度比
        # 640x480 高一档，是 4 路场景的稳妥默认。
        self.res_combo.setCurrentText("800x600")
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["15", "25", "30", "60"])
        self.fps_combo.setCurrentText("30")
        # 变化即生效（重开摄像头应用新参数）
        self.res_combo.currentTextChanged.connect(self._on_res_fps_changed)
        self.fps_combo.currentTextChanged.connect(self._on_res_fps_changed)
        self.probe_btn = QPushButton("检测支持的分辨率")
        self.probe_btn.clicked.connect(self._probe_resolutions)
        res_form.addRow("分辨率", self.res_combo)
        res_form.addRow("帧率", self.fps_combo)
        res_form.addRow(self.probe_btn)
        layout.addWidget(res_group)

        # 手动参数滑杆
        param_group = QGroupBox("画面参数")
        param_form = QFormLayout(param_group)
        for prop_id, name, lo, hi in ADJUSTABLE_PROPS:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue((lo + hi) // 2)
            val_label = QLabel(str(slider.value()))
            h.addWidget(slider, 1)
            h.addWidget(val_label)
            slider.valueChanged.connect(
                lambda v, pid=prop_id, lbl=val_label: self._on_slider(pid, v, lbl))
            # 防抖：拖动期间不阻塞 UI，停止 80ms 后下发一次 UVC 命令
            # （120ms 用户觉得"反应迟钝"；80ms 跟手又不至于拖动时狂发）
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(80)
            t.timeout.connect(lambda pid=prop_id: self._apply_prop(pid))
            self._debounce[prop_id] = t
            param_form.addRow(name, row)
            self._sliders[prop_id] = (slider, val_label)
        layout.addWidget(param_group)

        # 按钮
        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("应用参数")
        self.apply_btn.clicked.connect(self._apply_all)
        self.auto_btn = QPushButton("恢复自动")
        self.auto_btn.clicked.connect(self._restore_auto)
        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.auto_btn)
        btn_row.addWidget(self.reset_btn)
        layout.addLayout(btn_row)
        layout.addStretch(1)

    # ---------- 对外接口 ----------

    def set_camera(self, camera, slot: int = None) -> None:
        for t in self._debounce.values():
            t.stop()  # 切换路时丢弃未决防抖，防止旧路参数写到新路
        self.camera = camera
        if camera is not None:
            self._load_from_camera()
        if slot is not None:
            self.current_label.setText(
                f"当前调节：Cam {slot + 1}" if camera is not None else "当前调节：--")
        else:
            self.current_label.setText(
                f"当前调节：Cam {camera.index + 1}" if camera is not None else "当前调节：--")

    def selected_resolution(self) -> tuple:
        """返回当前选中的 (宽, 高)。"""
        try:
            w, h = self.res_combo.currentText().split("x")
            return int(w), int(h)
        except Exception:
            return 640, 480

    def selected_fps(self) -> float:
        try:
            return float(self.fps_combo.currentText())
        except Exception:
            return 30.0

    def set_res_fps_blocking(self, width: int, height: int, fps: float):
        """把分辨率/帧率显示为摄像头实际值（不触发重开回调）。

        ⚠️ fps<=0 时必须忽略：Win7 DSHOW 读回 FPS 常为 0（diag 实测
        640x480@0.0）。若把 0 写进下拉框 → 录制时 fps=0 → 2.2/fps 除零
        → 录制线程崩溃（现场"float division by zero"）。0 表示未知，
        保留用户当前选择。
        """
        self.res_combo.blockSignals(True)
        self.fps_combo.blockSignals(True)
        for combo, text in ((self.res_combo, f"{width}x{height}"),):
            i = combo.findText(text)
            if i < 0:
                combo.addItem(text)
                i = combo.findText(text)
            combo.setCurrentIndex(i)
        if fps and fps > 0:
            text = str(int(round(fps)))
            i = self.fps_combo.findText(text)
            if i < 0:
                self.fps_combo.addItem(text)
                i = self.fps_combo.findText(text)
            self.fps_combo.setCurrentIndex(i)
        self.res_combo.blockSignals(False)
        self.fps_combo.blockSignals(False)

    # ---------- 参数保存 / 加载 ----------

    def get_params(self) -> dict:
        """收集当前面板全部参数（分辨率/帧率/滑杆值），用于保存。"""
        return {
            "resolution": self.res_combo.currentText(),
            "fps": self.fps_combo.currentText(),
            "props": {str(pid): slider.value()
                      for pid, (slider, _) in self._sliders.items()},
        }

    def apply_props(self, props: dict) -> None:
        """只应用滑杆参数（亮度/对比度等），不碰分辨率/帧率。"""
        for pid, (slider, label) in self._sliders.items():
            v = props.get(str(pid))
            if v is not None:
                v = int(v)
                slider.setValue(max(slider.minimum(), min(slider.maximum(), v)))
                label.setText(str(slider.value()))
        if self.camera is not None:
            self._apply_all()

    def set_params(self, params: dict) -> bool:
        """应用保存的全部参数；返回分辨率/帧率是否变化（需重开摄像头）。"""
        res_fps_changed = False
        self.res_combo.blockSignals(True)
        self.fps_combo.blockSignals(True)
        try:
            res = params.get("resolution")
            if res:
                i = self.res_combo.findText(res)
                if i < 0:
                    self.res_combo.addItem(res)
                    i = self.res_combo.findText(res)
                if self.res_combo.currentIndex() != i:
                    self.res_combo.setCurrentIndex(i)
                    res_fps_changed = True
            fps = params.get("fps")
            if fps:
                i = self.fps_combo.findText(fps)
                if i >= 0 and self.fps_combo.currentIndex() != i:
                    self.fps_combo.setCurrentIndex(i)
                    res_fps_changed = True
        finally:
            self.res_combo.blockSignals(False)
            self.fps_combo.blockSignals(False)
        self.apply_props(params.get("props", {}))
        return res_fps_changed

    def _on_res_fps_changed(self, *_):
        if self.on_res_fps_changed is not None:
            self.on_res_fps_changed()

    def _probe_resolutions(self):
        """检测摄像头真正支持的分辨率，弹出结果列表。"""
        from qt_compat import QMessageBox
        if self.camera is None:
            QMessageBox.information(self, "检测分辨率", "摄像头未连接")
            return
        before = (self.selected_resolution(), self.selected_fps())
        try:
            supported = self.camera.list_resolutions()
        except Exception as e:
            QMessageBox.warning(self, "检测失败", str(e))
            return
        # 恢复原分辨率
        try:
            w, h = before[0]
            self.camera.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.camera.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        except Exception:
            pass
        if not supported:
            QMessageBox.information(self, "检测分辨率", "未能探测到支持的分辨率")
            return
        msg = "摄像头支持的分辨率：\n" + "\n".join(f"  {w}x{h}" for w, h in supported)
        QMessageBox.information(self, "检测分辨率", msg)

    def _load_from_camera(self):
        """从当前摄像头读回参数，刷新滑杆。

        关键：读回值可能超出面板默认范围（如白平衡 4600 > 默认上限 4095），
        此时动态扩展滑杆范围并更新，而不是跳过——否则切换路时滑杆
        会保留上一路的值（显示旧参数、实际是新摄像头原始参数）。
        """
        # 同步分辨率/帧率显示为当前摄像头实际值（不触发重开回调）。
        # 否则切换路时下拉框残留上一路的选择，与实际摄像头不符，
        # 必须手动切到别的值再切回才会触发重开（用户踩过这个坑）。
        try:
            aw = int(self.camera.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ah = int(self.camera.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if aw > 0 and ah > 0:
                # fps 读回常为 0（不可靠），保持当前下拉选择
                self.set_res_fps_blocking(aw, ah, self.selected_fps())
        except Exception:
            pass

        for prop_id, (slider, label) in self._sliders.items():
            v = self.camera.get_prop(prop_id)
            if v is None:
                continue
            # 默认范围还原（避免上一路把范围撑大后残留）
            for pid, _name, lo, hi in ADJUSTABLE_PROPS:
                if pid == prop_id:
                    slider.setRange(lo, hi)
                    break
            # gain 等属性读回 -1 = 不支持，跳过（避免范围被 -1 污染）
            if v == -1 and slider.minimum() >= 0:
                continue
            iv = int(v)
            # 动态扩展滑杆范围以容纳摄像头实际值
            if iv > slider.maximum():
                slider.setMaximum(iv + 10)
            elif iv < slider.minimum():
                slider.setMinimum(iv - 10)
            slider.setValue(iv)
            label.setText(str(iv))

    def _on_slider(self, prop_id, value, label):
        label.setText(str(value))
        # 防抖：只刷新显示 + 重启计时器，停止拖动后才真正下发
        t = self._debounce.get(prop_id)
        if t is not None:
            t.start()

    def _apply_prop(self, prop_id):
        """把某个滑杆当前值下发到摄像头（防抖计时器到期时调用）。"""
        if self.camera is None:
            return
        slider, _ = self._sliders[prop_id]
        v = slider.value()
        # 值没变就跳过（UVC set 是慢调用，重复下发同值浪费且卡 UI）
        cur = self.camera.get_prop(prop_id)
        if cur is not None and abs(cur - v) < 0.5:
            return
        if prop_id == cv2.CAP_PROP_EXPOSURE:
            # 保险：曝光手动调节前再确认自动曝光已关
            # （部分固件 open() 时 0.75 不生效，自动开着 → 调了没反应）
            self.camera.set_prop(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
        self.camera.set_prop(prop_id, v)
        if self.on_prop_changed is not None:
            self.on_prop_changed(prop_id, v)

    def _apply_all(self):
        if self.camera is None:
            return
        for prop_id, (slider, _) in self._sliders.items():
            t = self._debounce.get(prop_id)
            if t is not None:
                t.stop()  # 取消未决防抖，立即应用
            v = slider.value()
            cur = self.camera.get_prop(prop_id)
            if cur is not None and abs(cur - v) < 0.5:
                continue  # 值没变，跳过
            self.camera.set_prop(prop_id, v)
            if self.on_prop_changed is not None:
                self.on_prop_changed(prop_id, v)

    def _restore_auto(self):
        """曝光/白平衡恢复自动。

        不同摄像头对 AUTO_EXPOSURE 取值理解不同：
        Windows DirectShow 常见 0.25=自动 / 0.75=手动；
        Linux V4L2 常见 1=自动 / 0=手动；部分国产 UVC 固件 0=手动/1=自动。
        逐个尝试候选值；**成功标准 = 曝光读数开始自己漂移**（自动增益在跑），
        而不是"set 后回读值变了"（值变≠自动模式）。
        """
        import time
        if self.camera is None:
            return

        def exposure_drift() -> bool:
            """间隔采样曝光值，变化即认为自动曝光在生效。"""
            e0 = self.camera.get_prop(cv2.CAP_PROP_EXPOSURE)
            for _ in range(4):
                time.sleep(0.25)
                e1 = self.camera.get_prop(cv2.CAP_PROP_EXPOSURE)
                if (e0 is not None and e1 is not None
                        and abs(e1 - e0) > 1e-6):
                    return True
            return False

        # 1) 曝光自动：试候选值，直到曝光值开始漂移
        auto_ok = False
        for val in (0.25, 1.0, 3.0, 0.75, 0.0):
            self.camera.set_prop(cv2.CAP_PROP_AUTO_EXPOSURE, val)
            time.sleep(0.2)  # UVC 设置需要时间落地
            if exposure_drift():
                auto_ok = True
                break

        # 2) 白平衡自动（Windows 上很多相机不支持 AUTO_WB，尽力而为）
        for val in (0.25, 1.0, 0.0):
            self.camera.set_prop(cv2.CAP_PROP_AUTO_WB, val)
            time.sleep(0.2)
            wb = self.camera.get_prop(cv2.CAP_PROP_WHITE_BALANCE_BLUE_U)
            if wb is not None and wb < 0:  # 负值 = 自动（部分固件约定）
                break
        self._load_from_camera()
        if self.on_auto_restored is not None:
            detail = ("自动曝光已生效" if auto_ok
                      else "该摄像头可能不支持自动曝光（已尝试 5 种取值）")
            self.on_auto_restored(auto_ok, detail)

    def _reset_defaults(self):
        """一键恢复默认：滑杆回中值 + 保持手动模式 + 全部应用 + 读回刷新。

        ⚠️ 不要在这里调用 _restore_auto()：
        1) 它会逐个试 5 个 AUTO_EXPOSURE 候选值、每个都要采样曝光漂移
           (~1.2s)，总耗时 6-8 秒——主线程卡死，用户感觉"点了没反应"；
        2) 曝光回到自动后，自动增益会覆盖亮度/对比度/增益的手动值，
           滑杆怎么拖画面都不变——"恢复默认没效果"的直接原因。
        恢复自动是独立按钮（恢复自动），两个功能分开。
        """
        for prop_id, (slider, label) in self._sliders.items():
            slider.setValue((slider.minimum() + slider.maximum()) // 2)
            label.setText(str(slider.value()))
        if self.camera is not None:
            # 确保手动模式（自动曝光开着时手动参数全被覆盖）
            self.camera.set_prop(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            self.camera.set_prop(cv2.CAP_PROP_AUTO_WB, 0.75)
            self._apply_all()
            # 读回实际值刷新滑杆（固件可能不接受中值，显示真实状态）
            self._load_from_camera()
            if self.on_auto_restored is not None:
                self.on_auto_restored(True, "已恢复默认参数（手动模式）")

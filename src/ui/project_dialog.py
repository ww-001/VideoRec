# -*- coding: utf-8 -*-
"""新建项目对话框：实验信息 + 装置 + 存放路径（路径必填）。"""
from __future__ import annotations

from pathlib import Path

from qt_compat import (QDialog, QDialogButtonBox, QFileDialog,
                       QFormLayout, QHBoxLayout, QLineEdit,
                       QPlainTextEdit, QPushButton, QVBoxLayout,
                       QMessageBox)

DEFAULT_PROJECTS_ROOT = r".\projects"


class NewProjectDialog(QDialog):
    """收集新建项目所需的全部信息。

    必填：项目名、项目保存路径、录像输出路径
    可空：实验信息、所用装置、环境备注
    """

    def __init__(self, projects_root: str = DEFAULT_PROJECTS_ROOT, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建项目")
        self.setMinimumWidth(560)
        self.projects_root = Path(projects_root)

        form = QFormLayout()

        # ---- 必填：项目名 ----
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如 social_interaction_20260803")
        form.addRow("项目名 *", self.name_edit)

        # ---- 可空：实验信息 ----
        self.experiment_edit = QPlainTextEdit()
        self.experiment_edit.setPlaceholderText(
            "动物/组别/处理/日期等（可空）\n如：C57BL/6 雄性 8w，control 组，社会交互实验")
        self.experiment_edit.setMaximumHeight(70)
        form.addRow("实验信息", self.experiment_edit)

        # ---- 可空：所用装置 ----
        self.apparatus_edit = QPlainTextEdit()
        self.apparatus_edit.setPlaceholderText(
            "相机型号/镜头/固定方式/补光等（可空）\n如：Logitech C920，俯视，支架固定，LED 补光")
        self.apparatus_edit.setMaximumHeight(70)
        form.addRow("所用装置", self.apparatus_edit)

        # ---- 可空：环境备注 ----
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "相机高度/角度/距离、灯光位置、笼子位置等（可空）")
        self.notes_edit.setMaximumHeight(70)
        form.addRow("环境备注", self.notes_edit)

        # ---- 必填：项目保存路径 ----
        self.project_dir_edit = QLineEdit()
        self.project_dir_edit.setPlaceholderText("项目保存位置（必填）")
        self._add_browse(form, "项目路径 *", self.project_dir_edit, is_dir=True)

        # ---- 必填：录像输出路径 ----
        self.video_dir_edit = QLineEdit()
        self.video_dir_edit.setPlaceholderText("录像输出位置（必填）")
        self._add_browse(form, "录像路径 *", self.video_dir_edit, is_dir=True)

        self.setLayout(QVBoxLayout())
        self.layout().addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._validate_and_accept)
        btns.rejected.connect(self.reject)
        self.layout().addWidget(btns)

        # 项目名变化时自动填充默认路径（未手动改过才填）
        self.name_edit.textChanged.connect(self._autofill_paths)
        self._paths_touched = {"project": False, "video": False}
        self.project_dir_edit.textChanged.connect(
            lambda: self._paths_touched.__setitem__("project", True))
        self.video_dir_edit.textChanged.connect(
            lambda: self._paths_touched.__setitem__("video", True))

    def _add_browse(self, form, label: str, edit: QLineEdit, is_dir: bool = True):
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        btn = QPushButton("浏览...")
        btn.clicked.connect(
            lambda: self._browse(edit, is_dir))
        row.addWidget(btn)
        form.addRow(label, row)

    def _browse(self, edit: QLineEdit, is_dir: bool):
        if is_dir:
            path = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text() or str(self.projects_root))
        else:
            path, _ = QFileDialog.getSaveFileName(self, "选择文件", edit.text())
        if path:
            edit.setText(path)

    def _autofill_paths(self, name: str):
        name = name.strip()
        if not name:
            return
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        if not self._paths_touched["project"]:
            self.project_dir_edit.blockSignals(True)
            self.project_dir_edit.setText(str(self.projects_root / safe))
            self.project_dir_edit.blockSignals(False)
        if not self._paths_touched["video"]:
            self.video_dir_edit.blockSignals(True)
            self.video_dir_edit.setText(str(self.projects_root / safe / "recordings"))
            self.video_dir_edit.blockSignals(False)

    def _validate_and_accept(self):
        name = self.name_edit.text().strip()
        project_dir = self.project_dir_edit.text().strip()
        video_dir = self.video_dir_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "项目名不能为空")
            return
        if not project_dir:
            QMessageBox.warning(self, "提示", "项目保存路径不能为空")
            return
        if not video_dir:
            QMessageBox.warning(self, "提示", "录像输出路径不能为空")
            return
        self.accept()

    # ---------- 结果 ----------

    def result_data(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "experiment_info": self.experiment_edit.toPlainText().strip(),
            "apparatus": self.apparatus_edit.toPlainText().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "project_dir": self.project_dir_edit.text().strip(),
            "video_dir": self.video_dir_edit.text().strip(),
        }

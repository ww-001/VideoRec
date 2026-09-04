# -*- coding: utf-8 -*-
"""PyQt5 / PyQt6 双兼容层。

用法：所有 UI 文件统一 `from qt_compat import Qt, QTimer, ...`，
代码写成 **PyQt5 风格**（枚举扁平化：Qt.Horizontal / Qt.AlignCenter /
Qt.LeftButton / Qt.Tool ...）。

- PyQt6 下：自动把嵌套枚举（Qt.Orientation.Horizontal）的成员值
  复制到 Qt 顶层（Qt.Horizontal），并兼容 QMouseEvent.position()。
- PyQt5 下：原生就是扁平枚举，直接透传。

依赖：pip install PyQt5（Win7 用 PyQt5.15.x）
"""
from __future__ import annotations

import os
import sys

_FORCE_QT5 = os.environ.get("VIDEOREC_QT5", "0") == "1"

try:
    if _FORCE_QT5:
        raise ImportError("forced PyQt5")
    # 优先 PyQt6（Win10+ 默认）
    from PyQt6.QtCore import (Qt, QTimer, QRect, QThread, QObject,
                              QPoint, QSize, QEvent,
                              pyqtSignal, pyqtSlot)
    from PyQt6.QtGui import (QColor, QImage, QKeySequence, QShortcut,
                             QPainter, QPen, QMouseEvent, QKeyEvent, QPixmap)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                                 QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QGridLayout, QGroupBox,
                                 QHBoxLayout, QLabel, QLineEdit,
                                 QMainWindow, QMessageBox, QPlainTextEdit, QScrollArea,
                                 QPushButton, QSlider, QSpinBox, QSplitter,
                                 QSplashScreen, QVBoxLayout, QWidget, QRubberBand)
    QT6 = True
except ImportError:
    # 回退 PyQt5（Win7 兼容）
    from PyQt5.QtCore import (Qt, QTimer, QRect, QThread, QObject,
                              QPoint, QSize, QEvent,
                              pyqtSignal, pyqtSlot)
    from PyQt5.QtGui import (QColor, QImage, QKeySequence,
                             QPainter, QPen, QMouseEvent, QKeyEvent, QPixmap)
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox,
                                 QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QGridLayout, QGroupBox,
                                 QHBoxLayout, QLabel, QLineEdit,
                                 QMainWindow, QMessageBox, QPlainTextEdit, QScrollArea,
                                 QPushButton, QShortcut, QSlider, QSpinBox, QSplitter,
                                 QSplashScreen, QVBoxLayout, QWidget, QRubberBand)
    QT6 = False


if QT6:
    # ---- PyQt6 → PyQt5 风格枚举扁平化 ----
    # PyQt6 的枚举是 Qt.Orientation.Horizontal 这种嵌套形式；
    # PyQt5 是 Qt.Horizontal 扁平形式。这里把 PyQt6 的嵌套枚举成员
    # 复制到 Qt 顶层，使代码统一写 PyQt5 风格。
    _ENUM_CLASSES = (
        "Orientation", "AlignmentFlag", "MouseButton", "WindowType",
        "ItemDataRole", "GlobalColor", "KeyboardModifier", "FocusPolicy",
        "CursorShape", "AspectRatioMode", "TransformationMode", "PenStyle",
        "BrushStyle", "Key", "WidgetAttribute", "TextFormat",
        "TextFlag", "WindowModality", "CaseSensitivity", "ScrollBarPolicy",
    )
    for _cls_name in _ENUM_CLASSES:
        _cls = getattr(Qt, _cls_name, None)
        if _cls is None:
            continue
        for _name in dir(_cls):
            if _name.startswith("_"):
                continue
            if not hasattr(Qt, _name):
                try:
                    setattr(Qt, _name, getattr(_cls, _name))
                except Exception:
                    pass

    # QImage.Format 枚举（QImage.Format_BGR888 等）
    for _name in dir(QImage.Format):
        if _name.startswith("_"):
            continue
        if not hasattr(QImage, _name):
            try:
                setattr(QImage, _name, getattr(QImage.Format, _name))
            except Exception:
                pass

    def mouse_pos(event) -> "object":
        """QMouseEvent 坐标：PyQt6 用 position()（QPointF），PyQt5 用 pos()。"""
        return event.position().toPoint()

    def mouse_global_pos(event) -> "object":
        """QMouseEvent 全局（屏幕）坐标：PyQt6 用 globalPosition()（QPointF），
        PyQt5 用 globalPos()。region_select 框选用。"""
        return event.globalPosition().toPoint()
else:
    def mouse_pos(event) -> "object":
        return event.pos()

    def mouse_global_pos(event) -> "object":
        return event.globalPos()


def exec_dialog(dlg) -> int:
    """QDialog.exec()：PyQt6 是 exec()，PyQt5 是 exec_()。"""
    if QT6:
        return dlg.exec()
    return dlg.exec_()


def qt_exec(obj) -> int:
    """通用 exec：QApplication / QDialog 在 PyQt5 下都是 exec_()。"""
    if QT6:
        return obj.exec()
    return obj.exec_()


# PyQt6 的 DialogCode 枚举
if QT6:
    DialogCode = QDialog.DialogCode
else:
    DialogCode = QDialog  # PyQt5: Accepted/Rejected 是 QDialog 类属性

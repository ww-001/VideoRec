# -*- coding: utf-8 -*-
"""VideoRec 入口。

行为学录制条件控制工具：
  1. USB 摄像头接入与参数调节
  2. 第一天建立参考帧，后续录制虚影对齐 + 量化对比
  3. ORB 自动对齐（位移/缩放提示）
  4. 鼠标区域触发录制（联动 fiberphotometry 等）
  5. 多摄像头多鼠（每鼠一路，后续扩展）

运行: python main.py
"""
from __future__ import annotations

import os
import sys

from qt_compat import (QApplication, QMessageBox, QSplashScreen, QPixmap,
                       qt_exec)

from ui.main_window import MainWindow


def _asset(path: str) -> str:
    """资源目录：PyInstaller 解包目录 / 源码 assets/。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", path)


def _cleanup_orphan_workers():
    """启动时清理上次异常退出残留的采集子进程（孤儿 worker）。

    16:52 事故链：主进程被强杀/崩溃后，worker 变孤儿并继续持有摄像头 →
    后续所有打开全失败。v10.14.9/10 的"心跳自杀"方案因误杀风险已禁用
    （rc=3 血泪），改为启动时清理：只杀命令行含 --capture-worker 且
    **父进程已死**的 VideoRec.exe（真孤儿），不影响当前正常实例。
    """
    if not getattr(sys, "frozen", False):
        return  # 源码运行不涉及 exe 孤儿
    try:
        import ctypes
        import subprocess
        kernel32 = ctypes.windll.kernel32
        out = subprocess.check_output(
            "wmic process where \"name='VideoRec.exe' and CommandLine like "
            "'%--capture-worker%'\" get ProcessId,ParentProcessId",
            shell=True, stderr=subprocess.DEVNULL, timeout=20)
        killed = 0
        for ln in out.decode("gbk", errors="replace").splitlines():
            ln = ln.strip()
            if not ln or not ln[0].isdigit():
                continue
            parts = ln.split()
            if len(parts) < 2:
                continue
            ppid, pid = int(parts[0]), int(parts[1])
            if pid == os.getpid():
                continue
            # 父进程是否还活着（OpenProcess 探测，安全）
            h = kernel32.OpenProcess(0x1000, False, ppid)  # QUERY_LIMITED_INFO
            if h:
                kernel32.CloseHandle(h)
                parent_alive = True
            else:
                parent_alive = False
            if not parent_alive:
                subprocess.run("taskkill /F /PID %d" % pid, shell=True,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                killed += 1
        if killed:
            print("[启动清理] 清理了 %d 个残留采集子进程" % killed)
    except Exception as e:
        print("[启动清理] 异常（不影响启动）:", e)


def main():
    # 启动时清理上次异常退出残留的采集子进程（孤儿 worker）
    _cleanup_orphan_workers()
    app = QApplication(sys.argv)

    # ---- 单实例锁（Win7 现场血泪：打开摄像头期间界面无画面，
    # 用户误以为卡死而重复双击 → 多实例抢同一摄像头 → 谁也打不开）----
    # 用 Windows CreateMutex（ctypes 直调 kernel32，无第三方依赖；
    # 进程退出/崩溃时内核自动释放，无残留问题）。
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\VideoRec_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(_mutex)
        QMessageBox.warning(
            None, "提示",
            "VideoRec 已在运行中！\n\n"
            "摄像头打开期间界面不会立即显示画面（可能需要 1-2 分钟），\n"
            "请查看任务栏/任务管理器，不要重复启动。\n\n"
            "若确认没有正在运行的实例：请打开任务管理器，\n"
            "结束所有 VideoRec.exe 进程后再启动。")
        return

    win = MainWindow()
    # 启动画面（WWT 实验室标识）
    splash = None
    splash_path = _asset("wwt_splash.png")
    if os.path.exists(splash_path):
        splash = QSplashScreen(QPixmap(splash_path))
        splash.show()
        app.processEvents()
    win.show()
    if splash is not None:
        splash.finish(win)
    rc = qt_exec(app)
    try:
        ctypes.windll.kernel32.CloseHandle(_mutex)
    except Exception:
        pass
    sys.exit(rc)


if __name__ == "__main__":
    # 采集子进程模式：--capture-worker <index> <shm_name> <w> <h> <fps> <delay>
    # （多进程采集架构：每路摄像头一个独立子进程，绕过 OpenCV cap_dshow
    #   同进程双实例互斥的 Win7 bug；由 WorkerCamera 启动）
    if len(sys.argv) > 1 and sys.argv[1] == "--capture-worker":
        from core.capture_worker import worker_main
        worker_main(sys.argv[2:])
    else:
        main()

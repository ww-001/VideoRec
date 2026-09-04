# VideoRec — 4-Channel USB Camera Recorder for Behavioral Neuroscience

> 🇨🇳 **中文版说明见下方「中文简介」段** / For Chinese users, see "中文简介" section below.

**Latest stable:** v10.14.11 · **First release:** v10.10 · **Platform:** Windows 7 SP1 (offline) · Windows 10/11 · **Stack:** Python 3.8.10 + PyQt5 + OpenCV + PyInstaller

A four-channel simultaneous USB camera recording tool for rodent behavioral experiments.
Multi-process capture architecture (one worker subprocess per channel), reference-frame ghost overlay for cross-day rig alignment, mouse-region click triggering, and TTL synchronization.
Designed for **offline Windows 7 rigs** with no internet access.

---

## ✨ Features

| Category | Capability |
|---|---|
| **Multi-camera capture** | 4× USB UVC cameras simultaneously, up to 800×600 @ 30 fps per channel |
| **Process isolation** | Each camera runs in an independent worker subprocess — one channel crash never kills the others |
| **Reference-frame alignment** | First-day reference frame + ghost overlay + ORB feature matching for positional consistency |
| **Quantitative consistency** | Brightness / contrast / histogram correlation / over-exposure ratio / blur metric, with green/yellow/red signal |
| **Region-click trigger** | Pick any screen region (e.g. fiber-photometry button) → click starts/stops all-channel recording |
| **TTL synchronization** | Frame-level timestamps written to sidecar CSV for cross-device alignment |
| **In-recording notes** | F1–F9 behavioral event markers written to per-channel meta.json |
| **Offline Win7 build** | PyInstaller onedir bundle; runs on stock Win7 SP1 with KB2999226 only |

---

## 📥 Download

Download the latest **`VideoRec_win7_v10.14.11.zip`** (≈200 MB) from the
[**Releases**](../../releases) page. Unzip anywhere → double-click `启动_Win7.bat`.

> **Target machine requirements:** Windows 7 SP1 (with `KB2999226` already installed for UCRT) or Windows 10/11 · USB 2.0/3.0 ports · UVC cameras (driver-free). For mouse-region trigger, **run both VideoRec and the trigger-target app as Administrator**.

---

## 🚀 Quick Start (from source)

```bash
# Requires Python 3.8.10 (3.8.x; 3.9+ breaks Win7 builds)
python -m pip install -r src/requirements.txt
python src/main.py
```

For building the offline Win7 bundle, see [`VideoRec_Win7打包经验.md`](VideoRec_Win7打包经验.md).

---

## 📚 Documentation

| Doc | Lang | What |
|---|---|---|
| [**src/使用说明书.md**](src/使用说明书.md) | 🇨🇳 中文 | Full user manual: install, UI, four-channel workflow, troubleshooting |
| [**VideoRec_Win7开发记录.md**](VideoRec_Win7开发记录.md) | 🇨🇳 中文 | Development log from v10.14 onwards; root-cause notes for each bugfix |
| [**VideoRec_Win7打包经验.md**](VideoRec_Win7打包经验.md) | 🇨🇳 中文 | PyInstaller Win10-build → Win7-run UCRT / api-ms-win-* packaging fixes |
| [**开发方案_多摄像头与TTL同步.md**](开发方案_多摄像头与TTL同步.md) | 🇨🇳 中文 | Multi-camera architecture and TTL sync design proposal |

---

## 🏗️ Architecture

```
VideoRec (4-Channel)
├── main UI (PyQt5, single process)
│   ├── core/camera.py        — UVC enumeration + per-camera parameter control
│   ├── core/capture_worker.py — multi-process capture (one subprocess per camera)
│   ├── core/recorder.py      — frame timestamping + AVI mux + drop detection
│   ├── core/analyzer.py      — brightness/contrast/histogram/blur metrics
│   ├── core/reference.py     — reference frame + ghost overlay + ORB alignment
│   ├── core/trigger.py       — global mouse hook for region-click trigger
│   └── core/avi_writer.py    — robust MJPG/XVID writer with fallback codec
└── ui/                       — PyQt5 windows, dialogs, preview widgets
```

`qt_compat.py` at the project root is the **single** Qt-5/Qt-6 compatibility layer
(see [`VideoRec_Win7开发记录.md`](VideoRec_Win7开发记录.md) for the rationale). New UI code must
go through `qt_compat`, never import `PyQt5` / `PyQt6` directly.

---

## 📦 Versioning

This project follows `vMAJOR.MINOR.PATCH`:

- **MAJOR** — architecture rewrite (e.g. v10.10 → v10.14: single-process → multi-process)
- **MINOR** — feature addition or major bugfix (e.g. v10.14.0 → v10.14.1)
- **PATCH** — packaging / docs / minor fix

Latest: **v10.14.11** (2026-08-13) — see [Releases](../../releases) for changelog.

---

## 📝 Citation

If you use VideoRec in your research, please cite:

```bibtex
@software{VideoRec,
  author       = {Wang, Wei},
  title        = {VideoRec: 4-Channel USB Camera Recorder for Behavioral Neuroscience},
  version      = {v10.14.11},
  year         = {2026},
  url          = {https://github.com/ww-001/VideoRec},
  note         = {Four-channel simultaneous USB recording with reference-frame alignment and TTL sync}
}
```

---

## 📜 License

[MIT](LICENSE) — Copyright © 2026 Wei Wang. See [LICENSE](LICENSE) for the full text.

---

## 中文简介

**VideoRec（四路版）** 是面向小鼠行为学实验的四路 USB 摄像头同步录制工具。
多进程采集架构（每路独立 worker 子进程），参考帧虚影对齐，鼠标区域点击触发，TTL 帧级时间戳。
专为**离线 Win7 实验机**设计（PyInstaller onedir 打包 + UCRT 修补）。

### 主要功能

- **四路同步录制**：4 路 USB 摄像头同时录制，最高 800×600 @ 30 fps/路
- **进程隔离**：每路独立子进程，单路崩溃不影响其他路（v10.14 多进程架构改造）
- **参考帧对齐**：第一天参考帧 + 半透明虚影叠加 + ORB 特征匹配，调机量化
- **量化对比**：亮度差 / 对比度差 / 直方图相关性 / 过曝比例 / 模糊度，绿黄红信号
- **鼠标区域触发**：框选屏幕上任一区域（如 fiberphotometry 开始按钮），点击即触发所有路同时录制
- **TTL 同步**：每帧写入系统时间戳到 sidecar CSV，跨设备数据对齐
- **行为备注**：F1-F9 实时打点，写入每路 meta.json
- **离线 Win7 打包**：onedir 模式，目标机只需装 KB2999226 即可运行

### 快速上手

下载 `VideoRec_win7_v10.14.11.zip`（[Releases](../../releases)）→ 解压 → 双击 `启动_Win7.bat`。

源码运行（开发机，需 Python 3.8.10）：
```bash
pip install -r src/requirements.txt
python src/main.py
```

### 详细文档

| 文档 | 内容 |
|---|---|
| [📘 使用说明书](src/使用说明书.md) | 安装、界面、四路版使用流程、故障排除 |
| [📗 开发记录](VideoRec_Win7开发记录.md) | v10.14 系列开发日志，每个 bug 的根因与修复 |
| [📙 打包经验](VideoRec_Win7打包经验.md) | PyInstaller Win10 打包 → Win7 运行的 UCRT / api-ms-win-* 全套修复 |
| [📕 多摄像头与TTL同步方案](开发方案_多摄像头与TTL同步.md) | 多摄像头架构与 TTL 同步设计 |

### 版本号

`v主版本.次版本.补丁`：

- **主版本** — 架构重写（如 v10.10 → v10.14：单进程 → 多进程）
- **次版本** — 功能新增或重大 bug 修复
- **补丁** — 打包 / 文档 / 小修补

当前：**v10.14.11**（2026-08-13）。

### 引用

```bibtex
@software{VideoRec,
  author       = {Wang, Wei},
  title        = {VideoRec: 四路 USB 摄像头同步录制工具（行为学神经科学）},
  version      = {v10.14.11},
  year         = {2026},
  url          = {https://github.com/ww-001/VideoRec},
  note         = {多进程四路同步录制，参考帧虚影对齐，TTL 同步'}
}
```

### License

[MIT](LICENSE) — Copyright © 2026 Wei Wang. 完整条款见 [LICENSE](LICENSE)。

---

## 🛠️ Development

For contributors:

```bash
# Clone
git clone https://github.com/ww-001/VideoRec.git
cd VideoRec

# Use Python 3.8.10 (mandatory for Win7 packaging compatibility)
python -m venv .venv
.venv\Scripts\activate
pip install -r src/requirements.txt

# Run
python src/main.py

# Build offline Win7 bundle
# See VideoRec_Win7打包经验.md for the full UCRT / api-ms-win-* pipeline
```

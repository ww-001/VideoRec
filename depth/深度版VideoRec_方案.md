# 深度版 VideoRec — 方案与实施文档

> 状态：**v1 独立脚本已就绪**（`depth_recorder.py`），未接 VideoRec GUI，未真机验证。
> 用户回头验证：插 D415（USB 3.0）→ 跑脚本 → 确认双流录制正常 → 再决定是否整合进 VideoRec GUI。

---

## 1. 背景与目标

- 现有 VideoRec 只支持普通 UVC 摄像头（RGB）。
- 用户有 **Intel RealSense D415**，希望录制时同时拿到 **RGB + 深度** 双流：
  - RGB → 现有 MovAl 管线照跑（YOLO 14 点关键点 → 行为分析）
  - 深度 → 以后加模块算高度类指标（站立/蜷缩/双鼠叠压），解决 RGB 判不了的问题
- 核心优势：**两条流来自同一个 RealSense pipeline = 硬件同步**，帧级对齐零误差；SDK `align` 把深度注册到彩色坐标系。

## 2. 设备要点（D415）

| 项 | 值 | 备注 |
|---|---|---|
| 类型 | 主动红外双目（非 ToF）| 需要 IR 投影 + 双目 |
| RGB | 1920x1080@30 卷帘快门 | OpenCV 可直接当 UVC 打开（标准 UVC）|
| 深度 | 最高 1280x720@30（Z16，毫米）| 需 pyrealsense2 解码 |
| 最近工作距离 | ~0.3m | ⚠️ 站立时小鼠距镜头 <0.3m → 深度失灵 |
| FOV | 65°×40°（深度，D400 系列最窄）| 35cm 高度覆盖 ~45×26cm；45cm ~57×33cm |
| 接口 | USB 3.0 必须 | USB 2.0 带不动双流 |

**架设结论（用户方案：外箱 + 无顶盖透明笼 + 俯视 ~35cm）**
- 可行；但深度建议 **40-45cm**（35cm 时小鼠站立距镜头 <0.3m 下限，深度恰好在该场景失灵）
- 箱子开口须 ≥ 视野（35cm 时 ~45×26cm），镜头要高出箱沿
- 透明笼壁反射红外 → 边缘鬼影：**降激光功率（100-200）+ 深度滤波器 + 分析时裁 ROI 中央区域**；磨砂是最后手段（牺牲透明度）

## 3. 独立脚本（已交付）

`depth_recorder.py` — 不依赖 VideoRec，直接跑：

```bash
pip install pyrealsense2
python depth_recorder.py --outdir D:\data\session1 --duration 600 --fps 30 --laser 150
```

输出：
- `RGB.avi`（MJPG，1280x720@30，OpenCV/MovAl 直接读）
- `depth/000000.png ...`（16bit 单通道毫米 PNG 序列，与 RGB 帧号一一对应）
- `timestamps.csv`（frame_idx, rgb_ts_ms, depth_ts_ms，硬件时钟）
- `meta.json`（参数记录）

参数：`--duration`（秒，0=手动）、`--fps` 15/30/60、`--laser` 0-360、`--filters on/off`（空间+时间+空洞填充）。

## 4. 整合进 VideoRec GUI（后续，未做）

架构（与现有代码风格一致）：

```
ui/settings_dialog.py  → 加「相机类型」下拉：普通 UVC / RealSense D415
core/realsense_camera.py（新文件）→ 实现与 core/camera.py 相同接口：
    open(index, w, h, fps) / read() -> (rgb, d16) / set_prop / get_prop / release
core/recorder.py → RecorderThread 支持双流：RGB.avi + depth PNG 序列 + timestamps
core/project.py  → project.json 记录 camera_type=realsense
```

- 预览：RGB 正常显示；可选叠加深度伪彩小窗
- 录制：沿用现有 RecorderThread 架构，多写一路深度
- 对齐：pipeline 内 `rs.align(rs.stream.color)`，深度与 RGB 同尺寸
- **打包注意**：PyInstaller 需收集 `realsense2.dll`（`--collect-all pyrealsense2` 或手动 hook）；体积 +~50MB

## 5. 分析侧（更后续）

- 深度 PNG 序列 → numpy 读入，算每帧：身高分布、体积、质心高度、双鼠高度差
- 指标：站立时长/频次、蜷缩（hunched）指数、叠压接触
- 与 14 点关键点时间轴对齐（帧号一一对应，天然对齐）

## 6. 待办清单（用户回头验证时）

- [ ] 插 D415 到 USB 3.0 口 → 跑 `depth_recorder.py` 30 秒试录
- [ ] 检查 RGB.avi 正常、depth PNG 有值（非全黑）、timestamps.csv 两列时间戳接近
- [ ] 调 `--laser` 观察箱壁鬼影（近距往 100-150 调）
- [ ] 确认后决定：整合进 VideoRec GUI（第 4 节）还是保持独立脚本

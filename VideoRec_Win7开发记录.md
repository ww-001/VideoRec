# VideoRec Win7 开发记录

> 项目：四路 USB 摄像头行为学录制软件（Win7 SP1 离线 + Win10 双平台）
> 代码：`D:\video_rec4_win7_offline\src`（多进程采集架构）
> 打包：Python 3.8.10 + PyInstaller 6.3.0（onedir），强制 PyQt5（`VIDEOREC_QT5=1`）
> 本记录从 v10.14 系列开始维护（2026-08-12 起）

---

## 版本历史

| 版本 | 日期 | 主要内容 |
|------|------|----------|
| v10.14 | 08-12 | 多进程采集架构（每路独立 worker 子进程写盘）+ 双系统兼容（qt_compat）+ 鼠标区域触发（pynput 全局监听，点击屏幕区域开始/停止录像，联动 fiberphotometry） |
| v10.14.1 | 08-12 | + 防重复启动（单实例） |
| v10.14.2 | 08-12 | + 黑屏/录制失败诊断 + 兼容写入器 |
| v10.14.3 | 08-12 | + 带宽降级 + 失败详情弹窗 |
| v10.14.4 | 08-12 | + 除零修复 |
| v10.14.5 | 08-12 | MJPG 双 set 协商保高分辨率 + 降级优先保分辨率 + 诊断实测 MJPG 能力 |
| v10.14.6 | 08-12 | 老板确认摄像头支持 MJPG → 默认档改 640x480 + 诊断分辨率×MJPG 组合枚举 |
| v10.14.7 | 08-12 | fourcc 读回不可全信 → 试读 3 帧验证流后再决定是否降级 |
| v10.14.8 | 08-12 | 同 v10.14.7 内容定稿交付（18:30） |
| v10.14.9 | 08-12 | + 心跳防孤儿机制（worker 查共享内存心跳，主进程死则自杀释放摄像头）——**有 bug，08-13 实测三路全灭，已废弃该机制** |
| v10.14.10 | 08-12 | 打包版本（改动未单列） |
| **v10.14.11** | **08-13** | **触发功能修复（两个根因）+ Win7 卡顿优化 + 防孤儿重做（看门狗移除、改启动清理）+ 框选闪退修复** ← 当前交付版 |

> 注：v10.14.5~v10.14.10 各目录的 `使用说明.md` 未随版本同步（内容停在 v10.14.8），v10.14.11 已更新并新增"三、更新内容"章节。待办：补全历史版本说明。

---

## v10.14.11 详细记录（2026-08-13）

### 背景：老板提出的两个任务（11:54）
1. 把旧版（v10.10）的"触发区"（移动侦测自动录像）移植过来——**重要功能**
2. v10.14 在 Win7 上运行卡顿，要优化

### 过程修正（18:56 老板明确）
下午先按"移植移动侦测触发区"做了 `trigger_zone.py`（ROI 框选 + 帧差检测 + 布防/测试面板），
老板实测后反馈：**与原有"触发区域设置"（鼠标点击触发）功能重复且没作用 → 撤销移动侦测方案，
改为修复原有的鼠标区域触发**。

### 根因 #1：鼠标触发不工作 = 跨线程 QTimer 永不 fire
- 链路：`MouseTrigger`（pynput 全局监听）→ `on_click` 回调在线程执行 → 原代码在线程里直接 `QTimer.singleShot` → **无 Qt 事件循环的线程里定时器永不触发** → 点击区域后什么都不发生
- 修复（main_window.py）：
  - 类属性 `_trigger_clicked = pyqtSignal()`（信号必须在类体定义）
  - `self.trigger.on_click = self._trigger_clicked.emit`（emit 线程安全）
  - `self._trigger_clicked.connect(self._on_trigger_click)`（queued connection 自动回主线程）
- 验证（_py38+PyQt5 脚本）：新方式主线程收到 ✓；旧方式（子线程 singleShot）确实不 fire——根因坐实

### 根因 #2：拖画框选闪退 = Qt6 专属 API（19:47 老板实测反馈）
- `region_select.py` 三处 `event.globalPosition()`（Qt6 API）→ PyQt5 打包版 AttributeError → PyInstaller frozen 环境未捕获异常 = 直接退出（闪退）
- 修复：`qt_compat.py` 新增 `mouse_global_pos(event)`（QT6: `globalPosition().toPoint()`；QT5: `globalPos()`）；`region_select.py` 三处替换
- 验证（QTest 模拟按下-拖动-释放）：框选正常、信号发出、零异常 ✓
- 教训：**全项目 grep Qt6-only API**（globalPosition/.position()/exec()/QAction）只命中 region_select.py，qt_compat 是唯一兼容层入口，新 UI 代码必须走 qt_compat

### Win7 卡顿优化（保留，与触发无关）
- 根因：主进程 30ms 全局 timer（≈33fps）刷新 4 路 800x600 预览 + 虚影叠加 + ORB 对齐 + 量化对比，Win7 老 CPU 被榨干
- 方案（动态降频，**不能只改 start(30)**——timer 是全局的）：
  - 待机：150ms（~7fps，够画框/移动检测）
  - 录像中：200ms（5fps）——录像由 worker 独立写盘，**画质零影响**
  - 停止后：恢复 150ms（预览保持运行，不黑屏）
  - ORB/量化节流 `_tick_count % 5` 配合（150ms×5 ≈ 750ms 一次）
- 补充认知：v10.10 的丢帧是单进程架构问题（采集+MJPEG+预览全挤一进程），v10.14 多进程架构免疫；USB/接线无责（VideoRecDiag 满帧测试 4×800x600@30 零丢帧）

### 防孤儿机制重做
- v10.14.9 心跳自杀看门狗两个 bug（08:06/08:15 实锤，老板现场三路全灭）：
  - Bug A：`_watch_parent` 线程在 shm attach 前启动 → NameError 误自杀（cam#1 秒退）
  - Bug B：心跳只在 `_read_status()` 写，主进程 open 阻塞时其他 worker 心跳停更 15s 误自杀（cam#2 16s 退）
- v10.14.11 处置：**移除心跳自杀机制**（camera.py 已回退 v10.14.9 之前状态），改为 **main.py 启动时清理孤儿 worker**（wmic 查 `--capture-worker` + 验父进程存活，只杀真孤儿）
- 心跳修复三处方案（监控线程后移/主进程统一写心跳/阈值 8s）仍待老板排期，作为备选

### 打包与交付
- 打包命令：`D:\video_rec4_win7_offline\_py38\python.exe -m PyInstaller VideoRec.spec`（VIDEOREC_QT5=1）
- 流程：py_compile 全量 → PyInstaller（先清 dist/build，dist 非空会 COLLECT 失败）→ 拷贝到 `VideoRec_win7_v10.14.11\`（拷前先杀残留 VideoRec 进程，否则 exe 被占用拷不动）
- 交付：`VideoRec_win7_v10.14.11\`（VideoRec.exe 3,137,780 B / 19:52）+ `VideoRec_win7_v10.14.11.zip`（19:58 重打，旧 16:29 zip 已改名 `_OLD_1629.zip`）
- 验证：exe 冒烟 12s 存活、stderr 非 OpenCV 噪音 0 行；使用说明.md 已更新

### 遗留待办
- [ ] 老板实测确认：框选不闪退 ✓（19:55 确认"可以了"）+ Win7 实机卡顿体感（待反馈）+ 点击触发实测
- [ ] 心跳修复三处方案（备选，待排期）
- [ ] v10.14.5~v10.14.10 使用说明补全版本史
- [ ] legacy 触发区原文件位置（`D:\video_rec4\legacy_src\video_rec_v2.py` 不存在）
- [ ] VideoRecDiag.spec PySide2 冲突、7 个未识别 python.exe、H81M-D PCIe x1 槽数确认

---

## 开发铁律（血泪教训汇总）

1. **Qt 版本统一**：全部走 `qt_compat`，禁止 PyQt5/PyQt6 混装（同进程双 Qt = 0xC0000409 秒崩无 traceback）；打包强制 PyQt5（PyQt6 不支持 Win7）
2. **Win7 打包必须 Python 3.8**（`_py38` 在项目根目录）：Py3.9 打的包 Win7 起不来；打包后检查 `_internal` 的 api-ms-win-* 大小对照 KB2999226 23175 表（详见 `VideoRec_Win7打包经验.md`）
3. **编辑前必须 dump 真实文件内容核实锚点**，禁止靠记忆/压缩前行号猜 oldText（12+ 次失败教训）
4. **exec 里避免内联 python 中文+引号**（PowerShell 截断 SyntaxError）；PowerShell 重定向是 UTF-16，python 读会 UnicodeDecodeError——一律 write 工具写 .py 脚本再执行
5. **测试后必须 close() + 清残留**（capture-worker 孤儿锁摄像头；老板机器上出现过 4 个残留 VideoRec 实例）
6. **matplotlib/图像方向铁律**：imshow 必须显式 extent + origin='upper'（详见 workspace MEMORY.md）
7. **pynput 触发回调必须走 Qt 信号回主线程**（本版修复的核心，别回退成 singleShot）

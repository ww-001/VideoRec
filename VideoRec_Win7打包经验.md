# PyInstaller Win10 打包 → Win7 运行 完整修复经验

> 2026-08-08 实战总结 · VideoRec.exe (Python 3.8.10 + PyInstaller 6.3.0 onedir)
> 目标：离线 Win7 SP1 (6.1.7601) 无补丁环境运行

---

## 一、问题现象

启动 VideoRec.exe 报两个错：

| 报错 | 原文 | 含义 |
|------|------|------|
| 弹窗 | `VideoRec.exe - 无法找到入口：无法定位程序输入点 ucrtbase.terminate 于动态链接库 api-ms-win-crt-runtime-l1-1-0.dll 上` | UCRT 转发链断裂 |
| Fatal | `Error loading Python DLL '...\_internal\python38.dll'. LoadLibrary:找不到指定的程序` | 127 = ERROR_PROC_NOT_FOUND |

---

## 二、根因（绕了 10 轮才找到，务必记住）

### 核心依赖链

```
VideoRec.exe (bootloader)
  └→ vcruntime140.dll (exe根)
       └→ api-ms-win-crt-runtime-l1-1-0.dll (转发器)
            └→ ucrtbase.dll
                 └→ 25 个 api-ms-win-core-*.dll  ← 真凶在这

python38.dll
  └→ 12 个 api-ms-win-crt-*.dll (math/locale/string/runtime/stdio/
     convert/time/environment/process/heap/conio/filesystem)
       └→ ucrtbase.dll
            └→ 25 个 api-ms-win-core-*.dll  ← 真凶在这
```

### 致命事实

1. **PyInstaller 在 Win10 上打包时，收集的 `api-ms-win-*` 全是 Win10 版**
   - Win10 的 System32 里 **0 个** `api-ms-win-*` 物理文件（Win10 用虚拟 API set schema）
   - PyInstaller 是从 Win10 的 WinSxS 里收集的 → 全是 Win10 版
2. **Win7 的 System32 原生有 `api-ms-win-core-*` 物理文件**（Win7 原生 API set）
   - 但 **没有** `api-ms-win-crt-*` 和 `ucrtbase.dll`（要装 KB2999226 才有）
3. **25 个 core 里只有 8 个在 KB2999226 包内**，其余 17 个必须用 Win7 System32 原生版
4. **exe 根目录 vs _internal 的搜索顺序**：
   - bootloader 早期（SetDllDirectory 生效前）：exe根 → System32 → 当前目录 → PATH
   - Python 加载期：exe根 → System32 → _internal（SetDllDirectory）
   - 所以 UCRT 必须放 exe 根，bootloader 才能第一时间找到

---

## 三、最终修复方案（v9，已验证有效）

### exe 根目录（与 VideoRec.exe 同级）

| 文件 | 版本 | 大小 |
|------|------|------|
| ucrtbase.dll | KB2999226 23175 | 984,448 |
| api-ms-win-crt-*.dll ×15 | KB2999226 23175 | 各 12K-64K |
| api-ms-win-core-*.dll ×8 | KB2999226 23175 | 各 11.6K-14K |
| api-ms-win-eventing-provider-l1-1-0.dll | KB2999226 23175 | 11,616 |
| vcruntime140.dll | 14.00.23026 | 88,752 |
| vcruntime140_1.dll | 14.28.29914 | 36,728 |
| msvcp140.dll | — | 590,112 |
| msvcp140_1.dll | — | 31,728 |
| msvcp140_2.dll | — | 193,520 |

### _internal 目录

- 保留：15 个 crt + 8 个 core（KB2999226 23175 版）+ python38.dll 等
- **删除：全部 23 个 Win10 版 `api-ms-win-core-*`**（让 Win7 用 System32 原生版）
- 最终 _internal = 52 文件（15 crt + 8 core + 29 其他）

### 8 个 KB 包内 core（必须自带，Win7 System32 没有）

```
api-ms-win-core-file-l1-2-0.dll          11,616
api-ms-win-core-file-l2-1-0.dll          11,616
api-ms-win-core-localization-l1-2-0.dll  14,176
api-ms-win-core-processthreads-l1-1-1.dll 12,128
api-ms-win-core-synch-l1-2-0.dll         12,128
api-ms-win-core-timezone-l1-1-0.dll      11,616
api-ms-win-core-xstate-l2-1-0.dll        11,616
api-ms-win-eventing-provider-l1-1-0.dll  11,616
```

### 其余 17 个 core（Win7 System32 原生有，不要打包）

```
api-ms-win-core-string-l1-1-0 / errorhandling / file-l1-1-0 / namedpipe
api-ms-win-core-handle / heap / libraryloader / synch-l1-1-0
api-ms-win-core-processthreads-l1-1-0 / processenvironment / datetime
api-ms-win-core-sysinfo / console / debug / rtlsupport / profile / memory
api-ms-win-core-util / interlocked
```

---

## 四、关键工具与验证方法

### 1. 判断 DLL 是不是 Win7 版（大小对照表）

KB2999226 23175 版标准大小（**唯一权威标准**，旧参考表作废）：

```
ucrtbase=984,448   runtime=16,224   conio=12,640
convert=15,712     environment=12,128  filesystem=13,664
heap=12,640        locale=12,128   math=20,832
multibyte=19,808   private=63,840  process=12,640
stdio=17,760       string=17,760   time=14,176   utility=12,128
```

**凡大小 ≠ 上表 = Win10 版嫌疑**（Win10 版 crt 约 20K-28K，core 约 19K）

### 2. 获取 KB2999226（Win7 UCRT 补丁）

- 下载页：`https://www.microsoft.com/en-us/download/details.aspx?id=49093`
- 直链：`https://download.microsoft.com/download/1/1/5/11565a9a-ea09-4f0a-a57e-520d5d138140/Windows6.1-KB2999226-x64.msu`（1,034,556 B）
- 解包：`expand xxx.msu -F:* dir` → 得主 CAB → 再 `expand xxx.cab -F:* dir`
- 用 **23175 版**（新）而非 18972 版（旧）
- 目录：`amd64_microsoft-windows-ucrt_..._6.1.7601.23175...` 和 `amd64_microsoft-windows-u..rsalcrt-apifwd-win7_..._6.1.7601.23175...`

### 3. 验证脚本（pefile）

```python
# 检查 python38.dll 的 UCRT 函数是否在新 ucrtbase.dll 全存在
# 检查转发器导出（terminate 等）
# 检查 DLL 导入表（哪些 dll、哪些函数）
```

### 4. 验证 zip 完整性

```python
import zipfile
z = zipfile.ZipFile(path)
names = set(z.namelist())
# 检查关键文件是否都在
```

---

## 五、血泪教训（10 轮排查总结）

1. **别只修表面**：第 1-6 轮修了 VC runtime、python38.dll 导入、KERNEL32/ADVAPI32 等，都是对的但不够——**真正的坑在 api-ms-win-core 转发链**。
2. **报错 127 不是"缺文件"而是"函数/转发解析失败"**：LoadLibrary 返回 127 = 某个依赖 DLL 加载了但函数对不上，或转发目标不存在。
3. **`无法定位程序输入点 X 于 Y.dll 上` = 转发链断裂**：Y 是转发器，它转发到 X 所在的 DLL，但那个 DLL 找不到/版本不匹配。
4. **Win10 打包机收集的 api-ms-win-\* 全是 Win10 版**——这是 PyInstaller 跨版本部署的最大坑。
5. **Win7 System32 原生有 api-ms-win-core-\*，但没有 api-ms-win-crt-\* 和 ucrtbase**——前者不要打包（用系统的），后者必须打包（Win7 没有）。
6. **exe 根 vs _internal 搜索顺序差异**：UCRT 全家桶放 exe 根（bootloader 早期就需要），不要只放 _internal。
7. **KB2533623 是 Vista 补丁**（只含 kernel32.dll），Win7 SP1 原生支持 API set，不需要它。
8. **诊断脚本必须检查 System32 的 api-ms-win-core-\***（之前只查了 crt-runtime 和 ucrtbase，漏了 core 系列）。
9. **打包前做"Win7 模拟"**：在打包机上用 `os.add_dll_directory` 只加载包内 DLL 测试 python38.dll 能否加载（能通过不代表 Win7 能过，但能抓内部不一致）。
10. **版本判断用大小对照表**，别信文件名或直觉。

---

## 六、可复用资产（勿删）

| 资产 | 位置 |
|------|------|
| KB2999226 MSU | `D:\video_rec4_win7_offline\Windows6.1-KB2999226-x64.msu` |
| 解包目录 | `kb2999226_extract\` + `kb2999226_cab\` |
| Win7 版 DLL 提取 | `kb2999226_win7dlls\` |
| 旧备份 | `_internal_bak_215550`、`VideoRec_win7.zip.old3_preUCRT` |
| 最终版 | `VideoRec_win7_v9.zip`（97,296,796 B） |
| 诊断脚本 | `diag5_Win7.bat`（随 v9 分发） |

---

## 七、下次打包清单（速查）

```
□ 1. 打包机确认：Python 3.8.10（最后免补丁版）、PyInstaller 6.3.0
□ 2. 打包后：检查 _internal 所有 api-ms-win-* 大小，对照 KB2999226 表
□ 3. Win10 版 core（约19K）→ 删除（Win7 用 System32 原生）
□ 4. crt/ucrtbase 若 Win10 版 → 用 KB2999226 23175 替换
□ 5. exe 根放：ucrtbase + 15 crt + 8 core + 5 VC runtime
□ 6. _internal 放：15 crt + 8 core（KB 版）
□ 7. 验证：python38.dll 依赖链、zip 完整性
□ 8. Win7 测试：diag5 脚本 → 双击 exe → 功能冒烟
```

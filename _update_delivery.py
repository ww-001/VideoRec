# -*- coding: utf-8 -*-
"""更新 VideoRec_win7_v10.14.11 交付目录 + zip（保留运行数据）"""
import os, shutil, zipfile

SRC = r"D:\video_rec4_win7_offline\_build_v10.14.11\VideoRec"
DST = r"D:\video_rec4_win7_offline\VideoRec_win7_v10.14.11"

# 1. 替换 _internal（删除旧的，复制新的）
old_internal = os.path.join(DST, "_internal")
if os.path.isdir(old_internal):
    shutil.rmtree(old_internal)
shutil.copytree(os.path.join(SRC, "_internal"), old_internal)

# 2. 替换 exe
shutil.copy2(os.path.join(SRC, "VideoRec.exe"), os.path.join(DST, "VideoRec.exe"))
print("exe + _internal updated")

# 3. 校验关键文件
for f in ("VideoRec.exe", "_internal\\PyQt5\\Qt5\\bin\\Qt5Core.dll",
          "_internal\\ucrtbase.dll", "_internal\\assets\\wwt_logo.png"):
    p = os.path.join(DST, f)
    print("  %-45s %s %d" % (f, "OK" if os.path.exists(p) else "MISSING",
                             os.path.getsize(p) if os.path.exists(p) else 0))

# 4. 更新 zip
zip_path = r"D:\video_rec4_win7_offline\VideoRec_win7_v10.14.11.zip"
if os.path.exists(zip_path):
    os.remove(zip_path)
base = os.path.basename(DST)
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(DST):
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.join(base, os.path.relpath(p, DST)))
print("zip updated: %.1f MB" % (os.path.getsize(zip_path) / 1e6))

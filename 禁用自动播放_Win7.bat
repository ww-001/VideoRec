@echo off
chcp 936 >nul
title U盘防护 - 禁用自动播放
color 0A

:: ===== 检查管理员权限, 没有则自动提权 =====
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限, 请在弹出的窗口点"是"...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo ============================================
echo   禁用自动播放  (Win7 录制专用机 U盘防护)
echo ============================================
echo.

:: ===== 1. 禁用所有驱动器自动播放 (0xFF = 全部类型) =====
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoDriveTypeAutoRun /t REG_DWORD /d 255 /f >nul
reg add "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoDriveTypeAutoRun /t REG_DWORD /d 255 /f >nul
reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoDriveTypeAutoRun /t REG_DWORD /d 255 /f >nul

:: ===== 2. 关闭 Autorun 自动运行 =====
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoAutorun /t REG_DWORD /d 1 /f >nul
reg add "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoAutorun /t REG_DWORD /d 1 /f >nul
reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoAutorun /t REG_DWORD /d 1 /f >nul

:: ===== 3. 验证写入结果 =====
echo [验证] 自动播放策略 (应为 0xff):
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoDriveTypeAutoRun
echo.
echo [验证] Autorun 开关 (应为 0x1):
reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer" /v NoAutorun

echo.
echo ============================================
echo   完成! 设置立即生效, 无需重启。
echo.
echo   补充建议(手动做一次):
echo     控制面板 - 自动播放 - 所有类型选"不执行操作"
echo     文件夹选项 - 查看 - 勾选"显示文件扩展名"
echo ============================================
echo.
pause

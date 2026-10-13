@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 启明随行 - 提交前密钥自检

if not defined PY set "PY=C:\Users\86186\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 交源码 / 打包 / 发压缩包之前，跑一次这个，确认 API Key 没被带出去。
echo （脚本只打印掩码，不会显示完整 Key）
echo.
"%PY%" check_model.py --no-test >nul 2>&1
"%PY%" check_secret.py
echo.
echo 退出码 0 = 可以提交；非 0 = 先按上面的提示处理。
pause

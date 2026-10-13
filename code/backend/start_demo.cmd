@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 启明随行 - 演示模式（不消耗额度）

if not defined PY set "PY=C:\Users\86186\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM ================================================================
REM  演示模式：把 MOCK_MODE 强制设为 true，起一个"点不花钱"的实例。
REM
REM  为什么要单独做一个脚本：
REM    .env 里写的是 MOCK_MODE=false（真实模型），开发调试时没问题。
REM    但服务一旦会被别人碰到 —— 给同学试用、录屏、答辩彩排，
REM    甚至只是开着电脑忘了关 —— 每一次点击都在真实消耗 API 额度。
REM    本脚本用 set 覆盖 .env 的值：python-dotenv 默认不覆盖已存在的
REM    环境变量，所以这里设的值优先（已实测生效，非推测）。
REM ================================================================

set MOCK_MODE=true

echo ============================================================
echo   启明随行 - 演示模式
echo ============================================================
echo.
echo 已强制 MOCK_MODE=true，本实例：
echo   [1] 不调用任何大模型，识别结果全部是内置示例
echo   [2] 被外人访问也不会消耗你的 API 额度
echo   [3] 页面顶部会出现黄色"演示模式"横幅——这是正确行为，不是故障
echo.
echo 需要用真实模型时：关掉本窗口，改双击 start_lan.cmd
echo.

"%PY%" check_model.py --no-test
echo.

echo 本机可用的访问地址（找「无线局域网适配器 WLAN」那一段的 IPv4）：
ipconfig | findstr /i "IPv4"
echo.
echo 手机请访问： http://^<上面 WLAN 的 IPv4^>:8135
echo 提示：真要拿手机测，用「手机开热点、电脑连上」比校园网可靠得多，
echo       校园网会把同一 WiFi 下的设备互相隔离。
echo.

"%PY%" -m uvicorn main:app --host 0.0.0.0 --port 8135
pause

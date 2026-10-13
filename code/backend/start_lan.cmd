@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 依赖装在 WorkBuddy 的隔离 venv 里；想换解释器先 set PY=...
if not defined PY set "PY=C:\Users\86186\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM 【重要】这里以前写死了 MOCK_MODE=true，结果"双击启动"就等于强制演示模式，
REM 用户配了 Key 也用不上，界面顶部一直挂黄色横幅。现在不再强制，
REM 由 .env 里的 MOCK_MODE 决定（默认 false）。想让后端只做演示：
REM     set MOCK_MODE=true 之后再运行本脚本。

echo ============================================================
echo   启明随行 - 局域网服务
echo ============================================================
echo.
echo 本机可用的访问地址（找「无线局域网适配器 WLAN」那一段的 IPv4）：
ipconfig | findstr /i "IPv4"
echo.
echo 手机请访问： http://^<上面 WLAN 的 IPv4^>:8135
echo 注意：192.168.73.1 / 192.168.202.1 是 VMware 虚拟网卡，手机连不上。
echo.
echo 若手机打不开，管理员 cmd 跑一次这条（只需一次，永久生效）：
echo   netsh advfirewall firewall add rule name="启明随行-8135" dir=in action=allow protocol=TCP localport=8135
echo.

"%PY%" check_model.py --no-test
echo.

"%PY%" -m uvicorn main:app --host 0.0.0.0 --port 8135
pause

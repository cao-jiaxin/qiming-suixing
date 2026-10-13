@echo off
REM 放行 8135 端口入站，让手机能连上本机服务
REM 用法：右键本文件 -> 以管理员身份运行（双击无效，会提示"请求的操作需要提升"）
chcp 65001 >nul

netsh advfirewall firewall add rule name="启明随行-8135" dir=in action=allow protocol=TCP localport=8135

echo.
echo ================= 校验 =================
netsh advfirewall firewall show rule name="启明随行-8135" | findstr /i "规则名称 已启用 方向 操作 LocalPort"
echo.
echo 上面能打印出规则即成功。此规则永久有效，不用重复运行。
echo.
echo 当前可用的手机访问地址（找「WLAN」那一段的 IPv4）：
ipconfig | findstr /i "IPv4"
echo.
echo 手机浏览器打开： http://^<WLAN 的 IPv4^>:8135
echo.
pause

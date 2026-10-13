@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 启明随行 - 模型自检

if not defined PY set "PY=C:\Users\86186\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 本脚本用于回答一个问题：现在到底是在用真实模型，还是在返回内置示例？
echo.
"%PY%" check_model.py
echo.
echo ------------------------------------------------------------
echo 想把真实模型打开？在下面这条命令后粘贴你的免费 Key 即可（输入不回显）：
echo   "%PY%" check_model.py --set-key
echo ------------------------------------------------------------
pause

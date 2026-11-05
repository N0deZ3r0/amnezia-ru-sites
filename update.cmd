@echo off
rem Обновляет lists\amnezia_top300.json и lists\amnezia_top1000.json.
rem Запускать при выключенном VPN. Нужен Python и пакет dnspython.
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --quiet dnspython
python update.py %*
echo.
pause

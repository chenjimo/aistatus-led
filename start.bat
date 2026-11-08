@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem One click: open LAN ports, then start middleware (which starts the phone bridge).
rem The autostart watcher passes --no-elevate so it never triggers a hidden UAC prompt.
if /I "%~1"=="--no-elevate" goto :run

rem If both firewall rules already exist, no elevation is needed.
netsh advfirewall firewall show rule name="AIStatusPhoneBridge-TCP" >nul 2>&1
if errorlevel 1 goto :checkadmin
netsh advfirewall firewall show rule name="AIStatusPhoneBridge-UDP" >nul 2>&1
if errorlevel 1 goto :checkadmin
goto :run

:checkadmin
net session >nul 2>&1
if not errorlevel 1 goto :run
echo Requesting administrator rights once (to open TCP 8770 / UDP 4211)...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
exit /b

:run
if /I "%~1"=="--elevated" shift
python main.py phone-bridge firewall
echo.
python main.py start
echo.
pause

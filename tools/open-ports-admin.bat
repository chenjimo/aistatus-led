@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo ============================================================
echo  AI Status - open LAN ports (needs Administrator)
echo  TCP 8765 (middleware API, for WeChat mini program)
echo  TCP 8770 (phone bridge HTTP/WS)
echo  UDP 4211 (LAN discovery)
echo ============================================================
python main.py phone-bridge firewall
echo.
echo To remove the rules later (admin PowerShell):
echo   netsh advfirewall firewall delete rule name="AIStatusLed-TCP-API"
echo   netsh advfirewall firewall delete rule name="AIStatusPhoneBridge-TCP"
echo   netsh advfirewall firewall delete rule name="AIStatusPhoneBridge-UDP"
echo.
pause

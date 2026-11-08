@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo ============================================================
echo  AI Status Phone Bridge - open LAN ports (needs Administrator)
echo  TCP 8770 (phone HTTP/WS) + UDP 4211 (discovery)
echo ============================================================
python main.py phone-bridge firewall
echo.
echo To remove the rules later (admin PowerShell):
echo   netsh advfirewall firewall delete rule name="AIStatusPhoneBridge-TCP"
echo   netsh advfirewall firewall delete rule name="AIStatusPhoneBridge-UDP"
echo.
pause

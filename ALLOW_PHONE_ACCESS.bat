@echo off
rem Run ONCE as Administrator: lets phones on your PRIVATE Wi-Fi reach the controller (port 8080).
netsh advfirewall firewall delete rule name="Room Lights controller" >nul 2>&1
netsh advfirewall firewall add rule name="Room Lights controller" dir=in action=allow protocol=TCP localport=8080 profile=private
if errorlevel 1 (
  echo.
  echo That needs Administrator rights: right-click this file and choose "Run as administrator".
) else (
  echo.
  echo Done. Phones on your private Wi-Fi can now open the controller.
)
pause

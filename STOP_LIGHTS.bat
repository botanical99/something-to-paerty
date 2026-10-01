@echo off
rem Stops the controller gracefully: any running party/music is stopped and the room is put back as it was.
setlocal
set PORT=%LIGHTS_PORT%
if "%PORT%"=="" set PORT=8080
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri http://localhost:%PORT%/api/shutdown -TimeoutSec 5 | Out-Null; Write-Host 'Stopping - restoring the room...' } catch { Write-Host 'The controller is not running on port %PORT%.' }"
timeout /t 6 >nul

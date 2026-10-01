@echo off
rem Runs the whole app against the built-in simulator. No lights, no gateway, no network access needed.
cd /d "%~dp0"
call START_LIGHTS.bat --simulate

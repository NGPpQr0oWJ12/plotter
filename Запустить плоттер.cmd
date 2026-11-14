@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0web-plotter\start.ps1" %*
if errorlevel 1 pause

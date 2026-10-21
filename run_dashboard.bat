@echo off
title Revenue Chaser
rem Visible fallback for the desktop shortcut "Revenue Chaser" (which runs launch.pyw with no console window):
rem it runs the same start.ps1 here, so its messages stay on screen. The dashboard itself runs hidden, its
rem output goes to the Logs tab (.cache\logs), and closing this window does not stop it: stop.ps1 does.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause

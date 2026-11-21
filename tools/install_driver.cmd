@echo off
REM Install the locally built CyberFinger SteamVR driver (see install_driver.ps1 for options).
REM Started by double-click, the script waits for Enter before the window closes.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_driver.ps1" %*
exit /b %ERRORLEVEL%

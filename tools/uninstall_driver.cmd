@echo off
REM Unregister the CyberFinger SteamVR driver and remove the copy made by install_driver.cmd.
REM Started by double-click, the script waits for Enter before the window closes.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_driver.ps1" -Uninstall %*
exit /b %ERRORLEVEL%

@echo off
REM Makes a Resonite build (the Steam copy, or another such as a private universe) load a Gale profile's
REM mods however it is started. Without arguments it lists the builds found and asks; -Status shows their state.
REM See README.md, "Loading Gale mods without Gale's launcher", and: resonite_gale_link.cmd -?
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0resonite_gale_link.ps1" %*
set "RC=%ERRORLEVEL%"
REM Keep the window open when started by double-click.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b %RC%

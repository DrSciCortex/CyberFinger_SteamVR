@echo off
REM Build the full CyberFinger installer: SteamVR driver + bridge app + Resonite mods, packaged with Inno Setup 6.3+.
REM Run it where "python" is the bridge's Python with PyInstaller installed (e.g. conda activate cybrgui).
REM Output: bridge\dist\installer\CyberFingerBridge_Setup_<version>.exe
setlocal
set "ROOT=%~dp0.."

call "%~dp0build_driver.cmd" < NUL || goto :fail

REM The Resonite mods for the installer's "Resonite mods" option, built into bridge\installer\resonite_mods. They
REM compile against Resonite's assemblies. On a PC without Resonite: CF_RESONITE_MODS=keep packages that folder as
REM it is (staged on another PC and copied over), CF_RESONITE_MODS=skip builds the installer without the mods.
if /i "%CF_RESONITE_MODS%"=="keep" goto :modsdone
if exist "%ROOT%\bridge\installer\resonite_mods" rmdir /s /q "%ROOT%\bridge\installer\resonite_mods"
if /i "%CF_RESONITE_MODS%"=="skip" goto :modsdone
python "%ROOT%\tools\stage_resonite_mods.py" || goto :fail
:modsdone

pushd "%ROOT%\bridge"
call ".\build.bat" < NUL
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" goto :fail
if not exist "%ROOT%\bridge\dist\CyberFingerBridge.exe" goto :fail

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined INNO_ISCC set "ISCC=%INNO_ISCC%"
if not defined ISCC (
    echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
    echo or set INNO_ISCC to the full path of ISCC.exe.
    goto :fail
)
"%ISCC%" "%ROOT%\bridge\installer\setup.iss" || goto :fail

echo.
echo Installer: %ROOT%\bridge\dist\installer
set "RC=0"
goto :end
:fail
echo.
echo Installer build FAILED.
set "RC=1"
:end
REM Keep the window open when started by double-click.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b %RC%

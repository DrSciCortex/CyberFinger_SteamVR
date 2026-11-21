@echo off
REM Build the CyberFinger SteamVR driver (Release) with the Visual Studio C++ tools, and run its tests.
REM Output: out\build\x64-Release\driver\cyberfinger      Next: tools\install_driver.cmd
REM Needs Visual Studio 2022 or newer with the "Desktop development with C++" workload.
setlocal
set "ROOT=%~dp0.."
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSDIR="
if not exist "%VSWHERE%" goto :novs
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
:novs
if not defined VSDIR (
    echo No Visual Studio with the C++ build tools was found.
    echo Open the Visual Studio Installer and add the "Desktop development with C++" workload.
    goto :fail
)
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || goto :fail

cd /d "%ROOT%"
if not exist "openvr\headers\openvr_driver.h" git submodule update --init openvr || goto :fail
if not exist "third_party\minhook\include\MinHook.h" git submodule update --init third_party/minhook || goto :fail

cmake -S . -B out\build\x64-Release -G Ninja -DCMAKE_BUILD_TYPE=Release || goto :fail
cmake --build out\build\x64-Release || goto :fail
ctest --test-dir out\build\x64-Release --output-on-failure || goto :fail

echo.
echo Built: %CD%\out\build\x64-Release\driver\cyberfinger
echo Next:  tools\install_driver.cmd   (with SteamVR closed)
set "RC=0"
goto :end
:fail
echo.
echo Build FAILED.
set "RC=1"
:end
REM Keep the window open when started by double-click.
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b %RC%

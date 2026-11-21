@echo off
REM ── CyberFinger Bridge Build Script ──
REM
REM Prerequisites:
REM   pip install pyinstaller pillow
REM   pip install -r requirements.txt
REM
REM   Inno Setup 6: https://jrsoftware.org/isdl.php (for installer)
REM
REM PRIVACY: This script strips personal paths (usernames, etc.)
REM from the resulting .exe binary.

echo ══════════════════════════════════════════════════════
echo   CyberFinger Bridge — Build
echo ══════════════════════════════════════════════════════
echo.

REM Step 1: Convert PNG icon to ICO (needs Pillow)
echo [1/4] Converting icon...
python -c "from PIL import Image; img = Image.open('assets/icon_32x32.png'); img.save('assets/icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 (
    echo WARNING: Could not create .ico file. Using default icon.
    echo Install Pillow: pip install pillow
)

REM Step 2: Clean previous build (removes cached paths)
echo [2/4] Cleaning previous build...
if exist "build" rmdir /s /q build
if exist "dist\CyberFingerBridge.exe" del /f "dist\CyberFingerBridge.exe"

REM Step 3: Build with PyInstaller
REM   --log-level WARN    less noise
REM   PYTHONDONTWRITEBYTECODE prevents .pyc caching with local paths
echo [3/4] Building executable (paths will be stripped)...
set PYTHONDONTWRITEBYTECODE=1
pyinstaller cyberfinger_bridge.spec --noconfirm --log-level WARN
if errorlevel 1 (
    echo ERROR: PyInstaller build failed!
    pause
    exit /b 1
)

REM Step 4: Verify no personal paths leaked
echo [4/4] Checking for path leaks...
findstr /i /c:"%USERNAME%" dist\CyberFingerBridge.exe >nul 2>&1
if not errorlevel 1 (
    echo WARNING: Username "%USERNAME%" found in binary!
    echo This may be in Python bytecode paths. Consider building
    echo from a path without your username, e.g. C:\Build\
) else (
    echo OK: Username not found in binary.
)

REM Copy assets
if not exist "dist" mkdir dist
copy /y assets\icon.png dist\ >nul 2>&1
copy /y assets\icon.ico dist\ >nul 2>&1

echo.
echo ══════════════════════════════════════════════════════
echo   Build complete!
echo   Executable: dist\CyberFingerBridge.exe
echo.
echo   To create installer, install Inno Setup 6 and run:
echo     iscc installer\setup.iss
echo.
echo   TIP: For guaranteed path-clean builds, clone the repo
echo   to a neutral path like C:\Build\CyberFinger\ first.
echo ══════════════════════════════════════════════════════
pause

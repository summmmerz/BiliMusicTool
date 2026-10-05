@echo off
rem ============================================================
rem  BiliMusicTool - single launcher (the only file you double-click)
rem
rem  IMPORTANT: keep this file ASCII-only with CRLF line endings.
rem  cmd.exe parses .bat files using the console OEM code page, so
rem  non-ASCII text here breaks on machines with a different locale.
rem  Chinese only appears in the *file name*, which is safe (NTFS
rem  stores names as UTF-16).
rem  Also: every literal ")" inside an "if (...)" block must be written
rem  as "^)", otherwise cmd treats it as the end of the block.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Products land in <package root>\output so the user can find them
set "BILIMUSIC_OUTPUT=%CD%\output"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)

if not defined PY goto NOPYTHON

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto OLDPYTHON

rem ---- first run: the program folder must be there ----
if not exist "program\serve.py" (
  echo.
  echo   [ERROR] "program" folder is missing next to this file.
  echo   Please unzip the whole archive first, then run again.
  echo   Do NOT run this file from inside the zip preview window.
  echo.
  pause
  exit /b 1
)

rem ---- ask the program itself whether dependencies are ready ----
pushd program
%PY% -c "import numpy, requests, yt_dlp, sys; sys.exit(0)" >nul 2>nul
set "DEPS_OK=%errorlevel%"
if not "%DEPS_OK%"=="0" (
  echo.
  echo   ============================================================
  echo    First run: installing required components
  echo    Needs internet access. Usually takes under a minute.
  echo    Please do not close this window.
  echo   ============================================================
  echo.
  %PY% -m pip install --upgrade pip >nul 2>nul
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   [ERROR] Installing components failed.
    echo   Please check your internet connection and try again.
    echo   If the download is slow, run this once in the same folder:
    echo     %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    popd
    pause
    exit /b 1
  )
  echo.
  echo   Components installed successfully.
)
popd

:MENU
chcp 65001 >nul 2>nul
cls
echo.
echo   ==========================================================
echo      BiliMusicTool
echo(     B-site songs  --  download  --  cover art  --  BPM
echo(     --  one-bar silence lead-in  --  44.1kHz / 320k MP3
echo   ==========================================================
echo.
echo      [1]  Open the web page   ^(recommended^)
echo      [2]  Command line mode
echo      [3]  Check environment
echo      [4]  Diagnose download
echo      [0]  Exit
echo.
set "CHOICE="
set /p "CHOICE=  Type a number and press Enter: "
if "%CHOICE%"=="1" goto WEB
if "%CHOICE%"=="2" goto CLI
if "%CHOICE%"=="3" goto CHECK
if "%CHOICE%"=="4" goto DIAG
if "%CHOICE%"=="0" goto QUIT
goto MENU

:WEB
echo.
echo   Starting the web page...
echo   Your browser should open by itself.
echo   To stop the program: close this window, or press Ctrl+C here.
echo.
pushd program
%PY% serve.py --open --port 8765
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
  echo.
  echo   [WARNING] The page could not start ^(exit code %RC%^).
  echo   The port may already be in use. Close other copies and retry.
  echo.
  pause
)
goto MENU

:CLI
echo.
echo   Command line mode.
echo   Type a song name and press Enter.
echo   For all options run:  python -m bilimusic --help
echo.
pushd program
%PY% -m bilimusic
popd
echo.
pause
goto MENU

:CHECK
echo.
pushd program
%PY% install_deps.py
%PY% selftest.py
popd
echo.
pause
goto MENU

:DIAG
echo.
echo   This will try a real audio download and print a full report.
echo   If the download fails, please copy the WHOLE report to the
echo   person who gave you this program.
echo.
pushd program
%PY% diagnose_download.py
popd
echo.
pause
goto MENU

:NOPYTHON
echo.
echo   ============================================================
echo    [ERROR] Python was not found on this computer.
echo   ============================================================
echo.
echo    This program needs Python 3.10 or newer. It is free.
echo.
echo    1. Open this address in your browser:
echo         https://www.python.org/downloads/
echo    2. Click the big yellow "Download Python" button.
echo    3. Run the downloaded file.
echo    4. IMPORTANT: on the first screen, tick the checkbox at the
echo       bottom that says  "Add python.exe to PATH"
echo    5. Click "Install Now" and wait for it to finish.
echo    6. Run this launcher again.
echo.
pause
exit /b 1

:OLDPYTHON
echo.
echo   [ERROR] Your Python is too old. Version 3.10 or newer is required.
%PY% -c "import sys; print('   Installed version: ' + sys.version.split()[0])"
echo.
echo   Please install a newer Python from https://www.python.org/downloads/
echo.
pause
exit /b 1

:QUIT
endlocal
exit /b 0

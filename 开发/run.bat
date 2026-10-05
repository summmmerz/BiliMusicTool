@echo off
rem ============================================================
rem  BiliMusicTool - command line entry (same as the .bat next to it,
rem  kept because it is shorter to type). ASCII-only + CRLF.
rem ============================================================
setlocal
cd /d "%~dp0.."

chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "BILIMUSIC_OUTPUT=%CD%\output"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo   [ERROR] Python was not found. Install Python 3.10+ and tick
  echo           "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

pushd program
if "%~1"=="" (
  %PY% -m bilimusic --help
) else (
  %PY% -m bilimusic %*
)
popd
pause
endlocal

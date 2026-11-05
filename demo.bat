@echo off
REM ---------------------------------------------------------------------
REM One-command demo (Windows).
REM
REM   demo.bat                             first clip in data\videos
REM   demo.bat data\videos\my_clip.mp4     a specific clip
REM   demo.bat --multi-bus                 3 buses -> cross-bus dedup visible
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if "%PYTHON%"=="" set PYTHON=python

echo --- checking the environment ---
"%PYTHON%" scripts\check_setup.py
if errorlevel 1 (
  echo.
  echo Setup check failed.  Try:  %PYTHON% -m pip install -r requirements.txt
  exit /b 1
)

echo.
echo --- running the demo ---
if exist "%~1" (
  "%PYTHON%" scripts\run_demo.py --source "%~1" %2 %3 %4 %5 %6
) else (
  "%PYTHON%" scripts\run_demo.py %*
)
endlocal

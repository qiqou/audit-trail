@echo off
rem Audit Tool - Windows one-click build (double-click to run)
rem Entry: scripts\build_windows.ps1  (all messages in Chinese inside the ps1)
chcp 65001 >nul
cd /d "%~dp0scripts"
powershell -NoProfile -ExecutionPolicy Bypass -File build_windows.ps1
set "EXITCODE=%errorlevel%"
cd /d "%~dp0"
echo.
if "%EXITCODE%"=="0" (
  echo [OK] Build succeeded. See dist folder. Press any key to close.
) else (
  echo [FAILED] Build did not finish. See messages above. Press any key to close.
)
pause >nul

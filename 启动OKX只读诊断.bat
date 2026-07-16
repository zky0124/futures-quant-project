@echo off
chcp 65001 >nul
setlocal
set "PROJECT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%启动OKX只读诊断.ps1"
if errorlevel 1 (
  echo.
  echo OKX read-only diagnostic failed. No order was sent.
  pause
  exit /b 1
)
echo.
echo OKX read-only diagnostic completed. No order was sent.
pause

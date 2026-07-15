@echo off
setlocal
set "PROJECT=%~dp0"
set "PYTHON=%FQ_PYTHON%"
if defined PYTHON if not exist "%PYTHON%" set "PYTHON="
if not defined PYTHON if exist "%PROJECT%.venv\Scripts\python.exe" set "PYTHON=%PROJECT%.venv\Scripts\python.exe"
if not defined PYTHON set "PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON%" goto no_python
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%PROJECT%"
"%PYTHON%" launch_workbench.py
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" goto failed
exit /b 0

:no_python
echo Python was not found. Set FQ_PYTHON or create .venv first.
pause
exit /b 1

:failed
echo Workbench failed with exit code %EXIT_CODE%.
echo See reports\workbench_launch_error.log for details.
pause
exit /b %EXIT_CODE%

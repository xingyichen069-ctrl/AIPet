@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
rem Shared ASCII entry. User-facing guidance is printed by Python.
if exist "runtime\python.exe" goto portable
if exist ".venv\Scripts\python.exe" goto venv
if exist ".venv" goto broken_venv
where uv >nul 2>&1
if errorlevel 1 goto system_python
set "UV_CACHE_DIR=%CD%\work\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%CD%\.python"
uv venv --python 3.12 .venv
if errorlevel 1 goto failed
goto venv

:system_python
set "AIPET_VERSION="
for %%V in (3.12 3.14 3.13 3.11) do call :try_version %%V
if defined AIPET_VERSION goto py_launcher
python -c "import sys,sysconfig;assert (3,11)<=sys.version_info[:2]<=(3,14) and sysconfig.get_platform()=='win-amd64'" >nul 2>&1
if errorlevel 1 goto no_python
python -X utf8 tools\first_run.py %*
goto result

:py_launcher
py -%AIPET_VERSION% -X utf8 tools\first_run.py %*
goto result

:portable
"runtime\python.exe" -X utf8 tools\first_run.py %*
goto result

:venv
".venv\Scripts\python.exe" -X utf8 tools\first_run.py %*
goto result

:try_version
if defined AIPET_VERSION exit /b 0
py -%1 -c "import sysconfig;assert sysconfig.get_platform()=='win-amd64'" >nul 2>&1
if not errorlevel 1 set "AIPET_VERSION=%1"
exit /b 0

:result
set "AIPET_RESULT=%ERRORLEVEL%"
if not "%AIPET_RESULT%"=="0" goto finish
if "%~1"=="launch" exit /b 0
:finish
pause
exit /b %AIPET_RESULT%

:broken_venv
echo The .venv folder has no Python executable. Rename it as a backup and retry.
echo Keep data, persona, memory and the old installation.
goto failed

:no_python
echo Install Python 3.12 x64 or uv, then run this entry again.
echo Python: https://www.python.org/downloads/windows/
echo uv: https://docs.astral.sh/uv/getting-started/installation/
echo For Python, enable Add python.exe to PATH. See README.md for Chinese steps.
goto failed

:failed
echo Setup was not completed. Keep the output above for troubleshooting.
pause
exit /b 1

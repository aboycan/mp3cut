@echo off
rem mp3cut installer for Windows 10 / 11.
rem Installs everything mp3cut needs: Python (only if missing, via winget, per-user),
rem the mp3cut command, the user PATH entry, and a private FFmpeg copy (only if missing).
rem No administrator rights needed. Safe to re-run.
rem   install.bat         interactive
rem   install.bat /y      accept all prompts
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "YES_FLAG="
if /I "%~1"=="/y" set "YES_FLAG=--yes"
if /I "%~1"=="--yes" set "YES_FLAG=--yes"
if /I "%~1"=="-y" set "YES_FLAG=--yes"

rem ---- 1. Python >= 3.8 ----
call :find_python
if defined PY goto have_python

echo Python 3.8 or newer is required and was not found.
where winget >nul 2>nul
if errorlevel 1 goto no_winget
if defined YES_FLAG goto do_winget
choice /C YN /M "Install Python 3.12 for your user now with winget (no admin needed)"
if errorlevel 2 goto no_python
:do_winget
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto have_python
rem PATH of this window is stale after installing; look in the per-user install location
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"
)
if defined PY goto have_python
echo Python was installed, but this window cannot see it yet.
echo Close this window, open a NEW terminal and run install.bat again.
goto fail

:no_winget
echo winget is not available on this PC.
:no_python
echo Install Python from https://www.python.org/downloads/  (tick "Add python.exe to PATH"),
echo then run install.bat again.
goto fail

:have_python
echo =^> Using Python:
call %PY% -c "import sys; print('   ', sys.executable, sys.version.split()[0])"

rem ---- 2. pip ----
call %PY% -m pip --version >nul 2>nul
if not errorlevel 1 goto have_pip
echo =^> pip not found; trying ensurepip
call %PY% -m ensurepip --user >nul 2>nul
call %PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo ERROR: pip is not available for this Python. Reinstall Python with pip enabled.
  goto fail
)
:have_pip

rem ---- 3. pip install --user . (no --user inside an active virtual environment) ----
set "USER_FLAG=--user"
for /f %%v in ('call %PY% -c "import sys; print(int(sys.prefix != getattr(sys, 'base_prefix', sys.prefix)))"') do (
  if "%%v"=="1" set "USER_FLAG="
)
echo =^> Installing mp3cut ...
call %PY% -m pip install --upgrade --no-warn-script-location %USER_FLAG% "%SCRIPT_DIR%"
if not errorlevel 1 goto installed
echo =^> Retrying with the locally installed setuptools (PyPI may be unreachable) ...
call %PY% -m pip install --upgrade --no-warn-script-location --no-build-isolation %USER_FLAG% "%SCRIPT_DIR%"
:installed
if errorlevel 1 (
  echo ERROR: pip install failed - see the messages above.
  goto fail
)
rem pip skips a reinstall when the version number is unchanged; force the current source in
echo =^> Installing the current mp3cut source ...
call %PY% -m pip install --no-deps --force-reinstall -q --no-warn-script-location %USER_FLAG% "%SCRIPT_DIR%"
if errorlevel 1 call %PY% -m pip install --no-deps --force-reinstall -q --no-warn-script-location --no-build-isolation %USER_FLAG% "%SCRIPT_DIR%"
if errorlevel 1 (
  echo ERROR: pip reinstall of mp3cut failed - see the messages above.
  goto fail
)

rem ---- 4. user PATH, FFmpeg prerequisite and verification (run outside the source folder) ----
pushd "%USERPROFILE%"
call %PY% -m mp3cutter.pathsetup install %YES_FLAG%
set "RC=%ERRORLEVEL%"
popd
goto done

:find_python
set "PY="
where py >nul 2>nul
if errorlevel 1 goto fp_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto fp_python
set "PY=py -3"
goto :eof
:fp_python
where python >nul 2>nul
if errorlevel 1 goto :eof
rem the Microsoft Store "python" stub fails this check, so it is skipped
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=python"
goto :eof

:fail
set "RC=1"

:done
rem keep the window open when started by double-click from Explorer
echo %CMDCMDLINE% | find /I "%~0" >nul
if not errorlevel 1 pause
endlocal & exit /b %RC%

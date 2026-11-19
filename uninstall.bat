@echo off
rem Uninstall mp3cut (Windows). Removes the package and, after confirmation, only the
rem user-PATH entry that install.bat added. MP3 files and other PATH entries are never touched.
setlocal EnableExtensions
set "YES_FLAG="
set "PIP_YES="
if /I "%~1"=="/y" (set "YES_FLAG=--yes" & set "PIP_YES=-y")

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

pushd "%USERPROFILE%"
%PY% -c "import mp3cutter" >nul 2>nul
if not errorlevel 1 %PY% -m mp3cutter.pathsetup remove %YES_FLAG%
%PY% -m pip uninstall %PIP_YES% mp3cut
popd
echo mp3cut uninstalled. Open a new terminal for PATH changes to take effect.
endlocal

@echo off
rem Restart everything (extra arguments are passed to restart.ps1; /nopause skips the final pause)
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "NOPAUSE="
set "PSARGS="

:parse
if "%~1"=="" goto run
if /i "%~1"=="/nopause" (
    set "NOPAUSE=1"
) else (
    set "PSARGS=%PSARGS% %~1"
)
shift
goto parse

:run
echo [restart_rin] running restart.ps1 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1"%PSARGS%
set "RC=%ERRORLEVEL%"
echo [restart_rin] finished (exit code %RC%)
if not defined NOPAUSE pause
exit /b %RC%

@echo off
rem Start Rin (VOICEVOX, Rin, Mate Engine) (extra arguments are passed to run.ps1; /nopause skips the final pause)
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
echo [start_rin] running run.ps1 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"%PSARGS%
set "RC=%ERRORLEVEL%"
echo [start_rin] finished (exit code %RC%)
if not defined NOPAUSE pause
exit /b %RC%

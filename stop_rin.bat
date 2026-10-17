@echo off
rem Stop Mate Engine and Rin (add -StopVoicevox to stop VOICEVOX too) (extra arguments are passed to stop.ps1; /nopause skips the final pause)
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
echo [stop_rin] running stop.ps1 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"%PSARGS%
set "RC=%ERRORLEVEL%"
echo [stop_rin] finished (exit code %RC%)
if not defined NOPAUSE pause
exit /b %RC%

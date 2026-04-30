@echo off
setlocal

rem ============================================================
rem  register_backup_task.bat
rem  Registers a Windows Task Scheduler entry that runs
rem  backup_all_campaigns.bat every Sunday at 9:00 AM local time.
rem  Run once. Re-run safe (uses /F to overwrite existing).
rem  Requires Administrator if the task already exists under another user.
rem ============================================================

set "SCRIPT=%~dp0backup_all_campaigns.bat"
set "TASKNAME=Greyhawk Campaign Backup"

if not exist "%SCRIPT%" (
    echo ERROR: %SCRIPT% not found.
    exit /b 1
)

schtasks /create /tn "%TASKNAME%" /tr "\"%SCRIPT%\"" /sc weekly /d SUN /st 09:00 /f
if errorlevel 1 (
    echo.
    echo ERROR: schtasks /create failed. If you saw an access-denied error,
    echo re-run this script in an Administrator command prompt.
    exit /b 1
)

echo.
echo Registered "%TASKNAME%" -- runs every Sunday at 09:00 local time.
echo.
echo Verify:    schtasks /query /tn "%TASKNAME%"
echo Run now:   schtasks /run   /tn "%TASKNAME%"
echo Remove:    schtasks /delete /tn "%TASKNAME%" /f
echo.
endlocal
exit /b 0

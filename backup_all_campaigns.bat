@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  backup_all_campaigns.bat
rem  Copies every saves\*.db across all greyhawk-* sibling folders
rem  to <folder>\backups\<basename>_YYYY-MM-DD_HHMM.db.
rem  Keeps the 5 most recent backups per source DB; older are deleted.
rem  Read-only on source DBs (copy, never move).
rem ============================================================

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

set "PARENT=%SRC%\.."
for %%I in ("%PARENT%") do set "PARENT=%%~fI"

rem Locale-independent timestamp via PowerShell
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%T"

echo.
echo ============================================================
echo  Greyhawk Campaign Backup
echo ============================================================
echo  Scanning : %PARENT%\greyhawk-*
echo  Stamp    : %STAMP%
echo.

set "PASS=0"
set "FAIL=0"
set "SKIP=0"
set "FILES=0"

for /d %%D in ("%PARENT%\greyhawk-*") do (
    set "DNAME=%%~nxD"
    set "SAVES=%%D\saves"
    set "BACKUPS=%%D\backups"

    echo  [!DNAME!]

    if not exist "!SAVES!" (
        echo    SKIP no saves\ folder
        set /a SKIP+=1
    ) else (
        set "FOUND=0"
        for %%F in ("!SAVES!\*.db") do set /a FOUND+=1

        if !FOUND! EQU 0 (
            echo    SKIP no .db files in saves\
            set /a SKIP+=1
        ) else (
            if not exist "!BACKUPS!" mkdir "!BACKUPS!" >nul 2>&1

            set "FOLDER_FAIL=0"
            for %%F in ("!SAVES!\*.db") do (
                set "BASE=%%~nF"
                set "DST=!BACKUPS!\!BASE!_%STAMP%.db"
                copy /y "%%F" "!DST!" >nul 2>&1
                if errorlevel 1 (
                    echo    FAIL %%~nxF copy failed
                    set "FOLDER_FAIL=1"
                ) else (
                    echo    OK   %%~nxF -^> backups\!BASE!_%STAMP%.db
                    set /a FILES+=1
                )
            )

            rem Prune: keep 5 newest per source DB basename
            for %%F in ("!SAVES!\*.db") do (
                set "BASE=%%~nF"
                set "N=0"
                for /f "delims=" %%X in ('dir /b /o-d "!BACKUPS!\!BASE!_*.db" 2^>nul') do (
                    set /a N+=1
                    if !N! GTR 5 (
                        del /q "!BACKUPS!\%%X" >nul 2>&1
                        if not errorlevel 1 echo    PRUNE removed %%X
                    )
                )
            )

            if !FOLDER_FAIL! EQU 0 (
                set /a PASS+=1
            ) else (
                set /a FAIL+=1
            )
        )
    )
)

echo.
echo ============================================================
echo  Done.
echo    Successful : !PASS!
echo    Failed     : !FAIL!
echo    Skipped    : !SKIP!
echo    Files      : !FILES!
echo ============================================================
echo.
endlocal
exit /b 0

@echo off
REM ============================================================================
REM sync_clones.bat
REM ---------------------------------------------------------------------------
REM Mirror code from this greyhawk-solo directory into every sibling
REM greyhawk-* folder alongside it.
REM
REM Usage:
REM   sync_clones.bat              Sync every sibling (writes files).
REM   sync_clones.bat dry          Dry run: list what WOULD change.
REM   sync_clones.bat --dry-run    Same as above.
REM
REM What gets synced:   everything in the project tree
REM What is NEVER touched in the siblings:
REM   saves\ (character DBs), config.json, *.db / *.db-shm / *.db-wal,
REM   __pycache__, .venv, venv, env, .git, .idea, .vscode, .pytest_cache, *.pyc
REM
REM Extraneous files in the sibling are left alone (no mirror/delete mode).
REM Only the source folder that contains THIS script is used as the source;
REM it is skipped during the sibling scan.
REM ============================================================================

setlocal enabledelayedexpansion

set "LIST_FLAG="
if /i "%~1"=="dry"       set "LIST_FLAG=/L"
if /i "%~1"=="--dry-run" set "LIST_FLAG=/L"
if /i "%~1"=="-n"        set "LIST_FLAG=/L"

REM Source = directory containing this script.
set "SRC=%~dp0"
if "!SRC:~-1!"=="\" set "SRC=!SRC:~0,-1!"

REM Parent = where we scan for siblings.
for %%p in ("!SRC!\..") do set "PARENT=%%~fp"
if "!PARENT:~-1!"=="\" set "PARENT=!PARENT:~0,-1!"

REM Source folder basename (e.g. greyhawk-solo) -- skip during scan.
for %%n in ("!SRC!") do set "SRCNAME=%%~nxn"

echo.
echo  greyhawk-solo -- Sync Clones
echo  ----------------------------------------
if defined LIST_FLAG (
    echo  Mode   : DRY RUN ^(nothing will be written^)
) else (
    echo  Mode   : LIVE ^(files will be overwritten^)
)
echo  Source : !SRC!
echo  Scan   : !PARENT!\greyhawk-*  ^(excluding !SRCNAME!^)
echo.

REM In live mode, silence per-file output. In dry run, show it.
if defined LIST_FLAG (
    set "QUIET=/NJH /NJS /NP /NC /NS"
) else (
    set "QUIET=/NFL /NDL /NJH /NJS /NP /NC /NS"
)

set "COUNT=0"
for /d %%d in ("!PARENT!\greyhawk-*") do (
    if /i not "%%~nxd"=="!SRCNAME!" (
        set /a COUNT+=1
        echo ------------------------------------------------------------
        echo Sibling: %%~nxd
        echo ------------------------------------------------------------
        robocopy "!SRC!" "%%d" /E !LIST_FLAG! !QUIET! ^
          /XD saves __pycache__ .venv venv env .git .idea .vscode .pytest_cache ^
          /XF config.json *.db *.db-shm *.db-wal *.pyc
        set "RC=!errorlevel!"
        if !RC! GEQ 8 (
            echo   ERROR: robocopy failed with exit code !RC!
        ) else (
            if defined LIST_FLAG (
                echo   dry run complete ^(no changes written^)
            ) else (
                echo   sync complete
            )
        )
        echo.
    )
)

if !COUNT!==0 (
    echo No sibling greyhawk-* folders found under !PARENT!.
    echo Nothing to sync.
    endlocal
    exit /b 0
)

echo Done. !COUNT! sibling^(s^) processed.
endlocal

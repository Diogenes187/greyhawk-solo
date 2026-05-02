@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: sync_engine.bat
:: Syncs the greyhawk-solo engine to all greyhawk-* siblings.
:: Run from any directory — uses script location as source.
:: Never touches saves/, config.json, or .db files.
:: ============================================================

set "SRC=%~dp0"
:: Strip trailing backslash
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

set "PARENT=%SRC%\.."
:: Resolve to absolute path
for %%I in ("%PARENT%") do set "PARENT=%%~fI"

echo.
echo ============================================================
echo  greyhawk-solo Engine Sync
echo ============================================================
echo  Source : %SRC%
echo  Scanning: %PARENT%\greyhawk-*
echo.

:: ── Collect target folders ───────────────────────────────────
set COUNT=0
for /d %%D in ("%PARENT%\greyhawk-*") do (
    set "FNAME=%%~nxD"
    if /i not "%%~nxD"=="greyhawk-solo" (
        set /a COUNT+=1
        set "TARGET_!COUNT!=%%D"
        echo   [!COUNT!] %%~nxD
    )
)

if %COUNT%==0 (
    echo  No greyhawk-* character folders found in %PARENT%
    echo.
    pause
    exit /b 0
)

echo.
set /p CONFIRM=Sync engine to all these folders? (yes/no):
if /i not "%CONFIRM%"=="yes" (
    echo Aborted.
    exit /b 0
)

echo.
echo ── Starting sync ───────────────────────────────────────────
set PASS=0
set FAIL=0

for /l %%I in (1,1,%COUNT%) do (
    set "DST=!TARGET_%%I!"
    set "DNAME="
    for %%X in ("!DST!") do set "DNAME=%%~nxX"

    echo.
    echo  [%%I/%COUNT%] !DNAME!

    rem engine/ -- full folder sync, exclude .db files
    robocopy "%SRC%\engine" "!DST!\engine" /E /XF "*.db" /NP /NFL /NDL /NJH /NJS >nul 2>&1
    if errorlevel 8 (
        echo    ERROR: engine/ copy failed
        set /a FAIL+=1
    ) else (
        echo    OK  engine/

        rem server/mcp_server.py
        copy /y "%SRC%\server\mcp_server.py" "!DST!\server\mcp_server.py" >nul 2>&1
        if errorlevel 1 (echo    WARN: server/mcp_server.py copy failed) else (echo    OK  server/mcp_server.py)

        rem schema files
        copy /y "%SRC%\schema\ddl.sql"     "!DST!\schema\ddl.sql"     >nul 2>&1
        if errorlevel 1 (echo    WARN: schema/ddl.sql copy failed)     else (echo    OK  schema/ddl.sql)

        copy /y "%SRC%\schema\starter.sql" "!DST!\schema\starter.sql" >nul 2>&1
        if errorlevel 1 (echo    WARN: schema/starter.sql copy failed) else (echo    OK  schema/starter.sql)

        rem clone/sync helper scripts
        if exist "%SRC%\clone_for_new_character.bat" (
            copy /y "%SRC%\clone_for_new_character.bat" "!DST!\clone_for_new_character.bat" >nul 2>&1
            if errorlevel 1 (echo    WARN: clone_for_new_character.bat copy failed) else (echo    OK  clone_for_new_character.bat)
        )
        if exist "%SRC%\clone_for_new_character.sh" (
            copy /y "%SRC%\clone_for_new_character.sh" "!DST!\clone_for_new_character.sh" >nul 2>&1
            if errorlevel 1 (echo    WARN: clone_for_new_character.sh copy failed) else (echo    OK  clone_for_new_character.sh)
        )

        rem sync scripts themselves
        copy /y "%SRC%\sync_engine.bat" "!DST!\sync_engine.bat" >nul 2>&1
        if errorlevel 1 (echo    WARN: sync_engine.bat copy failed) else (echo    OK  sync_engine.bat)

        copy /y "%SRC%\sync_engine.sh"  "!DST!\sync_engine.sh"  >nul 2>&1
        if errorlevel 1 (echo    WARN: sync_engine.sh copy failed)  else (echo    OK  sync_engine.sh)

        set /a PASS+=1
    )
)

echo.
echo ============================================================
echo  Done.  %PASS% folder(s) synced successfully, %FAIL% failed.
echo.
echo  Restart Claude Desktop to apply changes to all characters.
echo ============================================================
echo.
pause
endlocal

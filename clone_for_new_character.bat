@echo off
REM ============================================================================
REM clone_for_new_character.bat
REM ---------------------------------------------------------------------------
REM Clone this greyhawk-solo project into a sibling folder for a new character.
REM
REM   1. Prompts for a character name
REM   2. Creates ..\greyhawk-<slug>
REM   3. Copies every project file EXCEPT: saves\ contents, config.json,
REM      *.db / *.db-shm / *.db-wal, caches and build artifacts, .git
REM   4. Creates an empty saves\ folder in the clone
REM   5. Ensures schema\starter.sql and schema\ddl.sql are present in the clone
REM   6. Prints the exact Claude Desktop MCP config snippet to paste in
REM ============================================================================

setlocal enabledelayedexpansion

echo.
echo  greyhawk-solo -- New Character Clone
echo  ----------------------------------------
echo.

set /p "CHARNAME=Enter character name (e.g. Mei Lin, Elric): "
if "!CHARNAME!"=="" (
    echo ERROR: Character name cannot be empty.
    endlocal
    exit /b 1
)

REM Normalise via PowerShell: lowercase, spaces to underscores, strip junk.
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "('%CHARNAME%' -replace '\s+','_').ToLower() -replace '[^a-z0-9_-]',''"`) do set "SLUG=%%s"
if "!SLUG!"=="" (
    echo ERROR: Name normalised to empty slug. Try a plainer name.
    endlocal
    exit /b 1
)

REM Source = directory containing this script (strip trailing backslash).
set "SRC=%~dp0"
if "!SRC:~-1!"=="\" set "SRC=!SRC:~0,-1!"

REM Parent directory, resolved to absolute path.
for %%p in ("!SRC!\..") do set "PARENT=%%~fp"
if "!PARENT:~-1!"=="\" set "PARENT=!PARENT:~0,-1!"

set "DEST=!PARENT!\greyhawk-!SLUG!"

if exist "!DEST!" (
    echo.
    echo ERROR: Destination already exists:
    echo   !DEST!
    echo Pick a different character name or remove that folder first.
    endlocal
    exit /b 1
)

echo.
echo Slug        : greyhawk-!SLUG!
echo Source      : !SRC!
echo Destination : !DEST!
echo.
echo Copying project files (excluding saves, DBs, config.json, caches)...

REM robocopy returns 0-7 for success, 8+ for error.
robocopy "!SRC!" "!DEST!" /E /NFL /NDL /NJH /NJS /NP /NC /NS ^
  /XD saves __pycache__ .venv venv env .git .idea .vscode .pytest_cache ^
  /XF config.json *.db *.db-shm *.db-wal *.pyc > nul
set "RC=!errorlevel!"
if !RC! GEQ 8 (
    echo ERROR: robocopy failed with exit code !RC!.
    endlocal
    exit /b 1
)

REM Empty saves\ folder, with .gitkeep if the source had one.
if not exist "!DEST!\saves" mkdir "!DEST!\saves"
if exist "!SRC!\saves\.gitkeep" copy /Y "!SRC!\saves\.gitkeep" "!DEST!\saves\.gitkeep" > nul

REM Safety net: re-copy schema files even if excludes ever grow to cover them.
if not exist "!DEST!\schema" mkdir "!DEST!\schema"
if exist "!SRC!\schema\starter.sql" copy /Y "!SRC!\schema\starter.sql" "!DEST!\schema\starter.sql" > nul
if exist "!SRC!\schema\ddl.sql"     copy /Y "!SRC!\schema\ddl.sql"     "!DEST!\schema\ddl.sql"     > nul

REM Pre-compute a JSON-safe path (every \ doubled).
set "JSONPATH=!DEST:\=\\!"

echo.
echo ============================================================
echo  Clone complete.
echo ============================================================
echo.
echo Next steps:
echo.
echo 1. Create the character in the new folder:
echo.
echo      cd /d "!DEST!"
echo      python create_character.py
echo.
echo 2. Add this entry to your Claude Desktop config file:
echo.
echo      %%APPDATA%%\Claude\claude_desktop_config.json
echo.
echo    Merge it into the existing "mcpServers" block - do NOT
echo    overwrite other servers.
echo.
echo    ------------------------------------------------------------
echo    {
echo      "mcpServers": {
echo        "greyhawk-!SLUG!": {
echo          "command": "python",
echo          "args": ["!JSONPATH!\\server\\mcp_server.py"]
echo        }
echo      }
echo    }
echo    ------------------------------------------------------------
echo.
echo 3. Fully quit Claude Desktop (system tray icon - Quit) and relaunch.
echo.

endlocal

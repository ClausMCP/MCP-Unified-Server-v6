@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
:: ===============================================
:: MCP Portable Setup – полная установка в C:\Tools
:: ===============================================
setlocal enabledelayedexpansion

set "TOOLS_DIR=%~dp0tools"
set "INSTALLERS_DIR=%TOOLS_DIR%\installers"
set "PYTHON_DEPS_DIR=%~dp0python_deps"
set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PID_FILE=%VENV_DIR%\server.pid"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK_NAME=MCP_Server.lnk"
set "SCRIPT_PATH=%~dp0mcp_fs_server.py"
set "WORK_DIR=%~dp0"
set "PS_TLS=[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;"

:: Инъекция портативных инструментов в PATH
if exist "%TOOLS_DIR%\python\python.exe" (
    set "PATH=%TOOLS_DIR%\python;%TOOLS_DIR%\python\Scripts;%TOOLS_DIR%\tesseract;%TOOLS_DIR%\ffmpeg;%TOOLS_DIR%\pandoc;%TOOLS_DIR%\wkhtmltopdf;%TOOLS_DIR%\wkhtmltopdf\bin;%PATH%"
)

:: Проверка Python (системный или портативный)
set "BASE_PYTHON="
if exist "%TOOLS_DIR%\python\python.exe" (
    set "BASE_PYTHON=%TOOLS_DIR%\python\python.exe"
)
if not defined BASE_PYTHON (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=py -3"
)
if not defined BASE_PYTHON (
    python --version >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=python"
)

if not defined BASE_PYTHON (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org ^(check "Add to PATH"^),
    echo         or use option 3 to download the portable bundle, then option 4 to install it.
    pause >nul
    exit /b 1
)

echo [OK] Base Python: !BASE_PYTHON!
echo.

:: Проверка статуса сервера
set "SERVER_RUNNING=0"
set "PID="
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    if defined PID (
        tasklist /fi "PID eq !PID!" 2>nul | find /i "!PID!" >nul
        if not errorlevel 1 set "SERVER_RUNNING=1"
    )
)

:menu
cls
echo ================================================
echo        MCP Portable Server – Management
echo ================================================
echo.

if "!SERVER_RUNNING!"=="1" (
    echo [SERVER] Running (PID: !PID!^)
) else (
    echo [SERVER] Stopped
)
if exist "%PYTHON_EXE%" (
    echo [VENV]   Ready
) else (
    echo [VENV]   Not created  ^(run option 1 first^)
)
echo.

echo --- Environment Setup ---
echo  1. Full automatic setup (download tools + install deps)
echo  2. Recreate virtual environment (clean^)
echo  3. Download Python + external tools (online^)
echo  4. Install Python + tools from local bundle (offline^)
echo.
echo --- Dependencies ---
echo  5. Download all Python packages (online, to python_deps^)
echo  6. Install packages from python_deps (offline^)
echo  7. Check and install missing dependencies (smart^)
echo.
echo --- RAG + Cognitive Modules ---
echo  8. Install RAG + full-text indexing dependencies
echo  9. Install all cognitive plugins (hypothesis, world model, episodic memory^)
echo.
echo --- Configuration ---
echo  A. Fix paths in mcpServers.json (manual^)
echo  B. Auto-generate LM Studio config (portable paths^)
echo  C. Create .env file
echo.
echo --- Server Control ---
echo  D. Start / Stop server
echo  E. Add to startup
echo  F. Remove from startup
echo.
echo --- Diagnostics ---
echo  G. Run diagnostics (deps + system check^)
echo.
echo  H. Exit
echo.

choice /C 123456789ABCDEFGH /N /M "Choose action: "
set "CHOICE=%errorlevel%"

if "%CHOICE%"=="1" goto full_auto
if "%CHOICE%"=="2" goto clean_venv
if "%CHOICE%"=="3" goto download_tools
if "%CHOICE%"=="4" goto install_tools
if "%CHOICE%"=="5" goto online_download
if "%CHOICE%"=="6" goto offline_install
if "%CHOICE%"=="7" goto check_and_install
if "%CHOICE%"=="8" goto install_rag_full
if "%CHOICE%"=="9" goto install_cognitive
if "%CHOICE%"=="10" goto fix_config_paths
if "%CHOICE%"=="11" goto auto_fix_lmstudio
if "%CHOICE%"=="12" goto setup_env_file
if "%CHOICE%"=="13" goto toggle_server
if "%CHOICE%"=="14" goto add_autostart
if "%CHOICE%"=="15" goto remove_autostart
if "%CHOICE%"=="16" goto run_diagnostics
if "%CHOICE%"=="17" goto exit

goto menu

:: ========== G. DIAGNOSTICS ==========
:run_diagnostics
call :ensure_venv_for_setup
echo.
echo Running full diagnostics (deps, modules, databases, connectivity)...
echo.
"%PYTHON_EXE%" mcp_diagnostics.py
if errorlevel 1 (
    echo.
    echo [!] Critical problems found. Fix FAIL items above.
    echo     Missing deps can be installed via menu option 7.
)
pause
goto menu

:: ========== 1. FULL AUTO ==========
:full_auto
echo.
echo ================================================
echo   Full automatic setup (may take 10-20 minutes)
echo ================================================
call :download_tools_internal
call :install_tools_internal
call :clean_venv_silent
call "%PYTHON_EXE%" mcp_setup.py --online
call "%PYTHON_EXE%" mcp_setup.py --offline
call :install_rag_full_internal
call :install_cognitive_internal
call :auto_fix_lmstudio_internal
echo ✅ Full setup complete!
pause
goto menu

:: ========== 2. CLEAN VENV ==========
:clean_venv
echo.
call :clean_venv_silent
pause
goto menu

:clean_venv_silent
:: Перепроверяем базовый Python на случай вызова без прохождения установки.
:: Если BASE_PYTHON указывает на несуществующий файл — сбрасываем.
if defined BASE_PYTHON if not "!BASE_PYTHON!"=="python" if not "!BASE_PYTHON!"=="py -3" if not exist "!BASE_PYTHON!" set "BASE_PYTHON="
if not defined BASE_PYTHON (
    if exist "%TOOLS_DIR%\python\python.exe" (
        set "BASE_PYTHON=%TOOLS_DIR%\python\python.exe"
    )
)
if not defined BASE_PYTHON (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=py -3"
)
if not defined BASE_PYTHON (
    python --version >nul 2>&1
    if not errorlevel 1 set "BASE_PYTHON=python"
)
if not defined BASE_PYTHON (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org, or run options 3 then 4.
    exit /b 1
)
if exist "%VENV_DIR%" (
    echo Removing old .venv...
    rmdir /s /q "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not remove old venv. Close any running server, then retry.
        exit /b 1
    )
)
echo Creating new venv from !BASE_PYTHON!...
"!BASE_PYTHON!" -m venv "%VENV_DIR%" 2>nul
if not exist "%PYTHON_EXE%" (
    echo Portable/base Python failed, trying 'py -3'...
    py -3 -m venv "%VENV_DIR%" 2>nul
)
if not exist "%PYTHON_EXE%" (
    echo Trying 'python'...
    python -m venv "%VENV_DIR%" 2>nul
)
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Could not create venv. Install Python 3.10+ from python.org
    echo         ^(check "Add Python to PATH"^), then retry. Or run options 3 then 4.
    exit /b 1
)
"%PYTHON_EXE%" -m ensurepip --upgrade >nul 2>&1
"%PYTHON_EXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not available in the venv.
    exit /b 1
)
echo [OK] Virtual environment ready.
exit /b 0

:: ========== 3. DOWNLOAD TOOLS ==========
:download_tools
call :download_tools_internal
pause
goto menu

:download_tools_internal
echo Downloading external tools...
if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
if not exist "%INSTALLERS_DIR%" mkdir "%INSTALLERS_DIR%"

echo Downloading Python 3.10.11...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe' -OutFile '%INSTALLERS_DIR%\python-installer.exe'"
if exist "%INSTALLERS_DIR%\python-installer.exe" (echo   [OK] Python 3.10.11 downloaded) else (echo   [FAIL] Python 3.10.11 download FAILED - check URL/internet)

echo Downloading Tesseract...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe' -OutFile '%INSTALLERS_DIR%\tesseract-installer.exe'"
if exist "%INSTALLERS_DIR%\tesseract-installer.exe" (echo   [OK] Tesseract downloaded) else (echo   [FAIL] Tesseract download FAILED - check URL/internet)

echo Downloading ffmpeg...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%INSTALLERS_DIR%\ffmpeg.zip'"
if exist "%INSTALLERS_DIR%\ffmpeg.zip" (echo   [OK] ffmpeg downloaded) else (echo   [FAIL] ffmpeg download FAILED - check URL/internet)

echo Downloading pandoc...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/jgm/pandoc/releases/download/3.1.11/pandoc-3.1.11-windows-x86_64.zip' -OutFile '%INSTALLERS_DIR%\pandoc.zip'"
if exist "%INSTALLERS_DIR%\pandoc.zip" (echo   [OK] pandoc downloaded) else (echo   [FAIL] pandoc download FAILED - check URL/internet)

echo Downloading wkhtmltopdf...
powershell -NoProfile -Command "%PS_TLS% Invoke-WebRequest -Uri 'https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6-1/wkhtmltox-0.12.6-1.msvc2015-win64.exe' -OutFile '%INSTALLERS_DIR%\wkhtmltopdf-installer.exe'"
if exist "%INSTALLERS_DIR%\wkhtmltopdf-installer.exe" (echo   [OK] wkhtmltopdf downloaded) else (echo   [FAIL] wkhtmltopdf download FAILED - check URL/internet)

echo [OK] Tools downloaded to %INSTALLERS_DIR%
exit /b 0

:: ========== 4. INSTALL TOOLS ==========
:install_tools
call :install_tools_internal
pause
goto menu

:install_tools_internal
if not exist "%INSTALLERS_DIR%\python-installer.exe" (
    echo [ERROR] Tools not downloaded. Run option 3 first.
    exit /b 1
)
echo Installing Python locally...
start /wait "" "%INSTALLERS_DIR%\python-installer.exe" /quiet InstallAllUsers=0 TargetDir="%TOOLS_DIR%\python" PrependPath=0 Include_test=0 Include_launcher=0 Include_tcltk=0

echo Extracting ffmpeg...
if exist "%INSTALLERS_DIR%\ffmpeg.zip" (
    if not exist "%TOOLS_DIR%\ffmpeg-temp" mkdir "%TOOLS_DIR%\ffmpeg-temp"
    powershell -NoProfile -Command "Expand-Archive -Path '%INSTALLERS_DIR%\ffmpeg.zip' -DestinationPath '%TOOLS_DIR%\ffmpeg-temp' -Force"
    if not exist "%TOOLS_DIR%\ffmpeg" mkdir "%TOOLS_DIR%\ffmpeg"
    for /d %%i in ("%TOOLS_DIR%\ffmpeg-temp\ffmpeg-*") do (
        xcopy /s /e /y "%%i\bin\*" "%TOOLS_DIR%\ffmpeg\" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\ffmpeg-temp"
)

echo Extracting pandoc...
if exist "%INSTALLERS_DIR%\pandoc.zip" (
    if not exist "%TOOLS_DIR%\pandoc-temp" mkdir "%TOOLS_DIR%\pandoc-temp"
    powershell -NoProfile -Command "Expand-Archive -Path '%INSTALLERS_DIR%\pandoc.zip' -DestinationPath '%TOOLS_DIR%\pandoc-temp' -Force"
    if not exist "%TOOLS_DIR%\pandoc" mkdir "%TOOLS_DIR%\pandoc"
    for /d %%i in ("%TOOLS_DIR%\pandoc-temp\pandoc-*") do (
        xcopy /s /e /y "%%i\*" "%TOOLS_DIR%\pandoc\" >nul
    )
    rmdir /s /q "%TOOLS_DIR%\pandoc-temp"
)

echo Installing Tesseract...
if exist "%INSTALLERS_DIR%\tesseract-installer.exe" (
    rem NSIS: параметр /D= должен быть ПОСЛЕДНИМ и БЕЗ кавычек
    start /wait "" "%INSTALLERS_DIR%\tesseract-installer.exe" /S /D=%TOOLS_DIR%\tesseract
)
if exist "%TOOLS_DIR%\tesseract\tesseract.exe" (
    echo   [OK] Tesseract installed
) else (
    echo   [FAIL] Tesseract NOT installed - run installer manually: %INSTALLERS_DIR%\tesseract-installer.exe
)

echo Installing wkhtmltopdf...
if exist "%INSTALLERS_DIR%\wkhtmltopdf-installer.exe" (
    rem NSIS: параметр /D= должен быть ПОСЛЕДНИМ и БЕЗ кавычек
    start /wait "" "%INSTALLERS_DIR%\wkhtmltopdf-installer.exe" /S /D=%TOOLS_DIR%\wkhtmltopdf
)
if exist "%TOOLS_DIR%\wkhtmltopdf\bin\wkhtmltopdf.exe" (
    echo   [OK] wkhtmltopdf installed
) else (
    echo   [FAIL] wkhtmltopdf NOT installed - run installer manually: %INSTALLERS_DIR%\wkhtmltopdf-installer.exe
)

echo Installing Python (portable into tools\python)...
if not exist "%TOOLS_DIR%\python\python.exe" (
    if exist "%INSTALLERS_DIR%\python-installer.exe" (
        start /wait "" "%INSTALLERS_DIR%\python-installer.exe" /quiet InstallAllUsers=0 TargetDir="%TOOLS_DIR%\python" Include_launcher=0 PrependPath=0 Include_test=0 Shortcuts=0 AssociateFiles=0
    ) else (
        echo [WARN] python-installer.exe not found. Run option 3 first, or install Python from python.org.
    )
)

:: Обновляем PATH для текущей сессии
if exist "%TOOLS_DIR%\python\python.exe" set "BASE_PYTHON=%TOOLS_DIR%\python\python.exe"
echo Done. Check [OK]/[FAIL] lines above for each tool.
exit /b 0

:: ========== 5. ONLINE DOWNLOAD DEPS ==========
:online_download
call :ensure_venv_for_setup
"%PYTHON_EXE%" mcp_setup.py --online
pause
goto menu

:: ========== 6. OFFLINE INSTALL DEPS ==========
:offline_install
call :ensure_venv_for_setup
"%PYTHON_EXE%" mcp_setup.py --offline
pause
goto menu

:: ========== 7. CHECK & INSTALL MISSING ==========
:check_and_install
call :ensure_venv_for_setup
"%PYTHON_EXE%" mcp_setup.py --check
pause
goto menu

:: ========== 8. INSTALL RAG ==========
:install_rag_full
call :ensure_venv_for_setup
echo Installing RAG dependencies...
"%PYTHON_EXE%" -m pip install chromadb sentence-transformers tiktoken sqlalchemy apscheduler schedule PyPDF2 pdfplumber ebooklib pandas numpy scikit-learn
pause
goto menu

:install_rag_full_internal
"%PYTHON_EXE%" -m pip install chromadb sentence-transformers tiktoken sqlalchemy apscheduler schedule PyPDF2 pdfplumber ebooklib pandas numpy scikit-learn >nul 2>&1
exit /b 0

:: ========== 9. INSTALL COGNITIVE PLUGINS ==========
:install_cognitive
call :ensure_venv_for_setup
echo Installing cognitive plugin dependencies...
"%PYTHON_EXE%" -m pip install mcp networkx pydantic
echo Cognitive modules ready.
pause
goto menu

:install_cognitive_internal
"%PYTHON_EXE%" -m pip install mcp networkx pydantic >nul 2>&1
exit /b 0

:: ========== A. FIX CONFIG ==========
:fix_config_paths
set "CONFIG_PATH="
set /p CONFIG_PATH="Path to JSON file (Enter for C:\Tools\mcpServers.json): "
if "!CONFIG_PATH!"=="" set "CONFIG_PATH=C:\Tools\mcpServers.json"
call :fix_one_config "!CONFIG_PATH!"
pause
goto menu

:fix_one_config
set "TARGET_CFG=%~1"
if not exist "%TARGET_CFG%" (
    echo [X] File not found: %TARGET_CFG%
    exit /b 1
)
"%PYTHON_EXE%" mcp_setup.py --fix-config "%TARGET_CFG%" "%PYTHON_EXE%"
exit /b %errorlevel%

:: ========== B. AUTO LM STUDIO CONFIG ==========
:auto_fix_lmstudio
call :auto_fix_lmstudio_internal
pause
goto menu

:auto_fix_lmstudio_internal
set "PYTHON_PATH=%PYTHON_EXE%"
set "SERVER_SCRIPT=%SCRIPT_PATH%"
set "LMSTUDIO_DIR=%USERPROFILE%\.lmstudio"
if not exist "%LMSTUDIO_DIR%" mkdir "%LMSTUDIO_DIR%"
set "CONFIG_FILE=%LMSTUDIO_DIR%\mcp.json"

if exist "%CONFIG_FILE%" (
    for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
    set "YY=!dt:~2,2!" & set "YYYY=!dt:~0,4!" & set "MM=!dt:~4,2!" & set "DD=!dt:~6,2!"
    set "HH=!dt:~8,2!" & set "Min=!dt:~10,2!" & set "Sec=!dt:~12,2!"
    set "timestamp=!YYYY!!MM!!DD!_!HH!!Min!!Sec!"
    set "BACKUP=!CONFIG_FILE!.backup_!timestamp!"
    copy "!CONFIG_FILE!" "!BACKUP!" >nul
)

set "COMMAND=%PYTHON_PATH:\=\\%"
set "ARGS=%SERVER_SCRIPT:\=\\%"
set "MEMORY_PATH=%~dp0mcp_memory.db"
set "MEMORY_PATH=!MEMORY_PATH:\=\\!"

(
echo {
echo   "mcpServers": {
echo     "mcp_unified": {
echo       "command": "!COMMAND!",
echo       "args": ["!ARGS!"],
echo       "env": {
echo         "PYTHONIOENCODING": "utf-8",
echo         "MCP_MEMORY_PATH": "!MEMORY_PATH!",
echo         "MCP_OFFLINE_MODE": "auto",
echo         "MCP_AUTO_INDEX_SEARCH": "true"
echo       }
echo     }
echo   }
echo }
) > "%CONFIG_FILE%"

echo [OK] LM Studio config created: %CONFIG_FILE%
exit /b 0

:: ========== C. CREATE .env ==========
:setup_env_file
set "ENV_FILE=%~dp0.env"
(
echo # MCP Environment
echo MCP_OFFLINE_MODE=auto
echo MCP_AUTO_INDEX_FOLDERS=
echo MCP_WEB_CACHE_TTL=168
echo MCP_INDEX_INTERVAL_HOURS=6
echo MCP_AUTO_INDEX_SEARCH=true
echo MCP_EPISODIC_EMBEDDING=true
echo MCP_EPISODIC_RETENTION_DAYS=90
) > "%ENV_FILE%"
echo .env file created.
pause
goto menu

:: ========== D. START/STOP SERVER ==========
:toggle_server
if "!SERVER_RUNNING!"=="1" goto :server_stop
goto :server_start

:server_stop
if defined PID taskkill /pid !PID! /f >nul 2>&1
del "%PID_FILE%" 2>nul
set "SERVER_RUNNING=0"
echo Server stopped.
pause
goto menu

:server_start
:: Защита: без venv запускать нечего — создаём при отсутствии.
if not exist "%PYTHON_EXE%" (
    echo [WARN] Virtual environment not found at %VENV_DIR%.
    echo Creating it now...
    call :clean_venv_silent
)
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Cannot start: virtual environment is missing.
    echo Run option 1 ^(Full automatic setup^) first.
    pause
    goto menu
)
if not exist "%SCRIPT_PATH%" (
    echo [ERROR] Server script not found: %SCRIPT_PATH%
    pause
    goto menu
)
del "%PID_FILE%" 2>nul
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList ('\"%SCRIPT_PATH%\"') -WorkingDirectory '%WORK_DIR%' -WindowStyle Hidden -PassThru; $p.Id | Out-File -FilePath '%PID_FILE%' -Encoding ASCII"
timeout /t 2 >nul
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    set "SERVER_RUNNING=1"
    echo Server started with PID !PID!.
) else (
    echo Failed to start server.
    echo Check that dependencies are installed ^(option 7^) and run option G for diagnostics.
)
pause
goto menu

:: ========== E. ADD TO STARTUP ==========
:add_autostart
set "HIDDEN_LAUNCHER=%~dp0start_hidden.ps1"
(
echo # Auto-generated by setup.bat
echo $env:Path = '%TOOLS_DIR%\python;%TOOLS_DIR%\tesseract;%TOOLS_DIR%\ffmpeg;%TOOLS_DIR%\pandoc;%TOOLS_DIR%\wkhtmltopdf;' + $env:Path
echo $exe = '%PYTHON_EXE%'
echo $script = '%SCRIPT_PATH%'
echo $workdir = '%WORK_DIR%'
echo $pidFile = '%PID_FILE%'
echo $p = Start-Process -FilePath $exe -ArgumentList ('"' + $script + '"'^) -WorkingDirectory $workdir -WindowStyle Hidden -PassThru
echo $p.Id ^| Out-File -FilePath $pidFile -Encoding ASCII
) > "%HIDDEN_LAUNCHER%"
powershell -NoProfile -Command "$s = (New-Object -COM WScript.Shell).CreateShortcut('%STARTUP_DIR%\%LINK_NAME%'); $s.TargetPath = 'powershell.exe'; $s.Arguments = '-ExecutionPolicy Bypass -WindowStyle Hidden -File \"%HIDDEN_LAUNCHER%\"'; $s.WorkingDirectory = '%~dp0'; $s.Save()"
echo Added to startup.
pause
goto menu

:: ========== F. REMOVE FROM STARTUP ==========
:remove_autostart
if exist "%STARTUP_DIR%\%LINK_NAME%" del "%STARTUP_DIR%\%LINK_NAME%"
if exist "%~dp0start_hidden.ps1" del "%~dp0start_hidden.ps1"
echo Removed from startup.
pause
goto menu

:: ========== HELPER ==========
:ensure_venv_for_setup
if not exist "%VENV_DIR%" call :clean_venv_silent
if not exist "%PYTHON_EXE%" (
    echo Virtual environment missing. Creating...
    call :clean_venv_silent
)
exit /b 0

:exit
endlocal
exit /b 0
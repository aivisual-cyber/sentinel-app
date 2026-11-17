@echo off
rem Sentinel command-line tools. Uses Sentinel's OWN bundled Python (with ONNX Runtime/DirectML) -
rem never the Python on PATH, which lacks the packages. Works from the install folder (on PATH after
rem install) and from the unpacked USB bundle.
rem   sentinel gpu          which GPU/engine detection uses, and its speed
rem   sentinel parity       accuracy check: every engine vs the original PyTorch model
rem   sentinel benchmark    how many cameras this PC can analyse   (options: --images DIR^|VIDEO ...)
rem   sentinel health       engine status (healthy / degraded / critical)
rem   sentinel watchdog     one watchdog check (the SentinelWatchdog service runs it continuously)
rem   sentinel logs         open the log folder
setlocal
set "HERE=%~dp0"
set "PY=%HERE%python\python.exe"
set "APP=%HERE%app"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8:replace
set PYTHONNOUSERSITE=1
if not exist "%PY%" (
  echo Sentinel's Python was not found at "%PY%". Run this from the install or bundle folder.
  exit /b 2
)
if "%SENTINEL_PORT%"=="" set SENTINEL_PORT=8765
set "CMD=%~1"
if "%CMD%"=="" goto :help
shift
set "ARGS="
:collect
if "%~1"=="" goto :run
set ARGS=%ARGS% %1
shift
goto :collect
:run
if /i "%CMD%"=="gpu"       ( "%PY%" -s "%APP%\scripts\gpu_check.py" %ARGS% & exit /b %ERRORLEVEL% )
if /i "%CMD%"=="parity"    ( "%PY%" -s "%APP%\scripts\parity_check.py" %ARGS% & exit /b %ERRORLEVEL% )
if /i "%CMD%"=="benchmark" ( "%PY%" -s "%APP%\scripts\benchmark.py" %ARGS% & exit /b %ERRORLEVEL% )
if /i "%CMD%"=="watchdog"  ( "%PY%" -s "%APP%\scripts\watchdog.py" --once --url http://127.0.0.1:%SENTINEL_PORT%/api/health %ARGS% & exit /b %ERRORLEVEL% )
if /i "%CMD%"=="health"    ( "%PY%" -s -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:%SENTINEL_PORT%/api/health',timeout=5));print(d.get('status'),'-',(d.get('inference') or {}).get('device'));[print(' -',i) for i in d.get('issues') or []]" & exit /b %ERRORLEVEL% )
if /i "%CMD%"=="logs"      ( if defined SENTINEL_HOME (start "" "%SENTINEL_HOME%\logs") else (echo SENTINEL_HOME is not set - open your data folder\logs) & exit /b 0 )
:help
echo Sentinel tools ^(bundled Python: %PY%^)
echo   sentinel gpu          which GPU/engine detection uses, and its speed
echo   sentinel parity       accuracy check: every engine vs the original PyTorch model
echo   sentinel benchmark    cameras this PC can analyse  ^(--images DIR or VIDEO, --cameras 1 4 8^)
echo   sentinel health       engine status
echo   sentinel watchdog     one watchdog check
echo   sentinel logs         open the log folder
exit /b 1

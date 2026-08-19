@echo off
setlocal
rem ---------------------------------------------------------------
rem  Launch a debugging Chrome for the NGII downloader.
rem  Usage:  start_chrome_debug.bat [port]      (default 9222)
rem  Example: start_chrome_debug.bat 9333
rem ---------------------------------------------------------------

set "PORT=%~1"
if "%PORT%"=="" set "PORT=9222"
set "PROFILE=%TEMP%\ngii_chrome_%PORT%"

rem If the port is already taken, Chrome cannot open the debugging port:
rem the window appears but the downloader can never connect.
netstat -ano | findstr /C:"LISTENING" | findstr /C:":%PORT% " >nul
if not errorlevel 1 (
  echo [!] Port %PORT% is already in use. Pick another one, e.g.:
  echo     start_chrome_debug.bat 9333
  pause
  exit /b 1
)

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [!] chrome.exe not found. Edit the CHROME variable in this file.
  pause
  exit /b 1
)

rem A dedicated profile is required: with the normal profile an already
rem running Chrome just opens a new window and ignores the debugging port.
rem That is also why this script no longer kills chrome.exe.
start "" "%CHROME%" --remote-debugging-port=%PORT% --remote-debugging-address=127.0.0.1 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-popup-blocking https://map.ngii.go.kr/ms/map/NlipMap.do

echo Chrome started with CDP on http://localhost:%PORT%
echo Put the same address in the downloader and press [connect].
pause

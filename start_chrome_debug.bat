@echo off
taskkill /F /IM chrome.exe /T >/dev/null 2>&1
timeout /t 2 >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome_debug"
echo Chrome started. Please login and go to the map page.
pause

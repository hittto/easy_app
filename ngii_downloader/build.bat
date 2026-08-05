@echo off
chcp 65001 > nul
echo ============================================================
echo  NGII Downloader - EXE Build
echo ============================================================
echo.

if exist "dist\ngii_downloader" (
    echo [1/3] Removing previous build...
    rmdir /s /q "dist\ngii_downloader"
)
if exist "build\ngii_downloader" (
    rmdir /s /q "build\ngii_downloader"
)

echo [2/3] Running PyInstaller...
pyinstaller ngii_downloader.spec

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Build failed. Check the messages above.
    pause
    exit /b 1
)

echo.
echo [3/3] Build complete!
echo.
echo Output: dist\ngii_downloader\ngii_downloader.exe
echo.
echo * Distribute the entire dist\ngii_downloader\ folder.
echo   Do NOT copy only the .exe file.
echo.
pause

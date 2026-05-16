@echo off
REM Build a small patch ZIP containing only the app code (no venv, no Python).
REM Output: ..\releases\Image-Animator-patch-v<version>.zip (~100 KB)
REM This is what the in-app updater downloads from the GitHub release.

cd /d "%~dp0"

REM Read version from version.json
for /f "tokens=2 delims=:," %%a in ('findstr /c:"\"version\"" version.json') do (
    set VER=%%a
)
set VER=%VER: =%
set VER=%VER:"=%

set RELEASE_DIR=..\releases
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"

set PATCH_NAME=Image-Animator-patch-v%VER%.zip
set PATCH_PATH=%RELEASE_DIR%\%PATCH_NAME%

if exist "%PATCH_PATH%" del "%PATCH_PATH%"

powershell -NoProfile -Command "Compress-Archive -Path 'server.py','launcher.py','version.json','public' -DestinationPath '%PATCH_PATH%' -CompressionLevel Optimal -Force"

echo.
echo Patch built: %PATCH_PATH%
echo Upload this file as a GitHub release asset for v%VER%.
echo The in-app updater will download it automatically when team members click "Install update".

@echo off
REM Build a portable release ZIP of Image Animator using PyInstaller.

cd /d "%~dp0"

REM Make sure pyinstaller is in the venv
uv pip install pyinstaller

REM Clean previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build
uv run pyinstaller image-animator.spec --clean --noconfirm

if not exist "dist\Image Animator" (
    echo Build failed.
    exit /b 1
)

REM Read build number from version.json
for /f "tokens=2 delims=:," %%a in ('findstr /c:"\"build\"" version.json') do (
    set BUILD=%%a
)
set BUILD=%BUILD: =%

REM Zip up the dist folder
set RELEASE_NAME=Image-Animator-build-%BUILD%
set RELEASE_DIR=..\releases
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"

cd dist
powershell -Command "Compress-Archive -Path 'Image Animator\*' -DestinationPath '..\%RELEASE_DIR%\%RELEASE_NAME%.zip' -Force"
cd ..

echo.
echo Build complete: %RELEASE_DIR%\%RELEASE_NAME%.zip

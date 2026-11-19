@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ZIP=whisper.cpp-data.zip"
set "DATA=data"

if not exist "%DATA%\" (
    echo FAILED - data directory not found
    exit /b 1
)
if exist "%ZIP%" (
    echo Data archive already exists: %ZIP%
    exit /b 0
)

set "SEVENZ="
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles%\7-Zip\7z.exe"
if not defined SEVENZ if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles(x86)%\7-Zip\7z.exe"
if not defined SEVENZ where 7z.exe >nul 2>nul
if not defined SEVENZ if not errorlevel 1 set "SEVENZ=7z.exe"
if not defined SEVENZ where 7za.exe >nul 2>nul
if not defined SEVENZ if not errorlevel 1 set "SEVENZ=7za.exe"

if not defined SEVENZ (
    echo 7-Zip not found - downloading portable 7za.exe ...
    curl -L --fail -o "%TEMP%\7zr.exe" https://www.7-zip.org/a/7zr.exe >nul 2>&1
    if errorlevel 1 goto ZIP_NO7Z
    curl -L --fail -o "%TEMP%\7z-extra.7z" https://www.7-zip.org/a/7z2501-extra.7z >nul 2>&1
    if errorlevel 1 goto ZIP_NO7Z
    "%TEMP%\7zr.exe" x "%TEMP%\7z-extra.7z" 7za.exe -o"%TEMP%" -y >nul 2>&1
    del "%TEMP%\7zr.exe" >nul 2>&1
    del "%TEMP%\7z-extra.7z" >nul 2>&1
    if exist "%TEMP%\7za.exe" set "SEVENZ=%TEMP%\7za.exe"
    if not defined SEVENZ goto ZIP_NO7Z
)

echo Creating data archive (LZMA, multithreaded) ...
"%SEVENZ%" a -tzip -mm=LZMA -mx=9 -mmt=on -y "%ZIP%" "%DATA%" >nul
if errorlevel 1 (
    echo FAILED - data archive not created
    exit /b 1
)
for %%A in ("%ZIP%") do echo Created: %%~nxA (%%~zA bytes)
exit /b 0

:ZIP_NO7Z
echo FAILED - 7-Zip could not be provided (needed for LZMA compression)
exit /b 1
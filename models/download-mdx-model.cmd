@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "URL=https://huggingface.co/Politrees/UVR_resources/resolve/main/models/MDXNet/Kim_Vocal_2.onnx"
set "OUT=Kim_Vocal_2.onnx"
set "PART=Kim_Vocal_2.onnx.part"

if exist "%OUT%" for %%A in ("%OUT%") do if %%~zA GEQ 60000000 (
    echo MDX model already exists.
    exit /b 0
)
if exist "%OUT%" del "%OUT%" >nul 2>&1
if exist "%PART%" del "%PART%" >nul 2>&1

echo Downloading MDX Kim_Vocal_2 ...
curl.exe -L --fail --retry 3 --retry-delay 2 -o "%PART%" "%URL%"
if errorlevel 1 (
    echo MDX download failed.
    if exist "%PART%" del "%PART%" >nul 2>&1
    exit /b 1
)
for %%A in ("%PART%") do if %%~zA LSS 60000000 (
    echo MDX download is incomplete.
    del "%PART%" >nul 2>&1
    exit /b 1
)
move /y "%PART%" "%OUT%" >nul
echo MDX model ready: %OUT%
exit /b 0
@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  whisper.cpp  |  Unified Launcher
:: ============================================================

:INIT
if "%~1"=="" (
    echo.
    echo  No file detected.
    echo  Drop a video, audio, SRT or TXT file onto this script.
    echo.
    pause
    exit /b
)

set "DP=%~dp0"
cd /d "%~dp0"

set "MODEL=mistral-nemo"
set "EXE=data\whisper-cli.exe"
set "MODEL_LARGE=models\ggml-large-v2.bin"
set "MODEL_TINY=models\ggml-tiny.bin"
set "AUDTMP=%TEMP%\whisper-cli-audio"
set "ARGSFILE=%TEMP%\whisper-cli-args.txt"
set "FFMPEG=data\ffmpeg.exe"
set "VAD_MODEL=models\ggml-silero-v6.2.0.bin"
set "USE_MDX=1"
if defined FWHISPER_MDX set "USE_MDX=!FWHISPER_MDX!"
set "MDX_MODEL=models\Kim_Vocal_2.onnx"
set "MDX_RUNTIME=data\onnxruntime\__init__.py"

set "OUTSUFFIX=.srt"
set "WORKLIST_EMPTY="
set "ERRLOG=%TEMP%\whisper-cli-stderr.log"
set "STDOUTLOG=%TEMP%\whisper-cli-stdout.log"
if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1

set "_ext=%~x1"

set "IS_DIR=0"
pushd "%~1" 2>nul
if not errorlevel 1 (
    set "IS_DIR=1"
    set "DIR_PATH=%~1"
    popd
)

set "file_list="
set "file_count=0"

:COLLECT
set /a file_count+=1
set "file[!file_count!]=%~1"
set "file_list=!file_list! "%~1""
shift
if not "%~1"=="" goto COLLECT

set "TXT_COUNT=0"
set "OTHER_COUNT=0"
if "!IS_DIR!"=="1" call :COUNT_DIR_TYPES
if "!IS_DIR!"=="0" call :COUNT_ARG_TYPES
if !TXT_COUNT! gtr 0 (
    if !OTHER_COUNT! gtr 0 (
        echo.
        echo [WHISPER] Mixed TXT and media/SRT files are not supported.
        echo [WHISPER] Drop either TXT files or media files.
        echo.
        goto END
    )
    goto MODE_TXT
)

if /i "%_ext%"==".srt" goto MODE_SRT_TRANSLATE

:: ============================================================
::  Banner + Menu
:: ============================================================
:: ============================================================
::  Subroutine: banner + loaded file list
:: ============================================================
goto MAIN_MENU

:SHOW_HEADER
cls
call :BANNER
echo  !file_count! file(s) loaded.
set "li=0"
:FILE_LIST_LOOP
set /a li+=1
if !li! gtr !file_count! goto FILE_LIST_DONE
set "lf=!file[%li%]!"
for %%F in ("!lf!") do set "lfn=%%~nxF"
echo   - !lfn!
goto FILE_LIST_LOOP
:FILE_LIST_DONE
echo.
exit /b
:MAIN_MENU
set "HAS_MISSING_MODELS="
if not exist "!MODEL_LARGE!" set "HAS_MISSING_MODELS=1"
if not exist "!MODEL_TINY!" set "HAS_MISSING_MODELS=1"
if not exist "!VAD_MODEL!" set "HAS_MISSING_MODELS=1"
if not exist "!MDX_MODEL!" set "HAS_MISSING_MODELS=1"
if defined HAS_MISSING_MODELS (
    set "MENU_KEYS=123456E"
    set "MENU_PROMPT=  Select [1-6, E = Exit]: "
) else (
    set "MENU_KEYS=12345E"
    set "MENU_PROMPT=  Select [1-5, E = Exit]: "
)
call :SHOW_HEADER
echo  -------------------------------------------------------
echo   What do you want to do?
echo  -------------------------------------------------------
echo.
echo   [1]  Transcribe Auto          (language auto-detect)
echo   [2]  Transcribe German audio  (language forced)
echo   [3]  Transcribe English audio (language forced)
echo   [4]  Translate German audio   (Whisper DE, Ollama to EN)
echo   [5]  Translate English audio  (Whisper EN, Ollama to DE)
if defined HAS_MISSING_MODELS echo   [6]  Download Models          (shown only if a model is missing)
echo.
choice /c !MENU_KEYS! /n /m "!MENU_PROMPT!"
if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER

if defined HAS_MISSING_MODELS (
    if errorlevel 7 goto END
    if errorlevel 6 goto MODE_DOWNLOAD_MODELS
) else (
    if errorlevel 6 goto END
)
if errorlevel 5 goto MODE_TRANSLATE_EN
if errorlevel 4 goto MODE_TRANSLATE_DE
if errorlevel 3 goto MODE_EN
if errorlevel 2 goto MODE_DE
if errorlevel 1 goto MODE_AUTO

:: ============================================================
::  [1] AUTO
:: ============================================================
:MODE_AUTO
call :BUILD_WORKLIST
if defined WORKLIST_EMPTY goto END
call :ENSURE_GGML_MODEL large-v2
if errorlevel 1 goto WHISPER_MODEL_ABORT
echo.
echo [WHISPER] Starting transcription (language auto-detect) ...
echo.
set /a WL_TOTAL=!todo_count!
for /l %%i in (1,1,!WL_TOTAL!) do set "WLFILE[%%i]=!todo[%%i]!"
set "TR_LANG=auto"
set "TR_MODEL=%MODEL_LARGE%"
call :RUN_WHISPER_CLI
if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER
if errorlevel 1 goto TRANSCRIBE_FAIL
if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1
set "sx=0"
:AUTO_SPLIT_LOOP
set /a sx+=1
if !sx! gtr !todo_count! goto AUTO_SPLIT_DONE
set "sf=!todo[%sx%]!"
for %%F in ("!sf!") do call :SPLIT_WORDS_ONE "%%~dpnF" "!AUDTMP!\a!sx!.wav" display
goto AUTO_SPLIT_LOOP
:AUTO_SPLIT_DONE
del /q "!AUDTMP!\*.wav" >nul 2>&1
rd "!AUDTMP!" >nul 2>&1
call :RENAME_OUTPUT
call :CHECK_OUTPUT
goto CLEANUP_JSON


:: ============================================================
::  Modes 2/3/4/5: reuse existing DE/EN subtitles where possible
:: ============================================================
:MODE_DE
set "LANG=de" & set "LANG_NAME=German" & set "OUT_SUFFIX=.DE.srt" & set "OTHER_SUFFIX=.EN.srt" & set "OTHER_LANG=en" & set "OTHER_NAME=English" & set "NEED_BOTH=0"
goto MODE_SRTCHECK

:: ============================================================
:MODE_EN
set "LANG=en" & set "LANG_NAME=English" & set "OUT_SUFFIX=.EN.srt" & set "OTHER_SUFFIX=.DE.srt" & set "OTHER_LANG=de" & set "OTHER_NAME=German" & set "NEED_BOTH=0"
goto MODE_SRTCHECK

:: ============================================================
:MODE_TRANSLATE_DE
call :CHECK_PYTHON
if errorlevel 1 goto CLEANUP_EXIT
set "LANG=de" & set "LANG_NAME=German" & set "OUT_SUFFIX=.DE.srt" & set "OTHER_SUFFIX=.EN.srt" & set "OTHER_LANG=en" & set "OTHER_NAME=English" & set "NEED_BOTH=1"
goto MODE_SRTCHECK

:: ============================================================
:MODE_TRANSLATE_EN
call :CHECK_PYTHON
if errorlevel 1 goto CLEANUP_EXIT
set "LANG=en" & set "LANG_NAME=English" & set "OUT_SUFFIX=.EN.srt" & set "OTHER_SUFFIX=.DE.srt" & set "OTHER_LANG=de" & set "OTHER_NAME=German" & set "NEED_BOTH=1"
goto MODE_SRTCHECK

:: ============================================================
:MODE_SRTCHECK
if "!IS_DIR!"=="1" call :EXPAND_DIR
if "!IS_DIR!"=="1" if !file_count!==0 (
    echo.
    echo [WHISPER] No supported media files found in the dropped folder.
    echo.
    goto END
)
set "did_work="
set "wcount=0"
set "whisper_list="
set "job_count=0"
set "JOBFILE=%TEMP%\whisper-cli-jobs.txt"
if exist "!JOBFILE!" del "!JOBFILE!" >nul 2>&1
set "i=0"
:MODE_SRTCHECK_LOOP
set /a i+=1
if !i! gtr !file_count! goto MODE_SRTCHECK_RUN

set "src=!file[%i%]!"
set "src_path="
set "src_name="
for %%F in ("!src!") do (
    set "src_path=%%~dpF"
    set "src_name=%%~nF"
)

set "exist_out=0"
if exist "!src_path!!src_name!!OUT_SUFFIX!" set "exist_out=1"
set "exist_other=0"
if exist "!src_path!!src_name!!OTHER_SUFFIX!" set "exist_other=1"

if "!exist_out!"=="1" if "!exist_other!"=="1" (
    echo.
    echo [WHISPER] Subtitles already exist: !src_name!!OUT_SUFFIX! + !src_name!!OTHER_SUFFIX!
    goto MODE_SRTCHECK_NEXT
)
if "!exist_out!"=="1" if "!NEED_BOTH!"=="0" (
    echo.
    echo [WHISPER] Subtitle already exists: !src_name!!OUT_SUFFIX!
    goto MODE_SRTCHECK_NEXT
)

if "!exist_out!"=="1" (
    call :ADD_JOB "!src_path!!src_name!!OUT_SUFFIX!" "!src_path!!src_name!!OTHER_SUFFIX!" !OTHER_LANG!
    goto MODE_SRTCHECK_NEXT
)
if "!exist_other!"=="1" (
    call :ADD_JOB "!src_path!!src_name!!OTHER_SUFFIX!" "!src_path!!src_name!!OUT_SUFFIX!" !LANG!
    goto MODE_SRTCHECK_NEXT
)

set /a wcount+=1
set "wfile[!wcount!]=!src!"
set "whisper_list=!whisper_list! "!src!""
if "!NEED_BOTH!"=="1" call :ADD_JOB "!src_path!!src_name!!OUT_SUFFIX!" "!src_path!!src_name!!OTHER_SUFFIX!" !OTHER_LANG!

:MODE_SRTCHECK_NEXT
goto MODE_SRTCHECK_LOOP

:MODE_SRTCHECK_RUN
if !job_count! gtr 0 (
    call :ASK_CONTEXT
    if /i "!CTX!"=="C" goto CANCELLED_BY_USER
    if /i "!CTX!"=="R" goto MAIN_MENU
    if /i "!CTX!"=="E" goto CLEANUP_EXIT
)

call :ENSURE_GGML_MODEL large-v2
if errorlevel 1 goto WHISPER_MODEL_ABORT

set "whisper_done="
if !wcount! gtr 0 (
    echo.
    echo [WHISPER] Starting transcription in !LANG_NAME! - !wcount! files ...
    echo.
    set /a WL_TOTAL=!wcount!
    for /l %%i in (1,1,!WL_TOTAL!) do set "WLFILE[%%i]=!wfile[%%i]!"
    set "TR_LANG=!LANG!"
    set "TR_MODEL=%MODEL_LARGE%"
    call :RUN_WHISPER_CLI
    if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER
    if errorlevel 1 (
        echo [WHISPER] Whisper transcription failed - exit code !errorlevel!
        if exist "!ERRLOG!" echo.
        if exist "!ERRLOG!" findstr /v /i /c:"Matplotlib is building the font cache" "!ERRLOG!"
        if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1
        goto MODE_SRTCHECK_TRANSLATE
    )
    if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1
    set "did_work=1"
    set "whisper_done=1"
)
if not defined whisper_done goto MODE_SRTCHECK_TRANSLATE

set "WSMODE=display"
if "!NEED_BOTH!"=="1" set "WSMODE=sentence"
set "sx=0"
:ENGINE_SPLIT_LOOP
set /a sx+=1
if !sx! gtr !wcount! goto ENGINE_SPLIT_DONE
set "sf=!wfile[%sx%]!"
for %%F in ("!sf!") do call :SPLIT_WORDS_ONE "%%~dpnF" "!AUDTMP!\a!sx!.wav" "!WSMODE!"
goto ENGINE_SPLIT_LOOP
:ENGINE_SPLIT_DONE
del /q "!AUDTMP!\*.wav" >nul 2>&1
rd "!AUDTMP!" >nul 2>&1

set "wi=0"
:RENAME_WHISPER_LOOP
set /a wi+=1
if !wi! gtr !wcount! goto RENAME_WHISPER_DONE
set "wf=!wfile[%wi%]!"
set "wpath="
set "wname="
for %%F in ("!wf!") do (
    set "wpath=%%~dpF"
    set "wname=%%~nF"
)
if exist "!wpath!!wname!.srt" (
    move /y "!wpath!!wname!.srt" "!wpath!!wname!!OUT_SUFFIX!" >nul
) else (
    echo [WHISPER] No subtitle produced: !wname!.srt
)
goto RENAME_WHISPER_LOOP

:RENAME_WHISPER_DONE
goto MODE_SRTCHECK_TRANSLATE

:MODE_SRTCHECK_TRANSLATE
if !job_count! gtr 0 (
    echo.
    echo [WHISPER] Checking Ollama ...
    call :OLLAMA_SETUP
    if errorlevel 1 goto CLEANUP_EXIT
    if "!NEED_BOTH!"=="1" (
        python "translate.py" --clause-batch "!JOBFILE!" !CTX! "!MODEL!"
    ) else (
        python "translate.py" --batch "!JOBFILE!" !CTX! "!MODEL!"
    )
    if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER
    if errorlevel 1 (
        echo [WHISPER] Translation failed - exit code !errorlevel!
        goto MODE_SRTCHECK_CLEANUP
    )
    set "did_work=1"
)

:MODE_SRTCHECK_CLEANUP
set "ci=0"
:MODE_SRTCHECK_CLEAN_LOOP
set /a ci+=1
if !ci! gtr !file_count! goto MODE_SRTCHECK_DONE
set "cf=!file[%ci%]!"
set "cpath="
set "cname="
for %%F in ("!cf!") do (
    set "cpath=%%~dpF"
    set "cname=%%~nF"
)
if exist "!cpath!!cname!.json" del "!cpath!!cname!.json" >nul 2>&1
if exist "!cpath!!cname!.json.vadmap" del "!cpath!!cname!.json.vadmap" >nul 2>&1
for %%W in ("!cpath!!cname!*.wav") do if exist "%%W" del "%%W" >nul 2>&1
goto MODE_SRTCHECK_CLEAN_LOOP

:MODE_SRTCHECK_DONE
if exist "!JOBFILE!" del "!JOBFILE!" >nul 2>&1
if not defined did_work (
    echo.
    echo [WHISPER] Nothing to do - all dropped files already have subtitles.
    echo.
)
goto END

:: ============================================================
::  Subroutine: append one translation job
:: ============================================================
:ADD_JOB
set /a job_count+=1
>>"!JOBFILE!" echo "%~1"^|"%~2"^|%~3
exit /b

:: ============================================================
::  Subroutine: expand dropped folder into media files
:: ============================================================
:EXPAND_DIR
set "file_count=0"
pushd "!DIR_PATH!"
for /r %%F in (*) do (
    set "fext=%%~xF"
    set "ismedia="
    for %%E in (.mp4 .mkv .m4v .mov .avi .webm .wmv .flv .mpg .mpeg .ts .m2ts .mts .vob .3gp .ogv .mp3 .wav .m4a .aac .flac .ogg .opus .wma .aiff .aif .m4b .caf .amr .wv .ape) do (
        if /i "!fext!"=="%%E" set "ismedia=1"
    )
    if defined ismedia (
        set /a file_count+=1
        set "file[!file_count!]=%%F"
    )
)
popd
exit /b

:COUNT_ARG_TYPES
set "ti=0"
:COUNT_ARG_LOOP
set /a ti+=1
if !ti! gtr !file_count! exit /b
set "tf=!file[%ti%]!"
if /i "!tf:~-4!"==".txt" (
    set /a TXT_COUNT+=1
) else (
    set /a OTHER_COUNT+=1
)
goto COUNT_ARG_LOOP

:COUNT_DIR_TYPES
pushd "!DIR_PATH!"
for /r %%F in (*) do (
    if /i "%%~xF"==".txt" (
        set /a TXT_COUNT+=1
    ) else (
        set /a OTHER_COUNT+=1
    )
)
popd
exit /b

:EXPAND_TXT_DIR
set "file_count=0"
pushd "!DIR_PATH!"
for /r %%F in (*.txt) do (
    set /a file_count+=1
    set "file[!file_count!]=%%F"
)
popd
exit /b

:MODE_TXT
call :CHECK_PYTHON
if errorlevel 1 goto CLEANUP_EXIT
if "!IS_DIR!"=="1" call :EXPAND_TXT_DIR
call :SHOW_HEADER
echo  -------------------------------------------------------
echo   Translation direction:
echo  -------------------------------------------------------
echo.
echo   [1]  Translate to German   -^>  .DE.txt
echo   [2]  Translate to English  -^>  .EN.txt
echo.
choice /c 12E /n /m "  Select [1-2, E = Exit]: "
if errorlevel 3 goto CLEANUP_EXIT
if errorlevel 2 (set "TXT_TARGET=en" & set "TXT_EXT=EN" & goto TXT_DIR_DONE)
if errorlevel 1 (set "TXT_TARGET=de" & set "TXT_EXT=DE" & goto TXT_DIR_DONE)
goto MODE_TXT

:TXT_DIR_DONE
call :ASK_CONTEXT
if /i "!CTX!"=="C" goto CANCELLED_BY_USER
if /i "!CTX!"=="R" goto MAIN_MENU
if /i "!CTX!"=="E" goto CLEANUP_EXIT
echo.
set "TXTJOBS=%TEMP%\whisper-cli-txt-jobs.txt"
del "!TXTJOBS!" >nul 2>&1
set "TXT_JOBS=0"
set "ti=0"
:TXT_JOB_LOOP
set /a ti+=1
if !ti! gtr !file_count! goto TXT_JOB_DONE
set "tsrc=!file[%ti%]!"
for %%F in ("!tsrc!") do (
    set "tpath=%%~dpF"
    set "tname=%%~nxF"
)
set "tbase=!tname!"
if /i "!tbase:~-4!"==".txt" set "tbase=!tbase:~0,-4!"
if /i "!tbase:~-3!"==".DE" goto TXT_SKIP_INPUT
if /i "!tbase:~-3!"==".EN" goto TXT_SKIP_INPUT
set "tdst=!tpath!!tbase!.!TXT_EXT!.txt"
if exist "!tdst!" goto TXT_SKIP_EXISTS
echo "!tsrc!"^|"!tdst!" >> "!TXTJOBS!"
set /a TXT_JOBS+=1
goto TXT_JOB_LOOP
:TXT_SKIP_INPUT
echo [WHISPER] Skipping !tname! - input already has .DE.txt or .EN.txt suffix
echo.
goto TXT_JOB_LOOP
:TXT_SKIP_EXISTS
echo [WHISPER] Skipping !tname! - target !tbase!.!TXT_EXT!.txt already exists
echo.
goto TXT_JOB_LOOP
:TXT_JOB_DONE
if !TXT_JOBS!==0 (
    echo [WHISPER] Nothing to do - no TXT files need translation.
    echo.
    goto CLEANUP_EXIT
)
echo [WHISPER] Checking Ollama ...
call :OLLAMA_SETUP
if errorlevel 1 goto CLEANUP_EXIT
python "translate.py" --txt-batch "!TXTJOBS!" !CTX! !TXT_TARGET! "!MODEL!"
if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER
if errorlevel 1 (
    echo [WHISPER] Translation failed - exit code !errorlevel!
    if exist "!TXTJOBS!" del "!TXTJOBS!" >nul 2>&1
    goto CLEANUP_EXIT
)
if exist "!TXTJOBS!" del "!TXTJOBS!" >nul 2>&1
goto CLEANUP_EXIT

:: ============================================================
::  Error handling for transcription modes
:: ============================================================
:TRANSCRIBE_FAIL
set "fail_code=!errorlevel!"
echo.
echo [WHISPER] Transcription failed - exit code !fail_code!
if exist "!ERRLOG!" echo.
if exist "!ERRLOG!" findstr /v /i /c:"Matplotlib is building the font cache" "!ERRLOG!"
if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1
echo.
goto CLEANUP_JSON



:: ============================================================
::  SRT DIRECT TRANSLATION  (SRT dropped onto script)
:: ============================================================
:MODE_SRT_TRANSLATE
call :CHECK_PYTHON
if errorlevel 1 goto CLEANUP_EXIT
set "JOBFILE=%TEMP%\whisper-cli-jobs.txt"
if exist "!JOBFILE!" del "!JOBFILE!" >nul 2>&1
set "job_count=0"
set "jcount=0"
set "i=0"
:SRT_LOOP
set /a i+=1
if !i! gtr !file_count! goto SRT_SCAN_DONE

set "src=!file[%i%]!"
set "srt_path="
set "srt_name="
for %%F in ("!src!") do (
    set "srt_path=%%~dpF"
    set "srt_name=%%~nxF"
)
set "stem=!srt_name!"
if /i "!stem:~-4!"==".srt" set "stem=!stem:~0,-4!"
set "base=!stem!"
set "ext="
for %%A in ("!stem!") do (
    set "base=%%~nA"
    set "ext=%%~xA"
)
if /i "!ext!"==".DE" (
    call :SRT_ADD_TARGET EN
    goto SRT_LOOP_NEXT
)
if /i "!ext!"==".EN" (
    call :SRT_ADD_TARGET DE
    goto SRT_LOOP_NEXT
)
call :CHECK_LANG_EXT
if defined LANGEXT (
    call :SRT_ADD_TARGET DE
    call :SRT_ADD_TARGET EN
) else (
    set "base=!stem!"
    call :SRT_ADD_TARGET DE
)
:SRT_LOOP_NEXT
goto SRT_LOOP

:SRT_ADD_TARGET
set "tgt=%~1"
set "tname=!base!.!tgt!.srt"
set "dst=!srt_path!!tname!"
if exist "!dst!" (
    echo [WHISPER] Subtitle already exists: !tname!
    exit /b
)
if /i "!tgt!"=="EN" if exist "!srt_path!!base!.srt" (
    echo [WHISPER] Subtitle already exists: !base!.srt - treated as English
    exit /b
)
set "dup="
set "di=0"
:SRT_DUP_LOOP
set /a di+=1
if !di! gtr !jcount! goto SRT_DUP_DONE
if /i "!jds[%di%]!"=="!dst!" set "dup=1"
goto SRT_DUP_LOOP
:SRT_DUP_DONE
if defined dup (
    echo [WHISPER] Duplicate target skipped: !tname!
    exit /b
)
set /a jcount+=1
set "jds[!jcount!]=!dst!"
if /i "!tgt!"=="EN" (
    call :ADD_JOB "!src!" "!dst!" en
) else (
    call :ADD_JOB "!src!" "!dst!" de
)
exit /b

:CHECK_LANG_EXT
set "LANGEXT="
set "el="
if "!ext:~0,1!"=="." set "el=!ext:~1!"
if not defined el exit /b
echo !el!|findstr /r "^[A-Za-z][A-Za-z]$" >nul && set "LANGEXT=1"
echo !el!|findstr /r "^[A-Za-z][A-Za-z][A-Za-z]$" >nul && set "LANGEXT=1"
exit /b

:SRT_SCAN_DONE
if !job_count!==0 (
    echo.
    echo [WHISPER] Nothing to do - all dropped files already have translations.
    echo.
    if exist "!JOBFILE!" del "!JOBFILE!" >nul 2>&1
    goto END
)
call :ASK_CONTEXT
if /i "!CTX!"=="C" goto CANCELLED_BY_USER
if /i "!CTX!"=="R" goto MAIN_MENU
if /i "!CTX!"=="E" goto CLEANUP_EXIT
echo.
echo [WHISPER] Checking Ollama ...
call :OLLAMA_SETUP
if errorlevel 1 goto CLEANUP_EXIT
python "translate.py" --batch "!JOBFILE!" !CTX! "!MODEL!"
if "%errorlevel%"=="-1073741510" goto CANCELLED_BY_USER
if errorlevel 1 echo [WHISPER] Translation failed - exit code !errorlevel!
if exist "!JOBFILE!" del "!JOBFILE!" >nul 2>&1
goto END
:: ============================================================
::  Subroutine: build work list (skip files with existing subtitle)
:: ============================================================
:BUILD_WORKLIST
set "todo_list="
set "todo_count=0"
set "skip_count=0"
if "!IS_DIR!"=="1" call :EXPAND_DIR
set "bi=0"
:WORKLIST_LOOP
set /a bi+=1
if !bi! gtr !file_count! goto WORKLIST_READY
set "bf=!file[%bi%]!"
set "bfp="
set "bfn="
for %%F in ("!bf!") do (
    set "bfp=%%~dpF"
    set "bfn=%%~nF"
)
set "have="
if exist "!bfp!!bfn!.srt" set "have=!bfn!.srt"
if exist "!bfp!!bfn!.DE.srt" set "have=!bfn!.DE.srt"
if exist "!bfp!!bfn!.EN.srt" set "have=!bfn!.EN.srt"
if not defined have if exist "!bfp!!bfn!.*.srt" set "have=!bfn!.*.srt"
if defined have (
    echo [WHISPER] Subtitle already exists: !have!
    set /a skip_count+=1
    goto WORKLIST_LOOP
)
set /a todo_count+=1
set "todo[!todo_count!]=!bf!"
set "todo_list=!todo_list! "!bf!""
goto WORKLIST_LOOP

:WORKLIST_READY
if !todo_count!==0 (
    echo.
    echo [WHISPER] Nothing to do - all dropped files already have a subtitle.
    echo.
    set "WORKLIST_EMPTY=1"
    exit /b
)
exit /b

:: ============================================================
::  Subroutine: rename auto-detected subtitle by its language (de/en)
:: ============================================================
:RENAME_OUTPUT
if "!IS_DIR!"=="1" goto RENAME_DIR_OUTPUT
set "ri=0"
:RENAME_OUTPUT_LOOP
set /a ri+=1
if !ri! gtr !todo_count! exit /b
set "rf=!todo[%ri%]!"
set "rfp="
set "rfn="
for %%F in ("!rf!") do (
    set "rfp=%%~dpF"
    set "rfn=%%~nF"
)
if not exist "!rfp!!rfn!.srt" goto RENAME_OUTPUT_LOOP
set "lang="
if exist "!rfp!!rfn!.json" (
    for /f "usebackq tokens=2 delims=:" %%L in (`findstr /i /c:language "!rfp!!rfn!.json"`) do (
        set "lv=%%L"
        set "lv=!lv: =!"
        for /f "tokens=1 delims=," %%M in ("!lv!") do set "lv=%%M"
        set "lv=!lv:"=!"
        set "lang=!lv!"
    )
)
call :TO_UPPER
if defined ULANG if exist "!rfp!!rfn!.srt" move /y "!rfp!!rfn!.srt" "!rfp!!rfn!.!ULANG!.srt" >nul
goto RENAME_OUTPUT_LOOP

:TO_UPPER
set "ULANG="
set "l1=!lang:~0,1!"
set "l2=!lang:~1,1!"
set "l3=!lang:~2,1!"
for %%C in (!l1! !l2! !l3!) do (
    for %%U in (A B C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
        if /i "%%C"=="%%U" set "ULANG=!ULANG!%%U"
    )
)
exit /b

:RENAME_DIR_OUTPUT
if not defined DIR_PATH exit /b
pushd "!DIR_PATH!"
for /r %%J in (*.json) do (
    set "dpath=%%~dpJ"
    set "dname=%%~nJ"
    set "dlang="
    for /f "usebackq tokens=2 delims=:" %%L in (`findstr /i /c:language "%%J"`) do (
        set "lv=%%L"
        set "lv=!lv: =!"
        for /f "tokens=1 delims=," %%M in ("!lv!") do set "lv=%%M"
        set "lv=!lv:"=!"
        set "dlang=!lv!"
    )
    set "lang=!dlang!"
    call :TO_UPPER
    if defined ULANG if exist "!dpath!!dname!.srt" move /y "!dpath!!dname!.srt" "!dpath!!dname!.!ULANG!.srt" >nul
)
popd
exit /b

:: ============================================================
::  Subroutine: verify that a subtitle was written for every file
:: ============================================================
:CHECK_OUTPUT
if "!IS_DIR!"=="1" exit /b
set "ci2=0"
:CHECK_OUTPUT_LOOP
set /a ci2+=1
if !ci2! gtr !todo_count! exit /b
set "cf=!todo[%ci2%]!"
set "cfp="
set "cfn="
for %%F in ("!cf!") do (
    set "cfp=%%~dpF"
    set "cfn=%%~nF"
)
if exist "!cfp!!cfn!.srt" goto CHECK_OUTPUT_LOOP
if exist "!cfp!!cfn!.DE.srt" goto CHECK_OUTPUT_LOOP
if exist "!cfp!!cfn!.EN.srt" goto CHECK_OUTPUT_LOOP
echo [WHISPER] No subtitle written: !cfn!
goto CHECK_OUTPUT_LOOP

:: ============================================================
::  Subroutine: ask for context block count
:: ============================================================
:ASK_CONTEXT
echo.
echo  -------------------------------------------------------
echo   Context blocks for translation:
echo  -------------------------------------------------------
echo.
echo   Key  Benefit                        Speed
echo   ---  -----------------------------  --------------
echo   0    No context                     Fastest
echo   3    Good compromise                Moderate
echo   A    Full entity tracking (Mistral) Slow
echo   B    Full entity tracking (Gemma)   Slowest
echo.
:CTX_INPUT
set "CTX_KEY="
set /p "CTX_KEY=  Context blocks [0, 3, A/B = 10, E = Exit, R = Return, Enter = B]: "
if "%errorlevel%"=="-1073741510" (set "CTX=C" & exit /b)
if not defined CTX_KEY set "CTX_KEY=B"
if defined CTX_KEY set "CTX_KEY=!CTX_KEY: =!"
if /i "!CTX_KEY!"=="0" (set "CTX=0"  & exit /b)
if /i "!CTX_KEY!"=="3" (set "CTX=3"  & exit /b)
if /i "!CTX_KEY!"=="A" (set "CTX=10" & exit /b)
if /i "!CTX_KEY!"=="B" (set "CTX=10" & set "MODEL=translategemma:12b" & exit /b)
if /i "!CTX_KEY!"=="E" (set "CTX=E"  & exit /b)
if /i "!CTX_KEY!"=="R" (set "CTX=R"  & exit /b)
echo   Invalid choice - please enter 0, 3, A, B, E or R. Press Enter for B.
goto CTX_INPUT

:: ============================================================
::  Subroutine: check Python 3.x
:: ============================================================
:CHECK_PYTHON
where python >nul 2>nul
if errorlevel 1 goto PYTHON_MISSING
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" >nul 2>nul
if errorlevel 1 goto PYTHON_MISSING
exit /b

:PYTHON_MISSING
echo.
echo [WHISPER] No working Python 3.7+ found.
echo [WHISPER] translate.py requires Python 3.7 or newer (3.10+ recommended).
echo [WHISPER] Please install Python: https://www.python.org/downloads/
echo [WHISPER] During setup enable "Add python.exe to PATH", then run again.
echo.
exit /b 1

:: ============================================================
::  Subroutine: check Ollama / Python prerequisites
:: ============================================================
:OLLAMA_SETUP

call :CHECK_PYTHON
if errorlevel 1 exit /b 1

where ollama >nul 2>nul
if errorlevel 1 (
    echo [WHISPER] Ollama not found. Downloading installer ...
    curl -L https://ollama.com/download/OllamaSetup.exe -o "%temp%\OllamaSetup.exe"
    if errorlevel 1 (
        echo [WHISPER] Ollama download failed - check your internet connection.
        exit /b 1
    )
    echo [WHISPER] Installing Ollama ...
    start /wait "" "%temp%\OllamaSetup.exe" /silent
    if errorlevel 1 echo [WHISPER] Ollama installer returned an error.
    set "PATH=!PATH!;!LOCALAPPDATA!\Programs\Ollama"
    timeout /t 3 >nul
    where ollama >nul 2>nul
    if errorlevel 1 (
        echo [WHISPER] Ollama is not available - please install it manually.
        exit /b 1
    )
)

set "OLLAMA_HOST=127.0.0.1"
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q4_0"
set "OLLAMA_GPU_OVERHEAD=0"
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_LOG_LEVEL=error"
set "OLLAMA_NUM_THREADS=8"

python -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo [WHISPER] Installing Python package "requests" ...
    python -m pip install requests
    if errorlevel 1 (
        echo [WHISPER] Could not install "requests" - translation will not work.
        exit /b 1
    )
)

exit /b

:: ============================================================
::  JSON cleanup for modes 1-4
:: ============================================================
:CLEANUP_JSON
if "!IS_DIR!"=="1" (
    if defined DIR_PATH (
        pushd "!DIR_PATH!"
        for /r %%J in (*.json) do del "%%J" >nul 2>&1
        popd
    )
    goto END
)
set "ci=0"
:CLEANUP_JSON_LOOP
set /a ci+=1
if !ci! gtr !file_count! goto END
set "csrc=!file[%ci%]!"
for %%F in ("!csrc!") do (
    set "cpath=%%~dpF"
    set "cname=%%~nF"
)
for %%J in ("!cpath!!cname!*.json") do if exist "%%J" del "%%J" >nul 2>&1
goto CLEANUP_JSON_LOOP

:: ============================================================
::  Exit with optional WAV cleanup
:: ============================================================
:CLEANUP_EXIT
set "ci=0"
:CLEANUP_EXIT_LOOP
set /a ci+=1
if !ci! gtr !file_count! goto END
set "csrc=!file[%ci%]!"
for %%F in ("!csrc!") do (
    set "cpath=%%~dpF"
    set "cname=%%~nF"
)
set "cname_clean=!cname!"
if /i "!cname:~-3!"==".EN" set "cname_clean=!cname:~0,-3!"
for %%W in ("!cpath!!cname_clean!*.wav") do if exist "%%W" del "%%W" >nul 2>&1
goto CLEANUP_EXIT_LOOP

:: ============================================================
::  Cancelled by user
:: ============================================================
:CANCELLED_BY_USER
echo.
echo [WHISPER] Cancelled by user.
if exist "!ERRLOG!" del "!ERRLOG!" >nul 2>&1
goto END

:: ============================================================
::  Subroutine: split long SRT blocks at sentence boundaries
:: ============================================================
:SPLIT_JOB_FILES
for /f "usebackq tokens=1,2 delims=|" %%A in ("!JOBFILE!") do (
    set "js=%%~A"
    set "jd=%%~B"
    call :SPLIT_SRT_DISPLAY_FILE "!js!"
    call :SPLIT_SRT_DISPLAY_FILE "!jd!"
)
exit /b

:SPLIT_WORDS_ONE
if not exist "%~1.json" exit /b
if not exist "%~1.srt" exit /b
where python >nul 2>nul
if errorlevel 1 exit /b
if /i "%~3"=="sentence" goto SPLIT_WORDS_SENTENCE
python "translate.py" --words-srt "%~1.json" "%~1.srt" "%~2" >nul 2>>"!ERRLOG!"
goto SPLIT_WORDS_DONE

:SPLIT_WORDS_SENTENCE
python "translate.py" --sentence-srt "%~1.json" "%~1.srt" "%~2" >nul 2>>"!ERRLOG!"

:SPLIT_WORDS_DONE
if errorlevel 1 call :SPLIT_SRT_ONE "%~1"
exit /b

:SPLIT_SRT_ONE
if not exist "%~1.srt" exit /b
where python >nul 2>nul
if errorlevel 1 exit /b
python "translate.py" --split-srt "%~1.srt" >nul 2>&1
exit /b

:SPLIT_SRT_DISPLAY_FILE
if not exist "%~1" exit /b
where python >nul 2>nul
if errorlevel 1 exit /b
python "translate.py" --split-srt-display "%~1" >nul 2>&1
exit /b

:ENSURE_MDX_MODEL
if exist "!MDX_MODEL!" exit /b 0
if not exist "models\download-mdx-model.cmd" (
    echo [WHISPER] MDX download script not found.
    exit /b 1
)
echo.
echo [WHISPER] MDX model missing - downloading Kim_Vocal_2 ...
pushd "models"
call "download-mdx-model.cmd"
set "MDXERR=!errorlevel!"
popd
if exist "!MDX_MODEL!" exit /b 0
echo [WHISPER] MDX model download failed.
exit /b 1

:MDX_SETUP
set "MDX_ACTIVE="
if not "!USE_MDX!"=="1" exit /b
if not exist "!MDX_RUNTIME!" exit /b
where python >nul 2>nul
if errorlevel 1 exit /b
if not exist "!MDX_MODEL!" call :ENSURE_MDX_MODEL
if exist "!MDX_MODEL!" set "MDX_ACTIVE=1"
exit /b


:PREPARE_AUDIO
set "psrc=%~1"
set "pdst=%~2"
set "pidx=%~3"
set "pname=%~4"
if not defined MDX_ACTIVE goto PREPARE_RAW
set "pmdxin=!AUDTMP!\m!pidx!_in.wav"
set "pmdxvoc=!AUDTMP!\m!pidx!_voc.wav"
echo [WHISPER] MDX vocal isolation
"%FFMPEG%" -y -v error -i "!psrc!" -vn -ar 44100 -ac 2 -c:a pcm_s16le "!pmdxin!"
if errorlevel 1 goto PREPARE_MDX_FAIL
python "mdx.py" "!pmdxin!" "!pmdxvoc!" 2>>"!ERRLOG!"
if errorlevel 1 goto PREPARE_MDX_FAIL
if not exist "!pmdxvoc!" goto PREPARE_MDX_FAIL
"%FFMPEG%" -y -v error -i "!pmdxvoc!" -ar 16000 -ac 1 -c:a pcm_s16le "!pdst!"
set "PRC=!errorlevel!"
if exist "!pmdxin!" del "!pmdxin!" >nul 2>&1
if exist "!pmdxvoc!" del "!pmdxvoc!" >nul 2>&1
exit /b !PRC!

:PREPARE_MDX_FAIL
echo.
echo [WHISPER] MDX failed - using original audio: !pname!
if exist "!pmdxin!" del "!pmdxin!" >nul 2>&1
if exist "!pmdxvoc!" del "!pmdxvoc!" >nul 2>&1
echo.
goto PREPARE_RAW

:PREPARE_RAW
"%FFMPEG%" -y -v error -i "!psrc!" -vn -ar 16000 -ac 1 -c:a pcm_s16le "!pdst!"
exit /b !errorlevel!
:: ============================================================
::  Subroutine: transcribe worklist with whisper-cli
:: ============================================================
:RUN_WHISPER_CLI
if !WL_TOTAL!==0 exit /b 0
if not exist "%EXE%" (
    echo [WHISPER] whisper-cli.exe not found: %EXE%
    exit /b 1
)
if not exist "!FFMPEG!" (
    echo [WHISPER] ffmpeg not found: !FFMPEG!
    exit /b 1
)
if not exist "!AUDTMP!" mkdir "!AUDTMP!" >nul 2>&1
del /q "!AUDTMP!\*.wav" >nul 2>&1
del "!ARGSFILE!" >nul 2>&1
call :MDX_SETUP
if "!USE_MDX!"=="1" if not defined MDX_ACTIVE echo [WHISPER] MDX not available - using raw audio.
>>"!ARGSFILE!" echo -m
>>"!ARGSFILE!" echo !TR_MODEL!
>>"!ARGSFILE!" echo -l
>>"!ARGSFILE!" echo !TR_LANG!
>>"!ARGSFILE!" echo -osrt
>>"!ARGSFILE!" echo -ojf
>>"!ARGSFILE!" echo -pp
>>"!ARGSFILE!" echo -bo
>>"!ARGSFILE!" echo 5
>>"!ARGSFILE!" echo -bs
>>"!ARGSFILE!" echo 5
>>"!ARGSFILE!" echo -tp
>>"!ARGSFILE!" echo 0.0
>>"!ARGSFILE!" echo -nth
>>"!ARGSFILE!" echo 0.5
>>"!ARGSFILE!" echo -et
>>"!ARGSFILE!" echo 2.4
>>"!ARGSFILE!" echo -sns
>>"!ARGSFILE!" echo -mc
>>"!ARGSFILE!" echo 0
call :ENSURE_VAD_MODEL
if defined VAD_READY (
    >>"!ARGSFILE!" echo --vad
    >>"!ARGSFILE!" echo -vm
    >>"!ARGSFILE!" echo !VAD_MODEL!
)
set "AUDCOUNT=0"
set "AUDFAIL=0"
set "ai=0"
:WHISPER_AUDIO_LOOP
set /a ai+=1
if !ai! gtr !WL_TOTAL! goto WHISPER_AUDIO_RUN
set "asrc=!WLFILE[%ai%]!"
if not exist "!asrc!" (
    echo [WHISPER] Input not found: !asrc!
    set /a AUDFAIL+=1
    goto WHISPER_AUDIO_LOOP
)
set "apath="
set "aname="
for %%F in ("!asrc!") do (
    set "apath=%%~dpF"
    set "aname=%%~nF"
)
set "awav=!AUDTMP!\a!ai!.wav"
echo [WHISPER] Extracting: !aname!
echo.
call :PREPARE_AUDIO "!asrc!" "!awav!" "!ai!" "!aname!"
if errorlevel 1 (
    echo [WHISPER] Audio extraction failed: !aname!
    set /a AUDFAIL+=1
    goto WHISPER_AUDIO_LOOP
)
>>"!ARGSFILE!" echo -of
>>"!ARGSFILE!" echo !apath!!aname!
>>"!ARGSFILE!" echo !awav!
set /a AUDCOUNT+=1
goto WHISPER_AUDIO_LOOP

:WHISPER_AUDIO_RUN
if !AUDCOUNT!==0 (
    del "!ARGSFILE!" >nul 2>&1
    rd "!AUDTMP!" >nul 2>&1
    exit /b 1
)
echo.
if defined VAD_READY (
    echo [WHISPER] Transcribing + VAD: !AUDCOUNT! files ...
) else (
    echo [WHISPER] Transcribing: !AUDCOUNT! files ...
)
where python >nul 2>nul
if errorlevel 1 (
    "%EXE%" @"!ARGSFILE!" >"!STDOUTLOG!" 2>nul
    set "WRC=!errorlevel!"
) else (
    python "helper.py" run "!ARGSFILE!" "!STDOUTLOG!"
    set "WRC=!errorlevel!"
)
if "!WRC!"=="0" del "!STDOUTLOG!" >nul 2>&1
del "!ARGSFILE!" >nul 2>&1
exit /b !WRC!

:: ============================================================
::  Subroutine: ensure a GGML model exists (download via hf)
:: ============================================================
:ENSURE_GGML_MODEL
set "GMODEL=%~1"
set "GMODELPATH="
if /i "!GMODEL!"=="large-v2" set "GMODELPATH=%MODEL_LARGE%"
if /i "!GMODEL!"=="tiny" set "GMODELPATH=%MODEL_TINY%"
if not defined GMODELPATH (
    echo [WHISPER] Unknown GGML model: !GMODEL!
    exit /b 1
)
if exist "!GMODELPATH!" exit /b 0
echo.
echo [WHISPER] Model not found: !GMODELPATH!
call :DOWNLOAD_GGML_MODEL !GMODEL!
if exist "!GMODELPATH!" exit /b 0
echo [WHISPER] Model still missing: !GMODELPATH!
exit /b 1

:DOWNLOAD_GGML_MODEL
if not exist "models\download-ggml-model.cmd" (
    echo [WHISPER] Download script not found: models\download-ggml-model.cmd
    exit /b 1
)
echo [WHISPER] Downloading GGML model !GMODEL! ...
pushd "models"
call "download-ggml-model.cmd" !GMODEL!
set "GMERR=!errorlevel!"
popd
if not exist "!GMODELPATH!" (
    echo [WHISPER] Model download failed - exit code !GMERR!.
    exit /b 1
)
echo [WHISPER] Model downloaded: !GMODELPATH!
exit /b 0

:ENSURE_VAD_MODEL
set "VAD_READY="
if exist "!VAD_MODEL!" (
    set "VAD_READY=1"
    exit /b
)
if not exist "models\download-vad-model.cmd" (
    echo [WHISPER] VAD download script not found - continuing without VAD.
    exit /b
)
echo.
echo [WHISPER] VAD model missing - downloading Silero VAD v6.2.0 ...
pushd "models"
call "download-vad-model.cmd" silero-v6.2.0
set "VERR=!errorlevel!"
popd
if exist "!VAD_MODEL!" (
    set "VAD_READY=1"
    echo [WHISPER] VAD model ready.
) else (
    echo [WHISPER] VAD model download failed - continuing without VAD.
)
exit /b

:MODE_DOWNLOAD_MODELS
echo.
echo [WHISPER] Checking required models ...
set "GMFAIL=0"
call :ENSURE_GGML_MODEL tiny
if errorlevel 1 set "GMFAIL=1"
call :ENSURE_GGML_MODEL large-v2
if errorlevel 1 set "GMFAIL=1"
call :ENSURE_VAD_MODEL
if not exist "!VAD_MODEL!" set "GMFAIL=1"
call :ENSURE_MDX_MODEL
if errorlevel 1 set "GMFAIL=1"
echo.
if "!GMFAIL!"=="1" (
    echo [WHISPER] Some models could not be downloaded - see messages above.
) else (
    echo [WHISPER] All required models are available.
)
echo.
pause
goto MAIN_MENU

:WHISPER_MODEL_ABORT
echo.
echo [WHISPER] Whisper model is not available - aborting.
echo.
goto END
:: ============================================================
::  End
:: ============================================================
:BANNER
where python >nul 2>nul
if errorlevel 1 (
    echo   W H I S P E R . C P P
    echo.
    exit /b
)
python "helper.py" banner
if errorlevel 1 echo   W H I S P E R . C P P
exit /b

:END
echo.
pause
exit /b

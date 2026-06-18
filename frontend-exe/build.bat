@echo off
REM Sky Voice AI — desktop bundle build (Windows)
REM
REM Output:  dist\SkyVoiceAI\SkyVoiceAI.exe

setlocal enableextensions
cd /d %~dp0
set "ROOT=%CD%"
set "FRONTEND_DIR=%ROOT%\..\frontend"

if not exist "%FRONTEND_DIR%" (
    echo [!] frontend\ not found at %FRONTEND_DIR%
    exit /b 1
)

echo ^>^> npm run build (frontend)
pushd "%FRONTEND_DIR%"
call npm run build || goto :err
popd

echo ^>^> syncing dist -^> web\
if exist "%ROOT%\web" rmdir /s /q "%ROOT%\web"
xcopy /E /I /Y /Q "%FRONTEND_DIR%\dist" "%ROOT%\web" >nul || goto :err

if not exist "%ROOT%\.venv" (
    echo ^>^> creating venv (.venv)
    python -m venv .venv || goto :err
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet || goto :err

echo ^>^> pyinstaller
if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build"
if exist "%ROOT%\dist"  rmdir /s /q "%ROOT%\dist"
pyinstaller --noconfirm --log-level=WARN SkyVoiceAI.spec || goto :err

echo.
echo [OK] done — see dist\SkyVoiceAI\
exit /b 0

:err
echo [!] build failed.
exit /b 1

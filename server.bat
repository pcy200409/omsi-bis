@echo off
setlocal

rem  OMSI BIS 로컬 서버 켜기/끄기
rem  더블클릭하면 메뉴가 뜹니다.
rem  명령으로도 됩니다:  server.bat start / stop / restart   [포트]

set "HERE=%~dp0"
set "PY=%HERE%server\.venv\Scripts\python.exe"
set "CMD=%~1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8001"

if not exist "%PY%" goto nopy

if /i "%CMD%"=="start"   goto do_start
if /i "%CMD%"=="stop"    goto do_stop
if /i "%CMD%"=="restart" goto do_restart

:menu
cls
call :status
echo.
echo    OMSI BIS 서버   -   포트 %PORT%
echo    상태: %STATE%
echo.
echo     1. 켜기 / 다시 켜기
echo     2. 끄기
echo     3. 브라우저 열기
echo     0. 나가기
echo.
choice /c 1230 /n /m "   선택: " >nul
if errorlevel 4 goto end
if errorlevel 3 goto m_open
if errorlevel 2 goto m_stop
goto m_restart

:m_restart
call :stop
call :start
timeout /t 2 >nul
goto menu

:m_stop
call :stop
timeout /t 1 >nul
goto menu

:m_open
start "" "http://127.0.0.1:%PORT%/"
goto menu

:do_start
call :start
goto end

:do_stop
call :stop
goto end

:do_restart
call :stop
call :start
goto end

:status
set "STATE=꺼짐"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "STATE=켜짐 - PID %%p"
exit /b

:stop
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  taskkill /f /pid %%p >nul 2>&1
  set "KILLED=1"
)
if defined KILLED echo    [끔] 포트 %PORT% 서버를 종료했습니다.
if not defined KILLED echo    [-] 켜져 있지 않았습니다.
exit /b

:start
start "OMSI BIS 서버 %PORT%" /d "%HERE%server" "%PY%" -m uvicorn app:app --host 127.0.0.1 --port %PORT%
echo    [켬] http://127.0.0.1:%PORT%/
echo         별도 창에서 실행됩니다. 그 창을 닫으면 서버도 꺼집니다.
exit /b

:nopy
echo    [!] 파이썬 환경을 찾지 못했습니다.
echo        %PY%
echo        server\.venv 가 있는지 확인하세요.
pause
goto end

:end
endlocal

@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title 디스크 복구 도구 (읽기 전용)

if not exist "%~dp0disk_recover_tool.py" (
  echo [오류] disk_recover_tool.py 를 이 bat 파일과 같은 폴더에 넣어 주세요.
  pause
  exit /b 1
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [오류] Python 을 찾을 수 없습니다. https://www.python.org 에서 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)

net session >nul 2>&1
if errorlevel 1 (set "ISADMIN=0") else (set "ISADMIN=1")

set "ARG1=%~1"

:ask
cls
echo ==================================================
echo   디스크 복구 도구  (읽기 전용: 원본에는 쓰지 않습니다)
echo ==================================================
if "%ISADMIN%"=="0" echo   * 관리자 권한이 아닙니다. 이미지 파일은 열 수 있지만 드라이브/물리 디스크는 관리자 권한이 필요합니다.
echo.
echo 참조할 대상을 입력하세요.
echo   - 드라이브 문자       예) E:
echo   - 물리 디스크 번호    예) 1          (= \\.\PhysicalDrive1)
echo   - 이미지 파일 경로    예) D:\backup\disk.001   (파일을 이 창에 끌어다 놓아도 됩니다)
echo   - L = 연결된 디스크/볼륨 목록 보기      Q = 종료
echo.
set "SRC="
if defined ARG1 (
  set "SRC=%ARG1%"
  set "ARG1="
) else (
  set /p "SRC=대상: "
)
if not defined SRC goto ask
set "SRC=%SRC:"=%"
if /i "%SRC%"=="Q" exit /b 0
if /i "%SRC%"=="L" (
  echo.
  %PY% "%~dp0disk_recover_tool.py" --list-drives
  echo.
  pause
  goto ask
)

rem 숫자만 입력하면 물리 디스크 번호로 해석
echo %SRC%| findstr /r "^[0-9][0-9]*$" >nul && set "SRC=\\.\PhysicalDrive%SRC%"
rem E:\ 처럼 입력하면 E: 로 정리
if "%SRC:~-1%"=="\" if not "%SRC:~0,4%"=="\\.\" set "SRC=%SRC:~0,-1%"

set "ISDEV=0"
if /i "%SRC:~0,4%"=="\\.\" set "ISDEV=1"
echo %SRC%| findstr /r /i "^[a-z]:$" >nul && set "ISDEV=1"

if "%ISDEV%"=="1" if "%ISADMIN%"=="0" goto needadmin
if "%ISDEV%"=="0" if not exist "%SRC%" goto notfound
goto options

:needadmin
echo.
echo [!] 드라이브/물리 디스크를 읽으려면 관리자 권한이 필요합니다.
choice /c YN /n /m "관리자 권한으로 다시 실행할까요? (Y/N): "
if errorlevel 2 goto ask
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:notfound
echo.
echo [!] 파일을 찾을 수 없습니다. 경로를 다시 확인하세요.
pause
goto ask

:options
echo.
echo 추가 옵션이 있으면 입력하세요 (없으면 그냥 Enter).
echo   예) --partition 1        --check-data        --mode scan --cache D:\scan.pkl
echo   * 드라이브를 읽는 중에는 그 드라이브 안에 파일을 만들지 마세요(캐시/CSV 경로도 다른 디스크로).
set "OPT="
set /p "OPT=옵션: "

echo.
echo 대상: %SRC%
echo 복구한 파일은 반드시 '다른 디스크'에 저장하세요.
echo.
%PY% "%~dp0disk_recover_tool.py" "%SRC%" --browse %OPT%

echo.
choice /c YN /n /m "다른 대상을 다시 지정할까요? (Y/N): "
if errorlevel 2 exit /b 0
goto ask
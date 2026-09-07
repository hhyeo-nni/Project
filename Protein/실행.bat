@echo off
chcp 65001 >nul
REM ============================================================
REM  프로틴 할인 알림 - 일반 실행 (콘솔 창 없이 트레이에만 상주)
REM  더블클릭으로 실행하세요.
REM ============================================================

REM 스크립트가 있는 폴더로 작업 디렉터리를 고정한다
REM (폴더 경로에 한글/공백이 있어도 안전하게 동작)
cd /d "%~dp0"

REM 콘솔 창 없이 실행하려면 pythonw(윈도우용 파이썬)를 우선 사용한다.
REM pythonw가 없으면 python으로 대체 실행한다(이 경우 콘솔 창이 함께 뜬다).
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0main.py"
) else (
    echo [안내] pythonw를 찾지 못해 python으로 실행합니다. 콘솔 창이 함께 뜹니다.
    start "" python "%~dp0main.py"
)

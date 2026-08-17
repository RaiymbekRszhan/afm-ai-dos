@echo off
chcp 65001 >nul
setlocal enableextensions
REM ============================================================================
REM  Ai-dos — автозапуск казахского TTS на WINDOWS + правило брандмауэра.
REM  Запустить ОДИН РАЗ ОТ ИМЕНИ АДМИНИСТРАТОРА, из корня проекта, ПОСЛЕ того как
REM  omni_server\run.bat отработал руками:
REM      omni_server\install-autostart.bat
REM
REM  ⚠️ Задача ставится с запуском ПРИ СТАРТЕ СИСТЕМЫ от имени SYSTEM, а НЕ в
REM     папку «Автозагрузка». Автозагрузка срабатывает при ВХОДЕ ПОЛЬЗОВАТЕЛЯ:
REM     после перезагрузки сервер молчал бы, пока кто-нибудь не залогинится.
REM     Ровно на этом обжигались киоски (см. deploy\install-autostart.bat).
REM
REM  Сообщения на латинице намеренно (см. setup.bat).
REM ============================================================================

net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Run this file as Administrator ^(right click - Run as administrator^).
  pause
  exit /b 1
)

set "ROOT=%~dp0.."
pushd "%ROOT%" >nul
set "ROOT=%CD%"
popd >nul

set "TASK=AiDosOmniTTS"
set "PORT=8993"

echo [1/2] Registering scheduled task "%TASK%" ...
REM Вывод перенаправляем в файл: у задачи планировщика нет консоли, а в логе
REM сервис печатает время синтеза каждого куска — это единственный способ
REM увидеть, укладывается он в киосковые задержки или нет.
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
schtasks /Create /TN "%TASK%" ^
  /TR "cmd /c \"\"%ROOT%\omni_server\run.bat\" >> \"%ROOT%\logs\omni.log\" 2>&1\"" ^
  /SC ONSTART /RU SYSTEM /RL HIGHEST /F || (
  echo [ERROR] Could not create the task.
  pause
  exit /b 1
)

echo [2/2] Opening TCP %PORT% for the local network only ...
REM Не "любой адрес": сервис без аутентификации, наружу его пускать нельзя.
netsh advfirewall firewall delete rule name="Ai-dos OmniVoice TTS" >nul 2>&1
netsh advfirewall firewall add rule name="Ai-dos OmniVoice TTS" ^
  dir=in action=allow protocol=TCP localport=%PORT% profile=any ^
  remoteip=LocalSubnet,10.10.42.0/24,192.168.0.0/16 || (
  echo [ERROR] Could not add the firewall rule.
  pause
  exit /b 1
)

REM Сон машины = казахский голос пропал на всём флоте. Гасим засыпание и
REM гибернацию (экран пусть тухнет — это не мешает).
powercfg -change -standby-timeout-ac 0 >nul 2>&1
powercfg -change -hibernate-timeout-ac 0 >nul 2>&1
powercfg -change -disk-timeout-ac 0 >nul 2>&1

echo.
echo [OK] Installed.
echo     Start now:   schtasks /Run /TN "%TASK%"
echo     Status:      schtasks /Query /TN "%TASK%"
echo     Remove:      schtasks /Delete /TN "%TASK%" /F
echo     Check:       curl http://localhost:%PORT%/health
pause
endlocal

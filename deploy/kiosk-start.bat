@echo off
setlocal enableextensions
REM ============================================================================
REM  Ai-dos — запуск КИОСКА (Windows). Двойной клик открывает страницу
REM  видео-аватара (video_ui) в полноэкранном kiosk-режиме браузера.
REM
REM  СХЕМА: бэкенд (оркестратор + RAG + video_ui) крутится на Linux-сервере АФМ.
REM  Этот .bat НИЧЕГО не устанавливает и не поднимает сервисы — он лишь открывает
REM  браузер на video_ui сервера. Сервер должен быть уже запущен (run_api.sh /
REM  systemd на Linux-машине).
REM
REM  КАК ПОЛЬЗОВАТЬСЯ:
REM    1) Впиши в строку SERVER ниже IP (или имя) Linux-сервера АФМ, где :8100.
REM    2) Скопируй этот файл на рабочий стол киоска.
REM    3) Двойной клик — либо помести ярлык в автозагрузку:
REM       Win+R -> shell:startup -> положить сюда ярлык на этот .bat (запуск при входе).
REM
REM  ⚠️ МИКРОФОН по LAN-http: браузер разрешает доступ к микрофону только в
REM     secure-context (https или localhost). Для http://LAN это обходится флагами
REM     --unsafely-treat-insecure-origin-as-secure (+ --user-data-dir) и
REM     --use-fake-ui-for-media-devices (авто-разрешение, микрофон РЕАЛЬНЫЙ).
REM     Правильнее — отдавать video_ui по HTTPS (тогда флаги не нужны), но для
REM     LAN-пилота флагов достаточно.
REM ============================================================================

REM ===== НАСТРОЙ ЭТО =====
set "SERVER=192.168.165.10"
set "PORT=8100"
REM =======================

set "ORIGIN=http://%SERVER%:%PORT%"
set "URL=%ORIGIN%/"

REM --- Ждём, пока бэкенд поднимется (киоск мог включиться раньше сервера) ---
where curl >nul 2>nul || goto launch
echo [kiosk] waiting for backend %URL% ...
:waitloop
curl -s -o nul --max-time 3 "%URL%"
if errorlevel 1 (
  timeout /t 3 /nobreak >nul
  goto waitloop
)
echo [kiosk] backend is up, opening browser...

:launch
REM --- Ищем браузер: сначала Edge (есть на Windows 10/11), потом Chrome ---
set "BROWSER="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER (
  echo [kiosk] ERROR: Edge or Chrome not found. Install a browser.
  pause
  exit /b 1
)

REM Флаги Chromium (Edge и Chrome одинаковые):
REM   --kiosk                                     полноэкранный режим без рамок
REM   --user-data-dir=...                         отдельный профиль (нужен для флага ниже)
REM   --unsafely-treat-insecure-origin-as-secure  разрешает микрофон на http://LAN
REM   --use-fake-ui-for-media-devices             авто-разрешение микрофона (реального)
REM   --autoplay-policy=no-user-gesture-required  озвучка/видео играют без клика
REM   --disable-pinch / --overscroll...           тач-экран: без зума/свайпа-назад
start "" "%BROWSER%" ^
  --kiosk ^
  --user-data-dir="%LOCALAPPDATA%\AidosKiosk" ^
  --unsafely-treat-insecure-origin-as-secure=%ORIGIN% ^
  --use-fake-ui-for-media-devices ^
  --autoplay-policy=no-user-gesture-required ^
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble ^
  --disable-pinch --overscroll-history-navigation=0 ^
  --check-for-update-interval=31536000 ^
  "%URL%"

endlocal
exit /b 0

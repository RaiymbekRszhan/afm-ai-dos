@echo off
chcp 65001 >nul
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
REM    1) Впиши в блок «НАСТРОЙ ЭТО» адрес сервера и порт, где отдаётся video_ui.
REM    2) Скопируй этот файл на рабочий стол киоска.
REM    3) Двойной клик — либо помести ярлык в автозагрузку:
REM       Win+R -> shell:startup -> положить сюда ярлык на этот .bat (запуск при входе).
REM    Выход из киоска: закрыть ЭТО консольное окно (Alt+F4 по браузеру не поможет —
REM    скрипт поднимет его снова, это и есть режим «киоск 24/7»).
REM
REM  ⚠️ ФАЙЛ ДОЛЖЕН БЫТЬ С CRLF-переводами строк. cmd.exe спотыкается на метках,
REM     goto и блоках if(...) в файлах с LF. В репозитории это держит .gitattributes
REM     (*.bat text eol=crlf) — при правке из macOS/Linux не «выпрямляй» переводы.
REM
REM  ⚠️ МИКРОФОН по LAN-http: браузер разрешает доступ к микрофону только в
REM     secure-context (https или localhost). Для http://LAN это обходится флагами
REM     --unsafely-treat-insecure-origin-as-secure (+ --user-data-dir) и
REM     --use-fake-ui-for-media-devices (разрешение выдаётся автоматически, микрофон
REM     РЕАЛЬНЫЙ — берётся устройство ввода, выбранное в Windows по умолчанию;
REM     страница не задаёт deviceId, поэтому меняешь дефолт в Windows -> меняется
REM     микрофон киоска). Правильнее — отдавать video_ui по HTTPS (тогда флаги не
REM     нужны), но для LAN-пилота флагов достаточно.
REM ============================================================================

REM ===== НАСТРОЙ ЭТО =====
set "SERVER=10.10.42.44"
set "PORT=80"
REM Параметры страницы. kiosk=1 — режим киоска (крупные кнопки, бейдж языка, QR
REM на груди аватара, полный экран по первому тапу). БОЛЬШЕ НИЧЕГО НЕ НУЖНО, в
REM т.ч. на вертикальном экране: область видео уже подогнана под наши ролики
REM (1080x1152 с вшитыми полями по 150px -> полезный кадр 1080/852, cover срезает
REM ровно поля). Параметр ar=<ш>/<в> ПЕРЕБИВАЕТ эту подгонку — ставить его нужно
REM только если ЗАМЕНИЛИ ролики на другие по форме, иначе аватара обрежет по бокам.
REM Потоковая озвучка (первый звук через ~1 с вместо ~11 на казахском) — &stream=1.
REM Кавычки в set "..." уже защищают & — экранировать его через ^ НЕ надо
REM (иначе каретка попадёт в сам URL и страница получит мусорный параметр).
set "PAGE=?kiosk=1"

REM --- Номер точки: попадает в логи сервера полем kiosk -----------------------
REM Файл на всех 20 киосках ОДИН И ТОТ ЖЕ — номер берётся из имени машины
REM (%COMPUTERNAME%), редактировать на каждой точке нечего. Если имена безликие
REM (WIN-8KJ2...), положи рядом с этим .bat файл kiosk-id.txt с одной строкой
REM вроде astana-01 — он перебьёт имя машины.
set "KIOSK_ID=%COMPUTERNAME%"
if exist "%~dp0kiosk-id.txt" set /p KIOSK_ID=<"%~dp0kiosk-id.txt"
REM Сколько раз (по 3 с) ждать бэкенд перед тем, как открыть браузер всё равно.
set "WAIT_TRIES=60"
REM =======================

REM Порт 80 — дефолтный для http: Chrome нормализует origin и отбрасывает его.
REM Если оставить ":80" в --unsafely-treat-insecure-origin-as-secure, флаг НЕ
REM сматчится и микрофона не будет.
if "%PORT%"=="80" (set "ORIGIN=http://%SERVER%") else (set "ORIGIN=http://%SERVER%:%PORT%")
set "URL=%ORIGIN%/%PAGE%&id=%KIOSK_ID%"

REM --- Ждём, пока бэкенд поднимется (киоск мог включиться раньше сервера) ------
REM Проверяем /health, а НЕ /: страница отдаётся статикой и вернёт 200, даже когда
REM оркестратор лежит, — тогда киоск открылся бы «живым», но без ответов.
where curl >nul 2>nul || goto launch
set /a TRY=0
echo [kiosk] waiting for backend %ORIGIN%/health ...
:waitloop
curl -s -o NUL --max-time 3 "%ORIGIN%/health"
if not errorlevel 1 goto ready
set /a TRY+=1
if %TRY% GEQ %WAIT_TRIES% (
  echo [kiosk] backend is silent after %WAIT_TRIES% tries - opening browser anyway
  goto launch
)
timeout /t 3 /nobreak >nul
goto waitloop
:ready
echo [kiosk] backend is up, opening browser...

:launch
REM --- Ищем браузер: сначала Chrome, потом Edge (есть на Windows 10/11) --------
set "BROWSER="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER (
  echo [kiosk] ERROR: Chrome or Edge not found. Install a browser.
  pause
  exit /b 1
)

REM Экран киоска не должен гаснуть. Требует запуска от администратора, поэтому
REM по умолчанию ВЫКЛЮЧЕНО — раскомментируй, если скрипту можно менять питание:
REM powercfg /change monitor-timeout-ac 0
REM powercfg /change standby-timeout-ac 0

echo [kiosk] opening "%URL%"

REM Флаги Chromium (Edge и Chrome одинаковые):
REM   --kiosk                                     полноэкранный режим без рамок
REM   --app=<URL>                                 окно без адресной строки и вкладок
REM   --user-data-dir=...                         отдельный профиль (нужен для флага ниже
REM                                               и чтобы не перехватывался уже открытый Chrome)
REM   --unsafely-treat-insecure-origin-as-secure  разрешает микрофон на http://LAN
REM   --use-fake-ui-for-media-devices             авто-разрешение микрофона (устройство —
REM                                               то, что выбрано в Windows по умолчанию)
REM   --autoplay-policy=no-user-gesture-required  озвучка/видео играют без клика
REM   --kiosk-printing                            бланк уходит на принтер по умолчанию БЕЗ
REM                                               диалога печати (иначе он повиснет на экране)
REM   --no-first-run --no-default-browser-check   без мастера приветствия поверх киоска
REM   --disable-pinch / --overscroll...           тач-экран: без зума/свайпа-назад
:relaunch
start "" /wait "%BROWSER%" ^
  --kiosk ^
  --app="%URL%" ^
  --user-data-dir="%LOCALAPPDATA%\AidosKiosk" ^
  --unsafely-treat-insecure-origin-as-secure=%ORIGIN% ^
  --use-fake-ui-for-media-devices ^
  --autoplay-policy=no-user-gesture-required ^
  --kiosk-printing ^
  --no-first-run --no-default-browser-check ^
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble ^
  --disable-pinch --overscroll-history-navigation=0 ^
  --check-for-update-interval=31536000

REM Браузер закрыли (Alt+F4, сбой, обновление) — киоск не должен оставаться пустым.
echo [kiosk] browser exited, restarting in 3s (close this window to stop)
timeout /t 3 /nobreak >nul
goto relaunch

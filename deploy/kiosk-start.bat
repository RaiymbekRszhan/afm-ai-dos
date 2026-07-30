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
REM Потоковая озвучка ВКЛЮЧЕНА по умолчанию самой страницей (аватар начинает
REM говорить после первого куска, а не после полного синтеза). Здесь ничего
REM дописывать не нужно; принудительно выключить можно параметром &stream=0.
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
REM Файла нет = точка уедет в логи именем машины, и в отчёте её не опознать.
REM Ругаемся в консоль, чтобы это заметили при установке, а не через месяц
REM при разборе статистики. Однострочные if — блоки if(...)else(...) в cmd
REM ломаются, если файл когда-нибудь доедет с LF.
if not exist "%~dp0kiosk-id.txt" echo [kiosk] WARNING: kiosk-id.txt not found - logs will show "%COMPUTERNAME%"

REM --- Пропуск точки: сервер сверяет имя с ключом ---------------------------
REM Без ключа `id` — просто подпись, и отключённый регион обошёл бы рубильник,
REM убрав параметр из ярлыка. Ключ у каждого региона СВОЙ, лежит в kiosk-key.txt
REM рядом с этим файлом (кладём в архив при сборке).
set "KIOSK_KEY="
if exist "%~dp0kiosk-key.txt" set /p KIOSK_KEY=<"%~dp0kiosk-key.txt"
if not exist "%~dp0kiosk-key.txt" echo [kiosk] WARNING: kiosk-key.txt not found - server may refuse this kiosk
REM Сколько раз (по 3 с) ждать бэкенд перед тем, как открыть браузер всё равно.
set "WAIT_TRIES=60"
REM =======================

REM Порт 80 — дефолтный для http: Chrome нормализует origin и отбрасывает его.
REM Если оставить ":80" в --unsafely-treat-insecure-origin-as-secure, флаг НЕ
REM сматчится и микрофона не будет.
if "%PORT%"=="80" (set "ORIGIN=http://%SERVER%") else (set "ORIGIN=http://%SERVER%:%PORT%")
set "URL=%ORIGIN%/%PAGE%&id=%KIOSK_ID%"
if defined KIOSK_KEY set "URL=%URL%&key=%KIOSK_KEY%"

REM --- Ждём, пока бэкенд поднимется (киоск мог включиться раньше сервера) ------
REM Проверяем /health, а НЕ /: страница отдаётся статикой и вернёт 200, даже когда
REM оркестратор лежит, — тогда киоск открылся бы «живым», но без ответов.
REM
REM ⚠️ Проверяем через PowerShell, а НЕ через curl. curl НЕ использует системный
REM прокси Windows, а браузер использует: в регионе 30.07 Chrome открывал адрес
REM без проблем, а `.bat` вечно висел на строке waiting for backend. Проверка
REM обязана ходить тем же путём, что и браузер, иначе она проверяет не то.
REM Весь цикл ожидания — ВНУТРИ одного вызова PowerShell: запускать процесс
REM 60 раз дорого (~1 с только на старт каждого).
echo [kiosk] waiting for backend %ORIGIN%/health ...
powershell -NoProfile -Command "for ($i=0; $i -lt %WAIT_TRIES%; $i++) { try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%ORIGIN%/health'; exit 0 } catch { Start-Sleep -Seconds 3 } }; exit 1"
if %ERRORLEVEL% EQU 0 goto ready
REM ⚠️ Код 1 = бэкенд молчал всё время ожидания. Любой ДРУГОЙ код (9009 «нет
REM команды», запрет политикой) = проверить нечем — тогда просто открываем
REM браузер, а не отказываемся работать: отказ в безопасную сторону.
if %ERRORLEVEL% EQU 1 echo [kiosk] backend is silent after %WAIT_TRIES% tries - opening browser anyway
goto launch
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

REM --- Второй экземпляр киоска не запускаем -----------------------------------
REM Если браузер с профилем AidosKiosk УЖЕ работает, новый chrome.exe просто
REM передаёт ему команду и завершается САМ — мгновенно. Ниже стоит /wait, он
REM тут же возвращается, скрипт думает «браузер закрыли» и запускает снова:
REM новое окно каждые 3 секунды (жалоба 30.07 — киоск был поднят автозапуском,
REM а .bat кликнули руками). Поэтому сначала спрашиваем, не запущен ли он.
REM ⚠️ Фильтр по ИМЕНИ процесса обязателен. Без него проверка находила саму
REM себя: строка *AidosKiosk* лежит в командной строке ТОГО ЖЕ powershell.exe,
REM который её ищет, — и киоск не запускался НИКОГДА (30.07, поймано на точке).
powershell -NoProfile -Command "$b = @('chrome.exe','msedge.exe'); $p = Get-CimInstance Win32_Process | Where-Object { $b -contains $_.Name -and $_.CommandLine -like '*AidosKiosk*' }; if ($p) { exit 1 } else { exit 0 }"
REM ⚠️ Именно EQU 1, а не `if errorlevel 1`: последнее истинно для ЛЮБОГО кода
REM >= 1, включая 9009 «команда не найдена». На машине с заблокированным
REM PowerShell киоск решил бы «уже запущен» и не стартовал бы ВООБЩЕ. При любой
REM непонятной ошибке проверки идём запускать браузер — это безопасная сторона.
if %ERRORLEVEL% EQU 1 goto already

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
set /a FAILS=0
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

REM Браузер вышел. Два разных случая, и путать их нельзя:
REM   * процесс киоска ЖИВ  -> мы не закрылись, а передали команду уже
REM     работающему браузеру. Перезапускать нельзя — получим окно каждые 3 с;
REM   * процесса нет         -> браузер действительно закрыли (Alt+F4, сбой,
REM     обновление), и киоск не должен оставаться пустым.
REM ⚠️ Фильтр по ИМЕНИ процесса обязателен. Без него проверка находила саму
REM себя: строка *AidosKiosk* лежит в командной строке ТОГО ЖЕ powershell.exe,
REM который её ищет, — и киоск не запускался НИКОГДА (30.07, поймано на точке).
powershell -NoProfile -Command "$b = @('chrome.exe','msedge.exe'); $p = Get-CimInstance Win32_Process | Where-Object { $b -contains $_.Name -and $_.CommandLine -like '*AidosKiosk*' }; if ($p) { exit 1 } else { exit 0 }"
REM ⚠️ Именно EQU 1, а не `if errorlevel 1`: последнее истинно для ЛЮБОГО кода
REM >= 1, включая 9009 «команда не найдена». На машине с заблокированным
REM PowerShell киоск решил бы «уже запущен» и не стартовал бы ВООБЩЕ. При любой
REM непонятной ошибке проверки идём запускать браузер — это безопасная сторона.
if %ERRORLEVEL% EQU 1 goto already

REM Подряд идущие мгновенные выходы = браузер не стартует вовсе (сломанный
REM флаг, нет профиля, антивирус). Бесконечно мигать экраном бессмысленно —
REM останавливаемся и показываем, что смотреть.
set /a FAILS+=1
if %FAILS% GEQ 10 (
  echo [kiosk] browser exited 10 times in a row - giving up.
  echo [kiosk] Check the URL/flags printed above, then run this file again.
  pause
  exit /b 1
)
echo [kiosk] browser exited, restarting in 3s (close this window to stop)
timeout /t 3 /nobreak >nul
goto relaunch

:already
echo.
echo [kiosk] Ai-dos kiosk is ALREADY running in another window.
echo [kiosk] Nothing to do here - you can close this window.
echo [kiosk] To restart the kiosk: close the OTHER black window first.
timeout /t 15 /nobreak >nul
exit /b 0

@echo off
rem ============================================================
rem  Ai-dos - запуск киоска (видео-аватар АФМ) в один двойной клик
rem
rem  Бэкенд крутится на МАКЕ по адресу 192.168.18.42:8100.
rem  Если IP Мака поменяется - поправьте его ТОЛЬКО здесь, в HOST_IP
rem  (раньше адрес дублировался ещё и во флаге Chrome ниже отдельным
rem  литералом - при правке одного, но не другого, микрофон молча
rem  переставал работать: origin флага не совпадал с адресом страницы).
rem ============================================================

set "HOST_IP=192.168.18.42"
set "ORIGIN=http://%HOST_IP%:8100"
set "URL=%ORIGIN%/?kiosk=1"

rem --- ищем Chrome в обеих стандартных папках ---
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo Chrome не найден. Установите Google Chrome или впишите путь к chrome.exe.
  pause
  exit /b 1
)

rem --kiosk                       - полный экран без адресной строки
rem --kiosk-printing              - печать сразу на принтер по умолчанию, без диалога
rem --use-fake-ui-for-media-stream- ВЫДАЁТ доступ к микрофону автоматически (в
rem                                 режиме --kiosk окно разрешения не всплывает,
rem                                 иначе микрофон не включить). Берётся микрофон
rem                                 по умолчанию из Windows (Параметры - Звук - Ввод).
rem --unsafely-...secure          - разрешить микрофон на http-адресе киоска
rem --user-data-dir               - отдельный чистый профиль (нужен для флагов выше)
start "" "%CHROME%" --kiosk --kiosk-printing --use-fake-ui-for-media-stream --unsafely-treat-insecure-origin-as-secure="%ORIGIN%" --user-data-dir="C:\aidos-kiosk" "%URL%"

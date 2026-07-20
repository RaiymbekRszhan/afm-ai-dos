@echo off
rem ============================================================
rem  Ai-dos - запуск киоска (видео-аватар АФМ) в один двойной клик
rem
rem  Бэкенд крутится на МАКЕ по адресу 192.168.18.42:8100.
rem  Если IP Мака поменяется - поправьте его в ДВУХ местах ниже.
rem ============================================================

set "URL=http://192.168.18.42:8100/?kiosk=1"

rem --- ищем Chrome в обеих стандартных папках ---
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo Chrome не найден. Установите Google Chrome или впишите путь к chrome.exe.
  pause
  exit /b 1
)

rem --kiosk              - полный экран без адресной строки
rem --kiosk-printing     - печать сразу на принтер по умолчанию, без диалога
rem --unsafely-...secure - разрешить микрофон на http-адресе киоска
rem --user-data-dir      - отдельный чистый профиль (нужен для флага микрофона)
start "" "%CHROME%" --kiosk --kiosk-printing --unsafely-treat-insecure-origin-as-secure="http://192.168.18.42:8100" --user-data-dir="C:\aidos-kiosk" "%URL%"

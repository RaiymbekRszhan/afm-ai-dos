@echo off
chcp 65001 >nul
setlocal enableextensions
REM ============================================================================
REM  Ai-dos — настройка АВТОЗАПУСКА киоска. Запустить ОДИН РАЗ после того, как
REM  убедились, что kiosk-start.bat работает.
REM
REM  Зачем отдельным файлом: на киоске сенсорный экран без клавиатуры, а прежняя
REM  инструкция требовала Win+R -> shell:startup. Здесь то же самое одним запуском.
REM
REM  ⚠️ Windows запускает автозагрузку ПРИ ВХОДЕ ПОЛЬЗОВАТЕЛЯ, а не при подаче
REM     питания. Если система на входе спрашивает пароль, после перезагрузки
REM     киоск останется на экране блокировки и сам не поднимется. Нужен автовход:
REM       Win+R -> netplwiz -> снять «Требовать ввод имени пользователя и пароля».
REM     Если галочки нет: Параметры -> Учётные записи -> Варианты входа ->
REM     выключить «Требовать выполнение входа с Windows Hello», затем netplwiz.
REM
REM  Сообщения на латинице намеренно: консоль Windows в разных локалях рисует
REM  кириллицу по-разному, а эти строки читают при установке на месте.
REM ============================================================================

set "TARGET=%~dp0kiosk-start.bat"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ai-dos.lnk"

if not exist "%TARGET%" (
  echo [setup] ERROR: kiosk-start.bat not found next to this file.
  echo [setup] Unpack ALL files from the archive into ONE folder.
  pause
  exit /b 1
)

echo [setup] creating autostart shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Ai-dos kiosk'; $s.Save()"

if exist "%LNK%" (
  echo [setup] DONE - kiosk will start automatically at Windows logon.
) else (
  echo [setup] FAILED to create the shortcut. Do it manually:
  echo         Win+R  -^>  shell:startup  -^>  put a shortcut to kiosk-start.bat there
)

REM Экран киоска не должен гаснуть — иначе посетитель видит чёрный монитор.
REM Требует прав администратора; без них команды просто ничего не сделают.
echo [setup] disabling screen sleep (needs administrator rights)...
powercfg /change monitor-timeout-ac 0 >nul 2>nul
powercfg /change standby-timeout-ac 0 >nul 2>nul
powercfg /change disk-timeout-ac 0 >nul 2>nul

echo.
echo [setup] IMPORTANT: enable automatic logon, otherwise the kiosk will NOT
echo [setup] come back after a reboot:  Win+R  -^>  netplwiz  -^>  uncheck the
echo [setup] password requirement.
echo.
pause

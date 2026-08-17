@echo off
chcp 65001 >nul
setlocal enableextensions
REM ============================================================================
REM  Ai-dos — запуск казахского TTS (KazakhTTS-OmniVoice) на WINDOWS.
REM  Запускать ИЗ КОРНЯ проекта:   omni_server\run.bat
REM  Порт 8993 — тот, на который ходит оркестратор (OMNI_URL в его .env).
REM
REM  Сообщения на латинице намеренно (см. setup.bat).
REM ============================================================================
cd /d "%~dp0.."

if not exist ".venv-omni\Scripts\activate.bat" (
  echo [ERROR] .venv-omni not found. Run omni_server\setup.bat first.
  exit /b 1
)
call .venv-omni\Scripts\activate.bat

REM Модель и аудио-токенизатор лежат рядом — в сеть за ними ходить не нужно.
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
REM Питон печатает казахские тексты в лог — без этого cmd ловит UnicodeEncodeError.
set "PYTHONUTF8=1"

if not defined OMNI_MODEL   set "OMNI_MODEL=%CD%\models\omnivoice-kazakh"
REM cuda | cpu | auto. Держим cuda ЯВНО: если карта отвалится, сервис упадёт с
REM понятной ошибкой, а не уползёт молча на процессор и не начнёт тормозить киоск.
if not defined OMNI_DEVICE  set "OMNI_DEVICE=cuda"
if not defined OMNI_LANGUAGE set "OMNI_LANGUAGE=Kazakh"

REM Голос: клон с образца сотрудника АФМ. ⚠️ Транскрипт передаём ФАЙЛОМ (@путь):
REM в переменной окружения cmd.exe кириллицу корёжит по локали консоли, а текст
REM обязан ТОЧНО совпадать со сказанным в WAV — иначе клон плывёт.
if not defined OMNI_SPEAKER_WAV  set "OMNI_SPEAKER_WAV=%CD%\refs\ref_kk_omni.wav"
if not defined OMNI_SPEAKER_TEXT set "OMNI_SPEAKER_TEXT=@%CD%\refs\ref_kk_omni.txt"

REM Главный рычаг задержки: время синтеза пропорционально числу шагов. 32 —
REM качество, 16 — вдвое быстрее и почти неотличимо на слух, 8 — уже слышно.
if not defined OMNI_NUM_STEP set "OMNI_NUM_STEP=32"
if not defined OMNI_GUIDANCE set "OMNI_GUIDANCE=2.0"
if not defined OMNI_SPEED    set "OMNI_SPEED=0"

REM 0.0.0.0 — оркестратор живёт на ДРУГОЙ машине. Порт обязан быть открыт в
REM брандмауэре ТОЛЬКО для сети АФМ: аутентификации у сервиса нет.
if not defined OMNI_HOST set "OMNI_HOST=0.0.0.0"
if not defined OMNI_PORT set "OMNI_PORT=8993"

echo [omni] starting on %OMNI_HOST%:%OMNI_PORT% device=%OMNI_DEVICE% steps=%OMNI_NUM_STEP%
python omni_server\server.py
endlocal

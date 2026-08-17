@echo off
chcp 65001 >nul
setlocal enableextensions
REM ============================================================================
REM  Ai-dos — установка казахского TTS (KazakhTTS-OmniVoice) на WINDOWS.
REM  Запускать ИЗ КОРНЯ проекта, ОДИН РАЗ, на машине С ИНТЕРНЕТОМ:
REM      omni_server\setup.bat
REM
REM  ⚠️ ГЛАВНОЕ ОТЛИЧИЕ ОТ LINUX: на Windows обычный `pip install torch` ставит
REM     СБОРКУ БЕЗ CUDA. Видеокарта в ней просто не видна, и синтез идёт на
REM     процессоре в десятки раз медленнее. Поэтому torch ставится ОТДЕЛЬНО с
REM     индекса PyTorch (переменная TORCH_INDEX ниже), ДО остальных пакетов.
REM
REM  Сообщения на латинице намеренно: консоль Windows в разных локалях рисует
REM  кириллицу по-разному, а эти строки читают при установке на месте.
REM ============================================================================

REM --- 0. Python. Нужен 3.10-3.12 (для 3.13 колёса есть не у всех зависимостей).
where py >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python launcher "py" not found. Install Python 3.12 from python.org
  echo         and tick "Add python.exe to PATH".
  exit /b 1
)
set "PYEXE=py -3.12"
%PYEXE% --version >nul 2>&1 || set "PYEXE=py -3.11"
%PYEXE% --version >nul 2>&1 || (
  echo [ERROR] Need Python 3.11 or 3.12. Install it from python.org.
  exit /b 1
)
echo [1/5] Using: & %PYEXE% --version

REM --- 1. Видеокарта. Без CUDA сервис поднимется, но для киоска будет слишком
REM        медленным — предупреждаем сразу, а не после часа установки.
where nvidia-smi >nul 2>&1
if errorlevel 1 (
  echo [WARN] nvidia-smi not found: no NVIDIA GPU driver visible.
  echo        OmniVoice on CPU is too slow for the kiosk. Continue only for a test.
  pause
)

REM --- 2. Свой venv. НЕ СЛИВАТЬ с другими: omnivoice требует transformers 5.3+.
echo [2/5] Creating .venv-omni ...
%PYEXE% -m venv .venv-omni || exit /b 1
call .venv-omni\Scripts\activate.bat
python -m pip install --upgrade pip || exit /b 1

REM --- 3. torch С CUDA. cu124 подходит драйверам 550+; для более старых поставь
REM        cu121. Версию CUDA у драйвера показывает `nvidia-smi` (правый верх).
set "TORCH_INDEX=https://download.pytorch.org/whl/cu124"
echo [3/5] Installing torch from %TORCH_INDEX% ...
pip install torch torchaudio --index-url %TORCH_INDEX% || exit /b 1

REM --- 4. Библиотека инференса + обёртка-сервер.
echo [4/5] Installing omnivoice and server deps ...
pip install omnivoice==0.2.1 soundfile fastapi uvicorn[standard] pydantic huggingface_hub[cli] || exit /b 1

REM --- 5. Модель (2.3 GB) + аудио-токенизатор (768 MB).
REM     ⚠️ Токенизатор — ОТДЕЛЬНАЯ модель. Если её нет в подпапке audio_tokenizer,
REM        сервис полезет за ней в интернет и в офлайне не стартует вовсе.
echo [5/5] Downloading models (~3 GB, be patient) ...
hf download shyngys879/KazakhTTS-OmniVoice --local-dir models\omnivoice-kazakh || exit /b 1
hf download eustlb/higgs-audio-v2-tokenizer --local-dir models\omnivoice-kazakh\audio_tokenizer || exit /b 1

echo.
echo [OK] Done. Check GPU is really used:
echo      .venv-omni\Scripts\python -c "import torch;print(torch.cuda.is_available())"
echo      (must print True)
echo Start the service:  omni_server\run.bat
endlocal

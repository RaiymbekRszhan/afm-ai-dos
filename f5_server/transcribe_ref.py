"""Готовит референс для F5: обрезает refs/ref_ru.wav до <12 c (F5 иначе режет сам),
снимает транскрипт Whisper'ом и добавляет тишину по краям ->
refs/ref_ru_f5.wav + refs/ref_ru_f5_padded.wav + refs/ref_ru.txt.

В бою используется ИМЕННО _padded (см. f5_server/pad_ref.py — там про манеру
«стартовать мгновенно», которую F5 копирует с непаддингованного образца).
Раньше этот шаг был только в голове: скрипт писал ref_ru_f5.wav, а бой читал
ref_ru_f5_padded.wav — сменивший голос по инструкции не слышал никаких изменений.

Нужен ОДИН раз (результат коммитится в репозиторий и едет на офлайн-сеть АФМ).
Перезапускать только если МЕНЯЕШЬ референсный голос. Требует ffmpeg (ставит setup.sh
через imageio-ffmpeg) — это единственное место, где он нужен; сам синтез ffmpeg НЕ требует.

Запуск из корня проекта (venv .venv-f5 активирован):
    python f5_server/transcribe_ref.py [SRC_WAV] [CLIP_SEC]
"""
import sys

import numpy as np
import soundfile as sf

# torchcodec установлен-но-сломан (нет ffmpeg shared-libs); transformers 5.12 безусловно
# импортирует его в ASR-препроцессе -> краш. Отключаем -> пойдёт через ffmpeg-бинарник.
import transformers.pipelines.automatic_speech_recognition as _asr_mod  # noqa: E402
_asr_mod.is_torchcodec_available = lambda: False
import torch  # noqa: E402
from transformers import pipeline  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "refs/ref_ru.wav"
CLIP_SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 11.0
DST_WAV = "refs/ref_ru_f5.wav"
DST_TXT = "refs/ref_ru.txt"
SR = 16000

audio, sr = sf.read(SRC)
if audio.ndim > 1:
    audio = audio.mean(axis=1)
audio = audio.astype(np.float32)
if sr != SR:  # whisper ждёт 16к; на этой машине референс уже 16к, ресэмпл не нужен
    raise SystemExit(f"Ожидал 16 кГц, получил {sr}. Переконвертируй {SRC} в 16к моно.")

clip = audio[: int(CLIP_SEC * SR)]
sf.write(DST_WAV, clip, SR, subtype="PCM_16")
print(f"[ref] {DST_WAV}: {len(clip)/SR:.1f} c")

# Боевой референс — с тишиной по краям (иначе F5 клонирует резкий старт).
from pad_ref import pad_wav, padded_path  # noqa: E402  (лежит рядом, только stdlib)

padded = pad_wav(DST_WAV, padded_path(DST_WAV))
print(f"[ref] {padded}: это и есть F5_REF_AUDIO в бою")

asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
               torch_dtype=torch.float32, device="cpu")
text = asr(DST_WAV, generate_kwargs={"language": "russian", "task": "transcribe"})["text"].strip()
with open(DST_TXT, "w", encoding="utf-8") as f:
    f.write(text + "\n")
print(f"[ref] {DST_TXT}:\n{text}")

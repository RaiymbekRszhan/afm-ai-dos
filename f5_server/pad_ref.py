"""Добавляет тишину по краям референса F5 -> refs/<имя>_padded.wav.

ЗАЧЕМ. F5 клонирует не только тембр, но и МАНЕРУ референса. Если образец
начинается речью с нулевого сэмпла и обрывается без хвоста, модель копирует
привычку «стартовать мгновенно»: атака первого слова съедается, конец фразы
подрезан (замер 2026-07-17). Постобработкой это не чинится — звука там просто
нет. Тишина по краям образца даёт генерации разгон и хвост.

Поэтому в бою используется ИМЕННО padded-референс (`run_api.sh`, `run.sh`),
а `transcribe_ref.py` вызывает этот шаг сам. Отдельно скрипт нужен, если
padded-файл делают из готового `ref_ru_f5.wav` вручную.

Только stdlib — можно запускать из любого venv и на офлайн-сети АФМ.

    python f5_server/pad_ref.py                          # refs/ref_ru_f5.wav -> _padded
    python f5_server/pad_ref.py refs/my_voice.wav        # свой файл
    python f5_server/pad_ref.py refs/my.wav --head 250 --tail 300
"""
from __future__ import annotations

import argparse
import os
import wave

# Подобрано на слух и стоит в бою (refs/ref_ru_f5_padded.wav): 250 мс разгона
# и 300 мс хвоста. Больше — модель начинает заполнять лишнее время звуком.
HEAD_MS = 250
TAIL_MS = 300


def pad_wav(src: str, dst: str, head_ms: int = HEAD_MS, tail_ms: int = TAIL_MS) -> str:
    """Пишет копию WAV с тишиной по краям. Возвращает путь к результату."""
    with wave.open(src, "rb") as r:
        nch, sw, fr = r.getnchannels(), r.getsampwidth(), r.getframerate()
        frames = r.readframes(r.getnframes())
    silence = lambda ms: b"\x00" * (int(fr * ms / 1000) * nch * sw)  # noqa: E731
    with wave.open(dst, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        w.writeframes(silence(head_ms) + frames + silence(tail_ms))
    return dst


def padded_path(src: str) -> str:
    """refs/ref_ru_f5.wav -> refs/ref_ru_f5_padded.wav"""
    base, ext = os.path.splitext(src)
    return f"{base}_padded{ext}"


def main() -> int:
    p = argparse.ArgumentParser(description="Тишина по краям референса F5")
    p.add_argument("src", nargs="?", default="refs/ref_ru_f5.wav")
    p.add_argument("--out", default="", help="куда писать (по умолчанию <имя>_padded.wav)")
    p.add_argument("--head", type=int, default=HEAD_MS, help=f"мс тишины в начале ({HEAD_MS})")
    p.add_argument("--tail", type=int, default=TAIL_MS, help=f"мс тишины в конце ({TAIL_MS})")
    args = p.parse_args()

    if not os.path.isfile(args.src):
        print(f"Нет файла {args.src}")
        return 2
    dst = args.out or padded_path(args.src)
    pad_wav(args.src, dst, args.head, args.tail)
    with wave.open(dst) as r:
        print(f"[ref] {dst}: {r.getnframes() / r.getframerate():.2f} c "
              f"(+{args.head} мс в начале, +{args.tail} мс в конце)")
    print("[ref] это и есть боевой референс: F5_REF_AUDIO в run_api.sh / .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

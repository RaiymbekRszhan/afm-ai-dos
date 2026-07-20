#!/usr/bin/env python3
"""Быстрая проба ElevenLabs TTS (казахский) — сгенерировать звук и послушать.

ЭТО ЧЕРНОВИК ДЛЯ ОЦЕНКИ, не часть пайплайна. Нужен ИНТЕРНЕТ (в сети АФМ его нет —
запускать на Маке через точку телефона). Ключ берётся из переменной окружения
ELEVENLABS_API_KEY (в git не попадает).

Как получить ключ (бесплатно): elevenlabs.io -> регистрация -> внизу слева ваш
профиль -> "API Keys" -> скопировать.

Запуск:
    cd ~/Downloads/STT
    export ELEVENLABS_API_KEY="ваш_ключ"
    python tts_try_eleven.py                 # казахская фраза по умолчанию
    open out_eleven.mp3                       # послушать (macOS)

    # свой текст и голос (id берётся из списка, который печатает скрипт):
    python tts_try_eleven.py --voice <VOICE_ID> "Өз мәтініңіз"
    # другая модель:
    python tts_try_eleven.py --model eleven_turbo_v2_5

Модель по умолчанию — eleven_multilingual_v2 (лучшая многоязычная). Казахский у
ElevenLabs официально не заявлен: слушаем, как он произносит ә, қ, ң, ө, ұ, ү, і.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.elevenlabs.io/v1"

# Казахская фраза с «трудными» буквами (ә, қ, ң, ұ, ы, і) в стиле ответа офицера.
DEFAULT_TEXT = (
    "Сәлеметсіз бе! Мен Қазақстан Республикасы Қаржылық мониторинг "
    "агенттігінің цифрлық қызметкерімін. Сұрағыңызды қойыңыз — заң бойынша "
    "жауап беремін."
)


def _key() -> str:
    k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not k:
        sys.exit("Нет ключа. Сначала: export ELEVENLABS_API_KEY=\"ваш_ключ\"")
    return k


def _get(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={"xi-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def list_voices(key: str) -> list[tuple[str, str, str]]:
    data = _get(API + "/voices", key)
    out = []
    for v in data.get("voices", []):
        gender = (v.get("labels") or {}).get("gender", "")
        out.append((v["voice_id"], v.get("name", "?"), gender))
    return out


def synth(key: str, text: str, voice: str, model: str, out: str) -> None:
    body = json.dumps({
        "text": text,
        "model_id": model,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }).encode("utf-8")
    url = f"{API}/text-to-speech/{voice}?output_format=mp3_44100_128"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            audio = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        sys.exit(f"Ошибка API {e.code}: {detail[:400]}")
    with open(out, "wb") as f:
        f.write(audio)
    print(f"\nГотово: {out}  ({len(audio)} байт)")
    print(f"Послушать:  open {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Проба ElevenLabs TTS (казахский)")
    ap.add_argument("text", nargs="?", default=DEFAULT_TEXT, help="текст для озвучки")
    ap.add_argument("--voice", help="voice_id (по умолчанию — первый из аккаунта)")
    ap.add_argument("--model", default="eleven_multilingual_v2",
                    help="eleven_multilingual_v2 | eleven_turbo_v2_5 | eleven_v3")
    ap.add_argument("--out", default="out_eleven.mp3", help="куда сохранить звук")
    a = ap.parse_args()

    key = _key()
    try:
        voices = list_voices(key)
    except urllib.error.HTTPError as e:
        sys.exit(f"Не удалось получить список голосов (код {e.code}). "
                 "Проверьте ключ.")
    except urllib.error.URLError as e:
        sys.exit(f"Нет сети до api.elevenlabs.io ({e.reason}). "
                 "Нужен интернет (в сети АФМ его нет — запускать через телефон).")

    print("Доступные голоса (id — для --voice):")
    for vid, name, gender in voices[:25]:
        print(f"  {name:22} {gender:8} {vid}")

    voice = a.voice or (voices[0][0] if voices else None)
    if not voice:
        sys.exit("В аккаунте нет доступных голосов.")
    print(f"\nМодель: {a.model}\nГолос:  {voice}\nТекст:  {a.text}")
    synth(key, a.text, voice, a.model, a.out)


if __name__ == "__main__":
    main()

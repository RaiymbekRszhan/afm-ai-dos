"""KazakhTTS-OmniVoice — сервер казахского TTS (замена Spark).

Запускается ОТДЕЛЬНО (venv .venv-omni: transformers >= 5.3), потому что версии
несовместимы с основным API и со Spark (у того transformers 4.46). Основной API
зовёт сюда по HTTP.

Два контракта (оба ждёт app/clients/tts.py -> _omni), как у F5:
    POST /tts  JSON {"text": "...", "language": "kazakh"}      ->  audio/wav
               — как у Spark; голос задан НА СЕРВЕРЕ (env ниже);
    POST /tts  multipart {ref_audio, ref_text, gen_text}        ->  audio/wav
               — голос клонируется с образца, который прислал КЛИЕНТ (так
               оркестратор задаёт голос русского F5 на GPU-ноде АФМ). Промпт
               кэшируется по хешу образца, поэтому платим за токенизацию один
               раз, а не на каждый кусок ответа.
    GET  /health

Модель — shyngys879/KazakhTTS-OmniVoice (файнтюн k2-fsa/OmniVoice на KazakhTTS2).
В отличие от Spark она НЕ авторегрессивная (маскированное декодирование за
OMNI_NUM_STEP шагов), поэтому нет вырожденной генерации «0 семантических токенов»,
из-за которой Spark стохастически отдавал 422. Ретраи всё же оставлены — дёшево.

Настройки (env):
    OMNI_MODEL        путь к папке модели или repo_id. default: models/omnivoice-kazakh
    OMNI_DEVICE       cuda | cpu | mps | auto. default: auto
    OMNI_LANGUAGE     язык подсказки модели. default: Kazakh
    OMNI_PROMPT       .pt с готовым клон-промптом (VoiceClonePrompt.save)
    OMNI_SPEAKER_WAV  образец голоса для КЛОНИРОВАНИЯ (если нет OMNI_PROMPT)
    OMNI_SPEAKER_TEXT транскрипт образца (ОБЯЗАТЕЛЕН при OMNI_SPEAKER_WAV);
                      "@путь" — прочитать из файла в UTF-8 (единственный
                      вменяемый способ на Windows: cmd.exe кириллицу в
                      переменной окружения корёжит по локали консоли)
    OMNI_INSTRUCT     описание голоса (voice design) — если референса нет вовсе
    OMNI_SPEED        множитель темпа (1.0 = как оценит модель)
    OMNI_NUM_STEP     шагов декодирования (качество/скорость). default: 32
    OMNI_GUIDANCE     guidance scale. default: 2.0
    OMNI_PORT         default: 8811
"""
import asyncio
import hashlib
import io
import os
import threading
import time

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

MODEL = os.environ.get("OMNI_MODEL", "models/omnivoice-kazakh")
DEVICE = os.environ.get("OMNI_DEVICE", "auto")
LANGUAGE = os.environ.get("OMNI_LANGUAGE", "Kazakh")
PROMPT_PATH = os.environ.get("OMNI_PROMPT", "")
SPEAKER_WAV = os.environ.get("OMNI_SPEAKER_WAV", "")
SPEAKER_TEXT = os.environ.get("OMNI_SPEAKER_TEXT", "")
if SPEAKER_TEXT.startswith("@"):
    # Транскрипт из файла — тот же приём, что у F5_REF_TEXT в оркестраторе.
    with open(SPEAKER_TEXT[1:], encoding="utf-8") as _f:
        SPEAKER_TEXT = _f.read().strip()
INSTRUCT = os.environ.get("OMNI_INSTRUCT", "")
SPEED = float(os.environ.get("OMNI_SPEED", "0") or 0) or None
NUM_STEP = int(os.environ.get("OMNI_NUM_STEP", "32"))
GUIDANCE = float(os.environ.get("OMNI_GUIDANCE", "2.0"))
PORT = int(os.environ.get("OMNI_PORT", "8811"))
# По умолчанию слушаем ТОЛЬКО localhost. Для GPU-ноды АФМ (оркестратор на другой
# машине) в run.sh/юните ставится OMNI_HOST=0.0.0.0.
HOST = os.environ.get("OMNI_HOST", "127.0.0.1")
# Предел длины текста на запрос: защита от DoS и от бессмысленно долгого синтеза.
# Оркестратор шлёт куски <= TTS_KK_MAX_CHARS (~260).
MAX_CHARS = int(os.environ.get("OMNI_MAX_CHARS", "3000"))
# Повторов при пустом звуке. Модель не авторегрессивная и вырождается заметно
# реже Spark, поэтому по умолчанию хватает одного повтора.
RETRIES = int(os.environ.get("OMNI_RETRIES", "1"))


def _pick_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    if torch.cuda.is_available():
        return "cuda:0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


_device = _pick_device()
# fp16 — только на ускорителях: на CPU половинная точность в разы МЕДЛЕННЕЕ.
_dtype = torch.float16 if _device.startswith(("cuda", "mps")) else torch.float32

print(f"[omni] загружаю KazakhTTS-OmniVoice из {MODEL} на {_device} ({_dtype})...")
from omnivoice import OmniVoice, VoiceClonePrompt  # noqa: E402  (импорт после env)

_model = OmniVoice.from_pretrained(MODEL, device_map=_device, dtype=_dtype)

# Клон-промпт считаем ОДИН раз на старте, а не на каждый кусок: токенизация
# референса стоит секунды, а ответ гражданину режется на 3-5 кусков.
_prompt: "VoiceClonePrompt | None" = None
if PROMPT_PATH:
    _prompt = VoiceClonePrompt.load(PROMPT_PATH)
    print(f"[omni] клон-промпт загружен из {PROMPT_PATH}")
elif SPEAKER_WAV:
    if not SPEAKER_TEXT:
        raise SystemExit(
            "[omni] OMNI_SPEAKER_WAV задан без OMNI_SPEAKER_TEXT: без транскрипта "
            "образца клон получается заметно хуже. Укажи текст (или OMNI_PROMPT)."
        )
    _prompt = _model.create_voice_clone_prompt(SPEAKER_WAV, ref_text=SPEAKER_TEXT)
    print(f"[omni] клон-промпт построен из {SPEAKER_WAV}")
    # Кэшируем рядом с образцом: следующий старт не платит за токенизацию.
    cache = os.path.splitext(SPEAKER_WAV)[0] + ".omni.pt"
    try:
        _prompt.save(cache)
        print(f"[omni] клон-промпт сохранён в {cache} (OMNI_PROMPT для быстрого старта)")
    except OSError as e:
        print(f"[omni] не удалось сохранить клон-промпт: {e!r}")
print("[omni] готово.")

# Один экземпляр модели на процесс; синхронный эндпоинт исполняется в пуле
# потоков. Параллельные generate непредсказуемо расходуют память (OOM на GPU) —
# сериализуем инференс блокировкой (та же схема, что у Spark).
_infer_lock = threading.Lock()

app = FastAPI(title="KazakhTTS-OmniVoice")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "device": _device,
        # cloning — про СЕРВЕРНЫЙ образец (env). Клиентский приходит в multipart
        # и виден по client_refs: сколько чужих образцов уже закэшировано.
        "cloning": _prompt is not None,
        "client_refs": len(_client_prompts),
        "instruct": INSTRUCT or None,
    }


# Клиентские референсы (multipart) — по хешу образца, чтобы не токенизировать его
# на каждый кусок ответа. Клиент у нас один и образец один, поэтому 4 записей с
# запасом; больше — вытесняем самый старый, иначе память утекает на чужих WAV.
_client_prompts: dict[str, object] = {}
_CLIENT_PROMPT_MAX = 4


def _prompt_for_client_ref(audio: bytes, ref_text: str):
    """Клон-промпт для присланного клиентом образца (с кэшем)."""
    key = hashlib.sha256(audio).hexdigest() + "|" + ref_text
    cached = _client_prompts.get(key)
    if cached is not None:
        return cached
    # Читаем из памяти: временные файлы тут не нужны, create_voice_clone_prompt
    # принимает (waveform, sample_rate).
    data, sr = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.mean(axis=1))  # моно: образец может быть стерео
    with _infer_lock:
        prompt = _model.create_voice_clone_prompt((wav, sr), ref_text=ref_text)
    if len(_client_prompts) >= _CLIENT_PROMPT_MAX:
        _client_prompts.pop(next(iter(_client_prompts)))
    _client_prompts[key] = prompt
    print(f"[omni] клон-промпт от клиента посчитан и закэширован ({len(audio)} байт образца)")
    return prompt


def _infer(text: str, prompt=None):
    """Один прогон синтеза. Режимы: клон -> voice design -> авто."""
    kw = {"num_step": NUM_STEP, "guidance_scale": GUIDANCE}
    if SPEED:
        kw["speed"] = SPEED
    prompt = prompt if prompt is not None else _prompt
    if prompt is not None:
        kw["voice_clone_prompt"] = prompt
    elif INSTRUCT:
        kw["instruct"] = INSTRUCT
    # normalize_text=False: числа и юр-аббревиатуры уже раскрыты оркестратором
    # (app/clients/tts.py::_normalize_for_tts) — своя нормализация модели сломала
    # бы казахские числительные и падежи в ссылках на статьи.
    audios = _model.generate(text=text, language=LANGUAGE, normalize_text=False, **kw)
    return audios[0]


@app.post("/tts")
async def tts(request: Request):
    """Разводит два контракта по Content-Type.

    Разбор ручной, а не через pydantic-модель в сигнатуре: один и тот же путь
    должен принимать И JSON, И multipart, а FastAPI на смешанной сигнатуре
    (Form/File + модель) ломает ту ветку, которой нет в запросе.
    """
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        upload = form.get("ref_audio")
        text = (form.get("gen_text") or "").strip()
        ref_text = (form.get("ref_text") or "").strip()
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="Нет файла ref_audio")
        if not ref_text:
            raise HTTPException(status_code=400, detail="Нет ref_text (транскрипт образца)")
        audio_bytes = await upload.read()
        _check_text(text)
        # Токенизация образца блокирующая, как и синтез, — уводим в поток, иначе
        # первый запрос заморозил бы весь event loop сервиса.
        prompt = await asyncio.to_thread(_prompt_for_client_ref, audio_bytes, ref_text)
    else:
        payload = await request.json()
        text = (payload.get("text") or "").strip()
        _check_text(text)
        prompt = None    # голос с сервера (env) либо выбранный моделью
    return await asyncio.to_thread(_synthesize_blocking, text, prompt)


def _check_text(text: str) -> None:
    if not text:
        raise HTTPException(status_code=400, detail="Пустой текст")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"Текст длиннее {MAX_CHARS} символов")


def _synthesize_blocking(text: str, prompt=None):
    last_err: Exception | None = None
    for attempt in range(RETRIES + 1):
        started = time.monotonic()
        try:
            with _infer_lock:
                wav = _infer(text, prompt)
        except torch.cuda.OutOfMemoryError as e:
            # OOM повтором не лечится (только усугубляет) — отдаём сразу.
            torch.cuda.empty_cache()
            print(f"[omni] CUDA OOM: {e!r}")
            raise HTTPException(status_code=503, detail="TTS временно перегружен (нехватка памяти).")
        except Exception as e:
            last_err = e
            print(f"[omni] попытка {attempt + 1}/{RETRIES + 1} не удалась: {e!r}")
            continue
        if wav is None or len(wav) == 0:
            last_err = RuntimeError("синтез вернул пустой звук")
            print(f"[omni] попытка {attempt + 1}/{RETRIES + 1}: пустой звук")
            continue
        secs = len(wav) / _model.sampling_rate
        print(f"[omni] {len(text)} симв. -> {secs:.1f} c звука за {time.monotonic() - started:.1f} c")
        buf = io.BytesIO()
        sf.write(buf, wav, _model.sampling_rate, format="WAV", subtype="PCM_16")
        return Response(content=buf.getvalue(), media_type="audio/wav")
    # Наружу — обобщённое сообщение (сырой текст исключения может содержать пути
    # модели/детали окружения); подробности уже в логах сервиса выше.
    print(f"[omni] исчерпаны попытки ({RETRIES + 1}); последняя ошибка: {last_err!r}")
    raise HTTPException(
        status_code=422,
        detail="OmniVoice не смог озвучить текст.",
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

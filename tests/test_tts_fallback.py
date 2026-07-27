"""Фолбэк TTS и повторы на кусок: отказ движка не должен оставлять гражданина
в тишине.

Почему это есть: 27.07 на стенде казахский вопрос ушёл в ElevenLabs, интернета не
было — гражданин ждал 78 секунд и получил 502, хотя офлайн-Spark был жив рядом.
Теперь при отказе основного движка ответ досинтезируется запасным, а провайдер в
ответе — ФАКТИЧЕСКИЙ (по нему киоск выбирает темп проигрывания).
"""
import asyncio

import pytest

from app.clients import tts
from tests.util import wav_bytes


@pytest.fixture(autouse=True)
def clean_cooldown():
    """Предохранитель — модульное состояние: чистим между тестами."""
    tts._provider_down.clear()
    yield
    tts._provider_down.clear()


def _providers(monkeypatch, primary="eleven", fallback="spark", retries=0):
    monkeypatch.setattr(tts.settings, "tts_kk_provider", primary)
    monkeypatch.setattr(tts.settings, "tts_kk_fallback", fallback)
    monkeypatch.setattr(tts.settings, "tts_retries", retries)


def _record_calls(monkeypatch, fail_for: set[str]):
    """Подменяет диспетчер провайдеров: перечисленные — падают, прочие — отдают WAV."""
    calls: list[str] = []

    async def fake_one(text, language=None, provider=None):
        provider = provider or tts._provider_for(language)
        calls.append(provider)
        if provider in fail_for:
            raise ConnectionError(f"{provider} недоступен")
        return wav_bytes()

    monkeypatch.setattr(tts, "_synthesize_one", fake_one)
    return calls


def test_falls_back_to_offline_provider(monkeypatch):
    """Облако не ответило -> озвучиваем Spark и честно говорим, что это Spark."""
    _providers(monkeypatch)
    calls = _record_calls(monkeypatch, fail_for={"eleven"})

    audio, provider = asyncio.run(tts.synthesize_with_provider("Сәлеметсіз бе.", "kazakh"))
    assert provider == "spark"
    assert audio == wav_bytes()
    assert calls == ["eleven", "spark"]


def test_no_fallback_configured_raises(monkeypatch):
    """Без запасного движка поведение прежнее — ошибка наверх (502 у клиента)."""
    _providers(monkeypatch, fallback="")
    _record_calls(monkeypatch, fail_for={"eleven"})

    with pytest.raises(ConnectionError):
        asyncio.run(tts.synthesize_with_provider("Сәлеметсіз бе.", "kazakh"))


def test_cooldown_skips_broken_provider(monkeypatch):
    """После отказа минуту не дёргаем облако: иначе КАЖДЫЙ вопрос платит таймаут."""
    _providers(monkeypatch)
    calls = _record_calls(monkeypatch, fail_for={"eleven"})

    asyncio.run(tts.synthesize_with_provider("Бірінші сұрақ.", "kazakh"))
    calls.clear()
    _, provider = asyncio.run(tts.synthesize_with_provider("Екінші сұрақ.", "kazakh"))

    assert provider == "spark"
    assert calls == ["spark"]          # облако даже не пробовали


def test_cooldown_expires(monkeypatch):
    """Кулдаун истёк — основной движок снова пробуется (сеть могла вернуться)."""
    _providers(monkeypatch)
    calls = _record_calls(monkeypatch, fail_for=set())   # облако снова живо
    tts._mark_down("eleven")
    monkeypatch.setattr(tts.settings, "tts_fallback_cooldown", 0.0)

    _, provider = asyncio.run(tts.synthesize_with_provider("Сұрақ.", "kazakh"))
    assert provider == "eleven"
    assert calls == ["eleven"]


def test_success_clears_cooldown(monkeypatch):
    """Удачный синтез снимает пометку об отказе."""
    _providers(monkeypatch)
    _record_calls(monkeypatch, fail_for=set())
    tts._mark_down("eleven")
    monkeypatch.setattr(tts.settings, "tts_fallback_cooldown", 0.0)

    asyncio.run(tts.synthesize_with_provider("Сұрақ.", "kazakh"))
    assert "eleven" not in tts._provider_down


def test_chunk_retry_saves_answer(monkeypatch):
    """Единичный сетевой сбой на куске лечится повтором, а не рушит весь ответ."""
    _providers(monkeypatch, primary="spark", fallback="", retries=1)
    monkeypatch.setattr(tts.settings, "tts_kk_provider", "spark")
    attempts = {"n": 0}

    async def flaky(text, language=None, provider=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("сеть моргнула")
        return wav_bytes()

    monkeypatch.setattr(tts, "_synthesize_one", flaky)
    monkeypatch.setattr(tts.asyncio, "sleep", _no_sleep)

    audio, provider = asyncio.run(tts.synthesize_with_provider("Сұрақ.", "kazakh"))
    assert provider == "spark"
    assert audio == wav_bytes()
    assert attempts["n"] == 2


def test_config_error_is_not_retried(monkeypatch):
    """«Провайдер не настроен» повтором не лечится — уходим на фолбэк сразу."""
    _providers(monkeypatch, retries=3)
    attempts = {"n": 0}

    async def not_configured(text, language=None, provider=None):
        attempts["n"] += 1
        if provider == "eleven":
            raise RuntimeError("TTS=eleven, но не заданы ELEVENLABS_API_KEY")
        return wav_bytes()

    monkeypatch.setattr(tts, "_synthesize_one", not_configured)

    _, provider = asyncio.run(tts.synthesize_with_provider("Сұрақ.", "kazakh"))
    assert provider == "spark"
    assert attempts["n"] == 2          # eleven один раз, без повторов, затем spark


def test_fallback_uses_own_chunk_limits(monkeypatch):
    """У запасного движка своя нарезка: eleven держит 800 симв., Spark — 260."""
    _providers(monkeypatch)
    text = " ".join(f"Сөйлем нөмір {i} тексеру үшін." for i in range(1, 40))
    sizes: list[int] = []

    async def fake_one(text_part, language=None, provider=None):
        if provider == "eleven":
            raise ConnectionError("нет интернета")
        sizes.append(len(text_part))
        return wav_bytes()

    monkeypatch.setattr(tts, "_synthesize_one", fake_one)

    asyncio.run(tts.synthesize_with_provider(text, "kazakh"))
    spark_max = max(tts.settings.tts_max_chars, tts.settings.tts_kk_max_chars)
    assert sizes and all(size <= spark_max for size in sizes)


def test_russian_has_no_fallback_by_default():
    """У русского запасного движка нет: F5 — единственный офлайн-движок ru."""
    assert tts._fallback_for("russian") == ""


async def _no_sleep(_seconds):
    return None

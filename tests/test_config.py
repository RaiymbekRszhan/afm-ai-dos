"""tts_enabled — регрессия на находку AUDIT_2026-07-20.md: USE_ELEVEN=1 давал
tts_enabled=False (провайдер "eleven" не входил в допустимые), и /voice молча
отдавал JSON без звука вместо WAV. Покрываем все провайдеры на обоих языках.
"""
from app.config import Settings


def test_tts_enabled_true_for_default_f5_spark():
    assert Settings(tts_provider="f5", tts_kk_provider="spark").tts_enabled is True


def test_tts_enabled_true_for_say():
    assert Settings(tts_provider="say", tts_kk_provider="say").tts_enabled is True


def test_tts_enabled_true_for_eleven_when_configured():
    s = Settings(
        tts_provider="eleven",
        tts_kk_provider="eleven",
        elevenlabs_api_key="key",
        elevenlabs_voice_id="voice",
    )
    assert s.tts_enabled is True


def test_tts_enabled_false_for_eleven_without_credentials():
    # Регрессия: раньше "eleven" не был в списке допустимых ВООБЩЕ (независимо
    # от ключа) — /voice отдавал JSON без звука при USE_ELEVEN=1.
    # Креды зануляем явно: иначе тест ложно падает на машине, где ключ/голос
    # eleven лежат в .env (pydantic читает .env и в конструкторе Settings).
    s = Settings(
        tts_provider="eleven",
        tts_kk_provider="eleven",
        elevenlabs_api_key="",
        elevenlabs_voice_id="",
    )
    assert s.tts_enabled is False


def test_tts_enabled_false_when_only_kk_provider_misconfigured():
    # tts_enabled раньше проверял ТОЛЬКО tts_provider (ru) — kk мог быть битым
    # незамеченно. Теперь оба языковых провайдера должны быть валидны.
    # elevenlabs_* зануляем явно — тест не должен зависеть от .env (см. выше).
    s = Settings(
        tts_provider="f5",
        tts_kk_provider="eleven",
        elevenlabs_api_key="",
        elevenlabs_voice_id="",
    )
    assert s.tts_enabled is False


def test_tts_enabled_true_for_openai_when_base_url_and_model_set():
    s = Settings(
        tts_provider="openai",
        tts_kk_provider="openai",
        tts_base_url="http://localhost:9",
        tts_model="tts-1",
    )
    assert s.tts_enabled is True


def test_tts_enabled_false_for_openai_without_base_url():
    s = Settings(tts_provider="openai", tts_kk_provider="spark")
    assert s.tts_enabled is False


def test_tts_enabled_false_for_unknown_provider():
    s = Settings(tts_provider="does-not-exist", tts_kk_provider="spark")
    assert s.tts_enabled is False

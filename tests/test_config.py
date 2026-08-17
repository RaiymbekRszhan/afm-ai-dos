"""tts_enabled — регрессия на находку AUDIT_2026-07-20.md: USE_ELEVEN=1 давал
tts_enabled=False (провайдер "eleven" не входил в допустимые), и /voice молча
отдавал JSON без звука вместо WAV. Покрываем все провайдеры на обоих языках.
"""
from app.config import Settings


def test_tts_enabled_true_for_f5_spark_with_urls():
    # f5/spark считаются сконфигурированными ТОЛЬКО с непустым адресом сервера.
    assert Settings(
        tts_provider="f5", tts_kk_provider="spark",
        f5_url="http://f5:8810/tts", spark_url="http://spark:8809/tts",
    ).tts_enabled is True


def test_tts_enabled_false_for_f5_without_url():
    # Регрессия N3: раньше f5 считался валидным по одному имени, /health врал
    # "включён", а /voice падал 502 у гражданина. f5_url зануляем явно — тест не
    # должен зависеть от .env на машине разработчика.
    s = Settings(tts_provider="f5", tts_kk_provider="say", f5_url="")
    assert s.tts_enabled is False


def test_tts_enabled_false_for_spark_without_url():
    s = Settings(tts_provider="say", tts_kk_provider="spark", spark_url="")
    assert s.tts_enabled is False


def test_tts_enabled_true_for_omni_with_url():
    assert Settings(
        tts_provider="f5", tts_kk_provider="omni",
        f5_url="http://f5:8810/tts", omni_url="http://omni:8993/tts",
    ).tts_enabled is True


def test_tts_enabled_false_for_omni_without_url():
    # То же правило, что у f5/spark: имя провайдера без адреса — не конфигурация.
    # Иначе /health рапортует «включён», а казахский падает 502 уже у гражданина.
    s = Settings(tts_provider="say", tts_kk_provider="omni", omni_url="")
    assert s.tts_enabled is False


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

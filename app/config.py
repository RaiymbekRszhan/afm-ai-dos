from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки берутся из переменных окружения или файла .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    llm_base_url: str = "http://192.168.165.2:8901/v1"
    llm_model: str = "qwen3-next-80b-instruct"
    llm_api_key: str = "EMPTY"
    llm_temperature: float = 0.2
    llm_top_p: float = 0.95
    llm_max_tokens: int = 512

    # STT
    stt_url: str = "http://192.168.165.2:8004/transcribe"
    stt_default_language: str = "russian"
    # LLM-постобработка: исправляет ошибки распознавания перед поиском по базе.
    # Это ДОП. вызов LLM (медленный сервер АФМ), поэтому по умолчанию только для
    # казахского (там STT шумнее, WER ~12%); русский STT АФМ точнее, и коррекция
    # чаще лишь добавляет задержку. stt_correction — мастер-выключатель;
    # stt_correction_langs — для каких языков применять ("ru,kk"; пусто = выкл).
    stt_correction: bool = True
    stt_correction_langs: str = "kk"
    # Казахский STT: локальный Whisper-kk вместо сервера АФМ
    stt_kk_use_whisper: bool = True
    whisper_kk_model: str = "shyngys879/kazakh-whisper-large-v3-turbo"
    whisper_device: str = "auto"  # auto | cpu | mps | cuda

    # TTS — каждый язык на своём сервере (по HTTP). Провайдеры:
    #   f5     — русский (F5-TTS_RUSSIAN, F5_URL) — с ударениями (RUAccent)
    #   spark  — казахский (Spark-сервер, SPARK_URL)
    #   say    — системный голос macOS (только локальная отладка)
    #   openai — внешний OpenAI-совместимый /audio/speech сервер
    tts_provider: str = "f5"        # русский/по умолчанию: f5 | say | openai
    tts_kk_provider: str = "spark"  # казахский: spark | say | openai
    f5_url: str = ""  # HTTP-эндпоинт F5-TTS-сервера (русский TTS)
    spark_url: str = ""  # HTTP-эндпоинт Spark-TTS-сервера (казахский TTS)
    tts_format: str = "wav"
    # Длинное предложение (> ~182 симв.) TTS может обрезать — слишком длинные
    # предложения режем по словам на части не длиннее этого лимита.
    tts_max_chars: int = 180
    # Целые предложения группируем в куски до этого размера и отдаём F5 одним
    # запросом: F5 сам делит их на предложения с естественными паузами, поэтому
    # крупный кусок звучит слитно (меньше швов/обрывов), чем поштучная склейка.
    tts_group_chars: int = 600
    # Пауза (мс) на стыке склеиваемых кусков. Небольшая — паузы между предложениями
    # F5 делает сам; этот зазор нужен лишь между крупными кусками.
    tts_gap_ms: int = 80

    # OpenAI-совместимый TTS (если tts_provider=openai)
    tts_base_url: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    tts_api_key: str = "dummy"

    # RAG: внешний сервис (ragsvc на :8077, свой venv) — основной источник ответа.
    rag_url: str = "http://localhost:8077/ask"
    rag_timeout: float = 120.0

    # Загрузка аудио: предел размера файла (защита от перегруза памяти).
    max_upload_mb: int = 25

    @property
    def tts_enabled(self) -> bool:
        if self.tts_provider == "openai":
            return bool(self.tts_base_url and self.tts_model)
        return self.tts_provider in ("f5", "spark", "say")


settings = Settings()

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
    # Явный таймаут запроса к LLM (сек). Без него openai-SDK ждёт 600 с — и
    # LLM-коррекция STT может подвесить весь /voice на минуты.
    llm_timeout: float = 90.0

    # STT
    # 2026-07-03: АФМ перенёс сервис распознавания с 8004 на 8804
    stt_url: str = "http://192.168.165.2:8804/transcribe"
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
    #   eleven — ElevenLabs (облако, ELEVENLABS_*) — казахский на eleven_v3 звучит
    #            чисто; ⚠️ НУЖЕН ИНТЕРНЕТ (в сети АФМ его нет — только если IT
    #            откроют доступ к api.elevenlabs.io), платно по символам
    #   say    — системный голос macOS (только локальная отладка)
    #   openai — внешний OpenAI-совместимый /audio/speech сервер
    tts_provider: str = "f5"        # русский/по умолчанию: f5 | say | openai | eleven
    tts_kk_provider: str = "spark"  # казахский: spark | eleven | say | openai
    f5_url: str = ""  # HTTP-эндпоинт F5-TTS-сервера (русский TTS)
    # ElevenLabs (провайдер eleven). Ключ — из .env, в git не попадает. voice_id
    # выбирается в аккаунте (мужской для офицера). eleven_v3 — самая многоязычная
    # модель (казахский чистый); если недоступна на тарифе — eleven_multilingual_v2.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_v3"
    elevenlabs_url: str = "https://api.elevenlabs.io/v1"
    # eleven сам держит длинный текст — не рубим на мелкие куски, как F5 (меньше швов).
    elevenlabs_max_chars: int = 800
    # Клиентский референс для F5-серверов, которые ждут ref_audio+ref_text в КАЖДОМ
    # запросе (multipart /tts — напр. GPU-сервер АФМ на :8991). Если f5_ref_audio
    # задан — шлём multipart {ref_audio, ref_text, gen_text}; иначе — прежний
    # JSON {text, language} (локальный f5_server, где референс задан на сервере).
    f5_ref_audio: str = ""   # путь к WAV-референсу тембра (для multipart-режима)
    f5_ref_text: str = ""    # транскрипт референса; "@путь" — прочитать из файла
    spark_url: str = ""  # HTTP-эндпоинт Spark-TTS-сервера (казахский TTS)
    tts_format: str = "wav"
    # Длинное предложение (> ~182 симв.) TTS может обрезать — слишком длинные
    # предложения режем по словам на части не длиннее этого лимита.
    tts_max_chars: int = 180
    # То же для Spark (казахский). 180 — это предел F5 (он обрезает предложение
    # длиннее ~182 симв.); Spark принимает до 3000, и жёсткие 180 ему только вредят:
    # юридическое предложение (330+ симв. — норма для кодексов) рубится ПОСРЕДИ
    # фразы, и Spark комкает хвост, озвучивая огрызок как законченную реплику.
    # 260 — не «куски крупнее», а ЗАПАС, чтобы разрез дотянулся до ближайшей
    # запятой (в замере первая запятая стояла на 182-м символе). Выше не ставим:
    # время синтеза растёт квадратично (100 симв. -> 18 c, 330 симв. -> 117 c).
    tts_kk_max_chars: int = 260
    # Целые предложения группируем в куски до этого размера и отдаём F5 одним
    # запросом. Было 600 («крупный кусок — меньше швов»), но у multipart-F5 АФМ
    # бюджет длительности жёсткий (~0.054 с/символ), а точечный паддинг (_f5)
    # даёт фиксированные ~0.6 c запаса НА КУСОК: на куске 600 симв. это 2%
    # слабины — концы кусков и длинные числительные снова зажёвывались
    # (2026-07-17). На 240 запас ~5% на кусок, каждый конец получает свои
    # 0.6 c, а паузы на стыках ставит наша склейка (TTS_F5_GAP_MS 600/300).
    tts_group_chars: int = 240
    # Пауза (мс) на стыке склеиваемых кусков — ПОЛНАЯ, когда кусок кончился точкой;
    # на запятой берётся половина (см. _gap_ms_after): там середина предложения.
    # Было 80 мс в расчёте на то, что F5 оставит свой хвост тишины — он его
    # подрезает, и стык звучал так, будто второй кусок влезает, не дав первому
    # договорить. 300 мс — естественная пауза между фразами в живой речи.
    tts_gap_ms: int = 300
    # Пауза для F5 (русский) — своя. История подбора (2026-07-16..17): 300 —
    # куски «наезжали»; 600 — затянуто (паддинг добавил куску свой хвост тишины);
    # 400 — всё ещё плавало 0.5–0.7 c, потому что тишина по КРАЯМ куска у F5
    # случайная (41–157 мс). Теперь края ровняются до 30 мс (_trim_edge_silence),
    # и пауза на стыке = ровно этот зазор (+60 мс краёв). 350 на живом демо
    # всё ещё звучало затянуто (отзыв 2026-07-17) -> 280 после точки, 140 после
    # запятой (~340/200 слышимых). Казахские 300/150 (tts_gap_ms) не трогаем.
    tts_f5_gap_ms: int = 280

    # OpenAI-совместимый TTS (если tts_provider=openai)
    tts_base_url: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    tts_api_key: str = "dummy"

    # RAG: внешний сервис (ragsvc на :8077, свой venv) — основной источник ответа.
    rag_url: str = "http://localhost:8077/ask"
    # ДОЛЖЕН быть >= LLM_TIMEOUT в rag/ragsvc/config.py (по умолчанию там 240 с) +
    # запас на эмбеддинг/rerank/сеть. Раньше здесь стояло 120 — короче внутреннего
    # таймаута RAG-сервиса: при медленном LLM АФМ httpx на этой стороне обрывал
    # запрос раньше, чем RAG успевал сам честно дождаться ответа, и гражданин
    # получал 502 там, где терпеливое ожидание дало бы настоящий ответ. Если
    # меняешь LLM_TIMEOUT в rag/.env — подними и это значение.
    rag_timeout: float = 260.0
    # Предел длины вопроса ("/chat"): зеркалит MAX_QUESTION_CHARS в rag/.env, чтобы
    # оркестратор отклонял слишком длинный вопрос СРАЗУ (400), не тратя HTTP-запрос
    # к RAG, который его всё равно отклонит (422). Если поменяешь один — поменяй и
    # второй (rag/ragsvc/config.py MAX_QUESTION_CHARS), иначе оркестратор может
    # пропускать то, что RAG потом отклонит с менее понятным для клиента 422.
    max_question_chars: int = 2000
    # Предел длины текста на "/speak" (прямой синтез без RAG) — раньше был не
    # ограничен вовсе: _split_for_tts резал на куски без потолка на их число, и
    # запрос мог надолго занять единственный TTS/GPU-ресурс без семафора (тот
    # защищает только /voice). Голосовой ответ на практике укладывается в пару
    # тысяч символов (llm_max_tokens=512); 4000 — заметный запас сверху.
    max_speak_chars: int = 4000

    # Загрузка аудио: предел размера файла (защита от перегруза памяти).
    max_upload_mb: int = 25
    # Сколько запросов /voice обрабатывать одновременно. /voice — самый дорогой
    # путь (Whisper + до 10 вызовов TTS, минуты CPU/GPU); без лимита пачка
    # параллельных запросов кладёт TTS/GPU-ноду. Остальные ждут в очереди.
    max_concurrent_voice: int = 2
    # Известные провайдеры TTS без спец-проверки конфига (openai проверяется отдельно).
    _KNOWN_TTS_PROVIDERS = ("f5", "spark", "say", "eleven")

    def _provider_configured(self, provider: str) -> bool:
        if provider == "openai":
            return bool(self.tts_base_url and self.tts_model)
        if provider == "eleven":
            return bool(self.elevenlabs_api_key and self.elevenlabs_voice_id)
        return provider in self._KNOWN_TTS_PROVIDERS

    @property
    def tts_enabled(self) -> bool:
        # Оба языковых провайдера (ru: tts_provider, kk: tts_kk_provider) должны
        # быть валидны — иначе /voice может молча остаться без звука для одного
        # из языков, а settings.tts_enabled это не поймает.
        return self._provider_configured(self.tts_provider) and self._provider_configured(
            self.tts_kk_provider
        )


settings = Settings()

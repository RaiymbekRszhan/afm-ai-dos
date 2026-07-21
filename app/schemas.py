from pydantic import BaseModel, Field

from app.config import settings


class ChatRequest(BaseModel):
    # Зеркалит MAX_QUESTION_CHARS rag/ragsvc (см. app/config.py max_question_chars) —
    # отклоняем слишком длинный вопрос здесь (400), не тратя HTTP-запрос к RAG.
    question: str = Field(..., max_length=settings.max_question_chars)
    top_k: int | None = None
    language: str | None = None  # язык кнопки на экране: russian | kazakh


class ChatResponse(BaseModel):
    answer: str
    # RAG возвращает источники одной строкой (использованные нормы/контекст).
    sources: str


class TranscribeResponse(BaseModel):
    text: str            # итоговый текст (после коррекции, если включена)
    language: str
    raw_text: str | None = None  # сырой вывод STT до коррекции


class SpeakRequest(BaseModel):
    # См. app/config.py max_speak_chars — без предела /speak мог без семафора
    # надолго занять единственный TTS/GPU-ресурс.
    text: str = Field(..., max_length=settings.max_speak_chars)
    language: str | None = None  # russian | kazakh (по умолчанию из настроек)

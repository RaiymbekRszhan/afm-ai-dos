from pydantic import BaseModel


class ChatRequest(BaseModel):
    question: str
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
    text: str
    language: str | None = None  # russian | kazakh (по умолчанию из настроек)

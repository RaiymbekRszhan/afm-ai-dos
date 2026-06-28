"""Текстовые шаги оркестратора. Сейчас — только LLM-коррекция вывода STT.

Источник ответа на вопрос — внешний RAG-сервис (см. app/clients/rag.py).
"""

import logging
import re

from app.clients import llm
from app.config import settings

_guard_log = logging.getLogger("ai_dos.guard")


def _is_kk(language: str | None) -> bool:
    return (language or "").lower().startswith(("kaz", "kk", "kz", "қаз", "каз"))


# Грубый детектор попыток «сбить» системный промпт на ПУБЛИЧНОМ экране (ru/kk/en).
# НЕ блокируем (ответ и так ограничен базой + temperature=0) — только ЛОГИРУЕМ для
# аудита/мониторинга. Лучше лишний варнинг, чем пропустить попытку манипуляции.
_INJECTION_RE = re.compile(
    r"\b(ignore|disregard|forget)\b.{0,24}\b(previous|above|instruction|prompt|rule|system)\b"
    r"|system\s*prompt|you\s+are\s+now|act\s+as\b|pretend\b|jailbreak|developer\s+mode"
    r"|игнорир|забудь.{0,24}(инструкц|правил|промпт|систем)|ты\s+теперь\b|притвор"
    r"|режим\s+разработчика|нұсқау.{0,24}ұмыт|жүйелік\s+промпт",
    re.IGNORECASE,
)


def check_injection(question: str, language: str | None = None) -> bool:
    """Логирует подозрительный на prompt-injection вопрос. НЕ блокирует. True — подозрителен."""
    if question and _INJECTION_RE.search(question):
        _guard_log.warning("possible prompt-injection [%s]: %r", language or "?", question[:200])
        return True
    return False


def should_correct(language: str | None) -> bool:
    """Нужна ли LLM-коррекция STT для этого языка.

    Коррекция — это доп. вызов LLM (медленный сервер АФМ). По умолчанию включена
    только для казахского (шумный STT, WER ~12%); для русского STT АФМ точнее и
    коррекция чаще лишь добавляет задержку. Список языков — STT_CORRECTION_LANGS
    (через запятую, напр. "ru,kk"); пусто = выключено. STT_CORRECTION=false — совсем off.
    """
    if not settings.stt_correction:
        return False
    langs = {x.strip().lower() for x in settings.stt_correction_langs.split(",") if x.strip()}
    if not langs:
        return False
    return ("kk" if _is_kk(language) else "ru") in langs


CORRECTION_SYSTEM = (
    "Ты исправляешь ошибки автоматического распознавания речи (STT). "
    "Тебе дают текст, распознанный из аудио на {lang} языке. "
    "Исправь только явные ошибки распознавания: восстанови правильные слова, "
    "орфографию и пунктуацию на {lang} языке. "
    "НЕ отвечай на вопрос, НЕ добавляй ничего нового, НЕ меняй смысл и не сокращай. "
    "Если текст и так корректен — верни его без изменений. "
    "Верни ТОЛЬКО исправленный текст, без кавычек и пояснений."
)


def _lang_name(language: str | None) -> str:
    lang = (language or "").lower()
    if lang.startswith(("kaz", "kk", "kz", "қаз", "каз")):
        return "казахском"
    return "русском"


async def correct_transcript(text: str, language: str | None = None) -> str:
    """LLM-постобработка вывода STT: чинит ошибки распознавания.

    Возвращает исходный текст, если он пустой/слишком короткий или LLM недоступен.
    """
    if not text or len(text.strip()) < 2:
        return text
    messages = [
        {"role": "system", "content": CORRECTION_SYSTEM.format(lang=_lang_name(language))},
        {"role": "user", "content": text},
    ]
    try:
        corrected = await llm.chat(messages, max_tokens=256)
    except Exception:
        return text  # не валим распознавание из-за сбоя коррекции
    corrected = corrected.strip()
    return corrected or text

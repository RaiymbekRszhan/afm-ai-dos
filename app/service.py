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


# --- Уточнение вопроса при промахе STT --------------------------------------
# Сценарий (просьба пользователя 2026-07-17): STT исказил вопрос («Две штрафа за
# неуплату налогов»), RAG не нашёл ответа. Вместо голого отказа LLM предлагает
# исправленную формулировку, киоск озвучивает «Возможно, вы хотели спросить: …?
# Если да — скажите „да“», страница запоминает подсказку (X-Suggest) и шлёт её
# со СЛЕДУЮЩИМ вопросом; «да» в ответ = отвечаем на исправленный вопрос.
# Доп. вызов LLM происходит ТОЛЬКО на пути отказа — счастливый путь не замедлен.

# Маркеры фразы-отказа RAG (те же, за которые цепляется eval; сами фразы живут
# в rag/ragsvc/prompts.py — другой venv, напрямую не импортировать).
_NOT_FOUND_MARKS = ("нет точной информации", "нақты ақпарат жоқ")


def looks_not_found(answer: str) -> bool:
    """Ответ RAG — фраза-отказ «в базе нет»?"""
    low = (answer or "").lower()
    return any(m in low for m in _NOT_FOUND_MARKS)


# Утверждительный ответ на «возможно, вы хотели спросить…?»: короткая реплика
# из одних слов-согласий. СТРОГО все слова из списка: «да как подать заявление»
# — это уже новый вопрос, а не согласие.
_AFFIRM_WORDS = {
    # ru
    "да", "ага", "угу", "конечно", "верно", "точно", "давай", "давайте", "хочу",
    "да-да", "именно", "ну да",
    # kk
    "иә", "ия", "иа", "дұрыс", "солай", "әрине", "жарайды", "иә-иә",
}


def is_affirmative(text: str) -> bool:
    """Реплика — согласие («да», «иә, дұрыс») и ничего больше?"""
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", (text or "").lower())
    return bool(words) and len(words) <= 4 and all(w in _AFFIRM_WORDS for w in words)


SUGGEST_SYSTEM = (
    "Вопрос гражданина к цифровому офицеру Агентства РК по финансовому мониторингу "
    "распознан из речи С ОШИБКАМИ, и по нему не нашлось ответа в базе. "
    "Восстанови, что гражданин ХОТЕЛ СПРОСИТЬ на самом деле, и сформулируй это как "
    "естественный короткий вопрос по темам Агентства (финансовый мониторинг, штрафы, "
    "ПОД/ФТ, мошенничество, обращения граждан) на {lang} языке. "
    "Пример: «Две штрафа за неуплату налогов» — гражданин, вероятно, спрашивал "
    "«Какие штрафы за неуплату налогов?». "
    "Верни ТОЛЬКО исправленный вопрос, без кавычек и пояснений. "
    "Раскрытие аббревиатур и мелкие стилистические правки исправлением НЕ считаются. "
    "Если вопрос уже корректен или восстановить смысл невозможно — верни слово НЕТ."
)


def _norm_for_compare(text: str) -> str:
    return re.sub(r"[^\wәіңғүұқөһ]+", " ", text.lower()).strip()


async def suggest_question(question: str, language: str | None = None) -> str | None:
    """Исправленная формулировка вопроса или None (не вышло / и так корректен)."""
    if not question or len(question.strip()) < 4:
        return None
    messages = [
        {"role": "system", "content": SUGGEST_SYSTEM.format(lang=_lang_name(language))},
        {"role": "user", "content": question},
    ]
    try:
        raw = await llm.chat(messages, max_tokens=128)
    except Exception as e:
        _guard_log.warning("LLM-подсказка вопроса недоступна: %r", e)
        return None
    sug = raw.strip().strip('"«»„“')
    if not sug or len(sug) > 200:
        return None
    if _norm_for_compare(sug) in ("нет", "жоқ", "жок"):
        return None
    if _norm_for_compare(sug) == _norm_for_compare(question):
        return None  # LLM ничего не изменил — уточнять нечего
    return sug


def clarify_phrase(suggestion: str, language: str | None = None) -> str:
    """Устная фраза-уточнение для киоска (озвучивается вместо отказа)."""
    if _is_kk(language):
        return ("Нақты жауап таппадым. Мүмкін сіз былай сұрағыңыз келді: "
                f"«{suggestion}» Иә болса — батырманы басып, «иә» деңіз.")
    return ("Я не нашёл точного ответа. Возможно, вы хотели спросить: "
            f"«{suggestion}» Если да — нажмите кнопку и скажите «да».")


# --- Печать образцов заявлений (кнопка «Распечатать образец» на киоске) --------
# Если ответ про подачу заявления/жалобы или запись на приём — киоск предлагает
# распечатать бумажный бланк. Определяем по словам ответа (та же логика, что на
# фронте в detectPrintTemplates), а приглашение ДОПИСЫВАЕМ в ответ ДО синтеза:
# тогда аватар его ПРОГОВАРИВАЕТ, и оно же уходит в X-Answer на экран — читается
# органично, как часть ответа, а не отдельной плашкой. Список подошедших образцов
# (fl/ul/priem) уходит в заголовок X-Print — по нему страница строит меню печати.
_PRINT_RE_APP = re.compile(
    r"заявлени|обращени|жалоб|пожалов|ходатайств|подать|подаёт|подач|"
    r"арыз|шағым|өтініш", re.IGNORECASE)
# «Приём» ловим ТОЛЬКО в смысле личного приёма. Голое каз. «қабылдау» НЕ берём:
# оно значит и «принять» (шешім/заң/тәсіл қабылдау) — давало ложный бланк приёма.
_PRINT_RE_PRIEM = re.compile(
    r"личн\w*\s+при[еёи]м|на\s+при[еёи]м|запис\w*[^.]{0,40}при[еёи]м|"
    r"при[еёи]м\w*\s+граждан|жеке\s+қабылдау|қабылдауға\s+жаз", re.IGNORECASE)


def detect_print_templates(answer: str) -> list[str]:
    """Какие бумажные образцы предложить по смыслу ответа (id: fl/ul/priem)."""
    text = answer or ""
    ids: list[str] = []
    if _PRINT_RE_APP.search(text):
        ids += ["fl", "ul"]
    if _PRINT_RE_PRIEM.search(text):
        ids.append("priem")
    return ids


def with_print_offer(answer: str, language: str | None = None) -> str:
    """Дописать в ответ приглашение распечатать образец (озвучивается + на экран).

    Вставляем ПЕРЕД экранным блоком [ТАБЛИЦА], если он есть: strip_display_blocks
    режет НЕЗАКРЫТЫЙ блок до конца текста и съел бы приглашение, окажись оно после
    таблицы. Без таблицы — просто в конец ответа.
    """
    offer = ("Қаласаңыз, өтініш үлгісін басып шығарып бере аламын — "
             "«Үлгіні басып шығару» түймесін басыңыз."
             if _is_kk(language) else
             "Если хотите, я могу распечатать образец заявления — "
             "нажмите кнопку «Распечатать образец».")
    for marker in ("[ТАБЛИЦА]", "[КЕСТЕ]"):
        idx = answer.find(marker)
        if idx != -1:
            return answer[:idx].rstrip() + " " + offer + "\n" + answer[idx:]
    return answer.rstrip() + " " + offer


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
    # Лимит токенов — с запасом от длины входа: коррекция примерно той же длины,
    # что и оригинал; без запаса длинный ответ гражданина обрезается на полуслове.
    budget = max(256, len(text) // 2 + 64)
    try:
        corrected, finish = await llm.chat(messages, max_tokens=budget, return_finish=True)
    except Exception as e:
        # не валим распознавание из-за сбоя коррекции, но НЕ молчим — иначе долгую
        # деградацию LLM (лежит неделями) никто не заметит.
        _guard_log.warning("LLM-коррекция STT недоступна, отдаю сырой текст: %r", e)
        return text
    if finish == "length":
        # Ответ обрезан лимитом -> в RAG уйдёт усечённый вопрос. Безопаснее вернуть
        # оригинал целиком, чем обрубок.
        _guard_log.warning("LLM-коррекция обрезана лимитом токенов, отдаю сырой текст")
        return text
    corrected = corrected.strip()
    return corrected or text

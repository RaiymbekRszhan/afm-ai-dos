"""answer()/_for_tts()/фраза-отказ — офлайн, LightRAG подменяется заглушкой
(никакого LLM/эмбеддинга/сети не требуется).

Запуск: cd rag && source .venv/bin/activate && pytest tests/test_rag_engine.py
"""
import asyncio

import pytest

from ragsvc import rag_engine
from ragsvc.prompts import NOT_FOUND_KK, NOT_FOUND_RU


class _FakeRag:
    """Заглушка LightRAG: aquery() отдаёт заранее заданный ответ."""

    def __init__(self, resp):
        self._resp = resp

    async def aquery(self, question, param=None, system_prompt=None):
        return self._resp


def _run(coro):
    return asyncio.run(coro)


# ---------- _for_tts ----------

def test_for_tts_strips_markdown_cleanup_chars():
    assert rag_engine._for_tts("**жирный** и `код` и #заголовок") == "жирный и код и заголовок"


def test_for_tts_strips_ru_references_section():
    text = "Основной ответ по статье 214.\n### Источники\nУК РК, статья 214"
    assert rag_engine._for_tts(text) == "Основной ответ по статье 214."


def test_for_tts_strips_en_references_heading():
    text = "Answer text.\n## References\n[1] some doc"
    assert rag_engine._for_tts(text) == "Answer text."


def test_for_tts_strips_leading_trailing_whitespace():
    assert rag_engine._for_tts("  текст с пробелами  \n") == "текст с пробелами"


# ---------- LightRAG "no-context" заглушка (LC_FALLBACK_RE) ----------

@pytest.mark.parametrize("stub", [
    # Точный fail_response установленной lightrag-hku (prompt.py PROMPTS["fail_response"]).
    "Sorry, I'm not able to provide an answer to that question.[no-context]",
    "[no-context]",
    "Sorry, I'm not able to provide an answer to that question.",
])
def test_lc_fallback_re_matches_lightrag_stub_variants(stub):
    assert rag_engine._LC_FALLBACK_RE.search(stub)


def test_lc_fallback_re_does_not_match_normal_answer():
    assert not rag_engine._LC_FALLBACK_RE.search(
        "Штраф составляет 100 МРП согласно статье 214 УК РК."
    )


# ---------- answer(): проходит нормальный ответ ----------

def test_answer_returns_cleaned_text_for_normal_response():
    fake = _FakeRag("Ответ по базе.\n### Источники\nУК РК, статья 214")
    result = _run(rag_engine.answer(fake, "какой штраф?", lang="ru"))
    assert result == "Ответ по базе."


# ---------- answer(): пустой ответ LLM -> отказ, а не пустая строка ----------
# Регрессия: раньше пустая строка проходила мимо _LC_FALLBACK_RE и
# service.looks_not_found на стороне оркестратора, доходила до
# tts.synthesize("") и падала 502 "Ошибка синтеза речи" вместо вежливого
# отказа с номером 1458 (см. AUDIT_2026-07-20.md).

def test_answer_empty_string_becomes_not_found_ru():
    fake = _FakeRag("")
    result = _run(rag_engine.answer(fake, "мимо базы", lang="ru"))
    assert result == NOT_FOUND_RU


def test_answer_whitespace_only_becomes_not_found_kk():
    fake = _FakeRag("   \n  ")
    result = _run(rag_engine.answer(fake, "базадан тыс", lang="kk"))
    assert result == NOT_FOUND_KK


# ---------- answer(): LightRAG english fallback -> локализованный отказ ----------

def test_answer_lightrag_no_context_stub_becomes_localized_not_found():
    fake = _FakeRag("[no-context]Sorry, I'm not able to provide an answer to that question.")
    result_ru = _run(rag_engine.answer(fake, "мимо базы", lang="ru"))
    assert result_ru == NOT_FOUND_RU
    result_kk = _run(rag_engine.answer(fake, "мимо базы", lang="kk"))
    assert result_kk == NOT_FOUND_KK


# ---------- answer(): LLM выдал отказ НЕ НА ТОМ языке -> нормализуем к языку вопроса ----------
# Регрессия (демо): на казахский off-topic-вопрос LLM игнорировал мягкую языковую
# подсказку и отдавал РУССКИЙ NOT_FOUND_RU (извлечённый контекст был русскоязычным) —
# гражданин видел отказ на чужом языке. Ловим фразу-отказ по маркерам ядра фразы и
# приводим к _not_found(lang), чтобы язык отказа был детерминирован.

def test_answer_normalizes_wrong_language_not_found_to_question_lang():
    # LLM отдал РУССКИЙ отказ на КАЗАХСКИЙ вопрос -> должен стать казахским.
    assert _run(rag_engine.answer(_FakeRag(NOT_FOUND_RU),
                                  "Аргентинаның субъектілері кім?", lang="kk")) == NOT_FOUND_KK
    # И наоборот: казахский отказ на русский вопрос -> русский.
    assert _run(rag_engine.answer(_FakeRag(NOT_FOUND_KK),
                                  "мимо базы", lang="ru")) == NOT_FOUND_RU


def test_answer_normalizes_paraphrased_not_found_by_marker():
    # LLM перефразировал вокруг маркера «нет точной информации» — всё равно ловим.
    fake = _FakeRag("К сожалению, у меня нет точной информации по этому вопросу.")
    assert _run(rag_engine.answer(fake, "казахский вопрос", lang="kk")) == NOT_FOUND_KK


# ---------- висячая отсылка к экранной таблице БЕЗ блока [ТАБЛИЦА] ----------
# Регрессия (демо 2026-07-27): числонезависимое перечисление (субъекты) LLM прочитал
# словами (верно), но приписал «— барлық тізбек показано в таблице на экране» — таблицы
# нет (и по правилам быть не должно), да ещё русской формой в казахском ответе.

def test_strip_phantom_table_ref_removes_dangling_kk_clause():
    text = ("Оларға банктер, биржалар, сақтандыру ұйымдары және төлем ұйымдары кіреді "
            "— барлық тізбек показано в таблице на экране.")
    assert rag_engine._strip_phantom_table_ref(text) == (
        "Оларға банктер, биржалар, сақтандыру ұйымдары және төлем ұйымдары кіреді.")


def test_strip_phantom_table_ref_removes_dangling_ru_clause():
    text = "Пороги разные для видов операций, они показаны в таблице на экране."
    assert rag_engine._strip_phantom_table_ref(text) == "Пороги разные для видов операций."


def test_strip_phantom_table_ref_drops_whole_sentence():
    text = "Штраф составляет 40 МРП. Полный перечень показан в таблице на экране."
    assert rag_engine._strip_phantom_table_ref(text) == "Штраф составляет 40 МРП."


def test_strip_phantom_table_ref_keeps_when_block_present():
    text = ("Пороги показаны в таблице на экране.\n[ТАБЛИЦА]\nВид | Порог\n"
            "Недвижимость | 50 000 000 тенге\n[/ТАБЛИЦА]")
    assert rag_engine._strip_phantom_table_ref(text) == text


def test_strip_phantom_table_ref_ignores_answer_without_table_word():
    text = "Штраф составляет 40 МРП согласно статье 214."
    assert rag_engine._strip_phantom_table_ref(text) == text


def test_answer_strips_phantom_table_ref_end_to_end():
    fake = _FakeRag("Субъектілерге банктер мен биржалар кіреді "
                    "— толық тізбек показано в таблице на экране.")
    assert _run(rag_engine.answer(fake, "субъектілер кім?", lang="kk")) == (
        "Субъектілерге банктер мен биржалар кіреді.")


# ---------- answer(): None -> RuntimeError (aquery упал внутри LightRAG) ----------

def test_answer_none_response_raises_runtime_error():
    fake = _FakeRag(None)
    with pytest.raises(RuntimeError):
        _run(rag_engine.answer(fake, "вопрос", lang="ru"))


# ---------- answer(): stream=True (async generator) собирается в одну строку ----------

def test_answer_handles_async_generator_response():
    async def _gen():
        for chunk in ("Часть 1. ", "Часть 2."):
            yield chunk

    class _StreamRag:
        async def aquery(self, question, param=None, system_prompt=None):
            return _gen()

    result = _run(rag_engine.answer(_StreamRag(), "вопрос", lang="ru"))
    assert result == "Часть 1. Часть 2."

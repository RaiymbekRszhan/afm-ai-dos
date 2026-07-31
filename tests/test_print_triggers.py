"""Контракт триггеров печати бланков (N9): бэкенд (service.detect_print_templates)
и фронт (answer_render.js detectPrintTemplates) должны давать ОДИНАКОВЫЙ набор
образцов. Обе стороны гоняют один и тот же набор случаев (fixtures/print_triggers.json);
node-двойник — в video_ui/static/answer_render.test.js.
"""
import json
import os

from app import service

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "print_triggers.json")


def _cases():
    with open(_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_print_triggers_contract():
    for case in _cases():
        got = service.detect_print_templates(case["text"])
        assert got == case["expect"], f"{case['note']!r}: {got} != {case['expect']}"


# --- Язык приглашения — по тексту ответа, а не по переключателю киоска (A1) ---
# Гражданин на КАЗАХСКОМ киоске может спросить по-русски: RAG ответит по-русски
# (правило 1 промпта — язык ВОПРОСА), а приглашение раньше бралось по переключателю
# и приклеивало казахскую фразу к русскому ответу. Ломалась и озвучка: detect_lang
# судит по всему тексту, видит преимущественно русский и отдаёт всё F5 — русский
# голос читал казахское предложение. См. AUDIT_2026-07-31.md.

RU_ANSWER = ("Заявление можно подать через канцелярию Агентства или портал "
             "электронного правительства.")
KK_ANSWER = ("Өтінішті Агенттіктің кеңсесі арқылы немесе электрондық үкімет "
             "порталы арқылы беруге болады.")


def test_print_offer_follows_answer_language_not_kiosk():
    """Русский ответ на казахском киоске — приглашение РУССКОЕ."""
    from app.clients import tts

    out = service.with_print_offer(RU_ANSWER, tts.detect_lang(RU_ANSWER, "kazakh"))
    assert "Распечатать образец" in out
    assert "үлгісін" not in out


def test_print_offer_kazakh_answer_on_russian_kiosk():
    """Зеркально: казахский ответ в русском режиме — приглашение КАЗАХСКОЕ."""
    from app.clients import tts

    out = service.with_print_offer(KK_ANSWER, tts.detect_lang(KK_ANSWER, "russian"))
    assert "үлгісін" in out
    assert "Распечатать образец" not in out

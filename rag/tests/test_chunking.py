"""Нарезка по статьям/FAQ/абзацам — офлайн, без LLM/эмбеддингов/сети.

Запуск: cd rag && source .venv/bin/activate && pytest tests/test_chunking.py
"""
from ragsvc.chunking import chunk_code, chunk_faq, chunk_file, chunk_reference, parse_header


def test_parse_header_extracts_meta_and_strips_it_from_body():
    raw = "# LAW: Уголовный кодекс РК\n# SHORT: УК РК\n# TYPE: code\n# LANG: ru\n\nСтатья 1\nТекст"
    meta, body = parse_header(raw)
    assert meta == {"LAW": "Уголовный кодекс РК", "SHORT": "УК РК", "TYPE": "code", "LANG": "ru"}
    assert body == "Статья 1\nТекст"
    assert "LAW:" not in body


def test_parse_header_defaults_when_no_meta_lines():
    meta, body = parse_header("Просто текст без метаданных.")
    assert meta["TYPE"] == "reference"
    assert meta["LANG"] == "ru"
    assert body == "Просто текст без метаданных."


def test_chunk_code_ru_splits_by_article_and_labels_each():
    body = "Статья 214. Легализация доходов\nТекст статьи 214.\n\nСтатья 215. Иное\nТекст 215."
    units = chunk_code(body, "УК РК", "ru")
    assert len(units) == 2
    assert units[0].label == "УК РК, статья 214"
    assert "214" in units[0].text
    assert units[1].label == "УК РК, статья 215"


def test_chunk_code_kk_splits_by_bap_suffix():
    body = "214-бап. Мазмұны\nМәтін.\n\n215-бап. Тағы бір\nМәтін2."
    units = chunk_code(body, "ҚК РК", "kk")
    assert len(units) == 2
    assert units[0].label == "ҚК РК, 214-бап"
    assert units[1].label == "ҚК РК, 215-бап"


def test_chunk_code_article_with_dash_suffix_number():
    # Реальные кодексы РК используют номера вида "214-1" (доп. статья).
    body = "Статья 214-1. Доп. статья\nТекст доп. статьи."
    units = chunk_code(body, "УК РК", "ru")
    assert len(units) == 1
    assert units[0].label == "УК РК, статья 214-1"


def test_chunk_code_unmatched_article_number_falls_back_to_placeholder():
    # Если номер статьи не распознался (нет цифры после "Статья ") — не должно
    # падать, в цитате-метке должна быть заглушка "?", а не исключение.
    body = "Статья без номера сразу текст"
    units = chunk_code(body, "УК РК", "ru")
    assert len(units) == 1
    assert units[0].label == "УК РК, статья ?"


def test_chunk_code_uid_is_stable_for_same_text():
    body = "Статья 1. А\nТекст."
    u1 = chunk_code(body, "УК РК", "ru")[0]
    u2 = chunk_code(body, "УК РК", "ru")[0]
    assert u1.uid == u2.uid


def test_chunk_faq_splits_on_separator_and_uses_question_as_label():
    body = "Что такое ПОД/ФТ?\nОтвет 1.\n===\nКакой порог?\nОтвет 2."
    units = chunk_faq(body, "FAQ")
    assert len(units) == 2
    assert units[0].label == "FAQ: Что такое ПОД/ФТ"  # '?' обрезан
    assert "Ответ 1" in units[0].text
    assert units[1].label == "FAQ: Какой порог"


def test_chunk_reference_splits_on_blank_lines_into_paragraphs():
    body = "Абзац первый.\nПродолжение.\n\nАбзац второй."
    units = chunk_reference(body, "Справка")
    assert len(units) == 2
    assert units[0].label == "Справка: Абзац первый."
    assert units[1].label == "Справка: Абзац второй."


def test_chunk_file_routes_by_type_code():
    raw = "# TYPE: code\n# LANG: ru\n# SHORT: УК РК\n\nСтатья 1. А\nТекст."
    units = chunk_file(raw)
    assert len(units) == 1
    assert units[0].label == "УК РК, статья 1"


def test_chunk_file_routes_by_type_faq():
    raw = "# TYPE: faq\n# SHORT: FAQ\n\nВопрос?\nОтвет."
    units = chunk_file(raw)
    assert len(units) == 1
    assert units[0].label.startswith("FAQ:")


def test_chunk_file_defaults_to_reference_when_type_missing():
    raw = "# SHORT: Справка\n\nПросто абзац."
    units = chunk_file(raw)
    assert len(units) == 1
    assert units[0].label == "Справка: Просто абзац."

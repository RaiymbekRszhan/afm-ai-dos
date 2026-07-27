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

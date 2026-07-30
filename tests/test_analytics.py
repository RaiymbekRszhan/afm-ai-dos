"""Арифметика аналитики (app/analytics.py).

Этот модуль — единственный источник цифр и для веб-админки, и для CLI-отчёта,
поэтому ошибка здесь врёт сразу в двух местах.
"""
import json

import pytest

from app import analytics


def rec(**kw):
    """Строка аналитики с разумными умолчаниями (поля — как в logging_setup)."""
    base = {"ts": "2026-07-29T10:00:00Z", "id": "aaaa", "kiosk": "astana",
            "lang": "russian", "question": "вопрос", "answer": "ответ",
            "corrected": False, "answer_found": True, "suggested": False,
            "print_ids": [], "provider": "f5", "stt_ms": 500, "rag_ms": 1000,
            "tts_ms": 3000, "tts_first_ms": None, "total_ms": 4500, "error": None}
    base.update(kw)
    return base


@pytest.fixture
def logdir(tmp_path):
    """Каталог логов; кэш модуля сбрасывается, иначе тесты видят чужие данные."""
    analytics.reset_cache()
    yield tmp_path
    analytics.reset_cache()


def write(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


# ---------- чтение ----------
def test_reads_current_and_rotated_files(logdir):
    write(logdir / "interactions.jsonl", [rec(id="new")])
    write(logdir / "interactions.jsonl.2026-07-28", [rec(id="old")])
    assert {r["id"] for r in analytics.load(str(logdir))} == {"new", "old"}


def test_broken_line_does_not_kill_parsing(logdir):
    (logdir / "interactions.jsonl").write_text(
        json.dumps(rec(id="good")) + "\n{битое\n\n" + json.dumps(rec(id="good2")) + "\n",
        encoding="utf-8")
    assert len(analytics.load(str(logdir))) == 2


def test_missing_dir_is_empty_not_error(logdir):
    assert analytics.load(str(logdir / "нет-такого")) == []


def test_days_filter_cuts_old(logdir):
    write(logdir / "interactions.jsonl",
          [rec(id="fresh", ts="2099-01-01T00:00:00Z"),
           rec(id="ancient", ts="2000-01-01T00:00:00Z")])
    ids = {r["id"] for r in analytics.load(str(logdir), days=7)}
    assert ids == {"fresh"}


def test_unparsable_ts_is_kept(logdir):
    """Лучше показать обращение в общей куче, чем потерять из-за битого поля."""
    write(logdir / "interactions.jsonl", [rec(id="no-ts", ts="не время")])
    assert len(analytics.load(str(logdir), days=1)) == 1


def test_cache_returns_same_rows_until_file_changes(logdir):
    p = logdir / "interactions.jsonl"
    write(p, [rec(id="a")])
    first = analytics.load(str(logdir))
    assert len(first) == 1
    write(p, [rec(id="a"), rec(id="b")])
    assert len(analytics.load(str(logdir))) == 2   # отпечаток изменился — перечитали


def test_newest_first_sorts_by_time(logdir):
    rows = [rec(id="mid", ts="2026-07-28T10:00:00Z"),
            rec(id="new", ts="2026-07-29T10:00:00Z"),
            rec(id="old", ts="2026-07-27T10:00:00Z")]
    assert [r["id"] for r in analytics.newest_first(rows)] == ["new", "mid", "old"]


# ---------- элементарные метрики ----------
def test_pct_handles_zero_total():
    assert analytics.pct(7, 22) == "31.8%"
    assert analytics.pct(1, 0) == "—"


def test_percentile_ignores_non_numbers():
    assert analytics.percentile([1, 2, 3, None, "x"], 50) == 2
    assert analytics.percentile([], 50) is None


def test_latency_reports_nothing_on_empty():
    assert analytics.latency([], "total_ms") == {"p50": None, "p95": None,
                                                "max": None, "n": 0}


def test_latency_counts_zero_as_value():
    """total_ms=0 (мок/кэш) — это замер, а не его отсутствие."""
    assert analytics.latency([rec(total_ms=0)], "total_ms")["n"] == 1


# ---------- фильтры ----------
def test_loadtest_excluded_by_default():
    rows = [rec(kiosk="astana"), rec(kiosk="loadtest")]
    assert [r["kiosk"] for r in analytics.filter_rows(rows)] == ["astana"]


def test_asking_for_kiosk_ignores_exclusions():
    """Спросили именно про loadtest — значит показываем его."""
    rows = [rec(kiosk="astana"), rec(kiosk="loadtest")]
    assert len(analytics.filter_rows(rows, kiosk="loadtest")) == 1


def test_only_fallback_and_errors():
    rows = [rec(id="ok"), rec(id="gap", answer_found=False),
            rec(id="err", error="tts", answer_found=False)]
    assert [r["id"] for r in analytics.filter_rows(rows, only="fallback")] == ["gap"]
    assert [r["id"] for r in analytics.filter_rows(rows, only="errors")] == ["err"]


def test_search_looks_in_question_and_answer_case_insensitive():
    rows = [rec(id="q", question="Про НАЛОГИ", answer="—"),
            rec(id="a", question="—", answer="про налоги написано"),
            rec(id="no", question="—", answer="—")]
    found = {r["id"] for r in analytics.filter_rows(rows, search="налоги")}
    assert found == {"q", "a"}


# ---------- сводки ----------
def test_summarize_counts_and_shares():
    rows = [rec(), rec(answer_found=False), rec(suggested=True),
            rec(print_ids=["fl"]), rec(error="tts"), rec(lang="kazakh", provider="spark")]
    s = analytics.summarize(rows)
    assert (s["total"], s["ok"], s["errors"]) == (6, 5, 1)
    assert s["fallback"] == 1 and s["suggested"] == 1 and s["printed"] == 1
    assert dict(s["err_by_stage"]) == {"tts": 1}
    assert dict(s["langs"]) == {"russian": 5, "kazakh": 1}
    # Провайдеры считаются только по успешным: у сбойного обращения озвучки не было.
    assert dict(s["providers"]) == {"f5": 4, "spark": 1}


def test_summarize_ok_share_excludes_errors_from_fallback():
    """Ошибка — не «нет в базе»: при сбое STT вопроса могло и не быть."""
    rows = [rec(error="stt", answer_found=False), rec(answer_found=False)]
    assert analytics.summarize(rows)["fallback"] == 1


def test_by_kiosk_row_per_point():
    rows = [rec(kiosk="astana"), rec(kiosk="astana", answer_found=False),
            rec(kiosk="vko", error="tts")]
    by = {k["kiosk"]: k for k in analytics.by_kiosk(rows)}
    assert by["astana"]["total"] == 2 and by["astana"]["fallback"] == 1
    assert by["astana"]["fallback_pct"] == "50.0%"
    assert by["vko"]["errors"] == 1 and by["vko"]["p50"] is None


def test_by_kiosk_groups_nameless_under_dash():
    assert analytics.by_kiosk([rec(kiosk=None)])[0]["kiosk"] == "-"


def test_by_day_is_chronological_with_gaps_counted():
    rows = [rec(ts="2026-07-29T10:00:00Z"),
            rec(ts="2026-07-28T10:00:00Z", answer_found=False),
            rec(ts="2026-07-28T11:00:00Z", error="tts")]
    days = analytics.by_day(rows)
    assert [d["date"] for d in days] == ["2026-07-28", "2026-07-29"]
    assert days[0] == {"date": "2026-07-28", "total": 2, "errors": 1, "fallback": 1}


def test_by_day_skips_records_without_date():
    assert analytics.by_day([rec(ts="")]) == []


def test_top_questions_counts_duplicates():
    rows = [rec(question="один"), rec(question="один"), rec(question="два")]
    assert analytics.top_questions(rows, 5) == [["один", 2], ["два", 1]]


def test_top_questions_empty_when_texts_not_logged():
    """log_questions=off — тексты не писались, топ недоступен, а не пуст по смыслу."""
    assert analytics.top_questions([rec(question=None)], 5) == []


def test_top_unanswered_is_only_gaps():
    rows = [rec(question="есть в базе"),
            rec(question="нет в базе", answer_found=False),
            rec(question="сбой", error="tts", answer_found=False)]
    assert analytics.top_unanswered(rows, 5) == [["нет в базе", 1]]


# ---------- три разных исхода в поле error ----------
def test_summarize_splits_failures_refusals_and_not_heard():
    """Отказ рубильника — не сбой: иначе доля ошибок раздувается на ровном месте."""
    rows = [rec(), rec(error="tts"), rec(error="stt"), rec(error="empty"),
            rec(error="disabled"), rec(error="gate")]
    s = analytics.summarize(rows)
    assert s["errors"] == 5          # общий баланс: ok + errors = total
    assert s["failures"] == 2        # только stt/rag/tts
    assert dict(s["failures_by_stage"]) == {"tts": 1, "stt": 1}
    assert s["not_heard"] == 1       # empty — шум у киоска
    assert s["refused"] == 2         # наши же настройки сработали
    assert dict(s["refused_by_kind"]) == {"disabled": 1, "gate": 1}


def test_by_kiosk_splits_failures_from_refusals():
    rows = [rec(kiosk="astana", error="tts"), rec(kiosk="astana", error="disabled")]
    k = analytics.by_kiosk(rows)[0]
    assert k["errors"] == 2 and k["failures"] == 1 and k["refused"] == 1


def test_noise_counts_as_not_heard_not_as_gap():
    """Шум/петля с киоска НЕ должны попадать в «нет в базе» и в список пополнения.

    Ровно этот баг нашёлся на живой странице 30.07: «Продолжение следует.»
    (артефакт STT на тишине) числился пробелом в базе.
    """
    rows = [rec(question="настоящий вопрос", answer_found=False),
            rec(question="Продолжение следует.", error="noise"),
            rec(question="", error="empty")]
    s = analytics.summarize(rows)
    assert s["fallback"] == 1          # только настоящий пробел
    assert s["not_heard"] == 2         # шум + тишина
    assert s["failures"] == 0          # и это НЕ сбой
    # В списке «чем пополнять базу» шума быть не должно.
    assert analytics.top_unanswered(rows, 5) == [["настоящий вопрос", 1]]

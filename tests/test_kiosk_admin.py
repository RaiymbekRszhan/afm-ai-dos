"""Админка флота: heartbeat, статус и рубильник из UI."""
import pytest

from app import kiosks
from tests.util import wav_bytes


@pytest.fixture
def admin(tmp_path, monkeypatch):
    """Свои файлы флота/отключений + заданный токен; состояние чистится."""
    disabled = tmp_path / "kiosks-disabled.txt"
    fleet = tmp_path / "kiosks.txt"
    fleet.write_text("astana  город Астана\nturkestan  Туркестанская область\n",
                     encoding="utf-8")
    monkeypatch.setattr(kiosks.settings, "kiosks_disabled_file", str(disabled))
    monkeypatch.setattr(kiosks.settings, "kiosks_file", str(fleet))
    monkeypatch.setattr(kiosks.settings, "admin_token", "s3cret")
    # Пустой каталог логов: иначе тесты читали бы НАСТОЯЩИЙ logs/ разработчика —
    # и падали от его содержимого, и трогали бы ПДн граждан.
    empty = tmp_path / "logs"
    empty.mkdir()
    import app.main as main
    monkeypatch.setattr(main.settings, "log_dir", str(empty))
    kiosks.reset_cache()
    kiosks.reset_seen()
    yield {"disabled": disabled, "fleet": fleet, "token": "s3cret"}
    kiosks.reset_cache()
    kiosks.reset_seen()


def _rows(body):
    return {k["kiosk"]: k for k in body["kiosks"]}


# ---------- доступ ----------
def test_admin_hidden_without_token_configured(client, admin, monkeypatch):
    """Пустой ADMIN_TOKEN = админки нет вовсе: 404, а не 401."""
    monkeypatch.setattr(kiosks.settings, "admin_token", "")
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_token", "")
    assert client.get("/admin/kiosks").status_code == 404


def test_admin_rejects_wrong_token(client, admin, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_token", "s3cret")
    assert client.get("/admin/kiosks", params={"token": "nope"}).status_code == 403
    assert client.get("/admin/kiosks").status_code == 403


@pytest.fixture
def token(client, admin, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_token", admin["token"])
    return admin["token"]


# ---------- статус ----------
def test_status_lists_whole_fleet_even_if_never_seen(client, token):
    body = client.get("/admin/kiosks", params={"token": token}).json()
    rows = _rows(body)
    assert set(rows) == {"astana", "turkestan"}
    assert rows["astana"]["online"] is False
    assert rows["astana"]["ping_ago_s"] is None      # «не включалась»
    assert rows["astana"]["enabled"] is True


def test_ping_marks_kiosk_alive(client, token):
    r = client.post("/kiosk/ping", data={"kiosk": "astana"})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    rows = _rows(client.get("/admin/kiosks", params={"token": token}).json())
    assert rows["astana"]["online"] is True
    assert rows["astana"]["ping_ago_s"] == 0


def test_kiosk_goes_offline_after_timeout(client, token, monkeypatch):
    client.post("/kiosk/ping", data={"kiosk": "astana"})
    monkeypatch.setattr(kiosks.settings, "kiosk_offline_after_s", 0)
    rows = _rows(client.get("/admin/kiosks", params={"token": token}).json())
    assert rows["astana"]["online"] is False


def test_ping_tells_kiosk_it_is_disabled(client, token, admin):
    admin["disabled"].write_text("astana  Ремонт до пятницы\n", encoding="utf-8")
    kiosks.reset_cache()
    body = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()
    assert body["enabled"] is False
    assert body["message"] == "Ремонт до пятницы"


def test_asking_counts_as_alive(client, token):
    """Старая версия страницы не пингует — но вопрос тоже доказывает жизнь."""
    client.post("/voice", files={"data": ("q.wav", wav_bytes(), "audio/wav")},
                data={"language": "russian", "kiosk": "astana"})
    rows = _rows(client.get("/admin/kiosks", params={"token": token}).json())
    assert rows["astana"]["online"] is True
    assert rows["astana"]["asks"] == 1
    assert rows["astana"]["ask_ago_s"] == 0


def test_unknown_kiosk_shows_up_as_not_in_fleet(client, token):
    client.post("/kiosk/ping", data={"kiosk": "left-behind"})
    rows = _rows(client.get("/admin/kiosks", params={"token": token}).json())
    assert rows["left-behind"]["in_fleet"] is False
    assert rows["astana"]["in_fleet"] is True


# ---------- рубильник из UI ----------
def test_disable_from_admin_writes_file_and_blocks(client, token, admin):
    r = client.post("/admin/kiosks", data={
        "token": token, "kiosk": "turkestan", "enabled": "false",
        "message": "Киоск на ремонте"})
    assert r.status_code == 200
    assert _rows(r.json())["turkestan"]["enabled"] is False
    assert "turkestan Киоск на ремонте" in admin["disabled"].read_text(encoding="utf-8")

    blocked = client.post("/voice", files={"data": ("q.wav", wav_bytes(), "audio/wav")},
                          data={"language": "russian", "kiosk": "turkestan"})
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "Киоск на ремонте"


def test_enable_from_admin_removes_line(client, token, admin):
    admin["disabled"].write_text("turkestan  ремонт\nastana  тоже\n", encoding="utf-8")
    kiosks.reset_cache()
    r = client.post("/admin/kiosks", data={
        "token": token, "kiosk": "turkestan", "enabled": "true"})
    text = admin["disabled"].read_text(encoding="utf-8")
    assert "turkestan" not in text
    assert "astana" in text                      # соседние строки не тронуты
    assert _rows(r.json())["turkestan"]["enabled"] is True


def test_disable_twice_does_not_duplicate_line(client, token, admin):
    for msg in ("первая причина", "вторая причина"):
        client.post("/admin/kiosks", data={
            "token": token, "kiosk": "astana", "enabled": "false", "message": msg})
    lines = [l for l in admin["disabled"].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == ["astana вторая причина"]


def test_message_cannot_inject_newline(client, token, admin):
    """Текст приходит из формы: перевод строки подделал бы соседнюю запись."""
    client.post("/admin/kiosks", data={
        "token": token, "kiosk": "astana", "enabled": "false",
        "message": "ремонт\nturkestan тоже отключён"})
    text = admin["disabled"].read_text(encoding="utf-8")
    assert len([l for l in text.splitlines() if l.strip()]) == 1
    kiosks.reset_cache()
    assert kiosks.disabled_message("turkestan") is None


def test_maintenance_disables_everything(client, token, admin):
    r = client.post("/admin/kiosks", data={
        "token": token, "kiosk": "*", "enabled": "false", "message": "тех. работы"})
    body = r.json()
    assert body["maintenance"] == "тех. работы"
    rows = _rows(body)
    # Отключены все, но НЕ «лично» — кнопка точки при этом бессильна.
    assert all(not k["enabled"] for k in rows.values())
    assert all(not k["disabled_here"] for k in rows.values())
    assert client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["enabled"] is False


def test_maintenance_can_be_lifted(client, token, admin):
    client.post("/admin/kiosks", data={
        "token": token, "kiosk": "*", "enabled": "false", "message": "тех. работы"})
    r = client.post("/admin/kiosks", data={"token": token, "kiosk": "*", "enabled": "true"})
    assert r.json()["maintenance"] is None
    assert all(k["enabled"] for k in _rows(r.json()).values())


def test_admin_rejects_garbage_kiosk_id(client, token):
    r = client.post("/admin/kiosks", data={
        "token": token, "kiosk": "...", "enabled": "false"})
    assert r.status_code == 400


# ---------- отчётность: сводка, журнал, выгруз ----------
@pytest.fixture
def logs(tmp_path, monkeypatch):
    """Свой каталог логов с известным содержимым."""
    import json

    from app import analytics
    import app.main as main

    def rec(**kw):
        base = {"ts": "2026-07-29T10:00:00Z", "id": "a1", "kiosk": "astana",
                "lang": "russian", "question": "про налоги", "answer": "ответ про налоги",
                "answer_found": True, "suggested": False, "corrected": False,
                "print_ids": [], "provider": "f5", "stt_ms": 500, "rag_ms": 900,
                "tts_ms": 3000, "tts_first_ms": None, "total_ms": 4400, "error": None}
        base.update(kw)
        return base

    rows = [
        rec(id="r1"),
        rec(id="r2", question="про штрафы", answer_found=False),
        rec(id="r3", kiosk="vko", error="tts", ts="2026-07-28T10:00:00Z"),
        rec(id="r4", kiosk="loadtest", question="прогон"),
    ]
    (tmp_path / "interactions.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(main.settings, "log_dir", str(tmp_path))
    monkeypatch.setattr(main.settings, "admin_logs", True)
    # Записи с фиксированными датами — период не должен их отсекать.
    monkeypatch.setattr(main.settings, "log_retention_days", 100000)
    analytics.reset_cache()
    yield tmp_path
    analytics.reset_cache()


def test_stats_requires_token(client, token, logs):
    assert client.get("/admin/stats").status_code == 403


def test_stats_shape_and_numbers(client, token, logs):
    d = client.get("/admin/stats", params={"token": token, "days": 99999}).json()
    s = d["summary"]
    # loadtest отброшен по умолчанию — иначе прогоны портят статистику граждан.
    assert s["total"] == 3
    assert s["errors"] == 1 and s["fallback"] == 1
    assert d["by_day"][0]["date"] == "2026-07-28"
    assert ["про штрафы", 1] in d["top_unanswered"]
    assert d["texts_enabled"] is True


def test_stats_can_focus_on_one_kiosk(client, token, logs):
    d = client.get("/admin/stats",
                   params={"token": token, "days": 99999, "kiosk": "vko"}).json()
    assert d["summary"]["total"] == 1 and d["summary"]["errors"] == 1


def test_fleet_carries_period_numbers_from_logs(client, token, logs):
    """Счётчик в памяти обнуляется рестартом — в отчёте цифры должны быть из логов."""
    rows = _rows(client.get("/admin/kiosks",
                            params={"token": token, "days": 99999}).json())
    assert rows["astana"]["period_asks"] == 2
    assert rows["astana"]["period_fallback"] == 1
    assert rows["turkestan"]["period_asks"] == 0


def test_fleet_shows_kiosks_that_exist_only_in_logs(client, token, logs):
    """Старый/переименованный киоск не должен пропасть из таблицы вместе с историей."""
    rows = _rows(client.get("/admin/kiosks",
                            params={"token": token, "days": 99999}).json())
    # vko есть в логах, но НЕ в тестовом списке флота (astana + turkestan).
    assert "vko" in rows
    assert rows["vko"]["in_fleet"] is False
    assert rows["vko"]["period_errors"] == 1


def test_interactions_newest_first_and_paged(client, token, logs):
    d = client.get("/admin/interactions",
                   params={"token": token, "days": 99999, "limit": 2}).json()
    assert d["total"] == 3 and len(d["rows"]) == 2
    assert d["rows"][0]["ts"] >= d["rows"][1]["ts"]
    second = client.get("/admin/interactions",
                        params={"token": token, "days": 99999, "limit": 2,
                                "offset": 2}).json()
    assert len(second["rows"]) == 1


def test_interactions_filters(client, token, logs):
    def ids(**params):
        d = client.get("/admin/interactions",
                       params={"token": token, "days": 99999, **params}).json()
        return {r["id"] for r in d["rows"]}

    assert ids(only="fallback") == {"r2"}
    assert ids(only="errors") == {"r3"}
    assert ids(kiosk="vko") == {"r3"}
    assert ids(q="штрафы") == {"r2"}
    assert ids(kiosk="loadtest") == {"r4"}   # спросили прямо — показали


def test_interactions_hides_texts_when_disabled(client, token, logs, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_logs", False)
    d = client.get("/admin/interactions", params={"token": token, "days": 99999}).json()
    assert d["texts_enabled"] is False
    # Полей НЕТ вовсе, а не пустые строки: страница должна отличать «выключено»
    # от «вопрос был пустой».
    assert "question" not in d["rows"][0] and "answer" not in d["rows"][0]
    # Цифры при этом на месте — статистика не зависит от показа текстов.
    assert d["rows"][0]["total_ms"] is not None


def test_stats_hides_question_tops_when_texts_disabled(client, token, logs, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_logs", False)
    d = client.get("/admin/stats", params={"token": token, "days": 99999}).json()
    assert d["top_questions"] == [] and d["top_unanswered"] == []
    assert d["summary"]["total"] == 3          # сводка считается по-прежнему


def test_csv_export(client, token, logs):
    r = client.get("/admin/export.csv", params={"token": token, "days": 99999})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.content
    # BOM обязателен: без него Excel на Windows покажет кириллицу кракозябрами.
    assert body.startswith(b"\xef\xbb\xbf")
    text = body.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("ts,id,kiosk")
    assert "про штрафы" in text
    assert "loadtest" not in text


def test_csv_respects_filters_and_token(client, token, logs):
    assert client.get("/admin/export.csv", params={"days": 99999}).status_code == 403
    r = client.get("/admin/export.csv",
                   params={"token": token, "days": 99999, "only": "errors"})
    text = r.content.decode("utf-8-sig")
    assert "r3" in text and "r1" not in text


# ---------- перезагрузка страниц флота ----------
@pytest.fixture
def ui(tmp_path, monkeypatch):
    """Свои метка перезагрузки и каталог статики: настоящие трогать нельзя."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>1</html>", encoding="utf-8")
    monkeypatch.setattr(kiosks.settings, "kiosks_reload_file",
                        str(tmp_path / "kiosks-reload.txt"))
    monkeypatch.setattr(kiosks.settings, "ui_static_dir", str(static))
    kiosks.reset_ui_version()
    yield {"static": static}
    kiosks.reset_ui_version()


def test_ping_carries_ui_version(client, admin, ui):
    """Точка узнаёт версию кода страницы из обычного пинга.

    Отдельного канала нет намеренно: пинг уже ходит раз в минуту с каждой из 20
    точек, и вешать на него ещё одно поле дешевле, чем заводить второй опрос.
    """
    r = client.post("/kiosk/ping", data={"kiosk": "astana"})
    assert r.status_code == 200
    assert r.json()["ui_version"]


def test_reload_button_changes_version(client, admin, ui):
    """Кнопка в админке меняет версию — этим и перезагружаются точки."""
    before = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]

    r = client.post("/admin/reload-ui", data={"token": admin["token"]})
    assert r.status_code == 200

    after = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    assert after != before


def test_new_static_changes_version_without_button(client, admin, ui):
    """Выкатка нового кода страницы перезагружает флот САМА.

    Ради этого отпечаток и считается по файлам: иначе после каждого обновления
    пришлось бы помнить про кнопку, а забытая правка молча жила бы на точках
    неделями (браузер киоска открыт сутками и код не перечитывает).
    """
    before = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]

    kiosks.reset_ui_version()          # кэш версии живёт секунды, тесту ждать нечего
    (ui["static"] / "index.html").write_text("<html>2 — правка</html>", encoding="utf-8")

    after = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    assert after != before


def test_videos_do_not_change_version(client, admin, ui):
    """Замена ролика флот НЕ перезагружает: .mp4 тяжёлые, поведение не меняют."""
    before = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]

    kiosks.reset_ui_version()
    (ui["static"] / "idle.mp4").write_bytes(b"\x00" * 64)

    after = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    assert after == before


def test_version_is_stable_between_pings(client, admin, ui):
    """Ничего не менялось — версия та же. Иначе флот перезагружался бы вечно."""
    first = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    kiosks.reset_ui_version()
    second = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    assert first == second


def test_reload_requires_admin_token(client, admin, ui):
    """Перезагрузка флота — админское действие, не публичное."""
    # 403 (а не 401) — тот же код, что у остальных админских ручек.
    assert client.post("/admin/reload-ui", data={"token": "wrong"}).status_code == 403
    assert client.post("/admin/reload-ui").status_code == 403


def test_test_and_admin_files_do_not_reload_fleet(client, admin, ui):
    """Правка теста или админки НЕ перезагружает 20 публичных экранов.

    Браузер киоска не загружает ни `*.test.js`, ни `admin.html` — а раньше любой
    их файл входил в отпечаток, и коммит с одними тестами гасил бы экраны во всех
    регионах ни за чем. A3 в AUDIT_2026-07-31.md.
    """
    before = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]

    for name in ("vad.test.js", "admin.html", "admin_util.js", "diag.html"):
        kiosks.reset_ui_version()
        (ui["static"] / name).write_text("// правка", encoding="utf-8")
        after = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
        assert after == before, f"{name} не должен перезагружать флот"


def test_same_name_in_subdir_does_not_collide(client, admin, ui):
    """Отпечаток ключуется ПУТЁМ: одноимённые файлы в разных папках не сливаются.

    `templates/form.html` и `index.html` — разные файлы; при ключе по голому
    имени правка одного могла бы затереть запись другого.
    """
    sub = ui["static"] / "templates"
    sub.mkdir()
    (sub / "index.html").write_text("<html>бланк</html>", encoding="utf-8")
    kiosks.reset_ui_version()
    before = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]

    kiosks.reset_ui_version()
    (sub / "index.html").write_text("<html>бланк правленый</html>", encoding="utf-8")
    after = client.post("/kiosk/ping", data={"kiosk": "astana"}).json()["ui_version"]
    assert after != before

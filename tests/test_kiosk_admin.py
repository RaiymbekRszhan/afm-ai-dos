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

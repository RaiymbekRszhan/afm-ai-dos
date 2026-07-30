"""Проходная киоска: пропуск точки и ограничение темпа запросов.

Смысл пропуска: без него `?id=` — просто подпись, и отключённый регион обходит
рубильник, убрав параметр из ярлыка. Смысл лимита: MAX_CONCURRENT_VOICE держит
одновременность, но не темп — один клиент иначе занимает оба слота вечно.
"""
import pytest

from app import kiosks
from tests.util import wav_bytes


@pytest.fixture
def gate(tmp_path, monkeypatch):
    keys = tmp_path / "kiosks-keys.txt"
    keys.write_text("astana  goodkey\nturkestan  otherkey\n", encoding="utf-8")
    monkeypatch.setattr(kiosks.settings, "kiosk_keys_file", str(keys))
    monkeypatch.setattr(kiosks.settings, "kiosks_disabled_file",
                        str(tmp_path / "kiosks-disabled.txt"))
    monkeypatch.setattr(kiosks.settings, "kiosk_key_required", False)
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 0)   # лимит отдельно
    kiosks.reset_cache()
    kiosks.reset_seen()
    yield {"keys": keys}
    kiosks.reset_cache()
    kiosks.reset_seen()


def _ask(client, **extra):
    return client.post("/voice", files={"data": ("q.wav", wav_bytes(), "audio/wav")},
                       data={"language": "russian", **extra})


# ---------- пропуск ----------
def test_right_key_passes(client, gate):
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 200


def test_wrong_key_rejected(client, gate):
    r = _ask(client, kiosk="astana", key="nope")
    assert r.status_code == 403
    assert r.json()["detail"] == "Киоск не опознан."


def test_key_of_another_kiosk_rejected(client, gate):
    """Главное, ради чего пропуск: нельзя притвориться соседом, чтобы обойти рубильник."""
    assert _ask(client, kiosk="astana", key="otherkey").status_code == 403


def test_key_without_known_kiosk_rejected(client, gate):
    assert _ask(client, kiosk="unknown-city", key="goodkey").status_code == 403


def test_no_key_passes_in_soft_mode(client, gate):
    """Мягкий режим: точки, ещё не получившие архив, продолжают работать."""
    assert _ask(client, kiosk="astana").status_code == 200
    assert _ask(client).status_code == 200


def test_no_key_rejected_when_required(client, gate, monkeypatch):
    monkeypatch.setattr(kiosks.settings, "kiosk_key_required", True)
    assert _ask(client, kiosk="astana").status_code == 403
    # Верный ключ по-прежнему проходит.
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 200


def test_everything_passes_when_no_keys_configured(client, gate, monkeypatch):
    """Ключей нет вовсе — проверять нечем, приём граждан не глушим."""
    monkeypatch.setattr(kiosks.settings, "kiosk_keys_file", "/nonexistent/keys.txt")
    monkeypatch.setattr(kiosks.settings, "kiosk_key_required", True)
    kiosks.reset_cache()
    assert _ask(client, kiosk="astana").status_code == 200


def test_ping_checks_key_too(client, gate):
    """Подделанный пинг рисовал бы погасшую точку живой."""
    assert client.post("/kiosk/ping", data={"kiosk": "astana", "key": "goodkey"}).status_code == 200
    assert client.post("/kiosk/ping", data={"kiosk": "astana", "key": "nope"}).status_code == 403


def test_disabled_kiosk_cannot_pose_as_neighbour(client, gate):
    """Сквозной сценарий: рубильник теперь не обойти сменой имени."""
    kiosks.set_enabled("turkestan", False, "ремонт")
    assert _ask(client, kiosk="turkestan", key="otherkey").status_code == 503
    # Своим ключом под чужим именем — не пустят.
    assert _ask(client, kiosk="astana", key="otherkey").status_code == 403


# ---------- темп ----------
def test_rate_limit_kicks_in(client, gate, monkeypatch):
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 3)
    codes = [_ask(client, kiosk="astana", key="goodkey").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_rate_limit_is_per_kiosk(client, gate, monkeypatch):
    """Шумящая точка не должна ронять соседей."""
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 2)
    for _ in range(2):
        _ask(client, kiosk="astana", key="goodkey")
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 429
    assert _ask(client, kiosk="turkestan", key="otherkey").status_code == 200


def test_rate_limit_counts_anonymous_by_address(client, gate, monkeypatch):
    """Не назвался — считаем по адресу, иначе лимит обходится пустым id."""
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 2)
    codes = [_ask(client).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limit_off_by_zero(client, gate, monkeypatch):
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 0)
    codes = [_ask(client, kiosk="astana", key="goodkey").status_code for _ in range(5)]
    assert codes == [200] * 5


def test_rate_window_slides(client, gate, monkeypatch):
    """Через минуту счётчик отпускает — иначе точка залипла бы навсегда."""
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 1)
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 200
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 429
    kiosks._rate["astana"] = [kiosks.time.time() - 61]     # запрос был минуту назад
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 200


def test_gate_logged_as_error_in_analytics(client, gate, monkeypatch):
    """Отказ проходной виден в аналитике — иначе шум по точке не найти."""
    monkeypatch.setattr(kiosks.settings, "kiosk_rate_per_min", 1)
    _ask(client, kiosk="astana", key="goodkey")
    assert _ask(client, kiosk="astana", key="goodkey").status_code == 429


# ---------- готовность к строгому режиму ----------
def test_fleet_shows_which_kiosks_presented_a_key(client, gate, monkeypatch):
    """В мягком режиме запрос без ключа проходит МОЛЧА — без этого сигнала
    строгий режим включался бы наугад."""
    import app.main as main
    monkeypatch.setattr(main.settings, "admin_token", "adm")

    def fleet():
        d = client.get("/admin/kiosks", params={"token": "adm"}).json()
        return d, {k["kiosk"]: k for k in d["kiosks"]}

    # Пока никто не обращался — сказать про пропуск нечего.
    d, rows = fleet()
    assert rows["astana"]["has_key"] is None
    assert d["without_key"] == 0

    client.post("/kiosk/ping", data={"kiosk": "astana", "key": "goodkey"})
    _ask(client, kiosk="turkestan")            # без пропуска (старый архив)
    d, rows = fleet()
    assert rows["astana"]["has_key"] is True
    assert rows["turkestan"]["has_key"] is False
    assert d["without_key"] == 1, "не видно точку без пропуска — включать строгий режим рано"
    assert d["key_required"] is False

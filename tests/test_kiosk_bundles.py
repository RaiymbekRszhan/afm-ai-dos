"""Сборка архивов киосков: главное здесь — переводы строк и санитайзер id.

Обе проверяемые мины уже стреляли на пилоте: .bat с LF роняет cmd.exe
(«cannot find the batch label - waitloop»), а id с кириллицей санитайзер
страницы вырезает целиком, и точка молча уходит в логи без имени.
"""
import zipfile

import pytest

from scripts import make_kiosk_bundles as mkb


@pytest.fixture
def fleet_file(tmp_path, monkeypatch):
    """Подсовываем скрипту свой список точек вместо deploy/kiosks.txt."""
    def _write(text: str):
        p = tmp_path / "kiosks.txt"
        p.write_text(text, encoding="utf-8")
        monkeypatch.setattr(mkb, "LIST", p)
        return p
    return _write


# ---------- список точек ----------
def test_fleet_parses_id_and_human_name(fleet_file):
    fleet_file("# коммент\n\nastana  Астана (город)\naktobe  Актобе\n")
    assert mkb.read_fleet(None) == [("astana", "Астана (город)"), ("aktobe", "Актобе")]


def test_fleet_rejects_cyrillic_id(fleet_file):
    """Кириллицу вырежет index.html:262 — ловим это здесь, а не через месяц."""
    fleet_file("астана  Астана\n")
    with pytest.raises(SystemExit, match="санитайзер"):
        mkb.read_fleet(None)


def test_fleet_rejects_too_long_id(fleet_file):
    fleet_file(f"{'a' * 33}  Слишком длинный\n")
    with pytest.raises(SystemExit, match="санитайзер"):
        mkb.read_fleet(None)


def test_fleet_rejects_duplicate_id(fleet_file):
    """Две точки с одним id слиплись бы в одну строку отчёта."""
    fleet_file("astana  Астана\nastana  Астана вторая\n")
    with pytest.raises(SystemExit, match="дважды"):
        mkb.read_fleet(None)


def test_fleet_only_filters(fleet_file):
    fleet_file("astana  Астана\naktobe  Актобе\n")
    assert mkb.read_fleet("aktobe") == [("aktobe", "Актобе")]


def test_fleet_only_unknown_city_fails(fleet_file):
    fleet_file("astana  Астана\n")
    with pytest.raises(SystemExit, match="нет точки"):
        mkb.read_fleet("караганда")


# ---------- .bat ----------
def test_check_bat_rejects_lone_lf(tmp_path, monkeypatch):
    bad = tmp_path / "kiosk-start.bat"
    bad.write_bytes(b'@echo off\r\nset "PORT=80"\ngoto launch\r\n')  # одна строка с LF
    monkeypatch.setattr(mkb, "BAT", bad)
    with pytest.raises(SystemExit, match="LF вместо CRLF"):
        mkb.check_bat()


def test_check_bat_accepts_crlf(tmp_path, monkeypatch):
    good = tmp_path / "kiosk-start.bat"
    good.write_bytes(b'@echo off\r\nset "PORT=80"\r\n')
    monkeypatch.setattr(mkb, "BAT", good)
    assert mkb.check_bat() == good.read_bytes()


def test_real_bat_in_repo_is_crlf(monkeypatch):
    """Тот самый файл, который поедет на 20 точек."""
    data = mkb.check_bat()
    assert data.count(b"\n") - data.count(b"\r\n") == 0


def test_server_taken_from_bat():
    data = b'set "SERVER=10.10.42.44"\r\nset "PORT=80"\r\n'
    assert mkb.server_from_bat(data) == "10.10.42.44"
    data = b'set "SERVER=10.0.0.1"\r\nset "PORT=8100"\r\n'
    assert mkb.server_from_bat(data) == "10.0.0.1:8100"


# ---------- собранный архив ----------
def test_bundle_contents(tmp_path, monkeypatch, fleet_file):
    fleet_file("astana  Астана (город)\n")
    monkeypatch.setattr(mkb, "OUT", tmp_path / "bundles")
    monkeypatch.setattr("sys.argv", ["make_kiosk_bundles"])
    assert mkb.main() == 0

    z = zipfile.ZipFile(tmp_path / "bundles" / "aidos-kiosk-astana.zip")
    assert sorted(z.namelist()) == ["README.txt", "kiosk-id.txt", "kiosk-start.bat"]

    # cmd читает файл через `set /p` — лишний байт уехал бы прямо в имя точки.
    assert z.read("kiosk-id.txt") == b"astana\r\n"
    # .bat должен доехать байт в байт: любая перекодировка ломает cmd.
    assert z.read("kiosk-start.bat") == mkb.BAT.read_bytes()
    # BOM: без него старый notepad покажет кириллицу кракозябрами.
    readme = z.read("README.txt")
    assert readme.startswith(b"\xef\xbb\xbf")
    assert "astana" in readme.decode("utf-8-sig")

    send_list = (tmp_path / "bundles" / "SEND-LIST.txt").read_text(encoding="utf-8-sig")
    assert "aidos-kiosk-astana.zip" in send_list


def test_repo_fleet_is_valid():
    """Настоящий deploy/kiosks.txt: все 20 id проходят санитайзер и уникальны."""
    fleet = mkb.read_fleet(None)
    assert len(fleet) == 20
    assert len({k for k, _ in fleet}) == 20
    assert all(mkb.ID_RE.match(k) for k, _ in fleet)

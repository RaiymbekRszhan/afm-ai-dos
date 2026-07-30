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


@pytest.mark.parametrize("reader", [mkb.check_bat, mkb.check_autostart])
def test_real_bats_in_repo_are_crlf(reader):
    """Те самые файлы, которые поедут на 20 точек."""
    data = reader()
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
    # Реальный kiosks-keys.txt трогать нельзя: это секреты рабочего флота.
    monkeypatch.setattr(mkb, "KEYS", tmp_path / "keys.txt")
    monkeypatch.setattr("sys.argv", ["make_kiosk_bundles"])
    assert mkb.main() == 0

    z = zipfile.ZipFile(tmp_path / "bundles" / "aidos-kiosk-astana.zip")
    assert sorted(z.namelist()) == [
        "README.txt", "install-autostart.bat", "kiosk-id.txt", "kiosk-key.txt",
        "kiosk-start.bat"]

    # cmd читает файл через `set /p` — лишний байт уехал бы прямо в имя точки.
    assert z.read("kiosk-id.txt") == b"astana\r\n"
    # .bat должен доехать байт в байт: любая перекодировка ломает cmd.
    assert z.read("kiosk-start.bat") == mkb.BAT.read_bytes()
    assert z.read("install-autostart.bat") == mkb.AUTOSTART.read_bytes()
    # BOM: без него старый notepad покажет кириллицу кракозябрами.
    readme = z.read("README.txt")
    assert readme.startswith(b"\xef\xbb\xbf")
    assert "astana" in readme.decode("utf-8-sig")

    # Пропуск: ASCII + CRLF, cmd читает его тем же `set /p`.
    key = z.read("kiosk-key.txt")
    assert key.endswith(b"\r\n") and key[:-2].isalnum()

    send_list = (tmp_path / "bundles" / "SEND-LIST.txt").read_text(encoding="utf-8-sig")
    assert "aidos-kiosk-astana.zip" in send_list
    # Готовая ссылка с пропуском — по ней точка проверяется до рассылки.
    assert f"id=astana&key={key[:-2].decode()}" in send_list


def test_repo_fleet_is_valid():
    """Настоящий deploy/kiosks.txt: все 20 id проходят санитайзер и уникальны."""
    fleet = mkb.read_fleet(None)
    assert len(fleet) == 20
    assert len({k for k, _ in fleet}) == 20
    assert all(mkb.ID_RE.match(k) for k, _ in fleet)


# ---------- структура .bat (cmd падает молча, тестов у него нет) ----------
def _bat_code_lines(path):
    """Только исполняемые строки: REM и пустые выкидываем."""
    text = path.read_bytes().decode("utf-8")
    return [l for l in text.splitlines()
            if l.strip() and not l.strip().upper().startswith("REM")]


@pytest.mark.parametrize("which", ["BAT", "AUTOSTART"])
def test_bat_labels_and_blocks_are_consistent(which):
    """У cmd нет ни линтера, ни тестов: опечатка в метке или непарная скобка
    ломает файл уже НА ТОЧКЕ, где отладчика нет."""
    import re
    path = getattr(mkb, which)
    code = "\n".join(_bat_code_lines(path))
    labels = set(re.findall(r"^:([A-Za-z_]\w*)", code, re.M))
    gotos = set(re.findall(r"\bgoto\s+([A-Za-z_]\w*)", code))
    assert not (gotos - labels - {"eof"}), f"переход без метки: {gotos - labels}"
    # Считаем ГЛУБИНУ, а не пары: строка «) else (» и закрывает, и открывает.
    depth = 0
    for n, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(")"):
            depth -= 1
            assert depth >= 0, f"строка {n}: лишняя закрывающая скобка"
        if stripped.endswith("("):
            depth += 1
    assert depth == 0, f"незакрытых блоков: {depth}"


def test_bat_line_continuations_are_not_broken():
    """Пустая строка после ^ = cmd теряет продолжение и запускает браузер без флагов."""
    lines = mkb.BAT.read_bytes().decode("utf-8").splitlines()
    for i, l in enumerate(lines[:-1]):
        if l.rstrip().endswith("^"):
            assert lines[i + 1].strip(), f"строка {i + 1}: после ^ пустая строка"


def test_bat_guards_against_second_instance():
    """Главный баг 30.07: браузер с тем же профилем уже запущен -> новый процесс
    передаёт ему команду и выходит -> /wait возвращается -> цикл открывает окно
    каждые 3 секунды."""
    text = mkb.BAT.read_bytes().decode("utf-8")
    assert "AidosKiosk" in text and ":already" in text
    # ⚠️ Именно EQU 1: `if errorlevel 1` истинно и для 9009 «нет команды», и на
    # машине без PowerShell киоск НЕ ЗАПУСТИЛСЯ БЫ вовсе.
    assert "if %ERRORLEVEL% EQU 1 goto already" in text
    assert "if errorlevel 1 goto already" not in text
    # Проверка стоит и перед первым запуском, и в цикле перезапуска.
    assert text.count("if %ERRORLEVEL% EQU 1 goto already") == 2


def test_running_check_cannot_match_itself():
    """⚠️ Проверка ОБЯЗАНА фильтровать по ИМЕНИ процесса.

    Без этого она находила саму себя: строка *AidosKiosk* лежит в командной
    строке того же powershell.exe, который её ищет, — и киоск не запускался
    НИКОГДА (поймано на живой точке 30.07, экран «ALREADY running» сразу после
    «backend is up»). Тест сторожит именно это условие.
    """
    import re
    text = mkb.BAT.read_bytes().decode("utf-8")
    checks = [l for l in text.splitlines() if "powershell -NoProfile" in l]
    assert checks, "проверки «уже запущен» нет вовсе"
    for line in checks:
        assert "$_.Name" in line, "фильтра по имени процесса нет — найдёт саму себя"
        allowed = re.findall(r"'(\w+\.exe)'", line)
        assert allowed, "не видно списка процессов киоска"
        assert "powershell.exe" not in allowed and "cmd.exe" not in allowed


def test_running_check_is_not_inside_a_block():
    """Строка проверки содержит ( и ) — внутри многострочного if(...) cmd
    сломал бы разбор блока."""
    depth = 0
    for line in mkb.BAT.read_bytes().decode("utf-8").splitlines():
        s = line.strip()
        if not s or s.upper().startswith("REM"):
            continue
        if s.startswith(")"):
            depth -= 1
        if "powershell -NoProfile" in s:
            assert depth == 0, "проверка внутри блока if(...) — cmd упадёт"
        if s.endswith("("):
            depth += 1

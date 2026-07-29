"""Рубильник по точкам: отключённый киоск разворачивают ДО STT/RAG/TTS."""
import pytest

from app import kiosks
from tests.util import wav_bytes


@pytest.fixture
def disabled_list(tmp_path, monkeypatch):
    """Подсовываем свой файл списка и чистим кэш до и после теста."""
    path = tmp_path / "kiosks-disabled.txt"
    monkeypatch.setattr(kiosks.settings, "kiosks_disabled_file", str(path))
    kiosks.reset_cache()

    def _write(text: str | None):
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    yield _write
    kiosks.reset_cache()


# ---------- разбор файла ----------
def test_no_file_means_everyone_works(disabled_list):
    disabled_list(None)
    assert kiosks.disabled_message("astana") is None


def test_listed_kiosk_gets_its_message(disabled_list):
    disabled_list("turkestan  Киоск на ремонте до 5 августа\n")
    assert kiosks.disabled_message("turkestan") == "Киоск на ремонте до 5 августа"
    assert kiosks.disabled_message("astana") is None


def test_id_without_message_gets_default(disabled_list):
    disabled_list("zhetysu\n")
    assert kiosks.disabled_message("zhetysu") == kiosks.DEFAULT_MESSAGE


def test_comments_and_blank_lines_ignored(disabled_list):
    disabled_list("# vko  временно снят с рубильника\n\n   \nabai  ждём согласования\n")
    assert kiosks.disabled_message("vko") is None
    assert kiosks.disabled_message("abai") == "ждём согласования"


def test_star_disables_everything(disabled_list):
    """Окно обслуживания: гасим все точки разом, включая запросы без id."""
    disabled_list("*  Плановое обновление, вернёмся через час\n")
    assert kiosks.disabled_message("astana") == "Плановое обновление, вернёмся через час"
    assert kiosks.disabled_message(None) == "Плановое обновление, вернёмся через час"


def test_request_without_kiosk_id_not_blocked_by_name_list(disabled_list):
    disabled_list("turkestan  ремонт\n")
    assert kiosks.disabled_message(None) is None


def test_file_reread_after_change(disabled_list):
    """Отключение региона не должно требовать перезапуска сервиса."""
    path = disabled_list("turkestan  ремонт\n")
    assert kiosks.disabled_message("astana") is None
    # mtime может не измениться в пределах гранулярности — меняем и размер
    path.write_text("turkestan  ремонт\nastana  тоже отключили\n", encoding="utf-8")
    assert kiosks.disabled_message("astana") == "тоже отключили"


def test_unreadable_file_fails_open(disabled_list, monkeypatch):
    """Сломанный рубильник — повод чинить рубильник, а не глушить 20 точек."""
    disabled_list("turkestan  ремонт\n")
    assert kiosks.disabled_message("turkestan") is not None
    kiosks.reset_cache()

    def boom(*a, **kw):
        raise OSError("нет доступа")

    monkeypatch.setattr(kiosks.Path, "read_text", boom)
    assert kiosks.disabled_message("turkestan") is None


# ---------- эндпоинты ----------
def _post_voice(client, kiosk: str | None, url="/voice"):
    data = {"language": "russian"}
    if kiosk:
        data["kiosk"] = kiosk
    return client.post(url, files={"data": ("q.wav", wav_bytes(), "audio/wav")}, data=data)


def test_voice_blocked_for_disabled_kiosk(client, disabled_list):
    disabled_list("turkestan  Киоск на ремонте до 5 августа\n")
    r = _post_voice(client, "turkestan")
    assert r.status_code == 503
    assert r.json()["detail"] == "Киоск на ремонте до 5 августа"


def test_voice_works_for_other_kiosks(client, disabled_list):
    disabled_list("turkestan  ремонт\n")
    r = _post_voice(client, "astana")
    assert r.status_code == 200
    assert r.json()["answer"]


def test_voice_stream_blocked_for_disabled_kiosk(client, disabled_list):
    disabled_list("turkestan  ремонт\n")
    r = _post_voice(client, "turkestan", url="/voice/stream")
    assert r.status_code == 503
    assert r.json()["detail"] == "ремонт"


def test_blocked_request_does_not_reach_stt(client, disabled_list, monkeypatch):
    """Смысл ранней проверки: не платить за облако и не занимать слот семафора."""
    import app.main as main

    called = []

    async def tripwire(*a, **kw):
        called.append(1)
        return "не должно вызваться"

    monkeypatch.setattr(main.stt, "transcribe", tripwire)
    disabled_list("turkestan  ремонт\n")
    assert _post_voice(client, "turkestan").status_code == 503
    assert called == []


def test_blocked_request_is_logged_as_disabled(client, disabled_list, monkeypatch):
    """Люди у погашенной точки всё равно подходят — это должно быть видно."""
    import app.main as main

    written = []
    monkeypatch.setattr(main.logging_setup, "record_interaction",
                        lambda **rec: written.append(rec))
    disabled_list("turkestan  ремонт\n")
    _post_voice(client, "turkestan")
    assert len(written) == 1
    assert written[0]["error"] == "disabled"
    assert written[0]["kiosk"] == "turkestan"

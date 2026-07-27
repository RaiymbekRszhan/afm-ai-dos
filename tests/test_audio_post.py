"""Аудио-постобработка TTS: подрезка краёв, «хвост-сирота», гашение, паузы швов.

Это ТЕСТЫ-ЗАМОРОЗКА. Пороги в app/clients/tts.py (_EDGE_KEEP_MS, _ONSET_THRESH,
_BLOB_*, _F5_TAIL_FADE_MS, паузы на стыке) подобраны НА СЛУХ по живым артефактам
демо — «страшный звук» в хвосте, съеденные атаки слов, наезжающие куски. Границы
там тонкие (естественная пауза внутри речи до ~190 мс против отрыва артефакта
180–230 мс), и любой рефакторинг может сдвинуть их молча: регрессию поймали бы
только ушами на следующем демо.

Поэтому здесь проверяется НЕ «как правильно», а «как настроено сейчас», на
синтетических WAV (речь = меандр выше порога, пауза = нули). Если тест упал —
значит поведение звука изменилось: это либо осознанная правка (тогда обнови
ожидания и переслушай батарею scripts/tts_bench.py), либо регрессия.
"""
import io
import wave

import pytest

from app.clients import tts
from tests.util import wav_bytes, wav_from_segments, wav_nframes, wav_samples

FR = 24000  # F5 отдаёт 24 кГц — считаем на боевой частоте


def frames(ms: float) -> int:
    return int(FR * ms / 1000)


# Сколько кадров должно остаться после подрезки: сама речь + по _EDGE_KEEP_MS с краёв.
def expected_frames(speech_ms: float) -> int:
    return frames(speech_ms) + 2 * frames(tts._EDGE_KEEP_MS)


SPEECH = 5000   # заведомо громче _EDGE_THRESH (300)
QUIET = 100     # между _ONSET_THRESH (80) и _EDGE_THRESH (300): тихая атака С/Ш/Ф


# ---------- подрезка краёв ----------
def test_trim_evens_edges_to_keep_ms():
    """Гуляющая тишина по краям куска ровняется до _EDGE_KEEP_MS с каждой стороны."""
    blob = tts._trim_edge_silence(
        wav_from_segments([(300, 0), (2000, SPEECH), (300, 0)], FR))
    assert wav_nframes(blob) == expected_frames(2000)


def test_trim_keeps_quiet_onset():
    """Тихая атака (ниже порога сегментации, но выше _ONSET_THRESH) не срезается.

    Иначе трим съедал начало слова на глухой согласной («съедено начало куска»).
    """
    blob = tts._trim_edge_silence(
        wav_from_segments([(200, 0), (60, QUIET), (1500, SPEECH), (200, 0)], FR))
    assert wav_nframes(blob) == expected_frames(60 + 1500)


def test_trim_cuts_orphan_tail():
    """Короткий всплеск в самом конце, оторванный тишиной, — галлюцинация, режем."""
    blob = tts._trim_edge_silence(
        wav_from_segments([(100, 0), (2000, SPEECH), (250, 0), (300, SPEECH), (100, 0)], FR))
    assert wav_nframes(blob) == expected_frames(2000)


def test_trim_keeps_long_tail_speech():
    """Длинный хвост (> _BLOB_MAX_MS) — это речь, а не артефакт: не трогаем."""
    blob = tts._trim_edge_silence(
        wav_from_segments([(100, 0), (2000, SPEECH), (250, 0), (800, SPEECH), (100, 0)], FR))
    assert wav_nframes(blob) == expected_frames(2000 + 250 + 800)


def test_trim_keeps_short_pause_inside_speech():
    """Пауза короче _BLOB_JOIN_MS — внутри речи: сегменты склеиваются, хвост цел."""
    blob = tts._trim_edge_silence(
        wav_from_segments([(100, 0), (1800, SPEECH), (150, 0), (400, SPEECH), (100, 0)], FR))
    assert wav_nframes(blob) == expected_frames(1800 + 150 + 400)


def test_trim_keeps_tail_when_speech_too_short():
    """Речи до всплеска меньше _BLOB_MIN_SPEECH_MS — не режем.

    Страховка от обратного: у короткого куска (реплика «да») последний сегмент
    может быть и самим ответом.
    """
    blob = tts._trim_edge_silence(
        wav_from_segments([(100, 0), (800, SPEECH), (250, 0), (300, SPEECH), (100, 0)], FR))
    assert wav_nframes(blob) == expected_frames(800 + 250 + 300)


def test_trim_passthrough_when_not_wav():
    """Не WAV / не 16 бит — отдаём вход как есть: подрезка не должна ронять синтез."""
    assert tts._trim_edge_silence(b"not a wav at all") == b"not a wav at all"

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 8 бит — не наш формат
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(FR)
        w.writeframes(b"\x80" * 1000)
    eight_bit = buf.getvalue()
    assert tts._trim_edge_silence(eight_bit) == eight_bit


def test_trim_passthrough_when_all_silence():
    """Совсем тихий кусок (сегментов нет) возвращается как есть, а не пустым."""
    blob = wav_from_segments([(200, 0)], FR)
    assert tts._trim_edge_silence(blob) == blob


# ---------- гашение хвоста ответа ----------
def test_fade_out_tail_monotone_to_zero():
    """Последние _F5_TAIL_FADE_MS мс гаснут монотонно до нуля, тело не тронуто."""
    src = wav_from_segments([(500, 10000)], FR)
    out = tts._fade_out_tail(src, tts._F5_TAIL_FADE_MS)

    src_s, out_s = wav_samples(src), wav_samples(out)
    assert len(out_s) == len(src_s)
    fade = frames(tts._F5_TAIL_FADE_MS)
    body = len(src_s) - fade
    assert out_s[:body] == src_s[:body]                    # тело как было
    tail = [abs(v) for v in out_s[body:]]
    assert tail == sorted(tail, reverse=True)              # монотонно вниз
    assert tail[0] < 10000 and tail[-1] == 0               # начали гасить, кончили нулём


def test_fade_out_tail_noop():
    """ms<=0 и не-WAV — вход возвращается байт в байт."""
    src = wav_from_segments([(100, 10000)], FR)
    assert tts._fade_out_tail(src, 0) == src
    assert tts._fade_out_tail(b"not a wav", 40) == b"not a wav"


def test_fade_out_tail_shorter_than_fade():
    """Кусок короче окна гашения — гасим его целиком, а не падаем."""
    src = wav_from_segments([(10, 10000)], FR)          # 10 мс < 40 мс окна
    out = tts._fade_out_tail(src, tts._F5_TAIL_FADE_MS)
    assert wav_nframes(out) == wav_nframes(src)
    assert wav_samples(out)[-1] == 0


# ---------- пауза на стыке кусков ----------
def test_gap_ms_after_by_punctuation():
    """После точки — полная пауза, после запятой/двоеточия — половина."""
    assert tts._gap_ms_after("Кусок закончился.", 280) == 280
    assert tts._gap_ms_after("Так точно!", 280) == 280
    assert tts._gap_ms_after("Правда?", 280) == 280
    assert tts._gap_ms_after("Кусок оборван,", 280) == 140
    assert tts._gap_ms_after("перечисление:", 280) == 140
    assert tts._gap_ms_after("без знака вовсе", 280) == 140
    assert tts._gap_ms_after("хвостовые пробелы.   ", 280) == 280


def test_concat_uses_half_gap_after_comma():
    """Склейка берёт паузу по знаку на стыке (а не одинаковую на все швы)."""
    fr = 16000
    clip = wav_bytes(seconds=0.05, framerate=fr)         # 800 кадров
    out = tts._concat_wav([clip, clip], ["Первый кусок оборван,", "второй."], 300)
    assert wav_nframes(out) == 800 + int(fr * 150 / 1000) + 800


def test_concat_rejects_mismatched_format():
    """Куски с разной частотой склеивать нельзя — иначе тихо получим ускоренный звук."""
    with pytest.raises(RuntimeError, match="Несовместимые параметры WAV"):
        tts._concat_wav([wav_bytes(framerate=16000), wav_bytes(framerate=24000)],
                        ["Раз.", "Два."], 300)

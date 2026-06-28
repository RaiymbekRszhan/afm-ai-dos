"""Хелперы для тестов."""
import io
import wave


def wav_bytes(seconds: float = 0.05, framerate: int = 16000) -> bytes:
    """Короткий валидный WAV-блок (моно, 16-бит) для тестов TTS/конкатенации."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(b"\x00\x00" * int(framerate * seconds))
    return buf.getvalue()


def wav_nframes(blob: bytes) -> int:
    with wave.open(io.BytesIO(blob), "rb") as r:
        return r.getnframes()

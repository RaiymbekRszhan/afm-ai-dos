"""Хелперы для тестов."""
import array
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


def wav_from_segments(segments: list[tuple[float, int]], framerate: int = 24000,
                      nchannels: int = 1) -> bytes:
    """WAV из списка (длительность_мс, амплитуда) — синтетический звук для тестов.

    Амплитуда 0 — тишина, иначе меандр ±амплитуда (постоянная громкость, знак
    чередуется — так «речь» выглядит волной, а не постоянным смещением).
    Частота по умолчанию 24 кГц — как у F5, чтобы длительности в тестах
    совпадали с боевыми (пороги подрезки заданы в миллисекундах).
    """
    samples = array.array("h")
    for ms, amp in segments:
        for i in range(int(framerate * ms / 1000)):
            samples.extend([amp if i % 2 == 0 else -amp] * nchannels)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def wav_samples(blob: bytes) -> array.array:
    """Сэмплы WAV одним массивом int16 (каналы вперемешку, как в файле)."""
    with wave.open(io.BytesIO(blob), "rb") as r:
        frames = r.readframes(r.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    return samples

"""Семафор TTS-конкурентности (N12): прямой /speak идёт через тот же лимит, что и
/voice, — пачка вызовов не занимает единственный TTS/GPU-ресурс без ограничения.
Проверяем на seam-е _guarded_synthesize, без завязки на тайминги (по факту
одновременного входа под семафором=1).
"""
import asyncio

import app.main as main


def test_guarded_synthesize_serializes_under_limit(monkeypatch):
    async def scenario():
        monkeypatch.setattr(main, "_tts_sem", asyncio.Semaphore(1))
        state = {"cur": 0, "max": 0}
        gate = asyncio.Event()

        async def fake_synth(text, language=None):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await gate.wait()      # держим ресурс, пока не отпустим обе задачи
            state["cur"] -= 1
            return b"RIFF"

        monkeypatch.setattr(main.tts, "synthesize", fake_synth)

        t1 = asyncio.create_task(main._guarded_synthesize("a", "russian"))
        t2 = asyncio.create_task(main._guarded_synthesize("b", "russian"))
        # даём обеим задачам попытаться войти; при семафоре=1 вторая ждёт очереди
        for _ in range(5):
            await asyncio.sleep(0)
        assert state["max"] == 1   # одновременно синтезирует только ОДНА

        gate.set()
        results = await asyncio.gather(t1, t2)
        assert results == [b"RIFF", b"RIFF"]
        assert state["max"] == 1   # и после разблокировки лимит не превышался

    asyncio.run(scenario())

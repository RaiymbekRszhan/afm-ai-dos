# -*- coding: utf-8 -*-
"""Автозапуск авторежима Ai-dos при старте Unreal Editor (для работы ноды 24/7).

Положить в <проект UE>/Content/Python/ РЯДОМ с aidos_editor.py — файл с именем
init_unreal.py редактор исполняет сам при каждом запуске. Скрипт ждёт
AIDOS_AUTOWATCH_DELAY секунд (по умолчанию 30 — даём проекту и плагинам
догрузиться), затем включает a.watch() и пробует запустить трансляцию
Pixel Streaming. Вместе с watchdog.ps1 это даёт цикл без человека:
редактор упал → watchdog поднял → init_unreal включил авторежим.

Отключить (например, для ручной отладки): переменная окружения AIDOS_AUTOWATCH=0.
"""

import os
import time

import unreal

_DELAY = float(os.environ.get("AIDOS_AUTOWATCH_DELAY", "30"))
_state = {"t0": time.time(), "handle": None, "done": False}


def _try_start_stream():
    """Best-effort автостарт трансляции Pixel Streaming: без него после
    перезапуска редактора аватар говорит, но видео в браузер не идёт.
    Имя команды различается между версиями плагина — неизвестная команда
    безвредно напишет Unknown command в лог. Если ни одна не сработала,
    сверить API: в консоли ноды `import aidos_editor as a; a.dump_api()`
    и раздел «Живое видео» в unreal/README.md."""
    for cmd in ("PixelStreaming2.StartStreaming", "PixelStreaming.StartStreaming"):
        try:
            unreal.SystemLibrary.execute_console_command(None, cmd)
        except Exception:
            pass


def _tick(_dt):
    if _state["done"] or time.time() - _state["t0"] < _DELAY:
        return
    _state["done"] = True
    try:
        import aidos_editor as a
        a.watch()
        _try_start_stream()
        unreal.log("[aidos] init_unreal: авторежим watch() запущен")
    except Exception as e:
        unreal.log_warning("[aidos] init_unreal: автозапуск не удался: %s" % e)
    finally:
        if _state["handle"] is not None:
            unreal.unregister_slate_post_tick_callback(_state["handle"])
            _state["handle"] = None


if os.environ.get("AIDOS_AUTOWATCH", "1").strip().lower() not in ("0", "false", "off"):
    _state["handle"] = unreal.register_slate_post_tick_callback(_tick)
    unreal.log("[aidos] init_unreal: watch() включится через %d с "
               "(отключить: AIDOS_AUTOWATCH=0)" % _DELAY)

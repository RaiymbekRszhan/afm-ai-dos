"""Рубильник по точкам: какие киоски сейчас не принимают вопросы.

Зачем: точка может быть не готова (ремонт, регион ещё не согласован) или её
нужно временно погасить, а физически до неё — другой город. Список лежит
ФАЙЛОМ на сервере и перечитывается по времени изменения, поэтому отключение
региона = дописать строку. Перезапускать оркестратор не нужно: на киоске в
этот момент может идти разговор с гражданином.

    # kiosks-disabled.txt в корне проекта (путь — KIOSKS_DISABLED_FILE)
    turkestan   Киоск на ремонте до 5 августа
    zhetysu
    #vko        строка с # выключена — точка снова работает

Текст после id — то, что увидит гражданин на экране. Без текста берётся
`DEFAULT_MESSAGE`. Особый id `*` гасит приём на ВСЕХ точках сразу (окно
обслуживания), включая запросы без номера точки.

⚠️ Это рубильник ЭКСПЛУАТАЦИОННЫЙ, а НЕ защита доступа. Номер точки приходит
от браузера (`?id=` в адресе страницы) и подделывается тривиально: выключить
свой киоск он позволяет, закрыть доступ чужому — нет. Для настоящей границы
нужны списки IP или токен на точку.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.config import settings

log = logging.getLogger("ai_dos.api")

# Гражданин стоит у экрана и ждёт ответа — сообщение должно объяснять, а не
# показывать код ошибки. Конкретную причину дописывают в файле после id.
DEFAULT_MESSAGE = "Сервис временно недоступен. Приносим извинения."

# Особый id: гасит все точки разом.
ALL = "*"

# Кэш: (отпечаток файла, таблица). Отпечаток None = файла нет.
_cache: tuple[tuple[int, int] | None, dict[str, str]] | None = None


def _stamp(path: Path) -> tuple[int, int] | None:
    """Отпечаток файла: mtime в наносекундах + размер. Файла нет — None."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _parse(text: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kiosk_id, _, message = line.partition(" ")
        table[kiosk_id] = message.strip() or DEFAULT_MESSAGE
    return table


def _table() -> dict[str, str]:
    global _cache
    path = Path(settings.kiosks_disabled_file)
    stamp = _stamp(path)
    if _cache is not None and _cache[0] == stamp:
        return _cache[1]
    table: dict[str, str] = {}
    if stamp is not None:
        try:
            table = _parse(path.read_text(encoding="utf-8"))
        except OSError as e:
            # Нечитаемый файл НЕ должен глушить приём граждан: сломанный
            # рубильник — повод чинить рубильник, а не закрывать все 20 точек.
            log.warning("список отключённых киосков %s не прочитан: %r", path, e)
    _cache = (stamp, table)
    return table


def disabled_message(kiosk: str | None) -> str | None:
    """Текст для экрана, если точка отключена. Работает — None."""
    table = _table()
    if not table:
        return None
    if ALL in table:
        return table[ALL]
    return table.get(kiosk) if kiosk else None


def reset_cache() -> None:
    """Забыть прочитанное (нужно тестам: они подменяют путь на лету)."""
    global _cache
    _cache = None

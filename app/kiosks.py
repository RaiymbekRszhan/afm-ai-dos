"""Флот киосков: кто жив, кто отключён, и рубильник по точкам.

Три вещи в одном месте, потому что это одно состояние:

* **рубильник** — какие точки сейчас не принимают вопросы. Список лежит ФАЙЛОМ
  (`kiosks-disabled.txt`) и перечитывается по времени изменения, поэтому его
  можно править и руками по ssh, и из админки — оба пути видят одно и то же;
* **heartbeat** — когда точка последний раз давала о себе знать. Страница киоска
  пингует `/kiosk/ping`, сервер запоминает время. Без этого погасшую точку видно
  только по отсутствию строк в отчёте, то есть постфактум и на глаз;
* **список флота** — `deploy/kiosks.txt`, чтобы в админке были все 20 регионов
  с человеческими названиями, включая те, что не включались ни разу.

Формат файла отключений:

    turkestan   Киоск на ремонте до 5 августа
    zhetysu
    #vko        строка с # выключена — точка снова работает

Текст после id — то, что увидит гражданин. Без текста берётся `DEFAULT_MESSAGE`.
Особый id `*` гасит приём на ВСЕХ точках (окно обслуживания).

⚠️ Рубильник ЭКСПЛУАТАЦИОННЫЙ, а НЕ защита доступа. Номер точки приходит от
браузера (`?id=` в адресе) и подделывается тривиально: выключить свой киоск он
позволяет, закрыть доступ чужому — нет. Для настоящей границы нужны списки IP
или токен на точку.

⚠️ Heartbeat живёт В ПАМЯТИ процесса: после перезапуска `ai-dos-api` все точки
покажутся молчащими, пока не придёт следующий пинг (до `KIOSK_PING_SECONDS`).
Это осознанный размен — состояние «кто жив» дешевле пересобрать, чем хранить.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path

from app.config import settings

log = logging.getLogger("ai_dos.api")

# Гражданин стоит у экрана и ждёт ответа — сообщение должно объяснять, а не
# показывать код ошибки. Конкретную причину дописывают в файле после id.
DEFAULT_MESSAGE = "Сервис временно недоступен. Приносим извинения."

# Особый id: гасит все точки разом.
ALL = "*"

# Тот же набор символов, что у санитайзера страницы и `_clean_kiosk` в main.
_ID_CHARS = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def valid_id(kiosk_id: str) -> bool:
    """`*` (окно обслуживания) либо нормальный id точки.

    Кроме набора символов требуем хотя бы одну букву или цифру: id из одних
    точек и дефисов в отчёте читается как сбой, а не как название региона.
    """
    if kiosk_id == ALL:
        return True
    return bool(_ID_CHARS.match(kiosk_id)) and any(c.isalnum() for c in kiosk_id)

# Кэш файла отключений: (отпечаток, таблица). Отпечаток None = файла нет.
_cache: tuple[tuple[int, int] | None, dict[str, str]] | None = None
# Кэш списка флота — тот же приём.
_fleet_cache: tuple[tuple[int, int] | None, list[tuple[str, str]]] | None = None

# Кэш таблицы ключей — тот же приём.
_keys_cache: tuple[tuple[int, int] | None, dict[str, str]] | None = None

# Живость точек: kiosk -> {"ping": ts, "ask": ts, "asks": n}. В памяти процесса.
_seen: dict[str, dict[str, float]] = {}

# Темп запросов: кто -> времена последних обращений (скользящее окно).
_rate: dict[str, list[float]] = {}


def _stamp(path: Path) -> tuple[int, int] | None:
    """Отпечаток файла: mtime в наносекундах + размер. Файла нет — None."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ---------- рубильник ----------
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


def set_enabled(kiosk_id: str, enabled: bool, message: str = "") -> None:
    """Включить/выключить точку, сохранив остальные строки файла.

    Пишем через временный файл и os.replace: читатель (соседний запрос) увидит
    либо старую версию целиком, либо новую, но не половину.
    """
    if not valid_id(kiosk_id):
        raise ValueError(f"недопустимый id точки: {kiosk_id!r}")
    # Перевод строки в тексте разрезал бы файл и подделал соседнюю запись —
    # ровно та же причина, по которой чистится kiosk в /voice.
    message = " ".join(message.split()).strip()

    path = Path(settings.kiosks_disabled_file)
    try:
        old = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        old = []

    out: list[str] = []
    found = False
    for raw in old:
        stripped = raw.strip()
        body = stripped[1:].strip() if stripped.startswith("#") else stripped
        if body and body.partition(" ")[0] == kiosk_id:
            found = True
            if not enabled:  # переписываем строку свежим текстом
                out.append(f"{kiosk_id} {message}".strip())
            continue         # включаем — строку просто убираем
        out.append(raw)
    if not enabled and not found:
        out.append(f"{kiosk_id} {message}".strip())

    text = "\n".join(out).strip()
    text = text + "\n" if text else ""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    reset_cache()


# ---------- список флота ----------
def _parse_fleet(text: str) -> list[tuple[str, str]]:
    """`deploy/kiosks.txt`: строки «<id> <человеческое название>».

    Разбор намеренно продублирован в scripts/make_kiosk_bundles.py: сборщик
    архивов обязан работать на голой stdlib, без pydantic и настроек.
    """
    fleet: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kiosk_id, _, human = line.partition(" ")
        if valid_id(kiosk_id):
            fleet.append((kiosk_id, human.strip() or kiosk_id))
    return fleet


def fleet() -> list[tuple[str, str]]:
    global _fleet_cache
    path = Path(settings.kiosks_file)
    stamp = _stamp(path)
    if _fleet_cache is not None and _fleet_cache[0] == stamp:
        return _fleet_cache[1]
    rows: list[tuple[str, str]] = []
    if stamp is not None:
        try:
            rows = _parse_fleet(path.read_text(encoding="utf-8"))
        except OSError as e:
            log.warning("список киосков %s не прочитан: %r", path, e)
    _fleet_cache = (stamp, rows)
    return rows


# ---------- пропуск точки ----------
def _keys() -> dict[str, str]:
    """Таблица «id точки -> ключ» из файла (кэш по времени изменения)."""
    global _keys_cache
    path = Path(settings.kiosk_keys_file)
    stamp = _stamp(path)
    if _keys_cache is not None and _keys_cache[0] == stamp:
        return _keys_cache[1]
    table: dict[str, str] = {}
    if stamp is not None:
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                kiosk_id, _, key = line.partition(" ")
                key = key.strip()
                if key and valid_id(kiosk_id):
                    table[kiosk_id] = key
        except OSError as e:
            log.warning("список ключей киосков %s не прочитан: %r", path, e)
    _keys_cache = (stamp, table)
    return table


def key_ok(kiosk: str | None, key: str | None) -> bool:
    """Пускать ли запрос с этой точки.

    Смысл пропуска: без него `?id=` — просто подпись, и отключённый регион
    обходит рубильник, убрав параметр из ярлыка. С ключом сервер сверяет имя с
    ключом, и притвориться соседом уже нельзя.

    Мягкий режим (`KIOSK_KEY_REQUIRED=false`, по умолчанию): НЕВЕРНЫЙ ключ
    отвергаем всегда, отсутствие ключа — пропускаем. Так проверку можно
    выкатить, не погасив точки, которые ещё не получили архив.

    ⚠️ Ключ лежит текстом на машине в помещении, куда ходят люди: он закрывает
    постороннего в сети, но не того, кто стоит у самого киоска.
    """
    table = _keys()
    if not table:            # ключи ещё не заведены — проверять нечем
        return True
    expected = table.get(kiosk) if kiosk else None
    if key:
        # Ключ прислали: он обязан совпасть с ключом ИМЕННО этой точки. Иначе
        # чужой ключ + чужое имя = обход рубильника.
        return expected is not None and secrets.compare_digest(key, expected)
    return not settings.kiosk_key_required


# ---------- ограничение частоты ----------
def rate_ok(who: str) -> bool:
    """Не превышен ли темп запросов у этой точки (скользящее окно в минуту).

    Считаем по точке, а если она не назвалась — по адресу. Один клиент не должен
    занимать оба слота семафора бесконечно: тогда 20 городов ждут за ним.
    """
    limit = settings.kiosk_rate_per_min
    if limit <= 0:
        return True
    now = time.time()
    hits = _rate.setdefault(who, [])
    cutoff = now - 60
    hits[:] = [t for t in hits if t > cutoff]
    # Чтобы словарь не рос без конца от случайных адресов: раз в сколько-то
    # вызовов выкидываем тех, у кого за минуту не было ни одного запроса.
    if len(_rate) > 500:
        for k in [k for k, v in _rate.items() if not v]:
            del _rate[k]
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


# ---------- heartbeat ----------
def touch_ping(kiosk: str | None) -> None:
    """Точка дала о себе знать (периодический пинг со страницы)."""
    if kiosk:
        _seen.setdefault(kiosk, {})["ping"] = time.time()


def note_key(kiosk: str | None, has_key: bool) -> None:
    """Запоминаем, предъявила ли точка пропуск в последнем обращении.

    Нужно перед включением строгого режима (`KIOSK_KEY_REQUIRED=true`): в мягком
    режиме запрос БЕЗ ключа проходит молча, и узнать, все ли 20 регионов уже
    переустановили архив, было нечем — флаг щёлкали наугад, а о погашенных
    точках узнавали от них по телефону.
    """
    if kiosk:
        _seen.setdefault(kiosk, {})["key"] = 1.0 if has_key else 0.0


def touch_ask(kiosk: str | None) -> None:
    """С точки задали вопрос — она жива даже без пинга (старая версия страницы)."""
    if not kiosk:
        return
    rec = _seen.setdefault(kiosk, {})
    now = time.time()
    rec["ping"] = now
    rec["ask"] = now
    rec["asks"] = rec.get("asks", 0) + 1


def status_rows(now: float | None = None) -> list[dict]:
    """Строки для админки: весь флот + точки, которых нет в списке, но они пингуют."""
    now = now if now is not None else time.time()
    table = _table()
    maintenance = table.get(ALL)
    known = fleet()
    extra = sorted(set(_seen) - {k for k, _ in known})
    rows = []
    for kiosk_id, human in known + [(k, k) for k in extra]:
        seen = _seen.get(kiosk_id, {})
        own = table.get(kiosk_id)
        ping = seen.get("ping")
        rows.append({
            "kiosk": kiosk_id,
            "human": human,
            # Точка отключена либо лично, либо общим окном обслуживания.
            "enabled": own is None and maintenance is None,
            "disabled_here": own is not None,
            "message": own or maintenance or "",
            "online": ping is not None and (now - ping) <= settings.kiosk_offline_after_s,
            "ping_ago_s": int(now - ping) if ping else None,
            "ask_ago_s": int(now - seen["ask"]) if seen.get("ask") else None,
            "asks": int(seen.get("asks", 0)),
            # None — точка ещё не обращалась, про пропуск сказать нечего.
            "has_key": None if "key" not in seen else bool(seen["key"]),
            "in_fleet": kiosk_id not in extra,
        })
    return rows


def maintenance_message() -> str | None:
    """Текст окна обслуживания (`*`), если оно включено."""
    return _table().get(ALL)


# --- Версия страницы: чем киоск узнаёт, что пора перечитать себя -----------
# Браузер на точке открыт круглосуточно и код страницы сам не перечитывает: до
# этого правка в static/** доезжала до региона только с перезапуском .bat, то
# есть обзвоном 20 городов. Теперь версия едет в ответе на пинг, и точка
# перезагружается сама.
#
# Версию складываем из двух источников, и оба нужны:
#   * отпечаток файлов страницы — выкатка новой статики перезагружает флот САМА;
#   * метка-файл (кнопка в админке) — перезагрузить можно и без правки кода
#     (браузер завис, надо снять зависшее состояние, откатили конфиг).
_UI_VERSION_TTL = 5.0
_ui_cache: tuple[float, str] | None = None


def _ui_fingerprint() -> str:
    parts = []
    stamp = _stamp(Path(settings.kiosks_reload_file))
    parts.append(f"mark:{stamp[0]}:{stamp[1]}" if stamp else "mark:-")
    root = Path(settings.ui_static_dir)
    try:
        # Только код страницы. Ролики (.mp4) намеренно мимо: они тяжёлые, их
        # замена не меняет поведение, а перекачка на 20 точках — дорогая.
        files = sorted(p for p in root.rglob("*")
                       if p.suffix in (".html", ".js", ".css"))
    except OSError as e:
        log.warning("не прочитал код страницы %s: %r", root, e)
        files = []
    for p in files:
        s = _stamp(p)
        if s:
            parts.append(f"{p.name}:{s[0]}:{s[1]}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def ui_version() -> str:
    """Отпечаток текущей версии страницы киоска.

    Считается по времени изменения файлов, а не по их содержимому: пинги идут с
    20 точек каждую минуту, и читать всю статику ради этого незачем. Плюс
    кэш на несколько секунд — чтобы залп пингов стоил один обход каталога.
    """
    global _ui_cache
    now = time.monotonic()
    if _ui_cache is not None and now - _ui_cache[0] < _UI_VERSION_TTL:
        return _ui_cache[1]
    version = _ui_fingerprint()
    _ui_cache = (now, version)
    return version


def request_reload() -> None:
    """Сказать всем точкам перечитать страницу (кнопка в админке).

    Трогаем метку-файл: его время изменения входит в версию, значит ближайший
    пинг каждой точки вернёт новое значение. Ничего не рассылаем и никуда не
    стучимся — киоски забирают сами, поэтому команда доходит и до точки, которая
    сейчас недоступна: она увидит новую версию, когда вернётся на связь.
    """
    path = Path(settings.kiosks_reload_file)
    path.write_text(f"{time.time():.3f}\n", encoding="utf-8")
    reset_ui_version()


def reset_ui_version() -> None:
    """Забыть посчитанную версию (после записи метки и в тестах)."""
    global _ui_cache
    _ui_cache = None


def reset_cache() -> None:
    """Забыть прочитанное (нужно тестам и после записи файла)."""
    global _cache, _fleet_cache, _keys_cache
    _cache = None
    _fleet_cache = None
    _keys_cache = None
    reset_ui_version()


def reset_seen() -> None:
    """Забыть живость точек и накопленный темп (тесты)."""
    _seen.clear()
    _rate.clear()

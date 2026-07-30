"""Сборка архивов для рассылки по киоскам: по одному ZIP на город.

    python -m scripts.make_kiosk_bundles                 # все точки из deploy/kiosks.txt
    python -m scripts.make_kiosk_bundles --only astana   # один город (перевыпуск)
    python -m scripts.make_kiosk_bundles --list          # что будет собрано

ЗАЧЕМ ZIP, А НЕ .bat РОССЫПЬЮ. Файл на всех точках один и тот же, отличается
только `kiosk-id.txt` с названием точки — а именно оно попадает в логи полем
`kiosk` и в раздел «По киоскам» отчёта. Рассылать .bat отдельным файлом опасно
по двум причинам: мессенджеры и Windows ругаются на исполняемые скрипты, а
получатель может открыть его предпросмотром и пересохранить — переводы строк
станут LF, и cmd.exe упадёт на `cannot find the batch label - waitloop`.
Внутри ZIP байты не меняет никто.

ЧТО ВНУТРИ КАЖДОГО АРХИВА:
    kiosk-start.bat       — байт в байт из deploy/, с CRLF
    install-autostart.bat — ставит автозапуск одним запуском (на киоске сенсорный
                            экран без клавиатуры, Win+R там не нажать)
    kiosk-id.txt          — одна строка: id точки (ASCII, CRLF, без BOM)
    kiosk-key.txt         — пропуск ЭТОЙ точки: сервер сверяет имя с ключом,
                            иначе отключённый регион обошёл бы рубильник,
                            убрав `&id=` из ярлыка
    README.txt            — установка (UTF-8 с BOM: старый notepad без BOM
                            показывает кириллицу кракозябрами)

Скрипт на stdlib: в сети АФМ ставить пакеты неоткуда.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "deploy" / "kiosk-start.bat"
AUTOSTART = ROOT / "deploy" / "install-autostart.bat"
LIST = ROOT / "deploy" / "kiosks.txt"
OUT = ROOT / "out" / "kiosk-bundles"
# Пропуска точек: «<id> <ключ>». СЕКРЕТ и одновременно ИСТОЧНИК ПРАВДЫ —
# при пересборке архивов ключи берутся отсюда, иначе у регионов, которым уже
# отправили архив, пропуск перестал бы совпадать с серверным.
KEYS = ROOT / "kiosks-keys.txt"

# Тот же набор символов, что у санитайзера страницы (index.html:262) и сервера
# (app/main.py:_clean_kiosk). Если id не проходит здесь — он не пройдёт и там,
# только там это выяснится через месяц по пустой колонке в отчёте.
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

README = """Ai-dos — киоск АФМ. Установка занимает две минуты.

Точка: {human}
В логах сервера она будет называться: {kiosk_id}

Для установки понадобятся клавиатура и мышь — один раз. Дальше киоск работает
только сенсорным экраном, трогать его не нужно.


ШАГ 1. Распакуйте ВСЕ файлы архива в одну папку, например C:\\Aidos\\

   Файлы должны лежать РЯДОМ друг с другом. kiosk-id.txt задаёт название точки
   в логах; без него киоск запустится, но в отчёте будет виден как имя
   компьютера, и понять, какой это регион, будет нельзя.


ШАГ 2. Двойной клик по kiosk-start.bat — откроется полноэкранный киоск.

   Проверьте, что аватар отвечает на вопрос голосом. Если да — переходите
   к шагу 3. Если нет, напишите нам, что написано в чёрном окне.


ШАГ 3. Двойной клик по install-autostart.bat — киоск будет включаться сам.

   ЭТОТ ШАГ ОБЯЗАТЕЛЕН. Без него после отключения света или обновления Windows
   на экране останется рабочий стол, пока кто-то не придёт и не запустит киоск
   руками. Файл сам создаёт ярлык в автозагрузке и отключает засыпание экрана.


ШАГ 4. Включите автоматический вход в Windows.

   Windows запускает автозагрузку В МОМЕНТ ВХОДА ПОЛЬЗОВАТЕЛЯ, а не при подаче
   питания. Если система на входе спрашивает пароль, киоск после перезагрузки
   застрянет на экране блокировки — а клавиатуры на точке нет.

   Win+R -> netplwiz -> снять галочку «Требовать ввод имени пользователя
   и пароля» -> ОК -> ввести пароль этого пользователя дважды.
   Галочки нет? Параметры -> Учётные записи -> Варианты входа -> выключить
   «Требовать выполнение входа с Windows Hello», затем повторить netplwiz.

   Проверка: перезагрузите машину и не трогайте её. Через минуту-две киоск
   должен подняться сам.


Выход из киоска: закрыть чёрное консольное окно. Alt+F4 по браузеру не
поможет — скрипт откроет браузер снова, это и есть режим «киоск 24/7».

Нужны: Google Chrome или Microsoft Edge и сеть до сервера {server}.
Больше ничего устанавливать не нужно — распознавание речи, поиск по базе
и озвучка работают на сервере.

!!! НЕ открывайте .bat-файлы в блокноте и не пересохраняйте их.
Они обязаны остаться с CRLF-переводами строк; после пересохранения в другом
формате cmd.exe выдаст «cannot find the batch label - waitloop» и киоск не
запустится. Нужно что-то поправить — попросите новый архив.
"""


def read_fleet(only: str | None) -> list[tuple[str, str]]:
    if not LIST.exists():
        sys.exit(f"нет списка точек: {LIST}")
    fleet: list[tuple[str, str]] = []
    seen: set[str] = set()
    for n, raw in enumerate(LIST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kiosk_id, _, human = line.partition(" ")
        human = human.strip() or kiosk_id
        if not ID_RE.match(kiosk_id):
            sys.exit(f"{LIST}:{n}: id «{kiosk_id}» не пройдёт санитайзер "
                     f"(нужны латиница/цифры/._- до 32 символов)")
        if kiosk_id in seen:
            sys.exit(f"{LIST}:{n}: id «{kiosk_id}» встречается дважды — "
                     f"в отчёте две точки слипнутся в одну строку")
        seen.add(kiosk_id)
        if only and kiosk_id != only:
            continue
        fleet.append((kiosk_id, human))
    if only and not fleet:
        sys.exit(f"в {LIST} нет точки «{only}»")
    return fleet


def _read_crlf_bat(path: Path) -> bytes:
    """Проверяем ровно ту мину, которая уже стреляла: .bat с LF не запускается."""
    if not path.exists():
        sys.exit(f"нет файла киоска: {path}")
    data = path.read_bytes()
    lone_lf = data.count(b"\n") - data.count(b"\r\n")
    if lone_lf:
        sys.exit(f"{path}: {lone_lf} строк(и) с LF вместо CRLF — cmd.exe на таком "
                 f"файле падает. Перевыкачай из git (.gitattributes держит CRLF).")
    return data


def check_bat() -> bytes:
    return _read_crlf_bat(BAT)


def check_autostart() -> bytes:
    return _read_crlf_bat(AUTOSTART)


def load_or_make_keys(fleet: list[tuple[str, str]]) -> dict[str, str]:
    """Ключи точек: существующие БЕРЁМ КАК ЕСТЬ, новым выдаём случайные.

    Стабильность здесь важнее аккуратности: если пересборка выдаст новый ключ
    региону, которому архив уже отправлен, его киоск перестанет опознаваться —
    а на точке никого нет, кто это заметит и переустановит.
    """
    keys: dict[str, str] = {}
    if KEYS.exists():
        for raw in KEYS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            kiosk_id, _, key = line.partition(" ")
            if key.strip():
                keys[kiosk_id] = key.strip()
    fresh = [k for k, _ in fleet if k not in keys]
    for kiosk_id in fresh:
        keys[kiosk_id] = secrets.token_hex(12)
    if fresh:
        body = ["# Пропуска киосков: «<id> <ключ>». СЕКРЕТ — в git не попадает.",
                "# Этот файл нужен И на сервере (kiosks-keys.txt в корне проекта),",
                "# и здесь: отсюда ключи берутся при пересборке архивов.", ""]
        body += [f"{k} {keys[k]}" for k, _ in sorted(fleet)]
        # Точки, выбывшие из списка флота, не теряем: вернут — ключ совпадёт.
        body += [f"{k} {v}" for k, v in sorted(keys.items())
                 if k not in {i for i, _ in fleet}]
        KEYS.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"выдано новых ключей: {len(fresh)} -> {KEYS}")
    return keys


def server_from_bat(data: bytes) -> str:
    m = re.search(rb'set "SERVER=([^"]+)"', data)
    port = re.search(rb'set "PORT=([^"]+)"', data)
    host = m.group(1).decode() if m else "?"
    p = port.group(1).decode() if port else "80"
    return host if p == "80" else f"{host}:{p}"


def main() -> int:
    ap = argparse.ArgumentParser(description="ZIP-архивы киосков для рассылки")
    ap.add_argument("--only", help="собрать один город по id")
    ap.add_argument("--list", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    bat = check_bat()
    autostart = check_autostart()
    server = server_from_bat(bat)
    fleet = read_fleet(args.only)

    if args.list:
        print(f"Сервер в .bat: {server}\nБудет собрано {len(fleet)} архив(ов):")
        for kiosk_id, human in fleet:
            print(f"  aidos-kiosk-{kiosk_id}.zip   -> {human}")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)

    # Переименовали точку — от прошлой сборки остался архив со старым id.
    # Отправить его региону = записать город в логи чужим именем, поэтому
    # чистим сами. При --only не трогаем: там собирается одна точка из многих.
    if not args.only:
        keep = {f"aidos-kiosk-{k}.zip" for k, _ in fleet}
        for stale in sorted(OUT.glob("aidos-kiosk-*.zip")):
            if stale.name not in keep:
                stale.unlink()
                print(f"убран устаревший архив: {stale.name}")

    keys = load_or_make_keys(fleet)
    made = []
    for kiosk_id, human in fleet:
        path = OUT / f"aidos-kiosk-{kiosk_id}.zip"
        readme = README.format(human=human, kiosk_id=kiosk_id, server=server)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("kiosk-start.bat", bat)
            z.writestr("install-autostart.bat", autostart)
            # CRLF и здесь: оба файла читает cmd через `set /p`, и лишний байт в
            # конце строки уехал бы прямо в название точки или в её пропуск.
            z.writestr("kiosk-id.txt", (kiosk_id + "\r\n").encode("ascii"))
            z.writestr("kiosk-key.txt", (keys[kiosk_id] + "\r\n").encode("ascii"))
            z.writestr("README.txt", readme.encode("utf-8-sig"))
        made.append((kiosk_id, human, path))

    # Список рассылки: 20 адресатов руками в мессенджере — идеальное место
    # перепутать архивы, поэтому печатаем и кладём рядом файлом. Ссылку даём
    # целиком: по ней проверяется точка до того, как архив уехал в регион.
    lines = [f"Рассылка киосков Ai-dos (сервер {server})", ""]
    for kiosk_id, human, path in made:
        lines.append(f"{human}")
        lines.append(f"    архив:  {path.name}")
        lines.append(f"    ссылка: http://{server}/?kiosk=1&id={kiosk_id}"
                     f"&key={keys[kiosk_id]}")
        lines.append("")
    lines.append("Каждому региону — ТОЛЬКО его архив: внутри его имя и его пропуск.")
    lines.append("⚠️ Ссылки содержат пропуска — это секреты, наружу не выкладывать.")
    (OUT / "SEND-LIST.txt").write_text("\n".join(lines), encoding="utf-8-sig")

    print("\n".join(lines))
    print(f"Готово: {len(made)} архив(ов) в {OUT}")
    print(f"\n⚠️ Ключи нужны И НА СЕРВЕРЕ, иначе он никого не опознает:")
    print(f"  scp {KEYS.name} root@{server}:/root/afm-ai-dos/kiosks-keys.txt")
    print("Проверить один архив перед рассылкой:")
    print(f"  unzip -p {OUT}/aidos-kiosk-{made[0][0]}.zip kiosk-key.txt | od -c | head -2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

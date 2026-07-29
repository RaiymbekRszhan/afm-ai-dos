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
    kiosk-start.bat   — байт в байт из deploy/, с CRLF
    kiosk-id.txt      — одна строка: id точки (ASCII, CRLF, без BOM)
    README.txt        — установка в три шага (UTF-8 с BOM: старый notepad
                        без BOM показывает кириллицу кракозябрами)

Скрипт на stdlib: в сети АФМ ставить пакеты неоткуда.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "deploy" / "kiosk-start.bat"
LIST = ROOT / "deploy" / "kiosks.txt"
OUT = ROOT / "out" / "kiosk-bundles"

# Тот же набор символов, что у санитайзера страницы (index.html:262) и сервера
# (app/main.py:_clean_kiosk). Если id не проходит здесь — он не пройдёт и там,
# только там это выяснится через месяц по пустой колонке в отчёте.
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

README = """Ai-dos — киоск АФМ. Установка занимает две минуты.

Точка: {human}
В логах сервера она будет называться: {kiosk_id}

1. Распакуйте ОБА файла в одну папку, например C:\\Aidos\\
   kiosk-start.bat и kiosk-id.txt должны лежать РЯДОМ: второй задаёт название
   точки в логах. Без него киоск запустится, но в отчёте будет виден как имя
   компьютера, и понять, какой это город, будет нельзя.

2. Двойной клик по kiosk-start.bat — откроется полноэкранный киоск.

3. Автозапуск при входе в Windows:
   Win+R -> введите  shell:startup  -> положите в открывшуюся папку ЯРЛЫК
   на kiosk-start.bat (правый клик по файлу -> «Создать ярлык»).

Выход из киоска: закрыть чёрное консольное окно. Alt+F4 по браузеру не
поможет — скрипт откроет его снова, это и есть режим «киоск 24/7».

Нужны: Google Chrome или Microsoft Edge и сеть до сервера {server}.
Устанавливать больше ничего не нужно — распознавание речи, поиск по базе и
озвучка работают на сервере.

!!! НЕ открывайте kiosk-start.bat в блокноте и не пересохраняйте его.
Файл обязан остаться с CRLF-переводами строк; после пересохранения в другом
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


def check_bat() -> bytes:
    """Проверяем ровно ту мину, которая уже стреляла: .bat с LF не запускается."""
    if not BAT.exists():
        sys.exit(f"нет файла киоска: {BAT}")
    data = BAT.read_bytes()
    lone_lf = data.count(b"\n") - data.count(b"\r\n")
    if lone_lf:
        sys.exit(f"{BAT}: {lone_lf} строк(и) с LF вместо CRLF — cmd.exe на таком "
                 f"файле падает. Перевыкачай из git (.gitattributes держит CRLF).")
    return data


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

    made = []
    for kiosk_id, human in fleet:
        path = OUT / f"aidos-kiosk-{kiosk_id}.zip"
        readme = README.format(human=human, kiosk_id=kiosk_id, server=server)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("kiosk-start.bat", bat)
            # CRLF и здесь: файл читает cmd через `set /p`, и лишний байт в
            # конце строки уехал бы прямо в название точки.
            z.writestr("kiosk-id.txt", (kiosk_id + "\r\n").encode("ascii"))
            z.writestr("README.txt", readme.encode("utf-8-sig"))
        made.append((kiosk_id, human, path))

    # Список рассылки: 20 адресатов руками в мессенджере — идеальное место
    # перепутать архивы, поэтому печатаем и кладём рядом файлом.
    lines = [f"Рассылка киосков Ai-dos (сервер {server})", ""]
    lines += [f"{human:<45} {p.name}" for _, human, p in made]
    lines.append("")
    lines.append("Каждому городу — ТОЛЬКО его архив: имя точки внутри kiosk-id.txt.")
    (OUT / "SEND-LIST.txt").write_text("\n".join(lines), encoding="utf-8-sig")

    print("\n".join(lines))
    print(f"\nГотово: {len(made)} архив(ов) в {OUT}")
    print("Проверить один перед рассылкой:")
    print(f"  unzip -p {OUT}/aidos-kiosk-{made[0][0]}.zip kiosk-id.txt | od -c | head -2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

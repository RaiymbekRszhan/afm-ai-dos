"""Разовый конвертер: из текстовых выгрузок (.txt в _convert/) делает готовые
файлы базы (.md в data/) — чистит мусор, для кодексов извлекает нужные статьи,
для справочных бьёт на абзацы. Запуск из rag/: python _convert/build.py
"""
import re
from pathlib import Path

CONV = Path(__file__).resolve().parent
DATA = CONV.parent / "data"

# Шум редакций/служебное — выкидываем (ru и kk маркеры)
NOISE = re.compile(r"^\s*(Сноска\.|Ескерту\.|Оглавление|Мазмұны|Примечание РЦПИ|РЦПИ|"
                   r"Глава\s+\d|\d+-тарау|Параграф|\d+-параграф|Подлежит опубликованию)", re.I)
# Редакционные пометки об изменениях (история правок, НЕ нормативный текст).
# Важно: иначе строки «Статья 211-1 дополнена частью 4 ...» парсятся как новые статьи.
AMEND = re.compile(r"в соответствии с Законом РК от|см\.\s*стар\.\s*ред\.|"
                   r"^\s*Статья\s+\S+\s+(дополнен|изложен|исключен|утратил)", re.I)

ART_RU = re.compile(r"(?=^\s*Статья\s+\d+)", re.M)
ART_KK = re.compile(r"(?=^\s*\d+(?:-\d+)?-бап)", re.M)
NUM_RU = re.compile(r"Статья\s+(\d+(?:-\d+)?)")
NUM_KK = re.compile(r"(\d+(?:-\d+)?)-бап")


def clean(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines()
            if not NOISE.match(ln) and not AMEND.search(ln)]


def header(law, short, typ, lang) -> str:
    return f"# LAW: {law}\n# SHORT: {short}\n# TYPE: {typ}\n# LANG: {lang}\n\n"


def build_code(lines, lang, want):
    """Режем по статьям, оставляем нужные (want=None — все)."""
    body = "\n".join(lines)
    split = ART_KK if lang == "kk" else ART_RU
    num_re = NUM_KK if lang == "kk" else NUM_RU
    out, kept = [], []
    for part in split.split(body):
        part = part.strip()
        if not part:
            continue
        # пропускаем строки оглавления (заголовок статьи без тела)
        if len([ln for ln in part.splitlines() if ln.strip()]) <= 1:
            continue
        m = num_re.search(part[:60])  # номер берём из заголовка статьи
        if not m:
            continue
        num = m.group(1)
        if want is None or num in want:
            out.append(part)
            kept.append(num)
    return "\n\n".join(out), kept


def build_reference(lines, target=1200):
    """Группируем пункты в чанки ~target символов, разрывая на границе пункта «N.».
    Иначе deeply-numbered документы дают сотни крошечных чанков без контекста."""
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        if not ln.strip():
            continue   # пустые строки — выкидываем, иначе reference-нарезка рвёт группу
        is_point = re.match(r"^\s*\d+(?:-\d+)?\.\s", ln)
        if is_point and cur_len >= target:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return "\n\n".join(c.strip() for c in chunks if c.strip())


# (txt-файл, law, short_ru/kk, type, want_set|None)
UPK = {"64","65-1","66","67","68","69","70","71","78","96","97","115","131","135","202","205","208","209"}
UK  = {"190","214","215","217","217-1","218","218-1","223","236","244","245","255","256","257","258","259","260","266","273"}
APK = {"17","21","63","64","65","76","90","90-4","90-5","92","93","94","95","96","97","98","99","100","101"}
KOAP = {"150","211-1","214","214-1","722-2"}

JOBS = [
    # Коллекторская деятельность — закон целиком
    ("Коллекторская_деятельность", "Закон РК о коллекторской деятельности",
     "Закон о коллекторской деятельности", "Коллекторлық қызмет туралы Заң", "code", None),
    # Кодексы — только нужные статьи
    ("АПК_РК", "Административный процедурно-процессуальный кодекс РК", "АПК РК", "ӘРПК", "code", APK),
    ("УК", "Уголовный кодекс РК", "УК РК", "ҚК", "code", UK),
    ("УПК_РК", "Уголовно-процессуальный кодекс РК", "УПК РК", "ҚПК", "code", UPK),
    ("КоАП РК", "Кодекс Республики Казахстан об административных правонарушениях", "КоАП РК", "ӘҚБК", "code", KOAP),
    # Справочные
    ("Методика_определения_признаков_финансовых_пирамид",
     "Методика определения признаков финансовой пирамиды", "Методика финпирамид", "Қаржы пирамидасы әдістемесі", "reference", None),
    ("ПОЛОЖЕНИЕ_об_АФМ_РК", "Положение об Агентстве РК по финансовому мониторингу",
     "Положение об АФМ", "АҚМ туралы ереже", "reference", None),
    ("Порядок постановки на учёт СФМ", "Порядок постановки на учёт субъектов финансового мониторинга",
     "Порядок учёта СФМ", "ҚМ субъектілерін есепке қою тәртібі", "reference", None),
    ("Правила внутреннего контроля для СФМ", "Правила внутреннего контроля для субъектов финансового мониторинга",
     "Правила внутреннего контроля СФМ", "Ішкі бақылау қағидалары", "reference", None),
]

SLUG = {"Коллекторская_деятельность": "kollektor", "АПК_РК": "apk", "УК": "uk",
        "УПК_РК": "upk", "Методика_определения_признаков_финансовых_пирамид": "fin_piramidy",
        "ПОЛОЖЕНИЕ_об_АФМ_РК": "polozhenie_afm", "Порядок постановки на учёт СФМ": "uchet_sfm",
        "Правила внутреннего контроля для СФМ": "vnutr_kontrol", "КоАП РК": "koap"}


def find_txt(stem, lang):
    for name in (f"{stem}_{lang}.txt", f"{stem.replace(' ','_')}_{lang}.txt", f"{stem} _{lang}.txt"):
        p = CONV / name
        if p.exists():
            return p
    # подбор по началу имени
    for p in CONV.glob(f"*{lang}.txt"):
        if p.stem[:-3].rstrip("_ ").lower() == stem.rstrip("_ ").lower():
            return p
    return None


for stem, law, short_ru, short_kk, typ, want in JOBS:
    for lang, short in (("ru", short_ru), ("kk", short_kk)):
        src = find_txt(stem, lang)
        if not src:
            print(f"  ПРОПУСК {stem} {lang}: txt не найден")
            continue
        lines = clean(src.read_text(encoding="utf-8"))
        if typ == "code":
            body, kept = build_code(lines, lang, want)
            info = f"{len(kept)} статей" + (f" из набора {len(want)}" if want else " (целиком)")
        else:
            body = build_reference(lines)
            info = f"{body.count(chr(10)+chr(10))+1} абзацев"
        out = DATA / f"{SLUG[stem]}_{lang}.md"
        out.write_text(header(law, short, typ, lang) + body + "\n", encoding="utf-8")
        print(f"  {out.name}: {info}")

print("Готово.")

"""Синтез речи (TTS). Один интерфейс synthesize(), провайдеры:
  - f5     : русский (F5-TTS-сервер по HTTP) — с ударениями (RUAccent)
  - spark  : казахский (Spark-сервер по HTTP)
  - say    : системный голос macOS, только для локальной отладки
  - openai : внешний OpenAI-совместимый /audio/speech сервер
Провайдер выбирается в .env: TTS_PROVIDER / TTS_KK_PROVIDER.
"""
import asyncio
import io
import os
import re
import tempfile
import wave
from urllib.parse import urlparse, urlunparse

from app.config import settings


def _health_from(url: str) -> str:
    """URL /health из адреса /tts (устойчиво к URL без пути)."""
    p = urlparse(url)
    return urlunparse(p._replace(path="/health", params="", query="", fragment=""))

try:
    from num2words import num2words as _num2words
except ImportError:  # без пакета числа останутся цифрами (деградируем, не падаем)
    _num2words = None


def _resolve_lang(language: str | None) -> str:
    """Нормализует язык STT ('russian'/'kazakh'/'kk'/...) в 'ru' или 'kk'."""
    lang = (language or settings.stt_default_language or "russian").lower()
    if lang.startswith(("kaz", "kk", "kz", "қаз", "каз")):
        return "kk"
    return "ru"


# --- Произношение аббревиатур/латиницы для озвучки ------------------------
# TTS читает аббревиатуры и латиницу по буквам криво. Здесь — как их ПРОИЗНОСИТЬ.
# Меняем ТОЛЬКО текст для синтеза; ответ в JSON и на экране остаётся прежним.
# Порядок важен: более длинные/специфичные шаблоны должны идти ВЫШЕ коротких.
# Латиница/символы/бренды (для любого языка), регистр игнорируется:
_NORM_COMMON = [
    (r"https?://", ""),
    (r"\be-?Gov\s+mobile\b", "е-гов мобайл"),
    (r"\be-?Gov\b", "е-гов"),
    (r"\be-?Otinish\b", "е-Отиниш"),
    (r"\be-?Salyq-?Azamat\b", "е-салык азамат"),
    (r"\be-?Salyq\b", "е-салык"),
    (r"qamqor\.gov\.kz", "камкор"),
    (r"reestr\.uzs\.gov\.kz", "реестр"),
    (r"rchl\.govtec\.kz\S*", "сайте контроля"),
    (r"palata\.kz", "палата"),
    (r"\bWhatsApp\b", "вотсап"),
    (r"\bTelegram\b", "телеграм"),
    (r"\bInstagram\b", "инстаграм"),
    (r"\bFacebook\b", "фейсбук"),
    (r"\bTikTok\b", "тикток"),
    (r"\bApp\s*Store\b", "Эпп Стор"),
    (r"\bPlay\s*Market\b", "Плей Маркет"),
    (r"\bWi-?Fi\b", "вай-фай"),
    (r"\bBluetooth\b", "блютус"),
    (r"\bSIM-?бокс\b", "сим-бокс"),
    (r"\bSMS\b", "СМС"),
    (r"\bCVV\b", "си-ви-ви"),
    (r"\bPIN\b", "пин"),
    # банки (латиница) — для вопроса про блокировку карты
    (r"\bHome\s*Credit\b", "Хоум Кредит"),
    (r"\bFreedom\s*Finance\b", "Фридом Финанс"),
    (r"\bBank\s*RBK\b", "банк эр-бэ-ка"),
    (r"\bAl\s*Hilal\b", "Аль-Хиляль"),
    (r"\bKaspi\b", "Каспи"),
    (r"\bForte\b", "Форте"),
    (r"\bJusan\b", "Жусан"),
    (r"\bBereke\b", "Береке"),
    (r"\bBank\b", "банк"),
]
# Кириллические аббревиатуры — только для русского (kk-аналоги другие):
_NORM_RU = [
    (r"ПОД/ФТ", "противодействия отмыванию доходов и финансированию терроризма"),
    (r"\bОД/ФТ\b", "отмывания доходов и финансирования терроризма"),
    (r"\bФТ\b", "финансирование терроризма"),
    (r"\bОД\b", "отмывание доходов"),
    (r"\bАРРФР\b", "а-эр-эр-эф-эр"),
    (r"Web-?СФМ", "веб эс-эф-эм"),
    (r"\bСФМ\b", "субъект финансового мониторинга"),
    (r"\bАФМ\b", "а-эф-эм"),
    (r"\bМРП\b", "месячных расчётных показателей"),
    (r"\bДЭР\b", "Департамент экономических расследований"),
    (r"\bФМ-?1\b", "эф-эм один"),
    (r"\bв РК\b", "в Республике Казахстан"),
    (r"\bРК\b", "Республики Казахстан"),
    (r"№\s*", "номер "),
]

# Кодексы: полное раскрытие. После цифры (статья 218 УК) — родительный падеж
# («...Уголовного кодекса»), в остальных местах — именительный.
_RU_CODES = {
    "УПК": ("Уголовно-процессуальный кодекс", "Уголовно-процессуального кодекса"),
    "КоАП": ("Кодекс об административных правонарушениях",
             "Кодекса об административных правонарушениях"),
    "УК": ("Уголовный кодекс", "Уголовного кодекса"),
    "ГК": ("Гражданский кодекс", "Гражданского кодекса"),
    "НК": ("Налоговый кодекс", "Налогового кодекса"),
    "АПК": ("а-пэ-ка", "а-пэ-ка"),
}
_RU_CODE_RE = re.compile(r"\b(" + "|".join(_RU_CODES) + r")\b")
_RU_CODE_AFTER_NUM_RE = re.compile(
    r"(\d[\d\-.]*\s+)\b(" + "|".join(_RU_CODES) + r")\b")


def _ru_expand_codes(text: str) -> str:
    text = _RU_CODE_AFTER_NUM_RE.sub(
        lambda m: m.group(1) + _RU_CODES[m.group(2)][1], text)   # родительный
    return _RU_CODE_RE.sub(lambda m: _RU_CODES[m.group(1)][0], text)  # именительный
# --- Казахская морфология окончаний (для озвучки) ---
# Аббревиатуру раскрываем в полные слова, а падежный суффикс через дефис
# (ЭТД-ге, ҚНРДА-ны) ПЕРЕНОСИМ на раскрытое слово с правильным окончанием
# (сингармонизм front/back), а не отбрасываем.
_KK_FRONT_V = set("әеөүіиэ")
_KK_BACK_V = set("аоұыуяёю")


def _kk_front(word: str) -> bool:
    """Слово мягкое (front) по последней гласной? Иначе твёрдое (back)."""
    for ch in reversed(word):
        c = ch.lower()
        if c in _KK_FRONT_V:
            return True
        if c in _KK_BACK_V:
            return False
    return True


def _kk_case(suf: str | None):
    """Падеж по написанному на аббревиатуре суффиксу."""
    if not suf:
        return None
    s = suf.lower()
    if s in ("ның", "нің", "дың", "дің", "тың", "тің"): return "gen"
    if s in ("нан", "нен", "дан", "ден", "тан", "тен"): return "abl"
    if s in ("нда", "нде", "да", "де", "та", "те"): return "loc"
    if s in ("ға", "ге", "қа", "ке", "на", "не", "а", "е"): return "dat"
    if s in ("ны", "ні", "ды", "ді", "ты", "ті", "н"): return "acc"
    if s in ("мен", "бен", "пен"): return "ins"
    return None


# окончания (front, back) по типу основы и падежу
_KK_ENDINGS = {
    "poss": {  # притяжательная форма -ы/-і (агенттігі, департаменті, Республикасы)
        "acc": ("н", "н"), "gen": ("нің", "ның"), "dat": ("не", "на"),
        "loc": ("нде", "нда"), "abl": ("нен", "нан"), "ins": ("мен", "мен"),
    },
    "cons": {  # глухой согласный на конце (кодекс)
        "acc": ("ті", "ты"), "gen": ("тің", "тың"), "dat": ("ке", "қа"),
        "loc": ("те", "та"), "abl": ("тен", "тан"), "ins": ("пен", "пен"),
    },
    "vowel": {  # гласный на конце (шот-фактура)
        "acc": ("ні", "ны"), "gen": ("нің", "ның"), "dat": ("ге", "ға"),
        "loc": ("де", "да"), "abl": ("ден", "дан"), "ins": ("мен", "мен"),
    },
}


def _kk_inflect(expansion: str, wtype: str, case: str | None) -> str:
    if not case:
        return expansion
    front, back = _KK_ENDINGS[wtype][case]
    return expansion + (front if _kk_front(expansion) else back)


# Склоняемые аббревиатуры: (аббревиатура, раскрытие, тип основы). Длинные — выше.
_NORM_KK_DECL = [
    ("ҚНРДА", "Қаржы нарығын реттеу және дамыту агенттігі", "poss"),
    ("ӘҚБК", "Әкімшілік құқық бұзушылық туралы кодекс", "cons"),
    ("ӘРПК", "Әкімшілік рәсімдік-процестік кодекс", "cons"),
    ("ҚПК", "Қылмыстық-процестік кодекс", "cons"),
    ("ЭТД", "Экономикалық тергеп-тексеру департаменті", "poss"),
    ("ҚМС", "қаржы мониторингі субъектісі", "poss"),
    ("ҚМА", "Қаржы мониторингі агенттігі", "poss"),
    ("АҚМ", "Қаржы мониторингі агенттігі", "poss"),
    ("ЖСН", "жеке сәйкестендіру нөмірі", "poss"),
    ("ЭШФ", "электрондық шот-фактура", "vowel"),
    ("ҚР", "Қазақстан Республикасы", "poss"),
    ("ҚК", "Қылмыстық кодекс", "cons"),
    ("ҚМ", "қаржы мониторингі", "poss"),
]
# Простые замены без склонения (фразы, формы с цифрой) — применяются ПЕРВЫМИ.
_NORM_KK_SIMPLE = [
    (r"КЖ/ТҚ/ЖҚҚТҚҚ", "Қылмыстық жолмен алынған кірістерді заңдастыруға және терроризмді қаржыландыруға қарсы іс-қимыл"),
    (r"КЖ/ТҚ/ЖҚҚТҚ", "Қылмыстық жолмен алынған кірістерді заңдастыруға және терроризмді қаржыландыруға қарсы іс-қимыл"),
    (r"КЖ/ТҚҚ", "Қылмыстық жолмен алынған кірістерді заңдастыруға және терроризмді қаржыландыруға қарсы іс-қимыл"),
    (r"\bЖСН/БСН\b", "жеке немесе бизнес сәйкестендіру нөмірі"),
    (r"\bҚМ-?1\b", "қаржы мониторингінің бірінші"),
    (r"№\s*", "нөмір "),
]


# --- Числа: цифры -> слова (русский). F5/RUAccent читают только слова, не цифры. ---
def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма по числу: 1 рубль / 2 рубля / 5 рублей."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


# Число: тысячные разделители — пробел/nbsp (не точка: чтобы не ловить IP/версии);
# опц. десятичная дробь через запятую.
_NUM_RE = re.compile(r"\d{1,3}(?:[  ]\d{3})+(?:,\d+)?|\d+(?:,\d+)?")


def _num_repl(m: "re.Match") -> str:
    tok = m.group(0)
    core = tok.replace(" ", "").replace(" ", "")
    try:
        if "," in core:
            return _num2words(float(core.replace(",", ".")), lang="ru")
        return _num2words(int(core), lang="ru")
    except Exception:
        return tok


# --- Порядковые числительные с согласованием по роду/падежу ----------------
# num2words даёт мужской именительный («двести четырнадцатый»). В русском у
# порядкового меняется ТОЛЬКО последнее слово — отсекаем его окончание и ставим
# нужное. Тип основы: «-ий» (третий) — мягкий, «-ый/-ой» — твёрдый.
# Падеж None => оставить как есть (муж. им./вин.).
_ORD_HARD = {
    ("m", "nom"): None, ("m", "acc"): None, ("m", "gen"): "ого", ("m", "dat"): "ому",
    ("m", "prep"): "ом", ("m", "instr"): "ым",
    ("f", "nom"): "ая", ("f", "acc"): "ую", ("f", "gen"): "ой", ("f", "dat"): "ой",
    ("f", "prep"): "ой", ("f", "instr"): "ой",
}
_ORD_SOFT = {  # только «третий» (числа, оканчивающиеся на 3, кроме 13)
    ("m", "nom"): "ий", ("m", "acc"): "ий", ("m", "gen"): "ьего", ("m", "dat"): "ьему",
    ("m", "prep"): "ьем", ("m", "instr"): "ьим",
    ("f", "nom"): "ья", ("f", "acc"): "ью", ("f", "gen"): "ьей", ("f", "dat"): "ьей",
    ("f", "prep"): "ьей", ("f", "instr"): "ьей",
}


def _ordinal_ru(n: int, gender: str, case: str) -> str:
    """Порядковое числительное (n) в нужном роде/падеже. Опора — num2words."""
    base = _num2words(n, lang="ru", to="ordinal")  # муж. им.: «двести четырнадцатый»
    words = base.split()
    last = words[-1]
    table = _ORD_SOFT if last.endswith("ий") else _ORD_HARD
    end = table.get((gender, case))
    words[-1] = last if end is None else last[:-2] + end
    return " ".join(words)


# Управляющее слово -> (род, падеж). Женские косвенные падежи дают одинаковое
# окончание «-ой/-ьей», поэтому сведены к gen.
_ORD_GOV = {
    "статья": ("f", "nom"), "статью": ("f", "acc"), "статьи": ("f", "gen"),
    "статье": ("f", "gen"), "статьёй": ("f", "gen"), "статьей": ("f", "gen"),
    "часть": ("f", "nom"), "части": ("f", "gen"), "частью": ("f", "gen"),
    "глава": ("f", "nom"), "главы": ("f", "gen"), "главе": ("f", "gen"),
    "главу": ("f", "acc"), "главой": ("f", "gen"),
    "пункт": ("m", "nom"), "пункта": ("m", "gen"), "пункту": ("m", "dat"),
    "пункте": ("m", "prep"), "пунктом": ("m", "instr"),
    "подпункт": ("m", "nom"), "подпункта": ("m", "gen"), "подпункте": ("m", "prep"),
    "подпунктом": ("m", "instr"),
    "абзац": ("m", "nom"), "абзаца": ("m", "gen"), "абзаце": ("m", "prep"),
    "абзацем": ("m", "instr"),
    "раздел": ("m", "nom"), "раздела": ("m", "gen"), "разделе": ("m", "prep"),
    "разделом": ("m", "instr"),
}
_GOV_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _ORD_GOV), key=len, reverse=True))
    + r")\s+(\d{1,4})(?!\s*\d)",  # не хватаем первую триаду крупного числа («50 000»)
    re.IGNORECASE,
)
# Год: только 4-значное число перед «год/года/году» (муж. род).
_YEAR_CASE = {"год": ("m", "nom"), "года": ("m", "gen"), "году": ("m", "prep")}
_YEAR_RE = re.compile(r"\b(\d{4})\s+(год|года|году)\b", re.IGNORECASE)


def _ord_gov_sub(m: "re.Match") -> str:
    gender, case = _ORD_GOV[m.group(1).lower()]
    return f"{m.group(1)} {_ordinal_ru(int(m.group(2)), gender, case)}"


def _ord_year_sub(m: "re.Match") -> str:
    gender, case = _YEAR_CASE[m.group(2).lower()]
    return f"{_ordinal_ru(int(m.group(1)), gender, case)} {m.group(2)}"


def _pct_sub(m: "re.Match") -> str:
    """«20%» -> «двадцать процентов»; «1,5%» -> «одна целая пять десятых процента»."""
    tok = m.group(1)
    if "," in tok:  # дробь -> родительный ед. («процента»)
        return f"{_num2words(float(tok.replace(',', '.')), lang='ru')} процента"
    n = int(tok)
    return f"{_num2words(n, lang='ru')} {_ru_plural(n, 'процент', 'процента', 'процентов')}"


# Телефоны/горячие линии: «по номеру 1458», «телефон 1414» — читать ПО ЦИФРАМ.
# 4+ цифр после слова «номер»/«телефон» (короткие «приказ № 55» остаются числом).
_RU_DIGIT_WORDS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть",
                   "семь", "восемь", "девять"]
_RU_PHONE_RE = re.compile(r"\b(номер\w*|тел\.|телефон\w*)\s+(\d[\d\s\-()]{2,17}\d)",
                          re.IGNORECASE)


def _ru_digits(s: str) -> str:
    return " ".join(_RU_DIGIT_WORDS[int(d)] for d in s if d.isdigit())


def _ru_phone_sub(m: "re.Match") -> str:
    return f"{m.group(1)} {_ru_digits(m.group(2))}"


def _ru_numbers(text: str) -> str:
    """Цифры -> слова. В распознанных контекстах — ПОРЯДКОВЫЕ с согласованием по
    роду/падежу (статья 214 -> «двести четырнадцатая»; в 2024 году -> «...четвёртом
    году»; пунктом 5 -> «пятым»). «Номер/телефон NNNN» — по цифрам. Остальные
    числа — количественные; % -> процент(а/ов).
    Без num2words — no-op (числа останутся цифрами).
    """
    text = _RU_PHONE_RE.sub(_ru_phone_sub, text)   # номера/телефоны -> по цифрам
    if _num2words is None:
        return text
    text = _GOV_RE.sub(_ord_gov_sub, text)         # статья/пункт/часть N -> порядковое
    text = _YEAR_RE.sub(_ord_year_sub, text)       # NNNN год/года/году -> порядковое
    text = re.sub(r"(\d+(?:,\d+)?)\s*%", _pct_sub, text)  # проценты
    return _NUM_RE.sub(_num_repl, text)            # остальные числа -> количественные


# --- Числа на казахском: цифры -> слова -----------------------------------
# num2words казахский НЕ умеет (NotImplementedError), поэтому конвертер свой.
# Используется и без num2words: для kk цифры всегда озвучиваются.
_KK_ONES = ["", "бір", "екі", "үш", "төрт", "бес", "алты", "жеті", "сегіз", "тоғыз"]
_KK_TENS = ["", "он", "жиырма", "отыз", "қырық", "елу", "алпыс", "жетпіс", "сексен", "тоқсан"]
_KK_SCALE = ["", "мың", "миллион", "миллиард", "триллион"]
_KK_FRAC_PLACE = {1: "оннан", 2: "жүзден", 3: "мыңнан"}  # дробь: «бүтін оннан бес»
_KK_LET = "А-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ"
# Порядковая форма ПОСЛЕДНЕГО слова числа (остальные слова не меняются).
_KK_ORDINAL_LAST = {
    "бір": "бірінші", "екі": "екінші", "үш": "үшінші", "төрт": "төртінші",
    "бес": "бесінші", "алты": "алтыншы", "жеті": "жетінші", "сегіз": "сегізінші",
    "тоғыз": "тоғызыншы", "он": "оныншы", "жиырма": "жиырмасыншы",
    "отыз": "отызыншы", "қырық": "қырқыншы", "елу": "елуінші", "алпыс": "алпысыншы",
    "жетпіс": "жетпісінші", "сексен": "сексенінші", "тоқсан": "тоқсаныншы",
    "жүз": "жүзінші", "мың": "мыңыншы", "миллион": "миллионыншы",
    "миллиард": "миллиардыншы", "триллион": "триллионыншы",
}


def _kk_triple(n: int) -> list[str]:
    """0..999 -> слова (жүз/ондық/бірлік)."""
    words: list[str] = []
    h, rem = divmod(n, 100)
    if h:
        words.append("жүз" if h == 1 else f"{_KK_ONES[h]} жүз")
    t, o = divmod(rem, 10)
    if t:
        words.append(_KK_TENS[t])
    if o:
        words.append(_KK_ONES[o])
    return words


def _kk_cardinal(n: int) -> str | None:
    """Целое -> количественное (50000 -> 'елу мың'). None — если слишком большое."""
    if n == 0:
        return "нөл"
    neg = n < 0
    n = abs(n)
    groups: list[int] = []
    while n:
        n, r = divmod(n, 1000)
        groups.append(r)
    if len(groups) > len(_KK_SCALE):
        return None  # больше триллиона — пусть читается по цифрам
    parts: list[str] = []
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if not g:
            continue
        if idx == 1 and g == 1:  # 1000 -> «мың» (без «бір»)
            parts.append(_KK_SCALE[idx])
        else:
            parts.extend(_kk_triple(g))
            if idx:
                parts.append(_KK_SCALE[idx])
    res = " ".join(parts)
    return f"минус {res}" if neg else res


def _kk_ordinal(n: int) -> str:
    """Целое -> порядковое (214 -> 'екі жүз он төртінші')."""
    c = _kk_cardinal(n)
    if c is None:
        return str(n)
    words = c.split()
    words[-1] = _KK_ORDINAL_LAST.get(words[-1], words[-1] + "ыншы")
    return " ".join(words)


def _kk_digits(s: str) -> str:
    """Цифры по одной (для ЖСН/БСН, телефонов)."""
    return " ".join("нөл" if d == "0" else _KK_ONES[int(d)] for d in s)


def _kk_num_str(tok: str) -> str:
    had_sep = bool(re.search(r"[\s ]", tok))  # были ли разделители тысяч?
    core = re.sub(r"[\s ]", "", tok)  # убрать разделители тысяч (пробел/nbsp)
    if "," in core:
        intp, frac = core.split(",", 1)
        whole = _kk_cardinal(int(intp or 0))
        place = _KK_FRAC_PLACE.get(len(frac))
        if whole and place:
            return f"{whole} бүтін {place} {_kk_cardinal(int(frac))}"
        return f"{whole or _kk_digits(intp)} бүтін {_kk_digits(frac)}"
    # Длинный код БЕЗ разделителей (ЖСН/БСН, телефон) — по цифрам. А «100 000 000»
    # (с разделителями тысяч) — это сумма, её читаем словами («жүз миллион»).
    if len(core) >= 9 and not had_sep:
        return _kk_digits(core)
    c = _kk_cardinal(int(core))
    return c if c is not None else _kk_digits(core)


def _kk_num_repl(m: "re.Match") -> str:
    try:
        return _kk_num_str(m.group(0))
    except Exception:
        return m.group(0)


def _kk_pct_sub(m: "re.Match") -> str:
    try:
        return f"{_kk_num_str(m.group(1))} пайыз"
    except Exception:
        return m.group(0)


# «N-бап», «5-тармақ», «2024-жыл» -> порядковое (дефис+слово -> «... слово»).
_KK_ORD_HYPHEN_RE = re.compile(rf"\b(\d{{1,4}})-([{_KK_LET}]+)")
# Год через пробел: «2024 жыл/жылы/жылғы» -> порядковое.
_KK_YEAR_RE = re.compile(r"\b(\d{4})\s+(жыл\w*)", re.IGNORECASE)
# Сам суффикс порядкового, записанный после дефиса («2-ші»): его НЕ оставляем
# отдельным словом — порядковое числительное уже содержит нужное окончание.
_KK_ORD_SUFFIXES = {"ші", "шы", "ыншы", "інші", "ншы", "нші"}


def _kk_ord_hyphen_sub(m: "re.Match") -> str:
    ordinal = _kk_ordinal(int(m.group(1)))
    word = m.group(2)
    if word.lower() in _KK_ORD_SUFFIXES:
        return ordinal  # «2-ші» -> «екінші» (без дублирующего «ші»)
    return f"{ordinal} {word}"


def _kk_ord_year_sub(m: "re.Match") -> str:
    return f"{_kk_ordinal(int(m.group(1)))} {m.group(2)}"


# «1458 нөміріне» / «нөмір 1458» — короткий номер по цифрам.
_KK_PHONE_RE1 = re.compile(r"\b(\d{4,11})[\s\-]*(нөмір\w*)")
_KK_PHONE_RE2 = re.compile(r"\b(нөмір\w*|телефон\w*)\s+(\d{4,11})\b")


def _kk_numbers(text: str) -> str:
    """Цифры -> казахские слова. «N-бап»/годы — ПОРЯДКОВЫЕ, проценты -> пайыз,
    номера телефонов и длинные коды (ЖСН) — по цифрам, остальное — количественные."""
    text = _KK_PHONE_RE1.sub(lambda m: f"{_kk_digits(m.group(1))} {m.group(2)}", text)
    text = _KK_PHONE_RE2.sub(lambda m: f"{m.group(1)} {_kk_digits(m.group(2))}", text)
    text = _KK_ORD_HYPHEN_RE.sub(_kk_ord_hyphen_sub, text)  # N-бап -> порядковое
    text = _KK_YEAR_RE.sub(_kk_ord_year_sub, text)          # NNNN жыл -> порядковое
    text = re.sub(r"(\d+(?:,\d+)?)\s*%", _kk_pct_sub, text)  # проценты -> пайыз
    return _NUM_RE.sub(_kk_num_repl, text)                  # остальные -> количественные


# --- Латиница -> кириллица (фолбэк после словаря брендов) -------------------
# TTS ru/kk латиницу читает криво или молчит. Приближённая англо-транслитерация:
# слова из словаря _NORM_COMMON уже заменены, здесь добиваем всё остальное.
_TRANSLIT_DIGRAPHS = [
    ("tion", "шн"), ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("ck", "к"),
    ("ph", "ф"), ("th", "т"), ("qu", "кв"), ("oo", "у"), ("ee", "и"),
    ("ea", "и"), ("ay", "ей"), ("ey", "ей"), ("oy", "ой"), ("ai", "ей"),
    ("ow", "оу"), ("ng", "нг"),
]
_TRANSLIT_ONE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
}
_LAT_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def _translit_word(w: str) -> str:
    s = w.lower()
    # немая конечная e (google, store) — не читается
    if len(s) > 3 and s.endswith("e") and s[-2] not in "aeiou":
        s = s[:-1]
    out, i = [], 0
    while i < len(s):
        for dg, rep in _TRANSLIT_DIGRAPHS:
            if s.startswith(dg, i):
                out.append(rep)
                i += len(dg)
                break
        else:
            ch = s[i]
            if ch == "c" and i + 1 < len(s) and s[i + 1] in "eiy":
                out.append("с")           # ce/ci/cy -> с (office, city)
            else:
                out.append(_TRANSLIT_ONE.get(ch, ch))
            i += 1
    return "".join(out)


def _normalize_for_tts(text: str, language: str | None) -> str:
    """Раскрывает аббревиатуры/латиницу/числа в произносимый вид (только для озвучки)."""
    for pat, rep in _NORM_COMMON:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    # оставшаяся латиница -> примерная кириллица (словарь выше — приоритетнее)
    text = _LAT_TOKEN.sub(lambda m: _translit_word(m.group(0)), text)
    if _resolve_lang(language) == "ru":
        text = _ru_expand_codes(text)  # УК/УПК/КоАП -> полные названия с падежом
        for pat, rep in _NORM_RU:
            text = re.sub(pat, rep, text)
        return _ru_numbers(text)  # числа -> слова (ПОСЛЕ раскрытия аббревиатур)
    # казахский: сперва простые замены, затем склоняемые аббревиатуры
    for pat, rep in _NORM_KK_SIMPLE:
        text = re.sub(pat, rep, text)
    for abbr, expansion, wtype in _NORM_KK_DECL:
        rx = re.compile(rf"\b{abbr}(?:-([а-яёәіңғүұқөһ]+))?\b")
        text = rx.sub(
            lambda m, e=expansion, w=wtype: _kk_inflect(e, w, _kk_case(m.group(1))),
            text,
        )
    return _kk_numbers(text)  # числа -> слова (ПОСЛЕ раскрытия аббревиатур)


# Граница предложения: после .!?… и переноса строки.
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")

# Кусок «произносим», если в нём есть хотя бы одна буква (кириллица/латиница).
# Куски из одних цифр/знаков TTS не озвучивает: Spark выдаёт 0 токенов и падает.
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _has_speech(text: str) -> bool:
    return bool(_HAS_LETTER.search(text))


# Знаки конца клаузы: после них в живой речи и так пауза, поэтому оборвать
# длинное предложение здесь — естественно.
_CLAUSE_BREAK = re.compile(r"[,;:—–]")


def _break_at_clause(chunk: str) -> tuple[str, str]:
    """Делит накопленный кусок по ПОСЛЕДНЕЙ клаузе: (до знака, остаток).

    Знак у самого начала не берём (остаток был бы почти целым куском и всё
    равно поехал бы дальше) — тогда возвращаем кусок как есть.
    """
    best = -1
    for m in _CLAUSE_BREAK.finditer(chunk):
        best = m.end()
    if best > len(chunk) // 2:
        return chunk[:best].strip(), chunk[best:].strip()
    return chunk, ""


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    """Слишком длинное предложение режем на куски не длиннее лимита.

    Режем ПО ГРАНИЦЕ КЛАУЗЫ (запятая/тире/двоеточие), а не строго по словам:
    кусок, оборванный посреди фразы, TTS озвучивает как законченную реплику —
    даёт падающую интонацию и комкает хвост на ровном месте. По запятой конец
    куска попадает туда, где пауза уместна, и шов не слышен. Если знаков нет
    (длинное перечисление одними словами) — прежний словесный фолбэк.

    Увеличивать max_chars вместо этого нельзя: время синтеза растёт квадратично
    (замер на Spark: 100 симв. -> 18 c, 330 симв. -> 117 c).
    """
    parts: list[str] = []
    cur = ""
    for word in sentence.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            head, tail = _break_at_clause(cur)
            parts.append(head)
            cur = f"{tail} {word}".strip() if tail else word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        parts.append(cur)
    return parts


def _split_for_tts(text: str, sentence_max: int, group_max: int) -> list[str]:
    """Готовит текст к синтезу, минимизируя число склеиваемых кусков.

    F5 сам делит текст на предложения, но обрезает ОДНО предложение длиннее
    ~182 симв. (ru). Поэтому: слишком длинное предложение режем по словам
    (<= sentence_max), а целые предложения группируем в крупные куски
    (<= group_max) — F5 озвучит такой кусок слитно, с естественными паузами,
    без швов поштучной склейки. При group_max == sentence_max поведение
    прежнее (мелкие куски) — для TTS без собственной нарезки (spark).
    """
    text = text.strip()
    # 1) нормализуем предложения: слишком длинные дробим по словам
    sentences: list[str] = []
    for sent in _SENT_SPLIT.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > sentence_max:
            sentences.extend(_hard_split(sent, sentence_max))
        else:
            sentences.append(sent)
    if not sentences:
        return [text]
    # 2) группируем предложения в куски не длиннее group_max
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        if cur and len(cur) + 1 + len(sent) > group_max:
            chunks.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        chunks.append(cur)
    return chunks


def _gap_ms_after(text: str) -> int:
    """Сколько тишины вставить ПОСЛЕ куска — по тому, чем он закончился.

    Куски синтезируются порознь, и TTS подрезает собственный хвост тишины: без
    зазора следующий кусок «влезает», не дав предыдущему договорить. Но пауза
    нужна разная: после точки — как между фразами, после запятой — вдвое короче
    (там середина предложения, полная пауза рвала бы его надвое).
    """
    end = text.rstrip()[-1:]
    if end in ".!?":
        return settings.tts_gap_ms
    return settings.tts_gap_ms // 2


def _concat_wav(parts: list[bytes], texts: list[str] | None = None) -> bytes:
    """Склеивает несколько WAV-блоков в один, вставляя паузу между кусками.

    `texts` — исходные тексты кусков (той же длины, что `parts`): по ним пауза
    подбирается под знак на стыке. Без них — одинаковый зазор, как раньше.
    """
    if len(parts) == 1:
        return parts[0]
    out = io.BytesIO()
    writer: wave.Wave_write | None = None

    fmt: tuple[int, int, int] | None = None  # (nchannels, sampwidth, framerate) первого куска
    try:
        for idx, blob in enumerate(parts):
            with wave.open(io.BytesIO(blob), "rb") as reader:
                params = (reader.getnchannels(), reader.getsampwidth(),
                          reader.getframerate())
                if writer is None:
                    fmt = params
                    nch, sw, fr = params
                    writer = wave.open(out, "wb")
                    writer.setnchannels(nch)
                    writer.setsampwidth(sw)
                    writer.setframerate(fr)
                elif params != fmt:
                    # Куски с разной частотой/каналами склеивать нельзя — иначе
                    # получится ускоренный/искажённый звук БЕЗ ошибки. Падаем явно.
                    raise RuntimeError(
                        f"Несовместимые параметры WAV при склейке: {params} != {fmt}"
                    )
                if idx:  # пауза перед каждым, кроме первого — по знаку на стыке
                    ms = (_gap_ms_after(texts[idx - 1]) if texts
                          else settings.tts_gap_ms)
                    nch, sw, fr = fmt
                    n_frames = int(fr * ms / 1000)
                    writer.writeframes(b"\x00" * (n_frames * nch * sw))
                writer.writeframes(reader.readframes(reader.getnframes()))
    finally:
        if writer is not None:
            writer.close()
    return out.getvalue()


async def synthesize(text: str, language: str | None = None) -> bytes:
    """Текст -> аудио (WAV-байты). Длинный текст режется и склеивается."""
    if not text.strip():
        raise RuntimeError("Пустой текст для синтеза")
    # Раскрываем аббревиатуры/латиницу в произносимый вид (только для звука).
    text = _normalize_for_tts(text, language)
    # Склейка реализована для WAV; для иных форматов синтезируем одним куском.
    if settings.tts_format != "wav":
        return await _synthesize_one(text, language)
    # F5 сам делит текст на предложения — отдаём крупные куски (меньше швов).
    # Spark своей нарезки не имеет: режем сами, но его предел (3000) куда выше
    # F5-шных 180, поэтому даём предложению запас — чтобы разрез попал на запятую,
    # а не в середину фразы (иначе Spark комкает хвост огрызка).
    if _provider_for(language) == "spark":
        sent_max = max(settings.tts_max_chars, settings.tts_kk_max_chars)
        group = sent_max
    else:
        sent_max = settings.tts_max_chars
        group = (settings.tts_group_chars
                 if _provider_for(language) == "f5"
                 else settings.tts_max_chars)
    parts = _split_for_tts(text, sent_max, group)
    # Выкидываем куски без букв (одни цифры/знаки) — TTS на них падает.
    parts = [p for p in parts if _has_speech(p)]
    if not parts:
        raise RuntimeError("Нет произносимого текста для синтеза")
    if len(parts) == 1:
        return await _synthesize_one(parts[0], language)
    audios = [await _synthesize_one(part, language) for part in parts]
    return _concat_wav(audios, parts)


def _provider_for(language: str | None) -> str:
    """Какой TTS-провайдер обслуживает данный язык (kk -> свой провайдер)."""
    return settings.tts_kk_provider if _resolve_lang(language) == "kk" else settings.tts_provider


async def _synthesize_one(text: str, language: str | None = None) -> bytes:
    """Синтез одного фрагмента. Провайдер выбирается по языку."""
    provider = _provider_for(language)
    if provider == "say":
        return await _say(text, language)
    if provider == "openai":
        return await _openai(text)
    if provider == "f5":
        return await _f5(text, language)
    if provider == "spark":
        return await _spark(text, language)
    raise RuntimeError(f"Неизвестный TTS-провайдер: {provider!r}")


# ---------- macOS say (только локальная отладка) ----------
async def _say(text: str, language: str | None) -> bytes:
    # У macOS нет казахского голоса — для kk звучание будет неточным (это ок для отладки).
    voice = "Milena"  # русский голос
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "out.aiff")
        wav = os.path.join(d, "out.wav")
        p1 = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-o", aiff, text,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await p1.communicate()
        if p1.returncode != 0:
            raise RuntimeError(f"say error: {err.decode(errors='ignore')}")
        p2 = await asyncio.create_subprocess_exec(
            "afconvert", aiff, "-f", "WAVE", "-d", "LEI16@22050", "-c", "1", wav,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await p2.communicate()
        if p2.returncode != 0:
            raise RuntimeError(f"afconvert error: {err.decode(errors='ignore')}")
        with open(wav, "rb") as f:
            return f.read()


# ---------- F5-TTS_RUSSIAN (русский, отдельный сервис по HTTP) ----------
_f5_ref_cache: dict[str, tuple[bytes, str]] = {}  # путь -> (аудио-байты, ref_text)


def _f5_reference() -> tuple[bytes, str]:
    """Клиентский референс (аудио + транскрипт) для multipart-контракта F5.

    Кэшируем в памяти: WAV читается с диска один раз, а не на каждый кусок ответа.
    ref_text вида "@путь" читается из файла (удобно ссылаться на refs/ref_ru.txt).
    """
    path = settings.f5_ref_audio
    cached = _f5_ref_cache.get(path)
    if cached is not None:
        return cached
    if not os.path.isfile(path):
        raise RuntimeError(f"F5_REF_AUDIO={path!r} — файл референса не найден.")
    with open(path, "rb") as f:
        audio = f.read()
    ref_text = settings.f5_ref_text
    if ref_text.startswith("@"):
        ref_path = ref_text[1:]
        if not os.path.isfile(ref_path):
            raise RuntimeError(f"F5_REF_TEXT=@{ref_path} — файл транскрипта не найден.")
        with open(ref_path, encoding="utf-8") as f:
            ref_text = f.read().strip()
    if not ref_text.strip():
        raise RuntimeError(
            "F5_REF_TEXT пуст — multipart-сервер F5 требует транскрипт референса "
            "(укажи текст или @refs/ref_ru.txt в .env)."
        )
    result = (audio, ref_text)
    _f5_ref_cache[path] = result
    return result


async def _f5(text: str, language: str | None) -> bytes:
    """Вызывает F5-TTS-сервер (русский TTS). Два контракта:

    - multipart (если задан F5_REF_AUDIO): POST {ref_audio, ref_text, gen_text}
      — GPU-сервер АФМ, референс шлёт клиент;
    - JSON (иначе): POST {text, language} — локальный f5_server с серверным референсом.
    """
    import httpx
    if not settings.f5_url:
        raise RuntimeError(
            "TTS=f5, но F5_URL не задан. Запусти f5_server и укажи адрес в .env."
        )
    async with httpx.AsyncClient(timeout=180) as client:
        if settings.f5_ref_audio:
            audio, ref_text = _f5_reference()
            resp = await client.post(
                settings.f5_url,
                files={"ref_audio": ("ref.wav", audio, "audio/wav")},
                data={"ref_text": ref_text, "gen_text": text},
            )
        else:
            resp = await client.post(
                settings.f5_url,
                json={"text": text, "language": language or "russian"},
            )
        resp.raise_for_status()
        return resp.content


# ---------- Spark-TTS (казахский, отдельный сервис по HTTP) ----------
async def _spark(text: str, language: str | None) -> bytes:
    """Вызывает Spark-TTS-сервер. Контракт: POST {text, language} -> WAV."""
    import httpx
    if not settings.spark_url:
        raise RuntimeError(
            "TTS казахский = spark, но SPARK_URL не задан. Запусти spark_server."
        )
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(settings.spark_url,
                                 json={"text": text, "language": language or "kazakh"})
        resp.raise_for_status()
        return resp.content


# ---------- OpenAI-совместимый внешний сервер ----------
_openai_client = None


async def _openai(text: str) -> bytes:
    global _openai_client
    if not (settings.tts_base_url and settings.tts_model):
        raise RuntimeError("TTS_PROVIDER=openai, но не заданы TTS_BASE_URL / TTS_MODEL")
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(base_url=settings.tts_base_url, api_key=settings.tts_api_key)
    resp = await _openai_client.audio.speech.create(
        model=settings.tts_model,
        voice=settings.tts_voice or "default",
        input=text,
        response_format=settings.tts_format,
    )
    return resp.read()


# ---------- Health: доступность TTS-серверов (для /health оркестратора) ----------
async def healthy() -> dict:
    """Пингует /health у выбранных TTS-серверов (f5/spark). Пусто, если они не нужны."""
    import httpx

    providers = {settings.tts_provider, settings.tts_kk_provider}
    targets: dict[str, str] = {}
    if "f5" in providers and settings.f5_url:
        targets["f5"] = settings.f5_url
    if "spark" in providers and settings.spark_url:
        targets["spark"] = settings.spark_url

    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for name, url in targets.items():
            health_url = _health_from(url)
            try:
                resp = await client.get(health_url)
                resp.raise_for_status()
                out[name] = {"reachable": True}
            except Exception as e:
                out[name] = {"reachable": False, "error": str(e)}
    return out

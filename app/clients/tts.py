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

from app.config import settings

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
    (r"ПОД/ФТ", "под-эф-тэ"),
    (r"\bАРРФР\b", "а-эр-эр-эф-эр"),
    (r"Web-?СФМ", "веб эс-эф-эм"),
    (r"\bСФМ\b", "эс-эф-эм"),
    (r"\bАФМ\b", "а-эф-эм"),
    (r"\bМРП\b", "месячных расчётных показателей"),
    (r"\bДЭР\b", "Департамент экономических расследований"),
    (r"\bФМ-?1\b", "эф-эм один"),
    (r"\bУПК\b", "у-пэ-ка"),
    (r"\bАПК\b", "а-пэ-ка"),
    (r"\bКоАП\b", "ко-ап"),
    (r"\bУК\b", "у-ка"),
    (r"\bРК\b", "эр-ка"),
    (r"№\s*", "номер "),
]
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


def _ru_numbers(text: str) -> str:
    """Цифры -> слова. В распознанных контекстах — ПОРЯДКОВЫЕ с согласованием по
    роду/падежу (статья 214 -> «двести четырнадцатая»; в 2024 году -> «...четвёртом
    году»; пунктом 5 -> «пятым»). Остальные числа — количественные; % -> процент(а/ов).
    Без num2words — no-op (числа останутся цифрами).
    """
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
    core = re.sub(r"[\s ]", "", tok)  # убрать разделители тысяч (пробел/nbsp)
    if "," in core:
        intp, frac = core.split(",", 1)
        whole = _kk_cardinal(int(intp or 0))
        place = _KK_FRAC_PLACE.get(len(frac))
        if whole and place:
            return f"{whole} бүтін {place} {_kk_cardinal(int(frac))}"
        return f"{whole or _kk_digits(intp)} бүтін {_kk_digits(frac)}"
    if len(core) >= 9:  # длинные коды (ЖСН/БСН, телефоны) — по цифрам
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


def _kk_ord_hyphen_sub(m: "re.Match") -> str:
    return f"{_kk_ordinal(int(m.group(1)))} {m.group(2)}"


def _kk_ord_year_sub(m: "re.Match") -> str:
    return f"{_kk_ordinal(int(m.group(1)))} {m.group(2)}"


def _kk_numbers(text: str) -> str:
    """Цифры -> казахские слова. «N-бап»/годы — ПОРЯДКОВЫЕ, проценты -> пайыз,
    длинные коды (ЖСН/телефон) — по цифрам, остальное — количественные."""
    text = _KK_ORD_HYPHEN_RE.sub(_kk_ord_hyphen_sub, text)  # N-бап -> порядковое
    text = _KK_YEAR_RE.sub(_kk_ord_year_sub, text)          # NNNN жыл -> порядковое
    text = re.sub(r"(\d+(?:,\d+)?)\s*%", _kk_pct_sub, text)  # проценты -> пайыз
    return _NUM_RE.sub(_kk_num_repl, text)                  # остальные -> количественные


def _normalize_for_tts(text: str, language: str | None) -> str:
    """Раскрывает аббревиатуры/латиницу/числа в произносимый вид (только для озвучки)."""
    for pat, rep in _NORM_COMMON:
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    if _resolve_lang(language) == "ru":
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


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    """Слишком длинное предложение режем по словам, не превышая лимит."""
    parts: list[str] = []
    cur = ""
    for word in sentence.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            parts.append(cur)
            cur = word
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


def _concat_wav(parts: list[bytes]) -> bytes:
    """Склеивает несколько WAV-блоков в один, вставляя паузу между предложениями."""
    if len(parts) == 1:
        return parts[0]
    out = io.BytesIO()
    writer: wave.Wave_write | None = None
    gap = b""
    try:
        for idx, blob in enumerate(parts):
            with wave.open(io.BytesIO(blob), "rb") as reader:
                if writer is None:
                    writer = wave.open(out, "wb")
                    nch, sw, fr = (reader.getnchannels(), reader.getsampwidth(),
                                   reader.getframerate())
                    writer.setnchannels(nch)
                    writer.setsampwidth(sw)
                    writer.setframerate(fr)
                    n_frames = int(fr * settings.tts_gap_ms / 1000)
                    gap = b"\x00" * (n_frames * nch * sw)  # тишина нужной длины
                if idx and gap:                            # пауза перед каждым, кроме первого
                    writer.writeframes(gap)
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
    # Прочие провайдеры (spark) — прежними мелкими кусками.
    group = (settings.tts_group_chars
             if _provider_for(language) == "f5"
             else settings.tts_max_chars)
    parts = _split_for_tts(text, settings.tts_max_chars, group)
    # Выкидываем куски без букв (одни цифры/знаки) — TTS на них падает.
    parts = [p for p in parts if _has_speech(p)]
    if not parts:
        raise RuntimeError("Нет произносимого текста для синтеза")
    if len(parts) == 1:
        return await _synthesize_one(parts[0], language)
    audios = [await _synthesize_one(part, language) for part in parts]
    return _concat_wav(audios)


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
    lang = _resolve_lang(language)
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
async def _f5(text: str, language: str | None) -> bytes:
    """Вызывает отдельный F5-TTS-сервер. Контракт: POST {text, language} -> WAV."""
    import httpx
    if not settings.f5_url:
        raise RuntimeError(
            "TTS=f5, но F5_URL не задан. Запусти f5_server и укажи адрес в .env."
        )
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(settings.f5_url,
                                 json={"text": text, "language": language or "russian"})
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
            health_url = f"{url.rsplit('/', 1)[0]}/health"
            try:
                resp = await client.get(health_url)
                resp.raise_for_status()
                out[name] = {"reachable": True}
            except Exception as e:
                out[name] = {"reachable": False, "error": str(e)}
    return out

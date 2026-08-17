"""Включает клон голоса АФМ в ЧУЖОЙ обёртке OmniVoice на GPU-ноде (api.py).

На ноде `192.168.165.2:8992` стоит не наш omni_server, а обёртка админов ноды
(«KazakhTTS API»). Она зовёт model.generate() без референса, поэтому модель
выбирает голос сама: получается женский ~219 Гц, тогда как русский F5 говорит
мужским 147 Гц — «цифровой офицер» менял пол вместе с языком (замер 17.08).
Этот скрипт добавляет туда клон с нашего образца; после правки замерено 147 Гц.

Запуск на ноде:
    python3 patch_node_api.py /home/superuser/code/RakeBai/spark_tts/api.py
    python3 patch_node_api.py <путь к api.py> <каталог с Clone.wav и Clone.txt>

Образец везти файлами (refs/ref_kk_omni.wav -> Clone.wav,
refs/ref_kk_omni.txt -> Clone.txt), НЕ набирать текст руками: см. ниже.

Делает две вещи и ничего больше:
  1. после загрузки модели считает клон-промпт с образца (ОДИН раз при старте);
  2. подставляет его в model.generate() в обработчике /tts.

Идемпотентен: повторный запуск ничего не меняет. Перед правкой кладёт .bak.
Скриптом, а не руками: править отступы в живом файле через терминал — это как
раз тот случай, где ошибка стоит простоя, а кириллицу в консоли ещё и корёжит.
"""
import re
import shutil
import sys

REF_DIR_DEFAULT = "/home/superuser/code/RakeBai/spark_tts/ref"

BLOCK = '''

# --- голос АФМ: клон с образца (добавлено) ---------------------------------
# Промпт считается ОДИН раз при старте: токенизация образца стоит секунды, а
# оркестратор режет ответ на 3-5 кусков и шлёт их отдельными запросами.
# Транскрипт читается ИЗ ФАЙЛА в UTF-8 — казахский текст, набранный в консоли,
# легко превращается в кракозябры, а он обязан совпадать со сказанным в WAV.
REF_WAV = "__REF_DIR__/Clone.wav"
REF_TXT = "__REF_DIR__/Clone.txt"
_PROMPT = None
try:
    with open(REF_TXT, encoding="utf-8") as _f:
        _ref_text = _f.read().strip()
    _PROMPT = model.create_voice_clone_prompt(REF_WAV, ref_text=_ref_text)
    print(f"[tts] voice clone prompt ready from {REF_WAV}", flush=True)
except Exception as _e:
    # Сервис обязан подняться даже без образца: без клона голос просто выберет
    # модель, а падение оставило бы 20 точек без казахского вовсе.
    print(f"[tts] WARNING: no voice clone ({_e!r}) - model will pick a voice", flush=True)


def _clone_kw():
    return {"voice_clone_prompt": _PROMPT} if _PROMPT is not None else {}
# --- конец вставки ---------------------------------------------------------
'''

ANCHOR = 'print("KazakhTTS model loaded successfully!")'
GEN_ARG = re.compile(r"^(\s*)language=request\.language,\s*$", re.M)

path = sys.argv[1] if len(sys.argv) > 1 else "api.py"
ref_dir = (sys.argv[2] if len(sys.argv) > 2 else REF_DIR_DEFAULT).rstrip("/")
src = open(path, encoding="utf-8").read()

if "_clone_kw" in src:
    sys.exit("уже пропатчено — ничего не меняю")

if ANCHOR not in src:
    sys.exit(f"не нашёл строку-якорь: {ANCHOR!r} — правь вручную")
m = GEN_ARG.search(src)
if not m:
    sys.exit("не нашёл 'language=request.language,' в вызове generate — правь вручную")

shutil.copy(path, path + ".bak")

out = src.replace(ANCHOR, ANCHOR + BLOCK.replace("__REF_DIR__", ref_dir), 1)
m = GEN_ARG.search(out)                      # позиция сместилась после вставки
indent = m.group(1)                          # отступ берём из файла, не выдумываем
out = out[: m.end()] + f"\n{indent}**_clone_kw()," + out[m.end():]

open(path, "w", encoding="utf-8").write(out)
print(f"готово: {path} (бэкап {path}.bak); образец ждём в {ref_dir}/")

#!/usr/bin/env bash
# Сборка ОФЛАЙН-бандла модели для GPU-ноды АФМ (у неё нет выхода в интернет).
# Запускать ИЗ КОРНЯ проекта на машине, где модель уже скачана — например на Маке,
# где её тянул тестовый скрипт (~/.cache/huggingface):
#   bash omni_server/bundle.sh
# Результат: out/omni-models.tar.gz (~3 ГБ) — распаковывается на ноде в models/.
#
# Колёса (torch/transformers/omnivoice) сюда НЕ входят: они зависят от архитектуры
# и версии python целевой машины. Их качать на самой ноде или на боевом сервере
# (10.10.42.44, у него интернет есть) — см. omni_server/README.md.
set -euo pipefail

DEST="${DEST:-out/omni-bundle/omnivoice-kazakh}"
mkdir -p "$DEST"

# Забираем ИЗ HF-кэша, а не докачиваем: на кабеле АФМ интернета нет.
export HF_HUB_OFFLINE=1
PY="${PY:-python3}"
$PY - "$DEST" <<'EOF'
import shutil, sys
from pathlib import Path
from huggingface_hub import snapshot_download

dest = Path(sys.argv[1])
pairs = [
    ("shyngys879/KazakhTTS-OmniVoice", dest),
    # Аудио-токенизатор — ОТДЕЛЬНАЯ модель. Кладём в подпапку audio_tokenizer/:
    # тогда OmniVoice.from_pretrained берёт её оттуда и никуда не ходит.
    ("eustlb/higgs-audio-v2-tokenizer", dest / "audio_tokenizer"),
]
for repo, out in pairs:
    src = Path(snapshot_download(repo))  # только из кэша (HF_HUB_OFFLINE=1)
    out.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if f.is_dir():
            continue
        target = out / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f, target)  # copy, а не симлинк: в кэше лежат ссылки на blobs
    print(f"{repo} -> {out}")
EOF

mkdir -p out
tar -czf out/omni-models.tar.gz -C "$(dirname "$DEST")" "$(basename "$DEST")"
echo ""
echo "✅ out/omni-models.tar.gz — везти на ноду, распаковывать в models/"
du -sh out/omni-models.tar.gz

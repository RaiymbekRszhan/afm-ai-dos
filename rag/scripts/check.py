"""
Проверка перед запуском: жив ли эндпоинт эмбеддингов и какая у него
реальная размерность. Запусти ПЕРВЫМ делом и впиши EMBEDDING_DIM в .env.

    python -m scripts.check
"""
import asyncio
import sys
sys.path.insert(0, ".")

from ragsvc import config
from ragsvc.providers import _embed, llm_model_func


async def main():
    print(f"Embedding endpoint: {config.EMBEDDING_BASE_URL} / {config.EMBEDDING_MODEL}")
    try:
        vec = await _embed(["проверка связи"])
        dim = len(vec[0])
        print(f"  OK. Реальная размерность эмбеддинга = {dim}")
        if dim != config.EMBEDDING_DIM:
            print(f"  !!! В .env стоит EMBEDDING_DIM={config.EMBEDDING_DIM}. Поставь {dim}.")
    except Exception as e:
        print(f"  ОШИБКА эмбеддингов: {e}")

    print(f"\nLLM endpoint: {config.LLM_BASE_URL} / {config.LLM_MODEL or '(не задан)'}")
    if not config.LLM_MODEL:
        print("  LLM_MODEL пуст — впиши имя модели в .env")
        return
    try:
        out = await llm_model_func("Ответь одним словом: работает?", system_prompt="Ты тестовый ассистент.")
        print(f"  OK. Ответ модели: {out[:120]}")
    except Exception as e:
        print(f"  ОШИБКА LLM: {e}")


if __name__ == "__main__":
    asyncio.run(main())

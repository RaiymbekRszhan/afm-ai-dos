"""Конфигурация: читаем всё из .env, ничего не хардкодим."""
import os
from dotenv import load_dotenv

load_dotenv()


def _b(key: str, default: str = "0") -> bool:
    return os.getenv(key, default).strip() in ("1", "true", "True", "yes")


# Embedding
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "http://192.168.165.2:8806/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding-8b")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "dummy")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "4096"))
EMBEDDING_MAX_TOKENS = int(os.getenv("EMBEDDING_MAX_TOKENS", "8192"))

# LLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "dummy")

# Reranker
RERANK_ENABLED = _b("RERANK_ENABLED")
RERANK_BASE_URL = os.getenv("RERANK_BASE_URL", "")
RERANK_MODEL = os.getenv("RERANK_MODEL", "bge-reranker-v2-m3")
RERANK_API_KEY = os.getenv("RERANK_API_KEY", "dummy")

# RAG — только векторный поиск (naive). Граф знаний не используем.
WORKING_DIR = os.getenv("WORKING_DIR", "./rag_storage")
CHUNK_TOKEN_SIZE = int(os.getenv("CHUNK_TOKEN_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
# Сколько кандидатов тянуть из поиска и сколько чанков отдавать в LLM-контекст.
TOP_K = int(os.getenv("TOP_K", "40"))
CHUNK_TOP_K = int(os.getenv("CHUNK_TOP_K", "12"))

# Параллелизм/таймаут. При загрузке зовётся только эмбеддинг (LLM не дёргается —
# граф отключён); LLM_MAX_ASYNC ограничивает одновременные LLM-вызовы при ЗАПРОСЕ.
LLM_MAX_ASYNC = int(os.getenv("LLM_MAX_ASYNC", "4"))
MAX_PARALLEL_INSERT = int(os.getenv("MAX_PARALLEL_INSERT", "3"))  # документов параллельно при загрузке
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "240"))               # таймаут одного LLM-вызова (сек)

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8077"))


def assert_ready():
    missing = []
    if not LLM_MODEL:
        missing.append("LLM_MODEL")
    if not LLM_BASE_URL:
        missing.append("LLM_BASE_URL")
    if missing:
        raise RuntimeError(
            "Не заполнены обязательные параметры в .env: "
            + ", ".join(missing)
            + ". Узнай эндпоинт и имя генеративной модели у команды АФМ."
        )

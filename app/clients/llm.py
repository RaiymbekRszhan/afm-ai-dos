from openai import AsyncOpenAI

from app.config import settings

_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)


async def chat(messages: list[dict], max_tokens: int | None = None) -> str:
    """Запрос к LLM (qwen3-next-80b-instruct). Возвращает текст ответа."""
    resp = await _client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        max_tokens=max_tokens or settings.llm_max_tokens,
    )
    return resp.choices[0].message.content or ""

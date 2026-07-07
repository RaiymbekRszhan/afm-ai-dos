from openai import AsyncOpenAI

from app.config import settings

# Явный таймаут: иначе openai-SDK ждёт ответа до 600 с и может подвесить /voice.
_client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                      timeout=settings.llm_timeout)


async def chat(messages: list[dict], max_tokens: int | None = None,
               return_finish: bool = False):
    """Запрос к LLM (qwen3-next-80b-instruct).

    По умолчанию возвращает текст ответа. return_finish=True -> кортеж
    (текст, finish_reason): finish_reason == "length" означает, что ответ
    ОБРЕЗАН лимитом max_tokens (вызывающий может это учесть).
    """
    resp = await _client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        max_tokens=max_tokens or settings.llm_max_tokens,
    )
    choice = resp.choices[0]
    text = choice.message.content or ""
    if return_finish:
        return text, choice.finish_reason
    return text

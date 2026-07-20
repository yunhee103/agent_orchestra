"""노드 공용 유틸."""

from langchain_anthropic import ChatAnthropic

from config import FABLE, MODEL_CATALOG


def make_llm(model: str, max_tokens: int = 8192):
    """역할별 모델 인스턴스를 만든다. 카탈로그의 프로바이더로 디스패치한다.

    OpenAI/Google 패키지는 해당 모델을 실제로 쓸 때만 import한다
    (미설치 환경에서 Anthropic-only 사용을 막지 않기 위해).
    Fable 5는 안전 분류기가 요청을 거절(stop_reason=refusal)할 수 있어서
    서버사이드 폴백(Opus 4.8)을 기본으로 켠다.
    """
    provider = MODEL_CATALOG.get(model, "anthropic")
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, max_tokens=max_tokens)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, max_output_tokens=max_tokens)

    kwargs = {"model": model, "max_tokens": max_tokens}
    if model == FABLE:
        kwargs["default_headers"] = {
            "anthropic-beta": "server-side-fallback-2026-06-01"
        }
        kwargs["model_kwargs"] = {"fallbacks": [{"model": "claude-opus-4-8"}]}
    return ChatAnthropic(**kwargs)


def strip_code_fence(raw: str) -> str:
    """모델이 ```(lang) ... ``` 형태로 감싸 응답한 경우 펜스를 벗긴다."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


if __name__ == "__main__":
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence("```\ncode\n```") == "code"
    assert strip_code_fence("no fence") == "no fence"
    print("ok")

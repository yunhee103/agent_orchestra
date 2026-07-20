"""노드 공용 유틸."""

import os
from types import SimpleNamespace

from langchain_anthropic import ChatAnthropic

from config import FABLE, MODEL_CATALOG


class SubscriptionClaude:
    """Claude Code 구독 인증(Agent SDK)으로 호출하는 어댑터.

    ChatAnthropic.ainvoke와 같은 모양(messages -> .content 있는 응답)이라
    노드 코드는 어떤 경로로 호출되는지 몰라도 된다. API 크레딧 대신
    사용자의 Pro/Max 구독 한도를 쓴다. 호출마다 Claude Code 프로세스가
    하나 뜨므로 API 경로보다 시작 지연이 있다.
    """

    def __init__(self, model: str, max_tokens: int = 8192):
        self.model = model

    async def ainvoke(self, messages):
        # uvicorn(Windows)의 SelectorEventLoop는 서브프로세스 생성이 안 되므로
        # 전용 스레드에서 Proactor 루프를 만들어 CLI를 띄운다.
        import asyncio
        return await asyncio.to_thread(self._invoke_in_thread, messages)

    def _invoke_in_thread(self, messages):
        import asyncio
        import sys
        loop = (asyncio.ProactorEventLoop() if sys.platform == "win32"
                else asyncio.new_event_loop())
        try:
            return loop.run_until_complete(self._query(messages))
        finally:
            loop.close()

    async def _query(self, messages):
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      TextBlock, query)
        system_parts, user_parts = [], []
        for m in messages:
            if isinstance(m, tuple):          # ("system", "...") 형태
                role, content = m
            else:                             # SystemMessage / HumanMessage
                role = ("system" if m.__class__.__name__ == "SystemMessage"
                        else "human")
                content = m.content
            (system_parts if role == "system" else user_parts).append(content)

        options = ClaudeAgentOptions(
            model=self.model,
            # tools=[] 가 진짜 '도구 없음'(--tools ""). allowed_tools=[]는
            # '제한 없음'으로 해석돼 모델이 도구를 쓰다 턴을 소모한다.
            tools=[],
            max_turns=2,
            system_prompt="\n\n".join(system_parts) or None,
            # 서버에 API 키가 있어도 자식 CLI에는 물려주지 않는다 —
            # 구독 모드의 목적 자체가 키/크레딧 없이 호출하는 것.
            env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""},
        )
        text = ""
        async for msg in query(prompt="\n\n".join(user_parts), options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text += block.text
        return SimpleNamespace(content=text)


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
    if os.environ.get("CLAUDE_AUTH_MODE") == "subscription":
        return SubscriptionClaude(model, max_tokens)

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

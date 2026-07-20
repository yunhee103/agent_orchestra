"""트렌드 리서치 노드.

요청 도메인의 최신 기술 트렌드를 실시간 웹 검색으로 조사해서
결정 수집·설계 단계에 공급한다. 검색 API 계약 없이 동작하도록
DuckDuckGo HTML 엔드포인트를 직접 조회한다.
"""

import re

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from config import MODEL_CATALOG
from nodes.util import make_llm
from state import OrchestraState

SEARCH_URL = "https://html.duckduckgo.com/html/"
# ponytail: 정규식 파싱 — DDG 마크업이 바뀌면 결과 0건으로 조용히 실패하는 대신
# trend_report가 "조사 실패"로 남고 파이프라인은 계속 진행된다.
RESULT_RE = re.compile(
    r'class="result__a"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")

SUMMARIZE_PROMPT = """너는 기술 트렌드 리서처다. 아래 웹 검색 결과를 바탕으로
이 프로젝트의 기술 선택에 영향을 줄 최신 트렌드를 요약하라.

포함할 것: 관련 프레임워크/라이브러리의 현재 권장 버전과 모범 사례,
최근 뜨거나 지는 기술, 주의해야 할 변화(deprecated 등).
출처가 뒷받침하지 않는 내용은 지어내지 말 것. 5줄 이내 한국어 요약."""


async def search_web(query: str, max_results: int = 8) -> list[dict]:
    """DuckDuckGo HTML 검색. [{title, snippet}] 반환, 실패 시 빈 리스트."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                SEARCH_URL,
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (agent-orchestra)"},
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        return []
    results = []
    for m in RESULT_RE.finditer(resp.text):
        results.append({
            "title": TAG_RE.sub("", m.group("title")).strip(),
            "snippet": TAG_RE.sub("", m.group("snippet")).strip(),
        })
        if len(results) >= max_results:
            break
    return results


async def _gemini_grounded_trends(state: OrchestraState, model: str) -> str:
    """Gemini의 Google 검색 그라운딩으로 직접 조사한다.

    유틸 모델이 Gemini일 때만 사용 — 검색·요약이 한 호출로 끝나고
    DDG 스크래핑보다 검색 품질이 좋다.
    """
    from google.genai import types as genai_types
    from langchain_google_genai import ChatGoogleGenerativeAI

    llm = ChatGoogleGenerativeAI(model=model, max_output_tokens=1024)
    response = await llm.ainvoke(
        [SystemMessage(content=SUMMARIZE_PROMPT),
         HumanMessage(content=(
             f"프로젝트 요청: {state['user_request']}\n\n"
             "위 요청과 관련된 최신 기술 트렌드를 웹에서 검색해 요약하라."
         ))],
        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
    )
    return response.content


async def trend_research(state: OrchestraState) -> dict:
    """요청 관련 최신 트렌드를 조사해 요약을 상태에 넣는다."""
    model = state["models"]["utility"]
    if MODEL_CATALOG.get(model) == "google":
        try:
            report = await _gemini_grounded_trends(state, model)
            return {"trend_report": f"(Google 검색 그라운딩)\n{report}",
                    "llm_call_count": 1}
        except Exception:
            pass   # 그라운딩 실패 시 아래 DDG 경로로 폴백

    query = f"{state['user_request'][:80]} 기술 스택 모범 사례 2026"
    results = await search_web(query)
    if not results:
        return {"trend_report": "(웹 조사 실패 — 트렌드 정보 없이 진행)",
                "llm_call_count": 0}

    corpus = "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)
    llm = make_llm(state["models"]["utility"], max_tokens=1024)
    response = await llm.ainvoke([
        SystemMessage(content=SUMMARIZE_PROMPT),
        HumanMessage(content=f"프로젝트 요청: {state['user_request']}\n\n검색 결과:\n{corpus}"),
    ])
    return {"trend_report": response.content, "llm_call_count": 1}

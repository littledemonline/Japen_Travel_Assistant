import os
import re
import json
import urllib.parse
from datetime import datetime, date
from typing import Annotated, Sequence, TypedDict, Literal
import requests
from dotenv import load_dotenv
from duckduckgo_search import DDGS

def sanitize_maps_links(text: str) -> str:
    """
    LLM이 임의로 지어낸 goo.gl/maps/... 또는 maps.app.goo.gl/... 등의 가짜 단축 URL을
    실제 100% 작동하는 https://www.google.com/maps?q=장소명 형식으로 자동 치유/변환합니다.
    """
    if not text:
        return text
    
    lines = text.split("\n")
    fixed_lines = []
    
    for line in lines:
        if "goo.gl/maps" in line or "maps.app.goo.gl" in line:
            pattern = r'\[([^\]]*)\]\(https?://(?:goo\.gl/maps|maps\.app\.goo\.gl)/[^\)]+\)'
            matches = list(re.finditer(pattern, line))
            if matches:
                for m in reversed(matches):
                    before = line[:m.start()]
                    target = ""
                    quoted = re.findall(r'["\']([^"\']+)["\']', before)
                    if quoted:
                        target = quoted[-1].strip()
                    else:
                        bolded = re.findall(r'\*\*([^\*]+)\*\*', before)
                        if bolded:
                            target = bolded[-1].strip()
                        else:
                            cleaned_prefix = re.sub(r'^[-\*\d\.\s#:]+', '', before).strip()
                            parts = [p.strip() for p in cleaned_prefix.split(':') if p.strip()]
                            if parts:
                                target = parts[-1]
                    
                    target = re.sub(r'[\[\]\(\)\{\}]', '', target).strip()
                    if not target:
                        target = "일본 명소"
                    
                    encoded = urllib.parse.quote_plus(target)
                    safe_url = f"https://www.google.com/maps?q={encoded}"
                    line = line[:m.start()] + f"[🗺️ 지도 보기]({safe_url})" + line[m.end():]
        fixed_lines.append(line)
        
    return "\n".join(fixed_lines)

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ------------------------------------------------------------------
# 환경설정 로드
# ------------------------------------------------------------------
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(dotenv_path=env_path)
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
hotpepper_api_key = os.getenv("HOTPEPPER_API_KEY")
model_name = os.getenv("OPENAI_MODEL", "gpt-4o")
# 만약 .env에 gpt-6-astra 같은 실험적 이름이 있으면 표준 gpt-4o 계열로 보정
if "gpt-6" in model_name:
    model_name = "gpt-4o"

llm = ChatOpenAI(model=model_name, openai_api_key=api_key, temperature=0.7)

# 라우터 전용 LLM: 판단 속도 향상, 토큰 폭주 방지(max_tokens=200, temperature=0)
router_llm = ChatOpenAI(
    model="gpt-4o-mini" if "gpt-4" in model_name else model_name,
    openai_api_key=api_key,
    temperature=0,
    max_tokens=200
)

# ------------------------------------------------------------------
# 외부 도구 함수 (API & 검색)
# ------------------------------------------------------------------
def get_realtime_jpy_rate() -> float:
    """실시간 100엔당 원화 환율"""
    try:
        res = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=5)
        if res.status_code == 200:
            data = res.json()
            krw_per_1jpy = data["rates"]["KRW"]
            return round(krw_per_1jpy * 100, 2)
    except Exception:
        pass
    return 865.0

def search_hotpepper(keyword: str, count: int = 3) -> str:
    """핫페퍼 API로 일본 현지 맛집 검색"""
    if not hotpepper_api_key:
        return "Hot Pepper API Key가 설정되지 않았습니다."
    url = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
    params = {
        "key": hotpepper_api_key,
        "keyword": keyword,
        "format": "json",
        "count": count
    }
    try:
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            shops = res.json().get("results", {}).get("shop", [])
            if not shops:
                return f"'{keyword}'에 대한 핫페퍼 검색 결과가 없습니다."
            results = []
            for s in shops:
                results.append({
                    "식당명": s.get("name"),
                    "종류": s.get("genre", {}).get("name", "음식점"),
                    "예산": s.get("budget", {}).get("name", "정보 없음"),
                    "위치": s.get("address", ""),
                    "상세링크": s.get("urls", {}).get("pc", "")
                })
            return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return f"Hot Pepper 검색 실패: {e}"
    return "검색 결과 없음"

def search_ddg(query: str, max_results: int = 3) -> str:
    """덕덕고 웹 검색"""
    try:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return f"'{query}'에 대한 검색 결과가 없습니다."
        snippets = [{"제목": r.get("title"), "요약": r.get("body"), "링크": r.get("href")} for r in results]
        return json.dumps(snippets, ensure_ascii=False)
    except Exception as e:
        return f"웹 검색 오류: {e}"

# ------------------------------------------------------------------
# LangGraph 상태 (State) 정의
# ------------------------------------------------------------------
class TravelState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    next: str
    iterations: int  # 무한루프 방지용 카운터
    current_status: str

# ------------------------------------------------------------------
# Supervisor 라우터 정의
# ------------------------------------------------------------------
MEMBERS = ["itinerary_agent", "gourmet_agent", "research_agent"]

class SupervisorRouter(BaseModel):
    """다음 작업을 수행할 워커 에이전트 또는 완료 여부를 결정합니다."""
    next: Literal["itinerary_agent", "gourmet_agent", "research_agent", "FINISH"] = Field(
        description="다음에 작업을 수행할 에이전트"
    )
    reason: str = Field(description="선택 사유를 한 문장(20자 이내)으로 짧게 요약")

def supervisor_node(state: TravelState):
    """모든 워커의 산출물을 검토하고 다음 단계를 지시하거나 마무리를 결정하는 관리자 노드"""
    count = state.get("iterations", 0)
    
    # 안전장치: 최대 4회 왕복 후 강제 종료
    if count >= 3:
        return {"next": "FINISH", "iterations": count + 1, "current_status": "Supervisor: 결과 종합 완료"}

    system_prompt = f"""당신은 일본 여행 계획 전문가 팀의 총괄 관리자(Supervisor)입니다.
오늘 날짜: {date.today()}

팀원:
1. 'itinerary_agent': 일자별 동선 및 관광 명소 기획
2. 'gourmet_agent': 핫페퍼 현지 맛집/식당 조사
3. 'research_agent': 최신 교통 패스, 시즌 이벤트, 환율 정보 조사

[주의사항]
- 당신은 절대로 직접 여행 계획 본문을 작성하지 마세요.
- 오직 다음에 실행할 팀원을 지목하거나, 조사가 충분하면 'FINISH'만 선택하세요.
- reason은 1문장으로 매우 간단히 작성하세요.
"""

    messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
    router = router_llm.with_structured_output(SupervisorRouter)
    decision = router.invoke(messages)
    
    status_text = f"👑 Supervisor 지휘: {decision.next} 호출 (사유: {decision.reason})"
    return {
        "next": decision.next,
        "iterations": count + 1,
        "current_status": status_text
    }

# ------------------------------------------------------------------
# 워커 1: 일정 & 동선 에이전트 (Itinerary Agent)
# ------------------------------------------------------------------
def itinerary_agent_node(state: TravelState):
    """일자별 동선과 관광 스팟 기획"""
    prompt = """당신은 '일본 여행 일정 & 동선 플래너(Itinerary Agent)'입니다.
사용자의 여행 목적지, 기간, 취향에 맞춰 Day 1부터 Day N까지의 오전/오후/저녁 추천 이동 동선을 기획하세요.

[🚨 구글 지도 링크 작성 필수 규칙 - 위반 절대 금지]
- 절대로 'goo.gl/maps/...', 'maps.app.goo.gl/...' 같은 단축 URL을 임의로 지어내지 마세요! 가짜 주소가 되어 접속 시 404 에러가 납니다.
- 지도 링크는 반드시 실제 검색 쿼리 형태의 전체 주소로 작성하세요:
  형식: [🗺️ 지도 보기](https://www.google.com/maps?q=도시명+장소명)
  예시: [🗺️ 지도 보기](https://www.google.com/maps?q=도쿄+센소지)
무리한 일정보다는 이동 효율성과 체력을 고려한 동선을 제시하세요."""

    messages = [SystemMessage(content=prompt)] + list(state["messages"])
    response = llm.invoke(messages)
    return {
        "messages": [AIMessage(content=f"🗺️ **[일정 플래너]**\n{response.content}", name="itinerary_agent")],
        "current_status": "🗺️ 일정 플래너: 일자별 동선 기획 완료"
    }

# ------------------------------------------------------------------
# 워커 2: 핫페퍼 맛집 에이전트 (Gourmet Agent)
# ------------------------------------------------------------------
def gourmet_agent_node(state: TravelState):
    """현지 맛집 및 식당 탐색"""
    last_user_msg = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "")
    
    # 검색 키워드 추출
    keyword_prompt = f"""다음 사용자의 여행지/질문에서 핫페퍼 일본 식당 검색용 키워드 1개만 추출하세요 (예: '도쿄 스시', '오사카 라멘', '후쿠오카 모츠나베'). 키워드만 딱 출력하세요:
질문: {last_user_msg}"""
    kw_res = llm.invoke(keyword_prompt).content.strip().replace('"', '')
    
    restaurant_data = search_hotpepper(kw_res, count=3)
    
    prompt = f"""당신은 '일본 현지 미식 전문가(Gourmet Agent)'입니다.
핫페퍼(Hot Pepper) 검색 데이터를 바탕으로 여행자에게 추천할 식당 정보를 정리해주세요.
식당명, 대표 메뉴/장르, 1인당 예상 예산, 위치, 상세/예약링크를 깔끔하게 안내하세요.

[핫페퍼 검색 결과]
{restaurant_data}
"""
    messages = [SystemMessage(content=prompt)] + list(state["messages"])
    response = llm.invoke(messages)
    return {
        "messages": [AIMessage(content=f"🍜 **[미식/맛집 전문가]**\n{response.content}", name="gourmet_agent")],
        "current_status": "🍜 맛집 전문가: 핫페퍼 현지 식당 분석 완료"
    }

# ------------------------------------------------------------------
# 워커 3: 실시간 리서치 & 교통 에이전트 (Research Agent)
# ------------------------------------------------------------------
def research_agent_node(state: TravelState):
    """최신 날씨, 교통 패스, 행사 등 실시간 정보 조사"""
    last_user_msg = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "")
    
    kw_prompt = f"""다음 질문에서 실시간 웹 검색할 최적의 쿼리를 1개 작성하세요 (예: '오사카 10월 날씨 옷차림', '도쿄 지하철 패스 추천'):
질문: {last_user_msg}"""
    search_query = llm.invoke(kw_prompt).content.strip().replace('"', '')
    
    search_data = search_ddg(search_query, max_results=3)
    rate = get_realtime_jpy_rate()
    
    prompt = f"""당신은 '일본 여행 정보 & 교통 리서처(Research Agent)'입니다.
현재 실시간 환율: 100엔 = {rate}원
실시간 웹 검색 결과:
{search_data}

위 정보를 참고하여 알짜 교통 패스(스이카/파스모/지하철 패스), 계절 날씨/축제 팁, 환율 기준 예상 경비 팁을 핵심만 작성하세요.
"""
    messages = [SystemMessage(content=prompt)] + list(state["messages"])
    response = llm.invoke(messages)
    return {
        "messages": [AIMessage(content=f"🔍 **[실시간 리서처]**\n{response.content}", name="research_agent")],
        "current_status": "🔍 실시간 리서처: 최신 교통 및 여행 정보 조사 완료"
    }

# ------------------------------------------------------------------
# 최종 종합 노드 (Final Synthesis)
# ------------------------------------------------------------------
def final_synthesis_node(state: TravelState):
    """각 워커 에이전트들의 결과를 종합하여 사용자에게 고품질 최종 여행 가이드 제공"""
    rate = get_realtime_jpy_rate()
    system_prompt = f"""당신은 일본 여행 총괄 어시스턴트입니다.
지금까지 우리 팀(일정 플래너, 맛집 전문가, 실시간 리서처)이 수집하고 기획한 내용을 종합하여,
사용자가 보기 편하게 잘 정돈된 **'맞춤형 일본 여행 종합 가이드'**로 완성해 주세요.

[필수 구성]
1. ✈️ 여행 개요 & 핵심 포인트
2. 📅 일자별 상세 코스 (Day별 동선, 구글 지도 링크 포함)
3. 🍜 엄선된 현지 맛집 (예산 및 예약 링크 포함)
4. 🚇 알짜 대중교통 & 필수 패스 추천
5. 💰 예상 경비 & 환율 요약 (오늘 환율 100엔 = 약 {rate}원 기준)

[🚨 구글 지도(Google Maps) 링크 작성 필수 규칙 - 위반 절대 금지]:
- 절대로 'goo.gl/maps/...', 'maps.app.goo.gl/...' 같은 가짜 단축 URL(Short URL)을 임의로 지어내지 마세요! (존재하지 않는 가짜 주소가 되어 Dynamic Link Not Found 에러가 발생합니다)
- 지도 링크는 반드시 실제 검색 쿼리 형태의 전체 주소로만 작성하세요:
  형식: [🗺️ 지도 보기](https://www.google.com/maps?q=도시명+장소명)
  예시: [🗺️ 지도 보기](https://www.google.com/maps?q=도쿄+시부야+스크램블+교차로)

각 팀원의 전문 정보를 빠짐없이 자연스럽게 융합하여 마크다운 형태로 친절하게 작성하세요.
"""
    messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
    response = llm.invoke(messages)
    return {
        "messages": [AIMessage(content=response.content, name="travel_assistant")],
        "current_status": "✨ 최종 종합 여행 가이드 완성!"
    }

# ------------------------------------------------------------------
# 그래프 구성 (StateGraph)
# ------------------------------------------------------------------
def build_travel_graph():
    builder = StateGraph(TravelState)
    
    # 노드 등록
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("itinerary_agent", itinerary_agent_node)
    builder.add_node("gourmet_agent", gourmet_agent_node)
    builder.add_node("research_agent", research_agent_node)
    builder.add_node("final_synthesis", final_synthesis_node)
    
    # 엣지 연결
    builder.add_edge(START, "supervisor")
    
    # Supervisor의 분기 조건부 엣지
    def route_supervisor(state: TravelState):
        next_step = state.get("next")
        if next_step == "FINISH":
            return "final_synthesis"
        return next_step

    builder.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "itinerary_agent": "itinerary_agent",
            "gourmet_agent": "gourmet_agent",
            "research_agent": "research_agent",
            "final_synthesis": "final_synthesis"
        }
    )
    
    # 각 워커는 작업 후 다시 supervisor에게 보고
    for member in MEMBERS:
        builder.add_edge(member, "supervisor")
        
    builder.add_edge("final_synthesis", END)
    
    return builder.compile()

travel_graph = build_travel_graph()

# ------------------------------------------------------------------
# 외부(Streamlit 또는 터미널) 호출용 헬퍼 함수
# ------------------------------------------------------------------
def run_travel_assistant(user_input: str, conversation_history=None, status_callback=None):
    """
    Streamlit 또는 CLI에서 호출하여 여행 계획을 수립하는 함수
    - status_callback: 각 노드 실행 시 상태 메시지를 수신하는 함수(콜백)
    """
    messages = []
    if conversation_history:
        for m in conversation_history:
            if m.get("role") == "user":
                messages.append(HumanMessage(content=m["content"]))
            elif m.get("role") == "assistant":
                messages.append(AIMessage(content=m["content"]))
    
    messages.append(HumanMessage(content=user_input))
    
    initial_state: TravelState = {
        "messages": messages,
        "next": "supervisor",
        "iterations": 0,
        "current_status": "Supervisor 시작..."
    }
    
    # 스트리밍 실행으로 진행 상황 추적
    final_output = ""
    for event in travel_graph.stream(initial_state):
        for node_name, state_update in event.items():
            current_status = state_update.get("current_status", f"{node_name} 실행 중...")
            if status_callback:
                status_callback(node_name, current_status)
            
            if node_name == "final_synthesis":
                final_messages = state_update.get("messages", [])
                if final_messages:
                    final_output = final_messages[-1].content
                    
    return sanitize_maps_links(final_output)

# ------------------------------------------------------------------
# 단독 테스트 실행부
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("🇯🇵 일본 여행 멀티 에이전트 (Supervisor) 테스트 실행")
    print("=" * 60)
    
    test_query = "후쿠오카 2박 3일 1인 예산 50만원 여행 계획 짜줘. 맛있는 라멘과 모츠나베 먹고 싶어."
    print(f"\n[사용자 질문]: {test_query}\n")
    
    def print_status(node, msg):
        print(f"👉 [{node}] {msg}")
        
    result = run_travel_assistant(test_query, status_callback=print_status)
    print("\n" + "=" * 60)
    print("📋 [최종 결과]")
    print("=" * 60)
    print(result)

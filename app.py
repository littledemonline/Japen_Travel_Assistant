import streamlit as st
import os
import json
import uuid
import hmac
import urllib.parse
from datetime import datetime, date, timedelta
import requests
from dotenv import load_dotenv
from openai import OpenAI
from duckduckgo_search import DDGS

# ------------------------------------------------------------------
# 환경설정 및 모델 초기화
# ------------------------------------------------------------------
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(dotenv_path=env_path)
load_dotenv()

# Streamlit Cloud Secrets가 설정된 경우 환경변수로 동기화 (로컬 .env와 호환)
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)) and _k not in os.environ:
            os.environ[_k] = str(_v)
except Exception:
    pass

app_password = os.getenv("APP_PASSWORD")
api_key = os.getenv("OPENAI_API_KEY")
selected_model = os.getenv("OPENAI_MODEL", "gpt-6-astra")
hotpepper_api_key = os.getenv("HOTPEPPER_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None

try:
    from supervisor import run_travel_assistant, sanitize_maps_links
    SUPERVISOR_AVAILABLE = True
except Exception as e:
    SUPERVISOR_AVAILABLE = False
    def sanitize_maps_links(text):
        return text

st.set_page_config(
    page_title="AI 일본 여행 어시스턴트 | Japan Travel Chatbot",
    page_icon="🗾",
    layout="wide"
)

# ------------------------------------------------------------------
# 0. 비밀번호 인증 게이트 (API Key 무단 사용 방지)
# ------------------------------------------------------------------
def require_password() -> bool:
    """
    .env의 APP_PASSWORD와 일치하는 비밀번호를 입력해야만 앱 접근을 허용합니다.
    """
    if st.session_state.get("authenticated", False):
        return True

    if not app_password:
        st.error("`.env` 파일에 `APP_PASSWORD`가 설정되어 있지 않아 접근이 차단되었습니다.")
        return False

    def _verify_password():
        entered = st.session_state.get("password_input", "")
        if hmac.compare_digest(entered, app_password):
            st.session_state.authenticated = True
            st.session_state.pop("password_input", None)
        else:
            st.session_state.authenticated = False
            st.session_state.password_error = True

    st.text_input(
        "🔒 비밀번호",
        type="password",
        key="password_input",
        on_change=_verify_password,
        placeholder="비밀번호 입력 후 Enter",
    )
    if st.session_state.pop("password_error", False):
        st.error("비밀번호가 올바르지 않습니다.")

    return False

if not require_password():
    st.stop()

# ------------------------------------------------------------------
# 1. 실시간 엔화 환율 조회 (Open Exchange API, 30분 캐시)
# ------------------------------------------------------------------
@st.cache_data(ttl=1800)
def get_realtime_jpy_rate():
    """
    무료 오픈 환율 API를 통해 100엔당 원화(KRW) 환율을 실시간으로 가져옵니다.
    """
    try:
        res = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=5)
        if res.status_code == 200:
            data = res.json()
            krw_per_1jpy = data["rates"]["KRW"]
            return round(krw_per_1jpy * 100, 2)
    except Exception:
        pass
    return 862.5

current_jpy_rate = get_realtime_jpy_rate()

# ------------------------------------------------------------------
# 2. 핫페퍼 일본 현지 맛집 검색 함수 (Hot Pepper Gourmet API)
# ------------------------------------------------------------------
def search_hotpepper_restaurants(keyword: str, count: int = 3) -> str:
    """
    리크루트 핫페퍼 API를 통해 일본 현지 식당의 실제 상호, 장르, 예산, 주소, 예약 링크를 검색합니다.
    """
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
            data = res.json()
            shops = data.get("results", {}).get("shop", [])
            if not shops:
                return f"'{keyword}'에 대한 핫페퍼 검색 결과가 없습니다."
            
            results = []
            for s in shops:
                name = s.get("name")
                genre = s.get("genre", {}).get("name", "음식점")
                budget = s.get("budget", {}).get("name", "가격 정보 없음")
                address = s.get("address", "")
                pc_url = s.get("urls", {}).get("pc", "")
                results.append({
                    "식당명": name,
                    "종류": genre,
                    "예산": budget,
                    "위치": address,
                    "상세/예약링크": pc_url
                })
            return json.dumps(results, ensure_ascii=False)
        else:
            return f"Hot Pepper API 오류: {res.status_code}"
    except Exception as e:
        return f"Hot Pepper 요청 실패: {e}"

# ------------------------------------------------------------------
# 3. 덕덕고 무료 실시간 웹 검색 (DuckDuckGo Live Search)
# ------------------------------------------------------------------
def search_duckduckgo_web(query: str, max_results: int = 3) -> str:
    """
    API 키 없이 덕덕고를 통해 항공권 최신 가격, 축제, 숙소 정보 등을 실시간 검색합니다.
    """
    try:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return f"'{query}'에 대한 웹 검색 결과가 없습니다."
        
        snippets = []
        for r in results:
            snippets.append({
                "제목": r.get("title"),
                "요약": r.get("body"),
                "링크": r.get("href")
            })
        return json.dumps(snippets, ensure_ascii=False)
    except Exception as e:
        return f"DuckDuckGo 검색 오류: {e}"

# ------------------------------------------------------------------
# 4. 스카이스캐너 / 네이버 항공권 / 아고다 다이렉트 검색 링크 생성
# ------------------------------------------------------------------
def generate_travel_direct_links(destination: str) -> dict:
    """
    항공권 및 호텔의 실시간 최저가를 원클릭으로 비교할 수 있는 다이렉트 링크를 생성합니다.
    """
    encoded_dest = urllib.parse.quote(destination)
    return {
        "skyscanner": f"https://www.skyscanner.co.kr/transport/flights/sela/{encoded_dest}/",
        "naver_flight": f"https://flight.naver.com/flights/international/SEL-{encoded_dest}",
        "agoda": f"https://www.agoda.com/ko-kr/search?city={encoded_dest}"
    }

# ------------------------------------------------------------------
# OpenAI 도구(Function Tools) 정의
# ------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_hotpepper_restaurants",
            "description": "일본 현지 식당(라멘, 스시, 야키니쿠, 이자카야 등)의 실제 상호, 예산, 주소, 예약 링크를 일본 최대 외식 플랫폼인 핫페퍼(Hot Pepper)에서 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "검색할 지역과 음식 종류 (예: '新宿 ラーメン', '難波 焼肉', '도쿄 라멘')"
                    }
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_duckduckgo_web",
            "description": "실시간 최신 항공권 가격대, 특정 시즌의 일본 축제/단풍/벚꽃 일정, 특정 호텔 최신 후기 등을 웹에서 실시간으로 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색할 검색어 (예: '일본 도쿄 11월 항공권 가격', '오사카 가성비 호텔 추천')"
                    }
                },
                "required": ["query"]
            }
        }
    }
]

TOOL_MAP = {
    "search_hotpepper_restaurants": search_hotpepper_restaurants,
    "search_duckduckgo_web": search_duckduckgo_web
}

# ------------------------------------------------------------------
# 대화 히스토리 파일 저장소 관리 함수
# ------------------------------------------------------------------
HISTORY_DIR = os.path.join(current_dir, "chat_history")
os.makedirs(HISTORY_DIR, exist_ok=True)

def get_all_chats():
    chats = []
    for fname in os.listdir(HISTORY_DIR):
        if fname.endswith(".json"):
            fpath = os.path.join(HISTORY_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    chats.append(json.load(f))
            except Exception:
                continue
    chats.sort(key=lambda x: x.get("updated_at", x.get("created_at", "")), reverse=True)
    return chats

def save_chat(session_id, title, messages):
    fpath = os.path.join(HISTORY_DIR, f"{session_id}.json")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_at = now_str
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                created_at = json.load(f).get("created_at", now_str)
        except Exception:
            pass

    chat_data = {
        "id": session_id,
        "title": title,
        "created_at": created_at,
        "updated_at": now_str,
        "messages": messages
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(chat_data, f, ensure_ascii=False, indent=2)

def delete_chat(session_id):
    fpath = os.path.join(HISTORY_DIR, f"{session_id}.json")
    if os.path.exists(fpath):
        os.remove(fpath)

# ------------------------------------------------------------------
# 세션 상태 초기화
# ------------------------------------------------------------------
DEFAULT_GREETING = (
    "안녕하세요! 🗾 **일본 여행 전문 AI 어시스턴트**입니다.\n\n"
    "가고 싶으신 **일본 여행지, 여행 기간(날짜), 총 예산(금액)**을 편하게 말씀해 주시면, "
    "실시간 엔화 환율, 핫페퍼 현지 맛집, 항공권·선박편, 숙소, 지하철/버스 교통 패스까지 꽉 찬 계획을 세워드릴게요!\n\n"
    "어디로 언제 떠나고 싶으신가요?"
)

if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = str(uuid.uuid4())
    st.session_state.current_title = "새로운 일본 여행"
    st.session_state.messages = [{"role": "assistant", "content": DEFAULT_GREETING}]

# ------------------------------------------------------------------
# 시스템 프롬프트
# ------------------------------------------------------------------
SYSTEM_PROMPT = f"""
당신은 '일본 여행 전문 멀티 에이전트 AI 어시스턴트'입니다.
오늘 날짜 기준: {date.today()}
오늘의 실시간 엔화 환율: 100 JPY = 약 {current_jpy_rate} KRW

[핵심 역할 및 도구 활용 규칙]
1. 당신의 유일한 전문 분야는 '일본 여행'입니다.
2. [가드레일 - 엄격 준수]:
   - 가벼운 일상 인사("안녕하세요", "반가워요" 등)에는 친절하게 인사하며 여행 상담을 도와드리겠다고 맞이하세요.
   - 일본 여행과 전혀 상관없는 질문(코딩, 주식, 정치, 연예, 수학 문제 등)을 받으면 반드시 정중히 거절하고 여행 질문을 유도하세요:
     "죄송합니다. 저는 일본 여행 전문 어시스턴트입니다. 일본 여행(일정, 비행기/배편, 숙소, 지하철/버스 교통, 맛집, 예산 등)과 관련된 내용을 질문해 주시면 친절히 안내해 드리겠습니다!"

3. [실시간 도구(Tools) 적극 활용]:
   - 맛집을 추천할 때는 `search_hotpepper_restaurants` 도구를 적극 활용하여 일본 현지 실제 식당 상호, 예산, 주소를 가져와 추천하세요.
   - 최신 항공권 가격 동향, 특정 축제 일정, 추천 호텔이 궁금할 때는 `search_duckduckgo_web` 도구를 활용해 웹 검색 결과를 바탕으로 답변하세요.

4. [여행 계획 수립 시 필수 포함 항목]:
   ① 💰 **예산 분석 & 실시간 환율**:
      - 1인당 총 예산 지출 배분(항공·교통/숙박/식비·활동비/예비비) 표
      - 오늘의 실제 환율(100엔당 약 {current_jpy_rate}원)을 기준으로 엔화 환산 금액 병기 및 결제 팁(트래블로그, 현금 비중)
   ② ✈️🚢 **출발 교통편 (항공권 및 선박편)**:
      - 최적 항공편 (직항 노선, 추천 항공사, 출도착 공항 선택 팁)
      - 해당 지역이 부산권에서 출발 가능한 경우(후쿠오카, 오사카, 시모노세키 등) 선박/페리 옵션(뉴카멜리아, 퀸비틀, 팬스타 등) 비교 안내
   ③ 🚇🚌 **현지 대중교통 완벽 가이드**:
      - 공항 ↔ 시내 이동법, 지하철/버스 팁, IC카드(스이카/이코카/파스모), 알짜 '교통 패스'
   ④ 🏨 **숙소 추천**:
      - 1박당 예산에 맞는 실제 호텔/료칸 상호와 장점
   ⑤ 📅 **일자별 상세 여행 코스 (Day 1 ~ End)**:
      - 오전 / 오후 / 저녁 시간대별 추천 스팟 및 무리 없는 이동 동선
   ⑥ 🍜 **현지 식당 & 미식 추천 (핫페퍼 데이터 반영)**:
      - 실제 방문지 인근의 검증된 맛집과 추천 메뉴, 1인당 예상 식대
   ⑦ 💡 **필수 준비물 & 여행 팁**:
      - Visit Japan Web, 돼지코 플러그, 데이터(eSIM/유심) 등

5. [🗺️ 구글 지도(Google Maps) 연동 규칙]:
   - 추천하는 모든 숙소, 관광지/명소, 식당, 카페의 이름 뒤에는 반드시 구글 지도 다이렉트 검색 링크를 붙여주세요.
   - [🚨 단축 URL 절대 금지]: 절대로 'goo.gl/maps/...' 나 'maps.app.goo.gl/...' 같은 단축 URL을 지어내지 마세요. 404 오류가 발생합니다.
   - 반드시 표준 검색 쿼리 형식만 사용하세요: [🗺️ 지도 보기](https://www.google.com/maps?q={{도시명}}+{{장소명}})
"""

# ------------------------------------------------------------------
# 사이드바 레이아웃
# ------------------------------------------------------------------
with st.sidebar:
    st.title("🗾 여행 비서 설정")
    st.caption(f"🤖 구동 모델: `{selected_model}`")

    agent_mode = st.radio(
        "🧠 실행 모드 선택",
        ["👑 멀티 에이전트 (Supervisor)", "⚡ 기본 챗봇 (Single Agent)"],
        index=0 if SUPERVISOR_AVAILABLE else 1,
        help="Supervisor 모드는 일정 플래너, 맛집 전문가, 실시간 리서처 팀이 체계적으로 분업 및 협업하여 고품질 여행 계획을 세웁니다."
    )
    
    if st.button("➕ 새 여행 대화 시작", use_container_width=True, type="primary"):
        st.session_state.current_session_id = str(uuid.uuid4())
        st.session_state.current_title = "새로운 일본 여행"
        st.session_state.messages = [{"role": "assistant", "content": DEFAULT_GREETING}]
        st.rerun()

    st.markdown("---")
    st.subheader("💱 실시간 엔화 환율")
    st.metric(
        label="100 JPY (엔화)",
        value=f"{current_jpy_rate:,} 원",
        help="Open Exchange Rates 기준 실시간 환율 (30분 주기 자동 갱신)"
    )

    st.markdown("---")
    st.subheader("📜 저장된 여행 계획 목록")
    saved_chats = get_all_chats()
    if not saved_chats:
        st.caption("아직 저장된 대화가 없습니다.")
    else:
        for chat in saved_chats:
            c_id = chat["id"]
            c_title = chat.get("title", "제목 없음")
            is_active = (c_id == st.session_state.current_session_id)
            
            col_t, col_d = st.columns([0.8, 0.2])
            with col_t:
                btn_label = f"📌 {c_title}" if is_active else f"💬 {c_title}"
                if st.button(btn_label, key=f"chat_{c_id}", use_container_width=True):
                    st.session_state.current_session_id = c_id
                    st.session_state.current_title = c_title
                    st.session_state.messages = chat.get("messages", [])
                    st.rerun()
            with col_d:
                if st.button("❌", key=f"del_{c_id}", help="이 대화 삭제"):
                    delete_chat(c_id)
                    if is_active:
                        st.session_state.current_session_id = str(uuid.uuid4())
                        st.session_state.current_title = "새로운 일본 여행"
                        st.session_state.messages = [{"role": "assistant", "content": DEFAULT_GREETING}]
                    st.rerun()

    st.markdown("---")
    st.subheader("🗺️ 구글 지도 빠른 검색")
    search_place = st.text_input("궁금한 장소/식당/호텔", placeholder="예: 신주쿠역, 도톤보리, 교토 청수사")
    if search_place:
        encoded_query = urllib.parse.quote_plus(search_place.strip())
        gmap_url = f"https://www.google.com/maps?q={encoded_query}"
        st.link_button(f"🔍 '{search_place}' 구글맵 열기", gmap_url, use_container_width=True)

    st.markdown("---")
    if st.button("🔒 앱 잠금 (로그아웃)", use_container_width=True):
        st.session_state.authenticated = False
        st.rerun()

# ------------------------------------------------------------------
# 메인 채팅 화면
# ------------------------------------------------------------------
st.title(f"✈️ {st.session_state.current_title}")
st.caption("일본 여행 계획 수립, 핫페퍼 현지 맛집, 덕덕고 실시간 웹검색, 엔화 환율, 구글맵 연동 전문 어시스턴트")

# ------------------------------------------------------------------
# 상단 원클릭 맞춤 여행 플래너 (간편 조건 입력 폼)
# ------------------------------------------------------------------
is_new_session = (len(st.session_state.messages) <= 1)
with st.expander("📋 [원클릭 맞춤 플래너] 조건 입력하고 바로 계획 세우기", expanded=is_new_session):
    st.info("💡 출발 날짜, 기간, 인원, 출발지, 목적지, 예산을 선택하고 버튼을 누르면 조건에 딱 맞춘 여행 계획을 즉시 생성합니다! 계획 생성 후 아래 채팅창에서 일정 수정이나 추가 질문도 자유롭게 이어갈 수 있습니다.")
    with st.form("quick_planner_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            dep_date = st.date_input("📅 출발 날짜", value=date.today() + timedelta(days=7))
        with col2:
            duration = st.selectbox("⏱️ 여행 기간", ["2박 3일", "3박 4일", "4박 5일", "5박 6일", "1박 2일", "당일치기", "1주일 이상"], index=1)
        with col3:
            people_cnt = st.number_input("👥 여행 인원 (명)", min_value=1, max_value=20, value=2, step=1)

        col4, col5, col6 = st.columns(3)
        with col4:
            dep_city = st.selectbox("🛫 출발 지역", ["서울/인천 (인천/김포공항)", "부산 (김해공항/부산항 페리)", "대구", "청주", "제주", "기타 지역"])
        with col5:
            dest_city = st.selectbox("🗾 여행 목적지", ["도쿄 (도쿄/신주쿠/시부야/긴자)", "오사카/교토/고베 (간사이)", "후쿠오카/유후인/벳푸 (규슈)", "삿포로/오타루/비에이 (홋카이도)", "오키나와", "나고야", "기타 지역"])
        with col6:
            budget_won = st.number_input("💰 총 예산 (만원 단위)", min_value=10, max_value=3000, value=140, step=10, help="원화(KRW) 기준 전체 인원의 총 예산입니다.")

        themes = st.multiselect(
            "🎯 선호 여행 테마/취향 (복수 선택 가능)",
            ["🍜 현지 맛집/미식 탐방", "🛍️ 쇼핑/패션", "⛩️ 명소/유적지/신사", "♨️ 온천/료칸/힐링", "🏙️ 야경/전망대", "🌸 계절 명소/단풍/자연", "🎮 서브컬처/애니메이션", "💸 가성비/알뜰 여행"],
            default=["🍜 현지 맛집/미식 탐방", "🛍️ 쇼핑/패션"]
        )

        approx_jpy = round((budget_won * 10000) / (current_jpy_rate / 100)) if current_jpy_rate > 0 else 0
        per_person = round((budget_won * 10000) / people_cnt)
        st.caption(f"💴 **예산 환산**: 총 {budget_won:,}만원은 오늘 환율 기준 약 **{approx_jpy:,} JPY(엔화)** 수준이며, 1인당 약 **{per_person:,}원**입니다. (100엔 = {current_jpy_rate}원 기준)")

        form_submitted = st.form_submit_button("🚀 조건에 맞춰 원클릭 여행 계획 생성하기", type="primary", use_container_width=True)

# 이전 대화 렌더링
for msg in st.session_state.messages:
    if msg["role"] in ["user", "assistant"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# 사용자 입력 확인 (폼 제출 또는 하단 채팅창 입력)
user_prompt = None
custom_title = None

if form_submitted:
    dest_name = dest_city.split(' ')[0]
    custom_title = f"{dest_name} {duration} 여행 ({budget_won}만원)"
    user_prompt = (
        f"일본 여행 계획을 세워줘!\n\n"
        f"- 📅 출발 날짜: {dep_date.strftime('%Y년 %m월 %d일')}\n"
        f"- ⏱️ 여행 기간: {duration}\n"
        f"- 👥 여행 인원: {people_cnt}명\n"
        f"- 🛫 출발 지역: {dep_city}\n"
        f"- 🗾 여행 목적지: {dest_city}\n"
        f"- 💰 총 예산: {budget_won}만원 (1인당 약 {per_person:,}원, 약 {approx_jpy:,} JPY)\n"
        f"- 🎯 선호 테마: {', '.join(themes) if themes else '일반 추천'}\n\n"
        f"위 조건에 맞춰 최적 항공/교통편, 일자별 최적 이동 동선, 구글 지도 링크, 핫페퍼 검증 맛집, 실시간 환율 기준 예산 배분표를 체계적으로 기획해줘."
    )

if chat_prompt := st.chat_input("일본 여행에 대해 질문하거나 일정/예산을 입력하세요... (예: 2일차 식당 바꿔줘, 유용한 일본어 회화 알려줘)"):
    user_prompt = chat_prompt

if user_prompt:
    if len(st.session_state.messages) <= 1:
        if custom_title:
            st.session_state.current_title = custom_title
        else:
            clean_title = user_prompt.strip().replace("\n", " ")
            st.session_state.current_title = clean_title[:20] + ("..." if len(clean_title) > 20 else "")

    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    if not api_key:
        with st.chat_message("assistant"):
            st.error("⚠️ `.env` 파일에 `OPENAI_API_KEY`가 설정되어 있지 않습니다.")
    else:
        # 모델 대화 페이로드 구성
        conversation_history = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in st.session_state.messages:
            conversation_history.append({"role": msg["role"], "content": msg["content"]})

        with st.chat_message("assistant"):
            response_container = st.empty()
            full_response = ""
            
            try:
                if agent_mode == "👑 멀티 에이전트 (Supervisor)" and SUPERVISOR_AVAILABLE:
                    with st.status("👑 여행 전문가 팀(Supervisor)이 협업 중입니다...", expanded=True) as status_box:
                        def update_agent_status(node, msg):
                            status_box.write(f"- {msg}")
                        
                        full_response = run_travel_assistant(
                            user_input=user_prompt,
                            conversation_history=st.session_state.messages[:-1],
                            status_callback=update_agent_status
                        )
                        status_box.update(label="✅ 맞춤형 여행 계획 수립 완료!", state="complete", expanded=False)
                    
                    response_container.markdown(full_response)
                else:
                    if "gpt-6" in selected_model:
                        extra_cfg = {"reasoning_effort": "low"}
                    elif "gpt-5" in selected_model:
                        extra_cfg = {"reasoning_effort": "none"}
                    else:
                        extra_cfg = None

                    # 1단계: 모델 호출 및 도구 필요 여부 확인 (Tool Calling)
                    try:
                        first_response = client.chat.completions.create(
                            model=selected_model,
                            messages=conversation_history,
                            tools=TOOLS,
                            tool_choice="auto",
                            extra_body=extra_cfg
                        )
                        resp_msg = first_response.choices[0].message
                    except Exception as tool_err:
                        if "reasoning_effort" in str(tool_err) or "Function tools" in str(tool_err):
                            resp_msg = None
                        else:
                            raise

                    if resp_msg is None:
                        # gpt-6 계열에서 /v1/chat/completions의 function tools 제한 시 스트리밍 직접 응답
                        stream = client.chat.completions.create(
                            model=selected_model,
                            messages=conversation_history,
                            stream=True,
                            extra_body=extra_cfg
                        )
                        for chunk in stream:
                            content = chunk.choices[0].delta.content or ""
                            full_response += content
                            response_container.markdown(full_response + "▌")
                        response_container.markdown(full_response)
                    elif resp_msg.tool_calls:
                        conversation_history.append(resp_msg)
                        
                        for tool_call in resp_msg.tool_calls:
                            func_name = tool_call.function.name
                            func_args = json.loads(tool_call.function.arguments)
                            
                            tool_status_msg = "🔍 실시간 웹 검색 중..." if func_name == "search_duckduckgo_web" else "🍜 핫페퍼 일본 현지 맛집 검색 중..."
                            with st.spinner(tool_status_msg):
                                tool_result = TOOL_MAP[func_name](**func_args)
                            
                            conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": func_name,
                                "content": tool_result
                            })
                        
                        # 2단계: 도구 실행 결과를 취합하여 최종 스트리밍 응답
                        stream = client.chat.completions.create(
                            model=selected_model,
                            messages=conversation_history,
                            stream=True,
                            extra_body=extra_cfg
                        )
                        for chunk in stream:
                            content = chunk.choices[0].delta.content or ""
                            full_response += content
                            response_container.markdown(full_response + "▌")
                        response_container.markdown(full_response)
                    
                    else:
                        # 도구 호출 없이 바로 답변하는 경우
                        full_response = resp_msg.content or ""
                        response_container.markdown(full_response)
                
                # 어시스턴트 메시지 기록 저장 (가짜 링크 자동 치유)
                full_response = sanitize_maps_links(full_response)
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                save_chat(
                    session_id=st.session_state.current_session_id,
                    title=st.session_state.current_title,
                    messages=st.session_state.messages
                )
                st.rerun()
                
            except Exception as e:
                response_container.error(f"오류가 발생했습니다: {e}")

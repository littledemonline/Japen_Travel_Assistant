# 🗾 Japan Travel Assistant

OpenAI, LangGraph, Hot Pepper Gourmet API, DuckDuckGo 검색, 실시간 엔화 환율, Google Maps 링크를 연동한 **일본 여행 전문 AI 멀티 에이전트 챗봇(Streamlit)**입니다.

## 주요 기능

- **멀티 에이전트 플래닝 (Supervisor)**: 일정/교통 플래너, 일본 현지 맛집 전문가(Hot Pepper API), 실시간 웹 리서처(DuckDuckGo) 협업
- **원클릭 맞춤 플래너 & 대화형 챗봇**: 날짜, 기간, 인원, 출발지, 목적지, 예산 기반 일정 생성 및 후속 대화 수정
- **실시간 엔화 환율 반영**: `open.er-api.com` 기준 JPY ↔ KRW 자동 환산
- **Google Maps 바로가기 링크**: 추천 명소/숙소/식당 위치 검색 링크 제공
- **비밀번호 잠금**: `APP_PASSWORD` 인증으로 API Key 무단 사용 방지

## 로컬 실행 방법

1. `.env.example`을 복사해 `.env` 파일을 생성하고 값을 입력합니다.
   ```env
   APP_PASSWORD=원하는비밀번호
   OPENAI_API_KEY=your_openai_api_key_here
   OPENAI_MODEL=gpt-6-astra
   HOTPEPPER_API_KEY=your_hotpepper_api_key_here
   ```
2. 패키지를 설치하고 실행합니다.
   ```bash
   pip install -r requirements.txt
   streamlit run app.py
   ```

## Streamlit Community Cloud 배포 방법

1. 이 프로젝트 파일(`app.py`, `supervisor.py`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`)을 GitHub 저장소에 올립니다. (`.env`와 `chat_history/`는 `.gitignore`에 의해 제외됩니다.)
2. [Streamlit Community Cloud](https://share.streamlit.io)에서 저장소와 `app.py`를 선택합니다.
3. **Advanced settings → Secrets**에 아래 형식으로 환경변수를 입력하고 배포합니다.
   ```toml
   APP_PASSWORD = "원하는비밀번호"
   OPENAI_API_KEY = "your_openai_api_key_here"
   OPENAI_MODEL = "gpt-6-astra"
   HOTPEPPER_API_KEY = "your_hotpepper_api_key_here"
   ```


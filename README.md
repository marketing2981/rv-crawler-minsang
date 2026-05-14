# 스마트 리뷰 크롤러

자사몰(Cafe24 기반) URL 하나만 넣으면 리뷰를 자동으로 수집합니다.

- **Cafe24 + SNAP리뷰**: 페이지당 100건 빠른 수집
- **Cafe24 표준 게시판**: 페이지당 15건
- Crema / 일반 API 위젯도 자동 감지

## 로컬에서 돌리기 (Windows)

```powershell
pip install -r requirements.txt
python -m playwright install chromium
streamlit run app.py
```

`run_crawler.bat` 더블클릭이면 더 간단합니다.

## Streamlit Community Cloud로 배포

1. GitHub에 이 폴더를 새 리포지토리로 푸시
2. https://share.streamlit.io 에서 "New app" → 해당 리포지토리 선택 → `app.py`
3. 첫 빌드에서 `requirements.txt` + `packages.txt` 자동 적용
4. 처음 접속 시 Chromium이 자동 설치되어 다소 느릴 수 있음 (이후엔 정상 속도)

## 사용법

1. 자사몰 상품 상세 URL 입력 (홈 URL도 가능하나 상품 URL이 가장 빠름)
2. 최대 페이지 수 입력
   - SNAP리뷰: 1페이지 = 100건
   - Cafe24 표준: 1페이지 = 15건
3. "🚀 수집 시작" 클릭
4. 결과를 `.xlsx` 로 저장

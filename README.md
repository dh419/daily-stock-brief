# Daily Stock Brief

매일 장전·장후 주요 지수 및 관심 종목을 정적 HTML 리포트로 생성해 GitHub Pages로 게시하는 자동화 프로젝트입니다.

- **Pages**: https://dh419.github.io/daily-stock-brief/

## 동작 흐름
1. **데이터 수집**: GitHub Actions(`generate_stock_brief.yml`)가 오전(`07:00~08:30 KST`, 월~토)·오후(`20:00~21:30 KST`, 일·월~금) 크론 또는 수동 실행 시 Google Drive에서 세션별 JSON을 가져옵니다.
2. **HTML 생성 및 배포**: `scripts/sync_from_gdrive.py`가 `docs/YYYY-MM/`에 리포트를 생성하고, `scripts/update_index.py`가 `hub-data.json` 및 검색 차단(`robots.txt`, `noindex`)을 갱신해 Pages로 배포합니다.

## 구성
- `.github/workflows/generate_stock_brief.yml`: 스케줄 및 수동 실행 워크플로
- `scripts/sync_from_gdrive.py`: Drive JSON 수집 및 HTML 생성
- `scripts/generate_stock_brief.py`: 리포트 렌더링 및 마감 이력(`audit_history.json`) 처리
- `scripts/update_index.py`: `hub-data.json` 인덱스 및 검색 차단 자동 보정
- `templates/master_report.html`: 리포트 템플릿
- `docs/YYYY-MM/`: 일별 리포트 HTML
- `index.html`, `hub-data.json`, `audit_history.json`, `robots.txt`: 허브 및 정적 리소스

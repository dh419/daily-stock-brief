# Daily Stock Brief

매일 오전(장전)·오후(장후) 코스피, 삼성전자, SK하이닉스 등 주요 지표와 관심 종목을 분석해 정적 HTML 리포트로 생성하고 GitHub Pages로 게시하는 자동화 프로젝트입니다.

## 주요 기능
- **장전/장후 브리핑**: 6대 핵심 지표 도넛 차트, 관심 종목 카드 필터(`전체` / `상승` / `하락`) 및 상세 모달
- **누적 적중 추이**: 최근 1개월(22거래일) SVG 타임라인 차트 및 날짜별 클릭 상세 조회
- **누적 마감 로그**: `audit_history.json` 기반 전체 이력 누적, 검색·결과(`⭕`/`❌`) 필터·페이지네이션(25/50/100행) 지원
- **단일 HTML 셸**: `localStorage` 기반 무깜빡임 테마 복원 및 반응형 테이블 래퍼 적용

## 디렉터리 구조
- `scripts/generate_stock_brief.py`: 데이터 수집, 차트 렌더링, HTML 리포트 생성 스크립트
- `templates/master_report.html`: 리포트 마스터 HTML 템플릿
- `docs/YYYY-MM/`: 생성된 일별 오전·오후 리포트 HTML (`GitHub Pages` 배포 대상)

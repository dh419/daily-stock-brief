import os
import re
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')
TEMPLATES_DIR = os.path.join(REPO_ROOT, 'templates')
MASTER_HTML_PATH = os.path.join(TEMPLATES_DIR, 'master_report.html')
AUDIT_HISTORY_PATH = os.path.join(REPO_ROOT, 'audit_history.json')

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-4-hydrogen')
THINKING_BUDGET = int(os.environ.get('THINKING_BUDGET', '2048'))

KST = timezone(timedelta(hours=9))

def get_session_prompt(session, now_kst):
    date_str = now_kst.strftime('%Y-%m-%d')
    weekday_ko = ['월', '화', '수', '목', '금', '토', '일'][now_kst.weekday()]
    
    # Load recent audit history if exists
    audit_context = ""
    if os.path.exists(AUDIT_HISTORY_PATH):
        with open(AUDIT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            history = json.load(f)
            audit_context = f"\n[최근 누적 예측 vs 실제 이진 판정 기록 (최근 {len(history)}일)]\n" + json.dumps(history[-25:], ensure_ascii=False)

    if session == 'morning':
        session_desc = f"오전 모닝 브리핑 (매일 07:30 KST) - {date_str} ({weekday_ko})"
        task_focus = """
1. 간밤 마감된 미국 정규장(다우, S&P 500, 나스닥, 필라델피아 반도체 SOX, EWY), 미 국채 10년물 금리, WTI 국제 유가, 역외 NDF 원/달러 환율, 코스피200 야간 선물의 실시간 최신 수치를 검색하세요.
2. 글로벌 매크로, 미국 AI/반도체 펀더멘털, 국내 수급/이벤트, 외환 4개 축을 기준으로 **6대 다이나믹 액티브 베이스 지표**를 선정하고 가중치(총합 100%)와 방향성 시그널(상방/중립/하방)을 부여하세요.
3. 당일 한국 증시(코스피, 코스닥) 및 핵심 대장주(삼성전자, SK하이닉스)의 **시초가 갭(%)**과 **장중 예상 등락률(%) 범위**를 산출하세요.
4. 국내외 13개 핵심 관심 종목(삼성전자, SK하이닉스, SKHY, 엔비디아, 테슬라, 알파벳A, 메타, 아마존, 마이크론, 팔란티어, GE버노바, 블룸에너지, 웨스턴디지털)의 최신 등락률과 핵심 이슈를 전수 수집하세요.
"""
    else:
        session_desc = f"오후 마감 분석 리포트 (매일 18:00 KST) - {date_str} ({weekday_ko})"
        task_focus = """
1. 당일 마감된 국내 증시(코스피, 코스닥 종가 및 등락률), 외국인/기관 순매수 금액, 서울 외환시장 원/달러 환율 종가, 삼성전자·SK하이닉스 마감 종가 및 등락률, 유럽 증시 및 미국 프리마켓 동향을 실시간 검색하세요.
2. 오늘 오전 브리핑의 코스피·삼성전자·SK하이닉스 예측 밴드와 실제 종가를 대조하여 **엄격한 이진 판정(⭕ 적중 또는 ❌ 실패, 부분적중 없음)**을 내리고, 오차 유발 시 지표를 퇴출/신규 편입하는 피처 감사를 수행하세요.
3. 다음 거래일 개장을 대비한 6대 액티브 베이스 지표 세트와 내일 장 전망을 제시하세요.
4. 국내외 13개 핵심 관심 종목의 최신 마감/프리마켓 동향을 수집하세요.
"""

    prompt = f"""
당신은 한국 및 미국 증시, 매크로 경제, AI/반도체 밸류체인을 전담하는 최고 수준의 정량 투자 애널리스트입니다.
지금은 {session_desc} 시점입니다.

실시간 구글 검색을 활용하여 최신 시장 데이터와 외신·국내 뉴스를 수집하고, 반드시 아래 JSON 스키마 규격으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만 출력):

{task_focus}
{audit_context}

반드시 준수할 JSON 출력 스키마:
{{
  "reportTitle": "미국 및 한국 증시 {'모닝 브리핑' if session == 'morning' else '마감 분석 리포트'} ({now_kst.strftime('%Y년 %m월 %d일')} {weekday_ko}요일)",
  "executiveSummary": "핵심 지수 마감/예측 요약 및 당일 핵심 관전 포인트 2~3줄 요약",
  "macroCards": [
    {{ "title": "다우존스 / S&P500 / 나스닥 / SOX / 美10년물 / 원·달러 등 6개", "value": "현재 수치", "change": "+0.54% 또는 -1.20%", "direction": "up 또는 down 또는 neutral" }}
  ],
  "targets": [
    {{
      "name": "코스피 / 삼성전자 / SK하이닉스 (반드시 이 3개 순서대로)",
      "code": "KOSPI / 005930 / 000660",
      "gap": "시초가 -0.4% ~ -1.0%",
      "band": "-0.8% ~ +0.6%",
      "desc": "예상 지수/주가 밴드 및 지지·저항 핵심 코멘트"
    }}
  ],
  "indicators": [
    {{
      "name": "지표명 (6개)",
      "detail": "최신 확정 수치 및 상태 요약 (원문 출처 URL 포함 가능)",
      "weight": "20%",
      "signalText": "상방 / 하방 / 중립 + 짧은 사유",
      "signalDir": "up 또는 down 또는 neutral"
    }}
  ],
  "stocks": [
    {{
      "name": "종목명 (예: 삼성전자 (005930))",
      "price": "현재가/종가 (예: 208,000원)",
      "change": "+0.97%",
      "direction": "up 또는 down 또는 neutral",
      "summary": "핵심 이슈 및 수급 코멘트 2~3줄",
      "url": "관련 뉴스 또는 시세 출처 URL"
    }}
  ],
  "todayAudit": {{
    "date": "{now_kst.strftime('%m.%d')} ({weekday_ko})",
    "kospi": "예측 / 실제",
    "samsung": "예측 / 실제",
    "skhynix": "예측 / 실제",
    "result": "⭕ 또는 ❌ (오전이면 '대기')",
    "reason": "결과 및 오차 사유"
  }}
}}
"""
    return prompt

def render_html(data, session, now_kst):
    with open(MASTER_HTML_PATH, 'r', encoding='utf-8') as f:
        tpl = f.read()

    # Macro Cards
    macro_html = ""
    for m in data.get('macroCards', []):
        cls = "chg-up" if m['direction'] == 'up' else ("chg-down" if m['direction'] == 'down' else "chg-neutral")
        macro_html += f"""
      <div class="macro-card">
        <div class="macro-title">{m['title']}</div>
        <div class="macro-val">{m['value']}</div>
        <div class="macro-chg {cls}">{m['change']}</div>
      </div>"""

    # Target Cards
    target_html = ""
    for t in data.get('targets', []):
        target_html += f"""
      <div class="target-card">
        <div class="target-header">
          <div class="target-name">{t['name']}<span class="target-code">{t['code']}</span></div>
          <span class="target-gap" style="background:rgba(99,102,241,0.18); color:#a5b4fc;">{t['gap']}</span>
        </div>
        <div class="band-meter">
          <div class="band-fill"></div>
        </div>
        <div class="band-labels">
          <span>예상 밴드: <strong style="color:#fff;">{t['band']}</strong></span>
        </div>
        <div class="target-desc">{t['desc']}</div>
      </div>"""

    # Indicators Rows
    ind_html = ""
    for ind in data.get('indicators', []):
        sig_cls = "sig-up" if ind['signalDir'] == 'up' else ("sig-down" if ind['signalDir'] == 'down' else "sig-neutral")
        ind_html += f"""
          <tr>
            <td style="font-weight:700; color:#fff;">{ind['name']}</td>
            <td style="color:#cbd5e1;">{ind['detail']}</td>
            <td><span class="weight-pill">{ind['weight']}</span></td>
            <td><span class="sig-badge {sig_cls}">{ind['signalText']}</span></td>
          </tr>"""

    # Stocks Cards
    stocks_html = ""
    for s in data.get('stocks', []):
        chg_cls = "chg-up" if s['direction'] == 'up' else ("chg-down" if s['direction'] == 'down' else "chg-neutral")
        safe_name = s['name'].replace("'", "\\'")
        safe_price = f"{s['price']} ({s['change']})".replace("'", "\\'")
        safe_desc = s['summary'].replace("'", "\\'").replace('"', '&quot;')
        safe_url = s.get('url', '').replace("'", "\\'")
        stocks_html += f"""
      <div class="stock-card" onclick="openStockModal('{safe_name}', '{safe_price}', '{safe_desc}', '{safe_url}')">
        <div class="stock-header">
          <span class="stock-name">{s['name']}</span>
          <span class="stock-chg {chg_cls}">{s['change']}</span>
        </div>
        <div class="stock-sub">{s['price']}</div>
        <div class="stock-snippet">{s['summary']}</div>
      </div>"""

    # Audit History Table
    history = []
    if os.path.exists(AUDIT_HISTORY_PATH):
        with open(AUDIT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            history = json.load(f)

    if session == 'evening' and data.get('todayAudit') and data['todayAudit'].get('result') in ('⭕', '❌'):
        today_entry = data['todayAudit']
        # Replace if same date exists, else append
        history = [h for h in history if h['date'] != today_entry['date']]
        history.append(today_entry)
        history = history[-30:] # keep last 30
        with open(AUDIT_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    total_cnt = len(history)
    win_cnt = sum(1 for h in history if h.get('result') == '⭕')
    loss_cnt = total_cnt - win_cnt
    win_rate = f"{(win_cnt / total_cnt * 100):.1f}" if total_cnt > 0 else "0.0"

    audit_html = ""
    for h in reversed(history):
        audit_html += f"""
          <tr>
            <td style="font-weight:700; white-space:nowrap;">{h['date']}</td>
            <td>{h['kospi']}</td>
            <td>{h['samsung']}</td>
            <td>{h['skhynix']}</td>
            <td style="text-align:center;" class="eval-circle">{h['result']}</td>
            <td style="color:#94a3b8; font-size:0.82rem;">{h['reason']}</td>
          </tr>"""

    session_class = "morning" if session == 'morning' else "evening"
    session_badge = "☀️ 오전 모닝 브리핑 (07:30)" if session == 'morning' else "🌙 오후 마감 분석 (18:00)"

    html = tpl.replace('{{REPORT_TITLE}}', data.get('reportTitle', ''))
    html = html.replace('{{SESSION_CLASS}}', session_class)
    html = html.replace('{{SESSION_BADGE}}', session_badge)
    html = html.replace('{{REPORT_TIMESTAMP}}', now_kst.strftime('%Y-%m-%d %H:%M'))
    html = html.replace('{{EXECUTIVE_SUMMARY}}', data.get('executiveSummary', ''))
    html = html.replace('{{MACRO_CARDS_HTML}}', macro_html)
    html = html.replace('{{TARGET_CARDS_HTML}}', target_html)
    html = html.replace('{{INDICATORS_ROWS_HTML}}', ind_html)
    html = html.replace('{{STOCKS_CARDS_HTML}}', stocks_html)
    html = html.replace('{{AUDIT_TOTAL_COUNT}}', str(total_cnt))
    html = html.replace('{{AUDIT_WIN_COUNT}}', str(win_cnt))
    html = html.replace('{{AUDIT_LOSS_COUNT}}', str(loss_cnt))
    html = html.replace('{{AUDIT_WIN_RATE}}', win_rate)
    html = html.replace('{{AUDIT_ROWS_HTML}}', audit_html)

    return html

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=['morning', 'evening'], default='morning')
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")

    now_kst = datetime.now(KST)
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = get_session_prompt(args.session, now_kst)

    candidate_models = [
        GEMINI_MODEL,
        'gemini-4-hydrogen',
        'gemini-3.8-flash',
        'gemini-3.5-flash-lite',
        'gemini-3.5-flash'
    ]
    seen = set()
    models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]

    response = None
    last_err = None
    for model_name in models_to_try:
        try:
            print(f"Trying Gemini model '{model_name}'...")
            config_args = {
                'tools': [types.Tool(google_search=types.GoogleSearch())],
                'temperature': 0.2,
            }
            if THINKING_BUDGET > 0:
                config_args['thinking_config'] = types.ThinkingConfig(thinking_budget=THINKING_BUDGET)

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(**config_args),
            )
            print(f"Success with model: {model_name}")
            break
        except Exception as e:
            print(f"Model '{model_name}' failed: {e}")
            last_err = e
            time.sleep(3)

    if not response:
        raise RuntimeError(f"All model attempts failed: {last_err}")

    raw_text = response.text.strip()
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
    raw_text = re.sub(r'\s*```$', '', raw_text)
    data = json.loads(raw_text)

    html = render_html(data, args.session, now_kst)
    
    month_dir = os.path.join(DOCS_DIR, now_kst.strftime('%Y-%m'))
    os.makedirs(month_dir, exist_ok=True)
    out_file = os.path.join(month_dir, f"{now_kst.strftime('%Y-%m-%d')}-{args.session}.html")
    
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Successfully generated stock brief: {out_file}")

if __name__ == '__main__':
    main()

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

def parse_percent(val_str):
    if not val_str:
        return None
    m = re.search(r'([+-]?\d+(?:\.\d+)?)%', str(val_str))
    if m:
        return float(m.group(1))
    return None

def parse_range(range_str):
    if not range_str:
        return None, None
    matches = re.findall(r'([+-]?\d+(?:\.\d+)?)%', str(range_str))
    if len(matches) >= 2:
        return float(matches[0]), float(matches[1])
    elif len(matches) == 1:
        v = float(matches[0])
        return v, v
    return None, None

def render_band_meter(t, session):
    # Scale: -5.0% to +5.0% (center 0% at 50%)
    # clamp helper
    def to_percent_pos(val, min_v=-5.0, max_v=5.0):
        if val is None:
            return None
        clamped = max(min_v, min(max_v, val))
        return ((clamped - min_v) / (max_v - min_v)) * 100.0

    low_v, high_v = parse_range(t.get('band', ''))
    if low_v is None:
        low_v, high_v = parse_range(t.get('gap', ''))

    actual_v = parse_percent(t.get('band', '')) if session == 'evening' else None
    
    # Calculate positions
    band_html = ""
    if low_v is not None and high_v is not None:
        p_low = to_percent_pos(min(low_v, high_v))
        p_high = to_percent_pos(max(low_v, high_v))
        width = max(2.0, p_high - p_low)
        band_html = f'<div class="meter-band" style="left:{p_low:.1f}%; width:{width:.1f}%;"></div>'

    actual_html = ""
    is_hit = False
    if actual_v is not None:
        p_act = to_percent_pos(actual_v)
        if low_v is not None and high_v is not None and (low_v <= actual_v <= high_v):
            is_hit = True
        pin_class = "hit" if is_hit else "miss"
        actual_html = f'<div class="meter-actual-pin {pin_class}" style="left:{p_act:.1f}%;" title="실제 종가: {actual_v:+.2f}%"></div>'

    return f"""
      <div class="band-meter-wrap">
        <div class="meter-scale-header">
          <span>-5.0% (하방)</span>
          <span style="color:#e2e8f0; font-weight:800;">0.0% (기준)</span>
          <span>+5.0% (상방)</span>
        </div>
        <div class="meter-track">
          <div class="axis-zero"></div>
          <div class="axis-zero-label">0%</div>
          {band_html}
          {actual_html}
        </div>
        <div class="meter-legend">
          <span style="color:#a5b4fc;">예상 밴드: {t.get('band', '-')}</span>
          {f'<span style="color:{"#10b981" if is_hit else "#f87171"}; font-weight:800;">실제: {actual_v:+.2f}% ({"⭕ 적중" if is_hit else "❌ 이탈"})</span>' if actual_v is not None else '<span style="color:#94a3b8;">장중 진행 대기</span>'}
        </div>
      </div>
    """

def render_timeline_svg(history):
    # Render interactive/clean SVG timeline of binary results
    if not history:
        return '<div style="color:var(--text-dim); text-align:center; padding:20px;">누적 기록이 없습니다.</div>'
    
    # Show last 25 entries in chronological order (left to right)
    items = history[-25:]
    total = len(items)
    
    svg_w = 900
    svg_h = 100
    margin_x = 30
    step_x = (svg_w - margin_x * 2) / max(1, total - 1) if total > 1 else 0
    y_center = 45

    nodes_html = []
    lines_html = []
    
    # baseline
    lines_html.append(f'<line x1="{margin_x}" y1="{y_center}" x2="{svg_w - margin_x}" y2="{y_center}" stroke="#334155" stroke-width="2" stroke-dasharray="4 4" />')
    
    for idx, h in enumerate(items):
        cx = margin_x + idx * step_x
        is_hit = h.get('result') == '⭕'
        color = "#10b981" if is_hit else "#ef4444"
        cy = y_center - 16 if is_hit else y_center + 16
        
        # Line to center
        lines_html.append(f'<line x1="{cx:.1f}" y1="{y_center}" x2="{cx:.1f}" y2="{cy:.1f}" stroke="{color}" stroke-width="2" opacity="0.6" />')
        
        # Circle Node
        symbol = "⭕" if is_hit else "❌"
        date_short = h.get('date', '').split(' ')[0]
        nodes_html.append(f"""
          <g transform="translate({cx:.1f}, {cy:.1f})">
            <circle r="12" fill="#111827" stroke="{color}" stroke-width="2.5" />
            <text text-anchor="middle" dy="4" font-size="10" font-weight="bold" fill="{color}">{symbol}</text>
            <text text-anchor="middle" dy="{28 if is_hit else -20}" font-size="9" fill="#94a3b8" font-family="monospace">{date_short}</text>
          </g>
        """)

    svg_content = f"""
      <svg class="timeline-svg" viewBox="0 0 {svg_w} {svg_h}" preserveAspectRatio="xMidYMid meet">
        {''.join(lines_html)}
        {''.join(nodes_html)}
      </svg>
    """
    return svg_content

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

    # Target Cards with explicit 0% Axis Meter
    target_html = ""
    for t in data.get('targets', []):
        meter_html = render_band_meter(t, session)
        target_html += f"""
      <div class="target-card">
        <div class="target-header">
          <div class="target-name">{t['name']}<span class="target-code">{t['code']}</span></div>
          <span class="target-gap" style="background:rgba(99,102,241,0.18); color:#a5b4fc;">{t['gap']}</span>
        </div>
        {meter_html}
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

    # Audit History
    history = []
    if os.path.exists(AUDIT_HISTORY_PATH):
        with open(AUDIT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            history = json.load(f)

    if session == 'evening' and data.get('todayAudit') and data['todayAudit'].get('result') in ('⭕', '❌'):
        today_entry = data['todayAudit']
        history = [h for h in history if h['date'] != today_entry['date']]
        history.append(today_entry)
        history = history[-30:] # keep last 30
        with open(AUDIT_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    total_cnt = len(history)
    win_cnt = sum(1 for h in history if h.get('result') == '⭕')
    loss_cnt = total_cnt - win_cnt
    win_rate = f"{(win_cnt / total_cnt * 100):.1f}" if total_cnt > 0 else "0.0"

    chart_svg = render_timeline_svg(history)

    # Raw Data Table rows for popup modal
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
    html = html.replace('{{AUDIT_CHART_SVG}}', chart_svg)
    html = html.replace('{{AUDIT_ROWS_HTML}}', audit_html)

    return html

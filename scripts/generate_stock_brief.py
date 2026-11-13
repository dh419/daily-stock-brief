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

def extract_range_from_str(s):
    """Extract (low, high) percentages from strings like '-0.8% ~ +0.6%' or '+1.2%~+2.8%'"""
    if not s:
        return None, None
    matches = re.findall(r'([+-]?\d+(?:\.\d+)?)%', str(s))
    if len(matches) >= 2:
        v1, v2 = float(matches[0]), float(matches[1])
        return min(v1, v2), max(v1, v2)
    return None, None

def extract_single_pct(s):
    """Extract single percentage like '-0.98%' or '-3%대' -> -3.0"""
    if not s:
        return None
    m = re.search(r'([+-]?\d+(?:\.\d+)?)%', str(s))
    if m:
        return float(m.group(1))
    return None

def render_band_meter(t, session):
    """
    Renders the horizontal target band meter with a fixed 0.0% center line.
    Dynamically scales to [-12%, +12%] if values exceed +/-5%, otherwise [-5%, +5%].
    """
    if session == 'morning':
        low_v, high_v = extract_range_from_str(t.get('band', ''))
        actual_v = None
        pred_label = t.get('band', '-')
    else:
        low_v, high_v = extract_range_from_str(t.get('gap', ''))
        actual_v = extract_single_pct(t.get('band', ''))
        pred_label = t.get('gap', '-').replace('오전예측:', '').strip()

    # Dynamic scale check
    max_abs = 5.0
    for v in (low_v, high_v, actual_v):
        if v is not None and abs(v) > 5.0:
            max_abs = 12.0
            break

    def to_pct_pos(val):
        if val is None:
            return None
        clamped = max(-max_abs, min(max_abs, val))
        return ((clamped + max_abs) / (2.0 * max_abs)) * 100.0

    band_html = ""
    if low_v is not None and high_v is not None:
        p_low = to_pct_pos(low_v)
        p_high = to_pct_pos(high_v)
        width = max(2.0, p_high - p_low)
        band_html = f'<div class="meter-band" style="left:{p_low:.1f}%; width:{width:.1f}%;"></div>'

    actual_html = ""
    is_hit = False
    if actual_v is not None:
        p_act = to_pct_pos(actual_v)
        if low_v is not None and high_v is not None and (low_v <= actual_v <= high_v):
            is_hit = True
        pin_class = "hit" if is_hit else "miss"
        actual_html = f'<div class="meter-actual-pin {pin_class}" style="left:{p_act:.1f}%;" title="실제: {actual_v:+.2f}%"></div>'

    status_right = '<span style="color:#94a3b8;">장중 대기</span>'
    if actual_v is not None:
        status_right = f'<span style="color:{"#10b981" if is_hit else "#f87171"}; font-weight:800;">실제: {actual_v:+.2f}% ({"⭕" if is_hit else "❌"})</span>'

    return f"""
      <div class="band-meter-wrap">
        <div class="meter-scale-header">
          <span>-{max_abs:.0f}%</span>
          <span style="color:#e2e8f0; font-weight:800;">0.0%</span>
          <span>+{max_abs:.0f}%</span>
        </div>
        <div class="meter-track">
          <div class="axis-zero"></div>
          {band_html}
          {actual_html}
        </div>
        <div class="meter-legend">
          <span style="color:#a5b4fc;">예측: {pred_label}</span>
          {status_right}
        </div>
      </div>
    """

def render_quant_section_chart(history, field_key, title, color_accent):
    """
    Renders a quantitative SVG range-bar + actual-dot chart for a single asset (kospi / samsung / skhynix).
    Shows full 1 month (up to 30 trading days) with compact bar spacing, sparse X-axis labels, and interactive hover/click tooltip.
    """
    items = history[-30:]
    if not items:
        return ""

    svg_w = 880
    svg_h = 155
    pad_l, pad_r = 44, 16
    pad_t, pad_b = 22, 24
    plot_w = svg_w - pad_l - pad_r
    plot_h = svg_h - pad_t - pad_b

    # Determine dynamic Y scale covering all data (at least -4% to +4%)
    min_y, max_y = -4.0, 4.0
    parsed_pts = []
    for h in items:
        raw = h.get(field_key, '')
        parts = raw.split('/')
        pred_str = parts[0].strip() if len(parts) > 0 else ''
        act_str = parts[1].strip() if len(parts) > 1 else ''
        p_low, p_high = extract_range_from_str(pred_str)
        act_val = extract_single_pct(act_str)
        
        for v in (p_low, p_high, act_val):
            if v is not None:
                min_y = min(min_y, v - 0.8)
                max_y = max(max_y, v + 0.8)
        parsed_pts.append((h.get('date', '').split(' ')[0], p_low, p_high, act_val, h.get('date', ''), pred_str, act_str))

    min_y = max(-11.5, min_y)
    max_y = min(8.5, max_y)

    def y_pos(val):
        clamped = max(min_y, min(max_y, val))
        ratio = (clamped - min_y) / (max_y - min_y)
        return pad_t + plot_h * (1.0 - ratio)

    step_x = plot_w / max(1, len(parsed_pts))

    # Grid lines (-5%, 0%, +5%)
    grid_html = []
    for gv in [-5.0, 0.0, 5.0]:
        if min_y <= gv <= max_y:
            gy = y_pos(gv)
            stroke = "#94a3b8" if gv == 0.0 else "#1e293b"
            width = "1.5" if gv == 0.0 else "1"
            dash = "" if gv == 0.0 else 'stroke-dasharray="3 3"'
            label = "0.0%" if gv == 0.0 else f"{gv:+.0f}%"
            lbl_col = "#f8fafc" if gv == 0.0 else "#64748b"
            grid_html.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{svg_w - pad_r}" y2="{gy:.1f}" stroke="{stroke}" stroke-width="{width}" {dash} />')
            grid_html.append(f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" font-size="10" fill="{lbl_col}" font-family="monospace" font-weight="bold">{label}</text>')

    bars_html = []
    trend_pts = []
    total_pts = len(parsed_pts)
    for idx, (d_lbl, p_low, p_high, act_val, full_date, pred_str, act_str) in enumerate(parsed_pts):
        cx = pad_l + step_x * (idx + 0.5)
        if act_val is not None:
            ay = y_pos(act_val)
            trend_pts.append(f"{cx:.1f},{ay:.1f}")
        
        is_hit = (act_val is not None and p_low is not None and p_high is not None and p_low <= act_val <= p_high)
        dot_col = "#10b981" if is_hit else "#ef4444"
        tooltip_txt = f"{full_date} | 예측: {pred_str} / 실제: {act_str} ({'⭕ 적중' if is_hit else '❌ 이탈'})"
        safe_tip = tooltip_txt.replace("'", "\\'")

        # Group with interactive click/hover
        bars_html.append(f'<g style="cursor:pointer;" onclick="showChartTip(this, \'{safe_tip}\')">')
        bars_html.append(f'<title>{tooltip_txt}</title>')
        # Invisible hit area with highlight class for interactive touch
        bars_html.append(f'<rect class="hl-col" x="{cx - step_x/2:.1f}" y="{pad_t}" width="{step_x:.1f}" height="{plot_h}" fill="transparent" rx="2" />')
        
        # Draw Predicted Band Bar (compact width: 10px for 1 month)
        if p_low is not None and p_high is not None:
            y_top = y_pos(p_high)
            y_bot = y_pos(p_low)
            bar_h = max(3.0, y_bot - y_top)
            bars_html.append(f'<rect x="{cx - 5:.1f}" y="{y_top:.1f}" width="10" height="{bar_h:.1f}" rx="2" fill="{color_accent}" opacity="0.38" stroke="{color_accent}" stroke-width="1" />')

        # Draw Actual Close Dot
        if act_val is not None:
            ay = y_pos(act_val)
            bars_html.append(f'<circle cx="{cx:.1f}" cy="{ay:.1f}" r="3.8" fill="{dot_col}" stroke="#0b0f19" stroke-width="1.2" />')

        # Sparse X-axis date labels (every 5th item + first & last)
        if idx == 0 or idx == total_pts - 1 or idx % 5 == 0:
            bars_html.append(f'<text x="{cx:.1f}" y="{svg_h - 6}" text-anchor="middle" font-size="9" fill="#94a3b8" font-family="monospace">{d_lbl}</text>')
        
        bars_html.append('</g>')

    trend_line_html = ""
    if len(trend_pts) > 1:
        trend_line_html = f'<polyline fill="none" stroke="#ffffff" stroke-opacity="0.35" stroke-width="1.5" points="{" ".join(trend_pts)}" />'

    return f"""
      <div style="margin-bottom:14px; background:{color_accent}22; border:1px solid {color_accent}; border-radius:12px; padding:10px 14px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
          <span style="font-size:0.9rem; font-weight:800; color:#ffffff;">{title}</span>
          <span class="chart-tip-bar" style="font-size:0.74rem; color:#e2e8f0; font-weight:600;"></span>
        </div>
        <svg viewBox="0 0 {svg_w} {svg_h}" style="width:100%; height:auto; display:block;" preserveAspectRatio="xMidYMid meet">
          {''.join(grid_html)}
          {trend_line_html}
          {''.join(bars_html)}
        </svg>
      </div>
    """

def render_timeline_svg(history):
    if not history:
        return '<div style="color:var(--text-dim); text-align:center; padding:20px;">누적 기록이 없습니다.</div>'
    
    c1 = render_quant_section_chart(history, 'kospi', '코스피 (KOSPI)', '#10b981')
    c2 = render_quant_section_chart(history, 'samsung', '삼성전자 (005930)', '#2563eb')
    c3 = render_quant_section_chart(history, 'skhynix', 'SK하이닉스 (000660)', '#EA002C')
    return c1 + c2 + c3

def render_html(data, session, now_kst):
    with open(MASTER_HTML_PATH, 'r', encoding='utf-8') as f:
        tpl = f.read()

    # Macro Cards (with semantic color check for Rates/Oil/FX)
    macro_html = ""
    for m in data.get('macroCards', []):
        title_str = m['title']
        is_macro_pressure = any(k in title_str for k in ['금리', '유가', '환율', 'WTI'])
        if is_macro_pressure and m['direction'] == 'up':
            cls = "chg-neutral"
            extra_style = "color:#f59e0b;" # Amber warning for rate/oil/FX spike
        else:
            cls = "chg-up" if m['direction'] == 'up' else ("chg-down" if m['direction'] == 'down' else "chg-neutral")
            extra_style = ""
        macro_html += f"""
      <div class="macro-card">
        <div class="macro-title">{m['title']}</div>
        <div class="macro-val">{m['value']}</div>
        <div class="macro-chg {cls}" style="{extra_style}">{m['change']}</div>
      </div>"""

    # Target Cards with explicit 0% Axis Meter & Signature Colors
    target_html = ""
    for t in data.get('targets', []):
        meter_html = render_band_meter(t, session)
        t_col = '#10b981' # KOSPI Emerald
        if '삼성전자' in t['name'] or '005930' in t['code']:
            t_col = '#2563eb' # Samsung Blue
        elif 'SK하이닉스' in t['name'] or '000660' in t['code']:
            t_col = '#EA002C' # SK Red

        target_html += f"""
      <div class="target-card" style="background: {t_col}24; border: 1px solid {t_col};">
        <div class="target-header">
          <div class="target-name" style="color:#ffffff;">{t['name']}<span class="target-code" style="color:#cbd5e1;">{t['code']}</span></div>
          <span class="target-gap" style="background:{t_col}40; color:#ffffff; border:1px solid {t_col}80;">{t['gap']}</span>
        </div>
        {meter_html}
        <div class="target-desc" style="color:#e2e8f0;">{t['desc']}</div>
      </div>"""

    # Indicators Donut (Pie) Chart + Linked Legend (Zero text overflow on mobile!)
    import math
    ind_slices = []
    ind_legends = []
    up_weight = 0
    down_weight = 0
    total_w = 0
    active_inds = []
    for idx, ind in enumerate(data.get('indicators', []), start=1):
        w_match = re.search(r'(\d+)', str(ind.get('weight', '0')))
        w_val = int(w_match.group(1)) if w_match else 0
        if w_val <= 0:
            continue
        total_w += w_val
        s_dir = ind.get('signalDir', 'neutral')
        if s_dir == 'up':
            up_weight += w_val
            seg_col = '#ef4444'
        elif s_dir == 'down':
            down_weight += w_val
            seg_col = '#3b82f6'
        else:
            seg_col = '#64748b'
        active_inds.append((idx, ind, w_val, seg_col))

    if active_inds and total_w > 0:
        cx, cy, r_out, r_in = 60, 60, 52, 30
        start_deg = -90.0
        for idx, ind, w_val, seg_col in active_inds:
            sweep = (w_val / total_w) * 360.0
            end_deg = start_deg + sweep
            rad1 = math.radians(start_deg)
            rad2 = math.radians(end_deg)
            x1_o, y1_o = cx + r_out * math.cos(rad1), cy + r_out * math.sin(rad1)
            x2_o, y2_o = cx + r_out * math.cos(rad2), cy + r_out * math.sin(rad2)
            x1_i, y1_i = cx + r_in * math.cos(rad2), cy + r_in * math.sin(rad2)
            x2_i, y2_i = cx + r_in * math.cos(rad1), cy + r_in * math.sin(rad1)
            large_arc = 1 if sweep > 180 else 0
            d_path = f"M {x1_o:.1f} {y1_o:.1f} A {r_out} {r_out} 0 {large_arc} 1 {x2_o:.1f} {y2_o:.1f} L {x1_i:.1f} {y1_i:.1f} A {r_in} {r_in} 0 {large_arc} 0 {x2_i:.1f} {y2_i:.1f} Z"
            short_kw = re.sub(r'^\d+\.\s*\[.*?\]\s*', '', ind['name'])[:10]
            pop_txt = f"{re.sub(r'^\d+\.\s*', '', ind['name'])} ({w_val}% · {ind['signalText']})"
            safe_pop = pop_txt.replace("'", "\\'")
            
            ind_slices.append(f'<path d="{d_path}" fill="{seg_col}" stroke="#0b0f19" stroke-width="1.5" style="cursor:pointer;" onclick="showDonutTip(\'{safe_pop}\')" />')
            
            # Percent inside slice
            mid_rad = math.radians(start_deg + sweep / 2.0)
            lx = cx + ((r_out + r_in) / 2.0) * math.cos(mid_rad)
            ly = cy + ((r_out + r_in) / 2.0) * math.sin(mid_rad)
            ind_slices.append(f'<text x="{lx:.1f}" y="{ly + 3:.1f}" text-anchor="middle" font-size="8.5" font-weight="bold" fill="#fff" style="pointer-events:none;">{w_val}%</text>')
            start_deg = end_deg

            ind_legends.append(f'''
              <div onclick="showDonutTip('{safe_pop}')" style="cursor:pointer; display:flex; align-items:center; justify-content:space-between; gap:6px; font-size:0.78rem; padding:3px 6px; border-radius:6px; background:#0f172a;">
                <div style="display:flex; align-items:center; gap:6px; overflow:hidden;">
                  <span style="display:inline-block; width:8px; height:8px; border-radius:2px; background:{seg_col}; flex-shrink:0;"></span>
                  <span style="color:#e2e8f0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{short_kw}</span>
                </div>
                <span style="color:#fff; font-weight:800; flex-shrink:0;">{w_val}%</span>
              </div>
            ''')

    ind_bar_html = f"""
    <div style="background:var(--card-bg); border:1px solid var(--card-border); border-radius:12px; padding:12px 14px; margin-bottom:10px;">
      <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
        <div style="position:relative; width:120px; height:120px; flex-shrink:0; margin:0 auto;">
          <svg viewBox="0 0 120 120" style="width:100%; height:100%;">
            {''.join(ind_slices)}
            <text x="60" y="57" text-anchor="middle" font-size="9" font-weight="bold" fill="#f87171">▲ {up_weight}%</text>
            <text x="60" y="70" text-anchor="middle" font-size="9" font-weight="bold" fill="#60a5fa">▼ {down_weight}%</text>
          </svg>
        </div>
        <div style="flex:1; min-width:190px; display:flex; flex-direction:column; gap:4px;">
          {''.join(ind_legends)}
        </div>
      </div>
      <div id="donutTipBar" style="margin-top:8px; font-size:0.78rem; font-weight:700; color:#fbbf24; display:none; padding:6px 10px; background:#0f172a; border-radius:6px; border:1px solid #334155;"></div>
    </div>
    """ if ind_slices else ""

    # Indicators Mobile-friendly Cards with Audit Status Color Coding & #index Link
    ind_html = ""
    for idx, ind in enumerate(data.get('indicators', []), start=1):
        sig_cls = "sig-up" if ind['signalDir'] == 'up' else ("sig-down" if ind['signalDir'] == 'down' else "sig-neutral")
        name_str = ind['name']
        
        card_style = "background:var(--card-bg); border:1px solid var(--card-border);"
        title_extra = ""
        if '[신규]' in name_str:
            card_style = "background:rgba(168, 85, 247, 0.12); border:1px solid #a855f7;"
        elif '[유지]' in name_str:
            card_style = "background:rgba(245, 158, 11, 0.08); border:1px solid rgba(245, 158, 11, 0.55);"
        elif '[퇴출]' in name_str:
            card_style = "background:rgba(30, 41, 59, 0.4); border:1px dashed #475569; opacity:0.65;"
            title_extra = "text-decoration:line-through; color:#94a3b8;"

        ind_html += f"""
          <div id="ind-{idx}" style="{card_style} border-radius:12px; padding:12px 14px; transition: box-shadow 0.25s ease;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:6px; flex-wrap:wrap;">
              <div style="display:flex; align-items:center; gap:6px; flex:1; min-width:200px;">
                <span class="weight-pill" style="flex-shrink:0;">{ind['weight']}</span>
                <span style="font-weight:800; color:#fff; font-size:0.9rem; line-height:1.35; {title_extra}">{name_str}</span>
              </div>
              <span class="sig-badge {sig_cls}" style="flex-shrink:0;">{ind['signalText']}</span>
            </div>
            <div style="color:#cbd5e1; font-size:0.83rem; line-height:1.45;">{ind['detail']}</div>
          </div>"""

    # Stocks Cards with Brand Signature Colors
    BRAND_COLORS = {
        '삼성전자': '#2563eb',       # Samsung Blue
        '005930': '#2563eb',
        'SK하이닉스': '#EA002C',     # SK Red
        '000660': '#EA002C',
        'SKHY': '#EA002C',
        '엔비디아': '#76B900',       # NVIDIA Green
        'NVDA': '#76B900',
        '테슬라': '#E82127',         # Tesla Red
        'TSLA': '#E82127',
        '알파벳': '#4285F4',         # Google Blue
        'GOOGL': '#4285F4',
        '메타': '#0668E1',           # Meta Blue
        'META': '#0668E1',
        '아마존': '#FF9900',         # Amazon Orange
        'AMZN': '#FF9900',
        '마이크론': '#0077C8',       # Micron Blue
        'MU': '#0077C8',
        '팔란티어': '#94a3b8',       # Palantir Silver/Slate
        'PLTR': '#94a3b8',
        'GE 버노바': '#006B54',      # GE Vernova Green
        'GEV': '#006B54',
        '블룸에너지': '#10B981',     # Bloom Energy Emerald
        'BE': '#10B981',
        '웨스턴': '#005696',         # WDC Deep Blue
        'WDC': '#005696'
    }

    stocks_html = ""
    for s in data.get('stocks', []):
        chg_cls = "chg-up" if s['direction'] == 'up' else ("chg-down" if s['direction'] == 'down' else "chg-neutral")
        safe_name = s['name'].replace("'", "\\'")
        safe_price = f"{s['price']} ({s['change']})".replace("'", "\\'")
        safe_desc = s['summary'].replace("'", "\\'").replace('"', '&quot;')
        safe_url = s.get('url', '').replace("'", "\\'")
        
        brand_col = '#334155'
        for k, v in BRAND_COLORS.items():
            if k in s['name']:
                brand_col = v
                break

        stocks_html += f"""
      <div class="stock-card" style="background: {brand_col}38; border: 1px solid {brand_col};" onclick="openStockModal('{safe_name}', '{safe_price}', '{safe_desc}', '{safe_url}')">
        <div class="stock-header">
          <span class="stock-name" style="color:#ffffff;">{s['name']}</span>
          <span class="stock-chg {chg_cls}">{s['change']}</span>
        </div>
        <div class="stock-sub" style="color:#cbd5e1;">{s['price']}</div>
        <div class="stock-snippet" style="color:#e2e8f0;">{s['summary']}</div>
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
        history = history[-30:]
        with open(AUDIT_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    total_cnt = len(history)
    win_cnt = sum(1 for h in history if h.get('result') == '⭕')
    loss_cnt = total_cnt - win_cnt
    win_rate = f"{(win_cnt / total_cnt * 100):.1f}" if total_cnt > 0 else "0.0"

    chart_svg = render_timeline_svg(history)

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
    session_badge = "모닝 (07:30)" if session == 'morning' else "마감 (18:00)"

    html = tpl.replace('{{REPORT_TITLE}}', data.get('reportTitle', ''))
    html = html.replace('{{SESSION_CLASS}}', session_class)
    html = html.replace('{{SESSION_BADGE}}', session_badge)
    html = html.replace('{{REPORT_TIMESTAMP}}', now_kst.strftime('%Y-%m-%d %H:%M'))
    html = html.replace('{{EXECUTIVE_SUMMARY}}', data.get('executiveSummary', ''))
    html = html.replace('{{MACRO_CARDS_HTML}}', macro_html)
    html = html.replace('{{TARGET_CARDS_HTML}}', target_html)
    html = html.replace('{{INDICATORS_BAR_HTML}}', ind_bar_html)
    html = html.replace('{{INDICATORS_ROWS_HTML}}', ind_html)
    html = html.replace('{{STOCKS_CARDS_HTML}}', stocks_html)
    html = html.replace('{{AUDIT_TOTAL_COUNT}}', str(total_cnt))
    html = html.replace('{{AUDIT_WIN_COUNT}}', str(win_cnt))
    html = html.replace('{{AUDIT_LOSS_COUNT}}', str(loss_cnt))
    html = html.replace('{{AUDIT_WIN_RATE}}', win_rate)
    html = html.replace('{{AUDIT_CHART_SVG}}', chart_svg)
    html = html.replace('{{AUDIT_ROWS_HTML}}', audit_html)

    return html

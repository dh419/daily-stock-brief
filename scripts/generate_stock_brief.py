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
        gap_low, gap_high = extract_range_from_str(t.get('gap', ''))
        actual_v = None
        pred_label = t.get('band', '-')
        gap_label = t.get('gap', '').replace('시초가', '').strip()
    else:
        low_v, high_v = extract_range_from_str(t.get('gap', ''))
        gap_low, gap_high = None, None
        actual_v = extract_single_pct(t.get('band', ''))
        pred_label = t.get('gap', '-').replace('오전예측:', '').strip()
        gap_label = ""

    # Dynamic scale check
    max_abs = 5.0
    for v in (low_v, high_v, gap_low, gap_high, actual_v):
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

    gap_html = ""
    if gap_low is not None and gap_high is not None:
        g_low = to_pct_pos(gap_low)
        g_high = to_pct_pos(gap_high)
        g_w = max(2.0, g_high - g_low)
        gap_html = f'<div style="position:absolute; top:4px; bottom:4px; left:{g_low:.1f}%; width:{g_w:.1f}%; background:rgba(56, 189, 248, 0.45); border-left:2px solid #38bdf8; border-right:2px solid #38bdf8; border-radius:2px; z-index:7;" title="시초가: {gap_label}"></div>'

    actual_html = ""
    is_hit = False
    if actual_v is not None:
        p_act = to_pct_pos(actual_v)
        if low_v is not None and high_v is not None and (low_v <= actual_v <= high_v):
            is_hit = True
        pin_class = "hit" if is_hit else "miss"
        actual_html = f'<div class="meter-actual-pin {pin_class}" style="left:{p_act:.1f}%;" title="실제: {actual_v:+.2f}%"></div>'

    if session == 'morning':
        left_legend = f'<span style="color:#38bdf8;">시초: {gap_label}</span>' if gap_label else '<span style="color:#94a3b8;">시초 대기</span>'
        right_legend = f'<span style="color:#c7d2fe;">마감: {pred_label}</span>'
    else:
        left_legend = f'<span style="color:#c7d2fe;">마감: {pred_label}</span>'
        right_legend = f'<span style="color:{"#10b981" if is_hit else "#f87171"}; font-weight:800;">실제: {actual_v:+.2f}% ({"⭕" if is_hit else "❌"})</span>' if actual_v is not None else ''

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
          {gap_html}
          {actual_html}
        </div>
        <div class="meter-legend">
          {left_legend}
          {right_legend}
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
            short_kw = re.sub(r'^\d+\.\s*\[.*?\]\s*', '', ind['name'])[:6]
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
      <div style="display:flex; align-items:center; gap:12px; flex-wrap:nowrap;">
        <div style="position:relative; width:126px; height:126px; flex-shrink:0;">
          <svg viewBox="0 0 120 120" style="width:100%; height:100%;">
            {''.join(ind_slices)}
            <text x="60" y="57" text-anchor="middle" font-size="9" font-weight="bold" fill="#f87171">▲ {up_weight}%</text>
            <text x="60" y="70" text-anchor="middle" font-size="9" font-weight="bold" fill="#60a5fa">▼ {down_weight}%</text>
          </svg>
        </div>
        <div style="flex:1; min-width:0; display:flex; flex-direction:column; gap:4px;">
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
    session_badge = "개장 (07:00)" if session == 'morning' else "마감 (19:00)"

    html = tpl.replace('{{REPORT_TITLE}}', data.get('reportTitle', ''))
    html = html.replace('{{SESSION_CLASS}}', session_class)
    html = html.replace('{{SESSION_BADGE}}', session_badge)
    html = html.replace('{{REPORT_TIMESTAMP}}', now_kst.strftime('%Y-%m-%d %H:%M'))
    html = html.replace('{{EXECUTIVE_SUMMARY}}', data.get('executiveSummary', ''))
    html = html.replace('{{MACRO_CARDS_HTML}}', macro_html)
    html = html.replace('{{TARGET_CARDS_HTML}}', target_html)
    ql = data.get('quantLogic', {})
    if ql:
        regime_txt = ql.get('regime', '체제 미지정')
        score_txt = ql.get('score', '-')
        band_rule_txt = ql.get('bandRule', '-')
        rationale_txt = ql.get('rationale', '')
        quant_logic_html = f"""
    <div style="background:rgba(30,41,59,0.72); border:1px solid rgba(99,102,241,0.35); border-left:4px solid #6366f1; border-radius:10px; padding:12px 14px; margin-bottom:12px;">
      <div style="display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:6px;">
        <span style="background:#4f46e5; color:#ffffff; font-size:0.74rem; font-weight:800; padding:2px 8px; border-radius:5px;">{regime_txt}</span>
        <span style="background:#0f172a; color:#38bdf8; border:1px solid #334155; font-size:0.74rem; font-weight:700; padding:2px 8px; border-radius:5px;">{score_txt}</span>
        <span style="background:#0f172a; color:#fbbf24; border:1px solid #334155; font-size:0.74rem; font-weight:700; padding:2px 8px; border-radius:5px;">{band_rule_txt}</span>
      </div>
      <div style="font-size:0.84rem; color:#e2e8f0; line-height:1.45;">{rationale_txt}</div>
    </div>"""
    else:
        quant_logic_html = ""

    html = html.replace('{{QUANT_LOGIC_HTML}}', quant_logic_html)
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

def get_session_prompt(session, now_kst):
    date_str = now_kst.strftime('%Y-%m-%d')
    weekday_ko = ['월', '화', '수', '목', '금', '토', '일'][now_kst.weekday()]
    
    audit_context = ""
    if os.path.exists(AUDIT_HISTORY_PATH):
        with open(AUDIT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            history = json.load(f)
            audit_context = f"\n[최근 누적 예측 vs 실제 이진 판정 기록 (최근 {len(history)}일)]\n" + json.dumps(history[-25:], ensure_ascii=False)

    if session == 'morning':
        session_desc = f"개장 브리핑 (매일 07:00 KST) - {date_str} ({weekday_ko})"
        task_focus = """
[핵심 임무: 규칙 기반 퀀트 예측 (감에 의한 숫자 찍기 금지)]
다음 3단계 정량 공식을 반드시 순서대로 수행하여 예측 밴드를 산출하세요:

Step 1. 투명한 규칙 기반 6대 선행 지표 수집 및 스코어링 (Core 4개 고정 70% + Dynamic 2개 체제별 자동 교체 30%)
  [고정 Core 4대 지표 - 매일 필수 측정 (70%)]
  - ① 해외 선행(25%): MSCI 한국 ETF(EWY) 등락률 & 코스피200 야간선물
  - ② 반도체 동조(20%): 필라델피아 반도체(SOX), 마이크론(MU), SK하이닉스 ADR(SKHY), 엔비디아(NVDA)
  - ③ 역외 외환(15%): 뉴욕 NDF 원/달러 1개월물 변동폭 (서울 종가 대비 상승 시 하방, 하락 시 상방)
  - ④ 외국인 추세 수급(10%): 최근 3거래일 외국인 코스피 현·선물 누적 순매수 방향

  [규칙 기반 Dynamic 2대 지표 - 시장 체제(Regime) 우선순위에 따라 기계적 자동 선택 (30%, 각 15%)]
  * 임의 선택 금지! 아래 1~4번 우선순위 중 오늘 해당하는 첫 번째 체제의 지표 2개를 반드시 투입하고, 지표 설명에 '[체제: ...]'을 명시하세요:
    - 1순위 [매크로 이벤트] (FOMC·CPI·고용지표 발표 당일/익일): ⑤ 미 국채 2년물 금리 변동폭, ⑥ 달러인덱스(DXY)
    - 2순위 [변동성 경보] (VIX 20 이상 또는 전일 코스피 ±1.5% 초과): ⑤ VIX 지수 레벨, ⑥ 원/엔 환율
    - 3순위 [반도체 실적 시즌] (엔비디아·TSMC·삼성·하이닉스 실적/가이던스 주간): ⑤ SOX 대비 TSMC 괴리율, ⑥ 엔비디아 일일 등락률
    - 4순위 [평시 수급 장세] (위 1~3 해당 없음): ⑤ 외국인 코스피200 선물 순매수, ⑥ 프로그램 매매·옵션만기 수급 이벤트

  * 각 지표별로 상방(+1), 중립(0), 하방(-1)을 판정하고 가중치를 곱해 종합 시장 점수 S (-100 ~ +100점)를 계산하세요.

Step 2. 최근 5거래일 변동성(ATR) 기반 동적 밴드 폭(W) 결정 [과신 방지 규칙]
  - 최근 5거래일 중 하루라도 코스피 ±2.0% 이상 급등락이 있었다면 '고변동장'으로 분류하여 밴드 폭(W)을 최소 ±1.5% ~ ±2.5%(전체 폭 3.0%p 이상)로 넓게 잡으세요.
  - 평온장(±1% 이내 횡보)일 때만 ±0.8% ~ ±1.0%로 좁히세요.
  - [편향 방지]: 전일 외국인이 3,000억 이상 순매도했거나 NDF 환율이 +5원 이상 급등했다면, 간밤 미 증시가 올랐더라도 상방 점수를 절반으로 깎으세요.

Step 3. 종목별 베타(Beta) 차등 적용 및 최종 밴드 산출
  - 코스피(Beta 1.0), 삼성전자(Beta 1.2), SK하이닉스(Beta 1.8)의 변동성 차이를 반영해 [시초가 갭%]와 [장중 예측 밴드%]를 산출하세요.
  - 13개 핵심 관심 종목(삼성전자, SK하이닉스, SKHY, NVDA, TSLA, GOOGL, META, AMZN, MU, PLTR, GEV, BE, WDC)의 최신 등락률과 핵심 이슈를 전수 수집하세요.
"""
    else:
        session_desc = f"마감 분석 리포트 (매일 19:00 KST) - {date_str} ({weekday_ko})"
        task_focus = """
[핵심 임무: 이진 평가 감사(Audit) 및 오차 원인 정량 분류]
1. 당일 마감된 국내 증시(코스피, 코스닥 종가 및 등락률), 외국인/기관 순매수 금액, 서울 외환시장 원/달러 환율 종가, 삼성전자·SK하이닉스 마감 종가, 유럽 증시 및 미국 프리마켓 동향을 실시간 검색하세요.
2. 오늘 개장 브리핑의 코스피·삼성전자·SK하이닉스 예측 밴드와 실제 종가를 대조하여 **엄격한 이진 판정(⭕ 적중 또는 ❌ 실패, 부분적중 없음)**을 내리세요.
3. 실패(❌) 시 오차의 주원인을 반드시 **[① 방향성 오판(상방/하방 반대)]** 또는 **[② 변동성 과소평가(방향은 맞았으나 밴드 폭 초과)]**로 분류하고, 설명력을 상실한 지표를 퇴출(Discard)한 뒤 신규 선행 지표를 편입(Replace)하여 다음 거래일 6대 지표 세트를 확정하세요.
4. 국내외 13개 핵심 관심 종목의 최신 마감/프리마켓 동향을 전수 수집하세요.
"""

    prompt = f"""
당신은 한국 및 미국 증시, 매크로 경제, AI/반도체 밸류체인을 전담하는 퀀트 투자 애널리스트입니다.
지금은 {session_desc} 시점입니다.

실시간 구글 검색을 활용하여 최신 시장 데이터와 외신·국내 뉴스를 수집하고, 반드시 아래 JSON 스키마 규격으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON만 출력):

{task_focus}
{audit_context}

반드시 준수할 JSON 출력 스키마:
{{
  "reportTitle": "{now_kst.strftime('%m.%d')}({weekday_ko}) {'개장 브리핑' if session == 'morning' else '마감 & 지표 감사'}",
  "executiveSummary": "핵심 지수 마감/예측 요약 및 당일 핵심 관전 포인트 2줄 요약",
  "quantLogic": {{
    "regime": "적용된 체제명 (예: 변동성 경보 모드 / 매크로 이벤트 모드 / 평시 수급 모드)",
    "score": "직관적 방향성 요약 (예: 매수 우위 (4상방 · 1하방) 또는 매도 우위 (1상방 · 4하방))",
    "bandRule": "직관적 변동폭 표현 (예: 예상 등락폭 ±1.8% (주의) 또는 안정권 ±0.9%)",
    "rationale": "수식 없이 3단계 핵심 산출 과정만 간결히 표기: ① 지표 합산(예: 해외+25·반도체+20·NDF+15·수급-10 = +50) → ② 변동폭 결정(예: 5일 고변동장 ±1.8%) → ③ 종목별 배수 적용(코스피 1.0배·삼성 1.2배·하이닉스 1.8배로 마감 밴드 도출)"
  }},
  "macroCards": [
    {{ "title": "다우존스 / S&P500 / 나스닥 / SOX / 美10년물 / 원·달러 등 6개", "value": "현재 수치", "change": "+0.54% 또는 -1.20%", "direction": "up 또는 down 또는 neutral" }}
  ],
  "targets": [
    {{
      "name": "코스피 / 삼성전자 / SK하이닉스 (반드시 이 3개 순서대로)",
      "code": "KOSPI / 005930 / 000660",
      "gap": "{'시초가 -0.4% ~ -1.0%' if session == 'morning' else '오전예측: -0.8% ~ +0.6%'}",
      "band": "{'-0.8% ~ +0.6%' if session == 'morning' else '실제: -0.98% (❌ 실패)'}",
      "desc": "예상 지수/주가 밴드 및 지지·저항 핵심 코멘트 (2줄 이내)"
    }}
  ],
  "indicators": [
    {{
      "name": "{'[유지] ' if session == 'evening' else ''}지표명 (6개)",
      "detail": "최신 확정 수치 및 상태 요약",
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
      "summary": "핵심 이슈 및 수급 코멘트 2줄",
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

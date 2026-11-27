"""
Zero-base quant engine for the morning brief (deterministic, no LLM numbers).

Idea
----
The Korean session opens ~9h after the US close. Everything the US market priced
overnight (EWY, SOX, MU, NVDA, USD/KRW, VIX) is a *measurable surprise* versus
Korea's own previous close. We regress Korean gap / close returns on that
surprise with a rolling, volatility-normalised ridge model, then build the
forecast distribution from the model's own out-of-sample residuals.

Output per target (KOSPI, Samsung, SK hynix)
  - center   : expected close-to-close return (%)
  - gap      : expected opening gap (%)
  - q10/q90  : 80% predictive interval (asymmetric, data-driven)
  - p_up     : probability close > previous close
  - call     : UP / DOWN / NO-CALL (only when |edge| is meaningful)

Scoring is NOT "is the actual inside my band" (that rewards wide/symmetric
bands). We score with pinball loss + direction hit-rate on calls, each compared
to a naive zero-drift baseline of identical width (= skill score).
"""
import json
import sys
import argparse
import numpy as np
import pandas as pd
import yfinance as yf

TARGETS = {'kospi': '^KS11', 'samsung': '005930.KS', 'skhynix': '000660.KS'}
US = {'ewy': 'EWY', 'sox': '^SOX', 'mu': 'MU', 'nvda': 'NVDA', 'vix': '^VIX', 'krw': 'KRW=X'}
WINDOW = 250          # rolling training days
LAMBDA = 2.0          # ridge strength (on standardised features)
Q_LO, Q_HI = 0.10, 0.90
CALL_PROB = 0.60      # minimum p_up / p_down to issue a directional call
import os
_FLOWS = None
USE_FLOWS = os.environ.get('QE_FLOWS', '1') == '1'


def _dl(tickers, period):
    df = yf.download(list(tickers), period=period, auto_adjust=True, progress=False, group_by='ticker')
    return df


NAVER_H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130'}


def fetch_flows(code, years=4):
    """Daily foreign/institution net buy (shares) from Naver mobile API, paging by bizdate."""
    import requests
    out, bizdate = [], None
    stop = (pd.Timestamp.now() - pd.DateOffset(years=years)).strftime('%Y%m%d')
    for _ in range(40):
        q = f'pageSize=60' + (f'&bizdate={bizdate}' if bizdate else '')
        j = requests.get(f'https://m.stock.naver.com/api/stock/{code}/trend?{q}', headers=NAVER_H, timeout=10).json()
        if not j:
            break
        out += j
        last = j[-1]['bizdate']
        if last <= stop or last == bizdate:
            break
        bizdate = last
    num = lambda v: float(str(v).replace(',', '').replace('+', '') or 0)
    df = pd.DataFrame([{'date': pd.Timestamp(r['bizdate']), 'frg': num(r['foreignerPureBuyQuant']),
                        'org': num(r['organPureBuyQuant']), 'vol': num(r['accumulatedTradingVolume']),
                        'close': num(r['closePrice'])} for r in out])
    df = df.drop_duplicates('date').set_index('date').sort_index()
    adv = df['vol'].rolling(20, min_periods=5).mean()
    return pd.DataFrame({'frg': df['frg'] / adv, 'org': df['org'] / adv,
                         'frg_val': df['frg'] * df['close'], 'adv_val': adv * df['close']})


def flow_features():
    """Per-target flow series indexed by the day the flow happened (known that evening)."""
    s, h = fetch_flows('005930'), fetch_flows('000660')
    mkt = (s['frg_val'].add(h['frg_val'], fill_value=0)) / (s['adv_val'].add(h['adv_val'], fill_value=0))
    res = {'samsung': s[['frg', 'org']], 'skhynix': h[['frg', 'org']]}
    res['kospi'] = pd.DataFrame({'frg': mkt, 'org': (s['org'] + h['org']) / 2})
    for k, v in res.items():
        v = v.copy(); v['frg3'] = v['frg'].rolling(3).sum(); res[k] = v
    return res


def build_dataset(period='4y'):
    kr = _dl(TARGETS.values(), period)
    us = _dl(US.values(), period)
    us_close = pd.DataFrame({k: us[t]['Close'] for k, t in US.items()}).dropna(how='all')
    us_ret = us_close.pct_change() * 100
    us_ret['vix'] = us_close['vix'].diff()             # VIX points change
    us_ret['vix_lvl'] = us_close['vix']

    frames = {}
    for name, t in TARGETS.items():
        o, c = kr[t]['Open'], kr[t]['Close']
        d = pd.DataFrame({'open': o, 'close': c}).dropna()
        d = d[d['open'] > 0]
        d['prev'] = d['close'].shift(1)
        d['gap'] = (d['open'] / d['prev'] - 1) * 100
        d['ret'] = (d['close'] / d['prev'] - 1) * 100
        d['prev_ret'] = d['ret'].shift(1)
        frames[name] = d
    kospi_prev = frames['kospi']['ret']
    try:
        flows = flow_features() if USE_FLOWS else None
    except Exception as e:
        print(f'[quant] flow fetch failed: {e}'); flows = None
    global _FLOWS
    _FLOWS = flows

    rows = {}
    for name, d in frames.items():
        X = []
        for dt in d.index:
            u = us_ret[us_ret.index < dt]
            if len(u) == 0:
                X.append(None); continue
            last = u.iloc[-1]
            X.append(last)
        U = pd.DataFrame([x if x is not None else pd.Series(dtype=float) for x in X], index=d.index)
        f = pd.DataFrame(index=d.index)
        # overnight surprise: EWY move not yet reflected in Korea's previous close
        f['ewy_surprise'] = U['ewy'] - kospi_prev.reindex(d.index).shift(1).fillna(0)
        f['sox'] = U['sox']
        f['mu'] = U['mu']
        f['nvda'] = U['nvda']
        f['krw'] = U['krw']          # +: won weaker (risk-off)
        f['vix'] = U['vix']
        f['own_prev'] = d['prev_ret']  # mean reversion / momentum
        if flows is not None:
            fl = flows[name].reindex(d.index).shift(1)   # yesterday's flow, known before open
            f['frg'] = fl['frg']; f['frg3'] = fl['frg3']; f['org'] = fl['org']
        vol = d['ret'].ewm(span=20).std().shift(1)
        rows[name] = (f, d[['gap', 'ret']], vol, U['vix_lvl'])
    return rows


def _ridge(X, y, lam):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    Z1 = np.c_[np.ones(len(Z)), Z]
    I = np.eye(Z1.shape[1]); I[0, 0] = 0
    b = np.linalg.solve(Z1.T @ Z1 + lam * I, Z1.T @ y)
    return lambda x: np.r_[1.0, (x - mu) / sd] @ b


def _predict_one(f, y, vol, i, col):
    """Fit on [i-WINDOW, i) and predict row i. Targets are vol-normalised."""
    lo = max(0, i - WINDOW)
    Xtr, ytr, vtr = f.iloc[lo:i].values, y[col].iloc[lo:i].values, vol.iloc[lo:i].values
    ok = ~(np.isnan(Xtr).any(1) | np.isnan(ytr) | np.isnan(vtr))
    if ok.sum() < 120:
        return None
    model = _ridge(Xtr[ok], ytr[ok] / vtr[ok], LAMBDA)
    fitted = np.array([model(x) for x in Xtr[ok]])
    resid = ytr[ok] / vtr[ok] - fitted               # standardised residuals
    v = vol.iloc[i]
    m = model(np.nan_to_num(f.iloc[i].values.astype(float))) * v
    sims = m + resid * v                              # empirical predictive dist
    return m, sims


def forecast_row(f, y, vol, i):
    g = _predict_one(f, y, vol, i, 'gap')
    r = _predict_one(f, y, vol, i, 'ret')
    if g is None or r is None:
        return None
    center, sims = r
    p_up = float((sims > 0).mean())
    q10, q90 = np.quantile(sims, [Q_LO, Q_HI])
    call = 'UP' if p_up >= CALL_PROB else 'DOWN' if p_up <= 1 - CALL_PROB else 'NO-CALL'
    return dict(center=float(center), gap=float(g[0]), q10=float(q10), q90=float(q90),
                p_up=p_up, call=call, vol=float(vol.iloc[i]))


def pinball(y, q, tau):
    d = y - q
    return np.maximum(tau * d, (tau - 1) * d)


def backtest(rows, days=250):
    report = {}
    for name, (f, y, vol, _) in rows.items():
        n = len(f)
        recs = []
        for i in range(n - days, n):
            if np.isnan(y['ret'].iloc[i]):
                continue
            fc = forecast_row(f, y, vol, i)
            if fc is None:
                continue
            fc['actual'] = float(y['ret'].iloc[i])
            recs.append(fc)
        df = pd.DataFrame(recs).dropna()
        a = df['actual'].values
        # naive baseline: zero drift, same width
        half = (df['q90'] - df['q10']).values / 2
        pl_model = (pinball(a, df['q10'].values, Q_LO) + pinball(a, df['q90'].values, Q_HI)).mean()
        pl_naive = (pinball(a, -half, Q_LO) + pinball(a, half, Q_HI)).mean()
        calls = df[df['call'] != 'NO-CALL']
        hit = ((calls['call'] == 'UP') == (calls['actual'] > 0)).mean() if len(calls) else float('nan')
        mae_model = np.abs(a - df['center']).mean()
        mae_naive = np.abs(a).mean()
        report[name] = dict(
            days=len(df),
            coverage80=float(((a >= df['q10']) & (a <= df['q90'])).mean()),
            call_rate=float(len(calls) / len(df)),
            call_hit=float(hit),
            all_dir_hit=float(((df['center'] > 0) == (a > 0)).mean()),
            pinball_skill=float(1 - pl_model / pl_naive),
            mae_skill=float(1 - mae_model / mae_naive),
            corr=float(np.corrcoef(df['center'], a)[0, 1]),
        )
    return report


def today(rows):
    out = {}
    for name, (f, y, vol, vix) in rows.items():
        # Append a synthetic "today" row using latest US data
        i = len(f) - 1
        fc = forecast_row(f, y, vol, i)
        out[name] = fc
    return out


def latest_forecast(period='4y'):
    """Forecast for the next Korean session using the latest US close."""
    rows = build_dataset(period)
    us = _dl(US.values(), '10d')
    us_close = pd.DataFrame({k: us[t]['Close'] for k, t in US.items()}).dropna(how='all')
    # US holiday (e.g. Thanksgiving) -> no new overnight info -> 0
    raw = us_close.pct_change(fill_method=None).iloc[-1] * 100
    ewy_closed = pd.isna(raw['ewy'])
    last = raw.fillna(0)
    last['vix'] = float(np.nan_to_num(us_close['vix'].diff().iloc[-1]))
    out = {}
    for name, (f, y, vol, _) in rows.items():
        kospi_last = rows['kospi'][1]['ret'].dropna().iloc[-1]
        own_last = y['ret'].dropna().iloc[-1]
        x = pd.Series({'ewy_surprise': 0.0 if ewy_closed else last['ewy'] - kospi_last, 'sox': last['sox'], 'mu': last['mu'],
                       'nvda': last['nvda'], 'krw': last['krw'], 'vix': last['vix'], 'own_prev': own_last})
        if _FLOWS is not None:
            lf = _FLOWS[name].dropna().iloc[-1]
            x['frg'], x['frg3'], x['org'] = lf['frg'], lf['frg3'], lf['org']
        ret_full = y['ret'].dropna()
        vol_now = ret_full.ewm(span=20).std().iloc[-1]
        f2 = pd.concat([f, x.to_frame().T]); y2 = pd.concat([y, pd.DataFrame({'gap': [np.nan], 'ret': [np.nan]})])
        v2 = pd.concat([vol, pd.Series([vol_now])])
        f2.index = range(len(f2)); y2.index = range(len(y2)); v2.index = range(len(v2))
        out[name] = forecast_row(f2, y2, v2, len(f2) - 1)
        out[name]['inputs'] = {k: round(float(v), 2) for k, v in x.items()}
    return out


def to_brief_targets(fc):
    """Convert engine output into the brief's `targets` gap/band strings."""
    labels = {'kospi': ('코스피', 'KOSPI'), 'samsung': ('삼성전자', '005930'), 'skhynix': ('SK하이닉스', '000660')}
    res = []
    for k, (nm, code) in labels.items():
        o = fc[k]
        arrow = {'UP': '▲ 상승 콜', 'DOWN': '▼ 하락 콜', 'NO-CALL': '― 방향 유보'}[o['call']]
        res.append({
            'name': nm, 'code': code,
            'gap': f"시초가 {o['gap']:+.2f}%",
            'band': f"{o['q10']:+.2f}% ~ {o['q90']:+.2f}%",
            'desc': f"{arrow} · 상승확률 {o['p_up']*100:.0f}% · 기대종가 {o['center']:+.2f}% (80% 구간)",
            'quant': o,
        })
    return res


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['backtest', 'today'], default='today')
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.mode == 'backtest':
        res = backtest(build_dataset())
    else:
        res = latest_forecast()
    txt = json.dumps(res, ensure_ascii=False, indent=2, default=float)
    if a.out:
        open(a.out, 'w', encoding='utf-8').write(txt)
    sys.stdout.reconfigure(encoding='utf-8')
    print(txt)

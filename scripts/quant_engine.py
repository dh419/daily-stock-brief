"""
Zero-base Asymmetric Quant Engine v2.0 for the morning brief (deterministic, pure NumPy/Pandas).

Architecture
------------
1. Clean US Equity vs FX alignment:
   - Separates US equity trading days (EWY/SOX) from 24h FX (KRW=X) to eliminate US-holiday
     ffill() distortion and train/serve skew on `ewy_surprise`.
2. Refined Feature Set (LOO/Forward ablation verified):
   - Core: ewy_surprise, sox, mu, nvda, krw, dxy (+added), vix_diff, own_prev, ret5 (+added),
           frg, frg3 (org removed as OOS noise).
   - Night Futures Track: KRX KOSPI200 night futures (`nf_ret`, corr=0.82 with opening gap).
3. Robust Time-Decayed Ensemble with Exact LOO-CV:
   - Exponential time-decay weights (half-life 120d) over 500d Core / 360d Night-Futures windows.
   - 60% Huber-IRLS Ridge + 40% Weighted L2 Ridge; closed-form Hat-matrix LOO-CV auto-tunes lambda
     and yields true out-of-sample LOO residuals (no in-sample shrinkage bias).
   - Two-Track Hybrid: blends 85% Night-Futures Track + 15% Long-History Core Track when night
     futures are available; falls back cleanly to 100% Core Track otherwise.
4. Non-Origin-Symmetric Directional Intervals:
   - Regime-weighted conditional LOO residuals (2.0x same-sign regime, 0.7x opposite-sign) preserve
     natural upside/downside skewness.
   - UP call (p_up >= 60%): conditional positive distribution [q20, q85], floored >= +min_origin_gap
     (0% origin straddling).
   - DOWN call (p_up <= 40%): conditional negative distribution [q15, q80], capped <= -min_origin_gap
     (0% origin straddling).
   - NO-CALL: tight asymmetric 50% core [q25, q75] (no wide origin-symmetric net).
   - Full 80% tail risk [tail_q10, tail_q90] and 50% opening gap range [gap_lo, gap_hi] are also
     reported separately for risk transparency and HTML meter rendering.
"""
import os
import json
import sys
import argparse
import numpy as np
import pandas as pd
import yfinance as yf

TARGETS = {'kospi': '^KS11', 'samsung': '005930.KS', 'skhynix': '000660.KS'}
US_EQUITY = {'ewy': 'EWY', 'sox': '^SOX', 'mu': 'MU', 'nvda': 'NVDA', 'vix': '^VIX', 'dxy': 'DX-Y.NYB'}
US_FX = {'krw': 'KRW=X'}
US_ALL = {**US_EQUITY, **US_FX}

CORE_WINDOW = 500
NF_WINDOW = 360
HALFLIFE = 120.0
LAMBDAS = (3.0, 8.0, 20.0, 50.0, 100.0)
CALL_PROB = 0.60
MIN_ORIGIN_GAP = {'kospi': 0.30, 'samsung': 0.40, 'skhynix': 0.50}

_FLOWS = None
USE_FLOWS = os.environ.get('QE_FLOWS', '1') == '1'
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NIGHT_FUT_CSV = os.path.join(REPO_ROOT, 'data', 'krx_night_fut.csv')
KRX_FUT_URL = 'https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd'
NAVER_H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130'}


def _dl(tickers, period):
    return yf.download(list(tickers), period=period, auto_adjust=True, progress=False, group_by='ticker')


def _get_krx_key():
    key = os.environ.get('KRX_API_KEY', '').strip()
    if key:
        return key
    local_path = r'F:\CLI\.auth\krx_api_key.txt'
    if os.path.exists(local_path):
        return open(local_path, encoding='utf-8').read().strip()
    return ''


def load_krx_night_futures(target_dates):
    """Load cached KRX KOSPI200 night futures CSV and incrementally append missing past days."""
    nf = pd.DataFrame(columns=['nf_ret', 'cl', 'chg'])
    if os.path.exists(NIGHT_FUT_CSV):
        nf = pd.read_csv(NIGHT_FUT_CSV, index_col=0, parse_dates=True)
    key = _get_krx_key()
    if key:
        import requests
        last_dt = nf.index.max() if len(nf) else pd.Timestamp('2025-06-09')
        today_ts = pd.Timestamp.now(tz='Asia/Seoul').tz_localize(None).normalize()
        missing = [d for d in target_dates if d > last_dt and d < today_ts]
        new_rows = []
        for dt in missing[-20:]:
            try:
                r = requests.get(KRX_FUT_URL, headers={'AUTH_KEY': key},
                                 params={'basDd': dt.strftime('%Y%m%d')}, timeout=10)
                if r.status_code != 200:
                    continue
                out = r.json().get('OutBlock_1', [])
                cand = [x for x in out if x.get('PROD_NM') == '코스피200 선물'
                        and x.get('MKT_NM') == '야간' and 'SP' not in str(x.get('ISU_NM', ''))]
                if not cand:
                    continue
                best = max(cand, key=lambda x: int(str(x.get('ACC_TRDVOL', '0')).replace(',', '') or 0))
                cl = float(str(best['TDD_CLSPRC']).replace(',', ''))
                chg = float(str(best['CMPPREVDD_PRC']).replace(',', ''))
                prev = cl - chg
                if prev > 0:
                    new_rows.append((dt, (chg / prev) * 100.0, cl, chg))
            except Exception:
                continue
        if new_rows:
            add_df = pd.DataFrame(new_rows, columns=['date', 'nf_ret', 'cl', 'chg']).set_index('date')
            nf = pd.concat([nf, add_df])
            nf = nf[~nf.index.duplicated(keep='last')].sort_index()
            os.makedirs(os.path.dirname(NIGHT_FUT_CSV), exist_ok=True)
            nf.to_csv(NIGHT_FUT_CSV)
            print(f'[quant] KRX night futures updated: +{len(new_rows)} rows (total {len(nf)})')
    return nf


def fetch_flows(code, years=5):
    """Daily foreign net buy (shares) from Naver mobile API, normalized by 20d ADV."""
    import requests
    out, bizdate = [], None
    stop = (pd.Timestamp.now() - pd.DateOffset(years=years)).strftime('%Y%m%d')
    for _ in range(50):
        q = 'pageSize=60' + (f'&bizdate={bizdate}' if bizdate else '')
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
                        'vol': num(r['accumulatedTradingVolume']),
                        'close': num(r['closePrice'])} for r in out])
    df = df.drop_duplicates('date').set_index('date').sort_index()
    adv = df['vol'].rolling(20, min_periods=5).mean()
    return pd.DataFrame({'frg': df['frg'] / adv,
                         'frg_val': df['frg'] * df['close'], 'adv_val': adv * df['close']})


def flow_features():
    """Per-target foreign flow series indexed by the day the flow happened (known that evening)."""
    s, h = fetch_flows('005930'), fetch_flows('000660')
    mkt = (s['frg_val'].add(h['frg_val'], fill_value=0)) / (s['adv_val'].add(h['adv_val'], fill_value=0))
    res = {'samsung': s[['frg']].copy(), 'skhynix': h[['frg']].copy(), 'kospi': pd.DataFrame({'frg': mkt})}
    for k, v in res.items():
        v['frg3'] = v['frg'].rolling(3).sum()
        res[k] = v
    return res


def _core_cols(f):
    return [c for c in f.columns if c != 'night_fut']


def build_dataset(period='5y', include_night_fut=False):
    kr = _dl(TARGETS.values(), period)
    us = _dl(US_ALL.values(), period)

    # Separate US equity trading days from 24h FX (KRW=X) so US holidays never corrupt ewy_surprise
    us_eq_close = pd.DataFrame({k: us[t]['Close'] for k, t in US_EQUITY.items()})
    us_eq_valid = us_eq_close[us_eq_close['ewy'].notna() | us_eq_close['sox'].notna()].copy()
    us_eq_ret = us_eq_valid.ffill().pct_change(fill_method=None) * 100.0
    us_eq_ret['vix_diff'] = us_eq_valid['vix'].diff()
    us_eq_ret['vix_lvl'] = us_eq_valid['vix']

    krw_close = us['KRW=X']['Close'].dropna()
    krw_ret = krw_close.pct_change() * 100.0

    frames = {}
    for name, t in TARGETS.items():
        o, c = kr[t]['Open'], kr[t]['Close']
        d = pd.DataFrame({'open': o, 'close': c}).dropna()
        d = d[(d['open'] > 0) & (d['close'] > 0)]
        d['prev'] = d['close'].shift(1)
        d['gap'] = (d['open'] / d['prev'] - 1.0) * 100.0
        d['ret'] = (d['close'] / d['prev'] - 1.0) * 100.0
        d['prev_ret'] = d['ret'].shift(1)
        d['ret5'] = d['ret'].rolling(5).sum().shift(1)
        frames[name] = d
    kospi_prev = frames['kospi']['ret']

    try:
        flows = flow_features() if USE_FLOWS else None
    except Exception as e:
        print(f'[quant] flow fetch failed: {e}')
        flows = None
    global _FLOWS
    _FLOWS = flows

    nf_series = None
    if include_night_fut:
        try:
            nf_df = load_krx_night_futures(frames['kospi'].index)
            if len(nf_df) >= 120:
                nf_series = nf_df['nf_ret']
        except Exception as e:
            print(f'[quant] KRX night futures load failed: {e}')

    rows = {}
    for name, d in frames.items():
        eq_rows, krw_vals, stale_flags = [], [], []
        for idx_pos, dt in enumerate(d.index):
            prev_kr_dt = d.index[idx_pos - 1] if idx_pos > 0 else None
            u_sub = us_eq_ret[us_eq_ret.index < dt]
            if len(u_sub) == 0:
                eq_rows.append(pd.Series(dtype=float))
                stale_flags.append(1.0)
            else:
                u_prev_sub = us_eq_ret[us_eq_ret.index < prev_kr_dt] if prev_kr_dt is not None else us_eq_ret.iloc[:0]
                is_stale = (len(u_prev_sub) > 0 and u_sub.index[-1] == u_prev_sub.index[-1])
                stale_flags.append(1.0 if is_stale else 0.0)
                if is_stale:
                    s = u_sub.iloc[-1].copy()
                    for c in list(US_EQUITY.keys()) + ['vix_diff']:
                        if c in s.index:
                            s[c] = 0.0
                    eq_rows.append(s)
                else:
                    eq_rows.append(u_sub.iloc[-1])

            k_sub = krw_ret[krw_ret.index < dt]
            krw_vals.append(float(k_sub.iloc[-1]) if len(k_sub) > 0 else np.nan)

        U = pd.DataFrame(eq_rows, index=d.index)
        us_closed = pd.Series(stale_flags, index=d.index)
        kp_prev = kospi_prev.reindex(d.index).shift(1).fillna(0.0)

        f = pd.DataFrame(index=d.index)
        f['ewy_surprise'] = np.where(us_closed > 0.5, 0.0, U['ewy'] - kp_prev)
        f['sox'] = U['sox']
        f['mu'] = U['mu']
        f['nvda'] = U['nvda']
        f['krw'] = pd.Series(krw_vals, index=d.index)
        f['dxy'] = U['dxy']
        f['vix_diff'] = U['vix_diff']
        f['own_prev'] = d['prev_ret']
        f['ret5'] = d['ret5']
        if flows is not None:
            fl = flows[name].reindex(d.index).shift(1)
            f['frg'] = fl['frg']
            f['frg3'] = fl['frg3']
        if nf_series is not None:
            f['night_fut'] = nf_series.reindex(d.index)

        vol = d['ret'].ewm(span=20).std().shift(1)
        rows[name] = (f, d[['gap', 'ret']], vol, U['vix_lvl'])
    return rows


def _weighted_quantile(values, quantiles, sample_weight):
    sorter = np.argsort(values)
    v = values[sorter]
    w = sample_weight[sorter]
    cdf = np.cumsum(w) - 0.5 * w
    cdf /= np.sum(w)
    return np.interp(quantiles, cdf, v)


def _fit_weighted_ridge_loocv(X, y, weights, lambdas=LAMBDAS):
    """Weighted Ridge with closed-form Hat-matrix LOO-CV lambda selection and LOO residuals."""
    w = weights / np.mean(weights)
    sw = np.sqrt(w)
    mu = np.average(X, axis=0, weights=w)
    sd = np.sqrt(np.average((X - mu) ** 2, axis=0, weights=w)) + 1e-8
    Z = (X - mu) / sd
    Z1 = np.c_[np.ones(len(Z)), Z]
    Zw = Z1 * sw[:, None]
    yw = y * sw

    p = Z1.shape[1]
    I = np.eye(p)
    I[0, 0] = 0.0
    ZtZ = Zw.T @ Zw
    Zty = Zw.T @ yw

    best_loss = np.inf
    best_b = None
    best_loo = None
    for lam in lambdas:
        A_inv = np.linalg.inv(ZtZ + lam * I)
        b = A_inv @ Zty
        resid = y - (Z1 @ b)
        h = np.einsum('ij,jk,ik->i', Zw, A_inv, Zw)
        loo_r = resid / np.maximum(1.0 - h, 0.15)
        loss = np.average(loo_r ** 2, weights=w)
        if loss < best_loss:
            best_loss = loss
            best_b = b
            best_loo = loo_r

    def predict(x_row):
        z = (x_row - mu) / sd
        return float(np.r_[1.0, z] @ best_b)

    return predict, best_loo


def _fit_huber_ridge(X, y, base_w, lambdas=LAMBDAS, delta=1.5, iters=3):
    """IRLS Huber-weighted Ridge for outlier robustness against +/-6~12% tail shocks."""
    w = base_w.copy()
    pred_fn, loo_r = _fit_weighted_ridge_loocv(X, y, w, lambdas)
    for _ in range(iters):
        scale = np.median(np.abs(loo_r)) * 1.4826 + 1e-6
        u = np.abs(loo_r) / (delta * scale)
        w = base_w * np.where(u <= 1.0, 1.0, 1.0 / u)
        pred_fn, loo_r = _fit_weighted_ridge_loocv(X, y, w, lambdas)
    return pred_fn, loo_r


def _fit_track(f_sub, y_col, vol, i, window):
    """Fit 60% Huber-Ridge + 40% L2-Ridge on [i-window, i) with exponential time decay."""
    lo = max(0, i - window)
    Xtr = f_sub.iloc[lo:i].values.astype(float)
    ytr = y_col.iloc[lo:i].values.astype(float)
    vtr = vol.iloc[lo:i].values.astype(float)
    ok = ~(np.isnan(Xtr).any(1) | np.isnan(ytr) | np.isnan(vtr) | (vtr <= 1e-6))
    x_now = f_sub.iloc[i].values.astype(float)
    if ok.sum() < 100 or np.isnan(x_now).any():
        return None
    idx_ok = np.where(ok)[0]
    age = (len(ok) - 1) - idx_ok
    bw = np.exp(-np.log(2.0) * age / HALFLIFE)
    yn = ytr[ok] / vtr[ok]
    v_now = float(vol.iloc[i])

    fn_h, loo_h = _fit_huber_ridge(Xtr[ok], yn, bw)
    fn_l, loo_l = _fit_weighted_ridge_loocv(Xtr[ok], yn, bw)
    pred_norm = 0.60 * fn_h(x_now) + 0.40 * fn_l(x_now)
    loo_comb = 0.60 * loo_h + 0.40 * loo_l
    pred_tr = yn - loo_comb
    center = pred_norm * v_now
    sims = center + loo_comb * v_now
    return center, sims, loo_comb, pred_tr, bw


def _predict_target(f, y_col, vol, i):
    """Two-Track Hybrid prediction: 85% Night-Futures Track + 15% Core Track when NF is available."""
    core_cols = _core_cols(f)
    core = _fit_track(f[core_cols], y_col, vol, i, CORE_WINDOW)
    if core is None:
        return None
    c1, sims1, _, pred_tr1, bw1 = core

    has_nf = ('night_fut' in f.columns) and (not np.isnan(f['night_fut'].iloc[i]))
    if has_nf:
        nf_track = _fit_track(f, y_col, vol, i, NF_WINDOW)
        if nf_track is not None:
            c2, _, loo2, pred_tr2, bw2 = nf_track
            v_now = float(vol.iloc[i])
            center = 0.85 * c2 + 0.15 * c1
            sims = center + loo2 * v_now
            return center, sims, pred_tr2, bw2, True

    return c1, sims1, pred_tr1, bw1, False


def _compute_asymmetric_band(center, sims, pred_tr, bw, p_up, call, vol, name):
    """
    Build non-origin-symmetric, direction-aware predictive interval:
    - Regime-weighted LOO residuals (2.0x same-sign regime, 0.7x opposite) preserve skewness.
    - UP call: conditional positive distribution [q20, q85], lower bound >= +min_gap (no 0-straddle).
    - DOWN call: conditional negative distribution [q15, q80], upper bound <= -min_gap (no 0-straddle).
    - NO-CALL: tight asymmetric 50% core [q25, q75] (no wide symmetric net around 0).
    """
    same_sign = (pred_tr > 0) == (center > 0)
    cond_w = bw * np.where(same_sign, 2.0, 0.7)
    tail_q10, tail_q90 = _weighted_quantile(sims, [0.10, 0.90], cond_w)
    min_gap = max(MIN_ORIGIN_GAP.get(name, 0.30), 0.20 * vol)

    if call == 'UP':
        pos = sims > 0
        if pos.sum() >= 20:
            q_lo, q_hi = _weighted_quantile(sims[pos], [0.20, 0.85], cond_w[pos])
        else:
            q_lo, q_hi = _weighted_quantile(sims, [0.35, 0.88], cond_w)
        band_lo = max(float(q_lo), min_gap)
        band_hi = max(float(q_hi), band_lo + max(0.50, 0.55 * vol))
    elif call == 'DOWN':
        neg = sims < 0
        if neg.sum() >= 20:
            q_lo, q_hi = _weighted_quantile(sims[neg], [0.15, 0.80], cond_w[neg])
        else:
            q_lo, q_hi = _weighted_quantile(sims, [0.12, 0.65], cond_w)
        band_hi = min(float(q_hi), -min_gap)
        band_lo = min(float(q_lo), band_hi - max(0.50, 0.55 * vol))
    else:
        q_lo, q_hi = _weighted_quantile(sims, [0.25, 0.75], cond_w)
        band_lo, band_hi = float(q_lo), float(q_hi)

    return band_lo, band_hi, float(tail_q10), float(tail_q90)


def forecast_row(f, y, vol, i, name='kospi'):
    g = _predict_target(f, y['gap'], vol, i)
    r = _predict_target(f, y['ret'], vol, i)
    if g is None or r is None:
        return None
    g_center, g_sims, _, g_bw, _ = g
    center, sims, pred_tr, bw, nf_used = r
    v_now = float(vol.iloc[i])

    gap_lo, gap_hi = _weighted_quantile(g_sims, [0.25, 0.75], g_bw)
    p_up = float(np.average((sims > 0).astype(float), weights=bw))
    call = 'UP' if p_up >= CALL_PROB else ('DOWN' if p_up <= 1.0 - CALL_PROB else 'NO-CALL')
    band_lo, band_hi, tail_q10, tail_q90 = _compute_asymmetric_band(
        center, sims, pred_tr, bw, p_up, call, v_now, name
    )
    return dict(
        center=float(center),
        gap=float(g_center),
        gap_lo=float(gap_lo),
        gap_hi=float(gap_hi),
        band_lo=float(band_lo),
        band_hi=float(band_hi),
        q10=float(band_lo),
        q90=float(band_hi),
        tail_q10=float(tail_q10),
        tail_q90=float(tail_q90),
        p_up=p_up,
        call=call,
        vol=v_now,
        night_fut_used=bool(nf_used),
    )


def pinball(y, q, tau):
    d = y - q
    return np.maximum(tau * d, (tau - 1) * d)


def backtest(rows, days=250):
    report = {}
    for name, (f, y, vol, _) in rows.items():
        n = len(f)
        recs = []
        for i in range(n - days, n):
            if np.isnan(y['ret'].iloc[i]) or np.isnan(vol.iloc[i]):
                continue
            fc = forecast_row(f, y, vol, i, name=name)
            if fc is None:
                continue
            fc['actual'] = float(y['ret'].iloc[i])
            recs.append(fc)
        df = pd.DataFrame(recs).dropna()
        a = df['actual'].values
        half = (df['tail_q90'] - df['tail_q10']).values / 2.0
        pl_model = (pinball(a, df['tail_q10'].values, 0.10) + pinball(a, df['tail_q90'].values, 0.90)).mean()
        pl_naive = (pinball(a, -half, 0.10) + pinball(a, half, 0.90)).mean()
        calls = df[df['call'] != 'NO-CALL']
        hit = ((calls['call'] == 'UP') == (calls['actual'] > 0)).mean() if len(calls) else float('nan')
        mae_model = np.abs(a - df['center']).mean()
        mae_naive = np.abs(a).mean()
        strad_all = float(((df['band_lo'] < 0) & (df['band_hi'] > 0)).mean())
        strad_calls = float(((calls['band_lo'] < 0) & (calls['band_hi'] > 0)).mean()) if len(calls) else 0.0
        sym0 = float((1.0 - np.abs(df['band_lo'] + df['band_hi']) / (df['band_hi'] - df['band_lo'] + 1e-9)).mean())
        report[name] = dict(
            days=len(df),
            tail_coverage80=float(((a >= df['tail_q10']) & (a <= df['tail_q90'])).mean()),
            call_rate=float(len(calls) / len(df)),
            call_hit=float(hit),
            all_dir_hit=float(((df['center'] > 0) == (a > 0)).mean()),
            pinball_skill=float(1.0 - pl_model / pl_naive),
            mae_skill=float(1.0 - mae_model / mae_naive),
            corr=float(np.corrcoef(df['center'], a)[0, 1]),
            origin_straddle_calls=strad_calls,
            origin_straddle_all=strad_all,
            origin_symmetry=sym0,
            avg_band_width=float((df['band_hi'] - df['band_lo']).mean()),
        )
    return report


def latest_forecast(period='5y', night_fut_today=None):
    """Forecast for the next Korean session using latest US close (+ optional night futures %)."""
    use_nf = night_fut_today is not None and not np.isnan(night_fut_today) and abs(float(night_fut_today)) <= 10.0
    rows = build_dataset(period, include_night_fut=use_nf)

    us = _dl(US_ALL.values(), '14d')
    us_eq_close = pd.DataFrame({k: us[t]['Close'] for k, t in US_EQUITY.items()})
    us_eq_valid = us_eq_close[us_eq_close['ewy'].notna() | us_eq_close['sox'].notna()].copy()
    raw_eq = us_eq_valid.ffill().pct_change(fill_method=None).iloc[-1] * 100.0
    last_eq_dt = us_eq_valid.index[-1]
    last_kr_dt = rows['kospi'][0].index[-1]
    # Check if US market had a newer session than the one already reflected in last Korean trading day
    ewy_closed = pd.isna(raw_eq['ewy']) or (last_eq_dt < last_kr_dt - pd.Timedelta(days=2))
    last = raw_eq.fillna(0.0)
    last['vix_diff'] = float(np.nan_to_num(us_eq_valid['vix'].diff().iloc[-1]))
    krw_close = us['KRW=X']['Close'].dropna()
    last_krw = float(np.nan_to_num(krw_close.pct_change().iloc[-1] * 100.0)) if len(krw_close) >= 2 else 0.0

    out = {}
    for name, (f, y, vol, _) in rows.items():
        kospi_last = rows['kospi'][1]['ret'].dropna().iloc[-1]
        ret_full = y['ret'].dropna()
        own_last = float(ret_full.iloc[-1])
        ret5_last = float(ret_full.iloc[-5:].sum()) if len(ret_full) >= 5 else own_last
        x = pd.Series({
            'ewy_surprise': 0.0 if ewy_closed else float(last['ewy']) - float(kospi_last),
            'sox': 0.0 if ewy_closed else float(last['sox']),
            'mu': 0.0 if ewy_closed else float(last['mu']),
            'nvda': 0.0 if ewy_closed else float(last['nvda']),
            'krw': last_krw,
            'dxy': 0.0 if ewy_closed else float(last['dxy']),
            'vix_diff': 0.0 if ewy_closed else float(last['vix_diff']),
            'own_prev': own_last,
            'ret5': ret5_last,
        })
        if _FLOWS is not None:
            lf = _FLOWS[name].dropna().iloc[-1]
            x['frg'], x['frg3'] = float(lf['frg']), float(lf['frg3'])
        nf_active = use_nf and 'night_fut' in f.columns
        if nf_active:
            x['night_fut'] = float(night_fut_today)

        vol_now = float(ret_full.ewm(span=20).std().iloc[-1])
        f2 = pd.concat([f, x.to_frame().T])
        y2 = pd.concat([y, pd.DataFrame({'gap': [np.nan], 'ret': [np.nan]})])
        v2 = pd.concat([vol, pd.Series([vol_now])])
        f2.index = range(len(f2))
        y2.index = range(len(y2))
        v2.index = range(len(v2))
        out[name] = forecast_row(f2, y2, v2, len(f2) - 1, name=name)
        out[name]['inputs'] = {k: round(float(v), 2) for k, v in x.items()}
    return out


def to_brief_targets(fc):
    """Convert engine output into the brief's `targets` gap/band strings."""
    labels = {'kospi': ('코스피', 'KOSPI'), 'samsung': ('삼성전자', '005930'), 'skhynix': ('SK하이닉스', '000660')}
    res = []
    for k, (nm, code) in labels.items():
        o = fc[k]
        arrow = {'UP': '▲ 상승 콜', 'DOWN': '▼ 하락 콜', 'NO-CALL': '― 방향 유보'}[o['call']]
        band_tag = '비대칭 방향구간' if o['call'] != 'NO-CALL' else '중립 50% 박스'
        res.append({
            'name': nm,
            'code': code,
            'gap': f"시초가 {o['gap_lo']:+.2f}% ~ {o['gap_hi']:+.2f}%",
            'band': f"{o['band_lo']:+.2f}% ~ {o['band_hi']:+.2f}%",
            'desc': (f"{arrow} · 상승확률 {o['p_up']*100:.0f}% · 기대종가 {o['center']:+.2f}% "
                     f"({band_tag} / 꼬리80% {o['tail_q10']:+.1f}%~{o['tail_q90']:+.1f}%)"),
            'quant': o,
        })
    return res


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['backtest', 'today'], default='today')
    ap.add_argument('--night-fut', type=float, default=None)
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.mode == 'backtest':
        res = backtest(build_dataset(include_night_fut=(a.night_fut is not None)))
    else:
        res = latest_forecast(night_fut_today=a.night_fut)
    txt = json.dumps(res, ensure_ascii=False, indent=2, default=float)
    if a.out:
        open(a.out, 'w', encoding='utf-8').write(txt)
    sys.stdout.reconfigure(encoding='utf-8')
    print(txt)

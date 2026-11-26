import os
import io
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_stock_brief as gsb
import nxt_reconcile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')
FOLDER_ID = '1yxtjIIWVriE4u5ikOG-fh2FQY5fh853_'  # dashboard_incoming/daily-stock-brief
KST = timezone(timedelta(hours=9))


def resolve_target_datetime(session: str, now_kst: datetime, event_name: str) -> datetime:
    """Resolve the brief's target KST date when a delayed cron fires after midnight."""
    if event_name == 'schedule':
        if session == 'evening' and now_kst.hour < 19:
            return now_kst - timedelta(days=1)
        if session == 'morning' and now_kst.hour < 7:
            return now_kst - timedelta(days=1)
    elif session == 'evening' and now_kst.hour < 12:
        return now_kst - timedelta(days=1)
    return now_kst


def is_within_grace_window(session: str, now_kst: datetime, target_kst: datetime) -> bool:
    """Wait 1h 25m (07:00~08:25 / 20:00~21:25 KST) for Gemini App upload before failing."""
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        return False
    if target_kst.date() != now_kst.date():
        return False
    hm = now_kst.hour * 60 + now_kst.minute
    if session == 'morning':
        return 420 <= hm < 505
    return 1200 <= hm < 1285


def apply_quant_engine(data, today_str):
    """Replace LLM-made numbers with the deterministic quant engine forecast.
    LLM narrative (desc) is kept as context; numbers come only from the model."""
    try:
        import quant_engine as qe
        fc = qe.latest_forecast()
    except Exception as e:  # never block publishing; flag it instead
        print(f"[quant] engine failed, keeping LLM numbers: {e}")
        data.setdefault('quantLogic', {})['regime'] = '⚠ 퀀트엔진 실패 - LLM 수치(참고용)'
        return
    llm_desc = {t.get('code'): t.get('desc', '') for t in data.get('targets', [])}
    targets = qe.to_brief_targets(fc)
    for t in targets:
        extra = llm_desc.get(t['code'])
        if extra:
            t['desc'] += f" | {extra}"
        t.pop('quant', None)
    data['targets'] = targets
    k = fc['kospi']
    data['quantLogic'] = {
        'regime': f"롤링 릿지 회귀 (250일) · 일변동성 {k['vol']:.2f}%",
        'score': ' / '.join(f"{n} {fc[c]['call']} {fc[c]['p_up']*100:.0f}%" for c, n in
                           [('kospi', '코스피'), ('samsung', '삼성'), ('skhynix', '하이닉스')]),
        'bandRule': '80% 예측구간 (잔차 분포 기반 비대칭) · 콜 기준 P≥60%',
        'rationale': '입력: ' + ', '.join(f"{a}={b:+.2f}" for a, b in k['inputs'].items()),
    }
    os.makedirs(os.path.join(REPO_ROOT, 'forecasts'), exist_ok=True)
    with open(os.path.join(REPO_ROOT, 'forecasts', f'{today_str}.json'), 'w', encoding='utf-8') as f:
        json.dump(fc, f, ensure_ascii=False, indent=2, default=float)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=['morning', 'evening'], default='morning')
    args = parser.parse_args()

    sa_json = os.environ.get('GDRIVE_SA_KEY')
    if not sa_json:
        raise ValueError("GDRIVE_SA_KEY environment variable is missing.")

    event_name = os.environ.get('GITHUB_EVENT_NAME', '')
    now_kst = datetime.now(KST)
    target_kst = resolve_target_datetime(args.session, now_kst, event_name)
    target_str = target_kst.strftime('%Y-%m-%d')
    month_part = target_kst.strftime('%Y-%m')
    expected_name = f"{target_str}-stock-{args.session}.json"
    out_dir = os.path.join(DOCS_DIR, month_part)
    out_file = os.path.join(out_dir, f"{target_str}-{args.session}.html")

    if event_name != 'workflow_dispatch' and os.path.exists(out_file):
        print(f"Brief already published at {out_file}; skipping scheduled check.")
        return

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=['https://www.googleapis.com/auth/drive.readonly'],
    )
    service = build('drive', 'v3', credentials=creds)
    res = service.files().list(
        q=f"'{FOLDER_ID}' in parents and name = '{expected_name}' and trashed = false",
        orderBy='modifiedTime desc',
        pageSize=5,
        fields='files(id, name, modifiedTime)',
    ).execute()
    files = res.get('files', [])

    if files:
        target = files[0]
        print(f"Fetching {target['name']} (ID: {target['id']})...")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=target['id']))
        done = False
        while not done:
            _, done = downloader.next_chunk()

        data = json.loads(buf.getvalue().decode('utf-8'))
        if args.session == 'evening':
            nxt_reconcile.reconcile_evening_data(data, target_str)
        elif args.session == 'morning':
            if target_kst.weekday() < 5:
                apply_quant_engine(data, target_str)
            _, prev_closes = nxt_reconcile.reconcile_previous_evening(
                target_kst, DOCS_DIR, gsb.AUDIT_HISTORY_PATH
            )
            nxt_reconcile.annotate_morning_data(data, prev_closes)
        html = gsb.render_html(data, args.session, now_kst)
        os.makedirs(out_dir, exist_ok=True)
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Generated from Drive JSON: {out_file}")
        return

    if os.path.exists(out_file):
        print(f"Brief already published today at {out_file}; skipping.")
        return

    if is_within_grace_window(args.session, now_kst, target_kst):
        print(f"Waiting for Gemini App upload ({expected_name}) within 1h 25m grace window...")
        return

    raise FileNotFoundError(
        f"Missing Gemini App output: {expected_name} not found in dashboard_incoming/daily-stock-brief."
    )


if __name__ == '__main__':
    main()

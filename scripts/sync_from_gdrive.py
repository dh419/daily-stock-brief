import os
import io
import sys
import json
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_stock_brief as gsb

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')
FOLDER_ID = '1yxtjIIWVriE4u5ikOG-fh2FQY5fh853_'  # dashboard_incoming/daily-stock-brief
KST = timezone(timedelta(hours=9))


def should_run_fallback(session: str, now_kst: datetime) -> bool:
    """Run Gemini API fallback if manual dispatch, past 85-min grace window (08:25/20:25 KST), or outside wait window."""
    if os.environ.get('FORCE_FALLBACK') == '1':
        return True
    hm = now_kst.hour * 60 + now_kst.minute
    if session == 'morning':
        # Wait 1h 25m for Gemini App between 07:00 (420) and 08:25 (505)
        return not (420 <= hm < 505)
    # Wait 1h 25m for Gemini App between 19:00 (1140) and 20:25 (1225)
    return not (1140 <= hm < 1225)


def run_gemini_fallback(session: str):
    print(f"Running Gemini API fallback for session='{session}'...")
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'generate_stock_brief.py')
    subprocess.run([sys.executable, script_path, '--session', session], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=['morning', 'evening'], default='morning')
    args = parser.parse_args()

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime('%Y-%m-%d')
    month_part = now_kst.strftime('%Y-%m')
    expected_name = f"{today_str}-stock-{args.session}.json"
    out_dir = os.path.join(DOCS_DIR, month_part)
    out_file = os.path.join(out_dir, f"{today_str}-{args.session}.html")

    sa_json = os.environ.get('GDRIVE_SA_KEY')
    target = None
    if sa_json:
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
    else:
        print("GDRIVE_SA_KEY missing; checking fallback...")

    if target:
        print(f"Fetching {target['name']} (ID: {target['id']})...")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=target['id']))
        done = False
        while not done:
            _, done = downloader.next_chunk()

        data = json.loads(buf.getvalue().decode('utf-8'))
        html = gsb.render_html(data, args.session, now_kst)
        os.makedirs(out_dir, exist_ok=True)
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Generated from Drive JSON: {out_file}")
        return

    print(f"Today's Drive JSON ({expected_name}) not found.")
    if os.path.exists(out_file):
        print(f"Brief already published today at {out_file}; skipping fallback.")
        return

    if should_run_fallback(args.session, now_kst):
        run_gemini_fallback(args.session)
    else:
        print("Within 1h25m Gemini App upload grace window; waiting before triggering fallback.")


if __name__ == '__main__':
    main()

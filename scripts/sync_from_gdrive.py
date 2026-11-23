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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')
FOLDER_ID = '1yxtjIIWVriE4u5ikOG-fh2FQY5fh853_'  # dashboard_incoming/daily-stock-brief
KST = timezone(timedelta(hours=9))


def is_within_grace_window(session: str, now_kst: datetime) -> bool:
    """Wait 1h 25m (07:00~08:25 / 20:00~21:25 KST) for Gemini App upload before failing."""
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        return False
    hm = now_kst.hour * 60 + now_kst.minute
    if session == 'morning':
        return 420 <= hm < 505
    return 1200 <= hm < 1285


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=['morning', 'evening'], default='morning')
    args = parser.parse_args()

    sa_json = os.environ.get('GDRIVE_SA_KEY')
    if not sa_json:
        raise ValueError("GDRIVE_SA_KEY environment variable is missing.")

    now_kst = datetime.now(KST)
    today_str = now_kst.strftime('%Y-%m-%d')
    month_part = now_kst.strftime('%Y-%m')
    expected_name = f"{today_str}-stock-{args.session}.json"
    out_dir = os.path.join(DOCS_DIR, month_part)
    out_file = os.path.join(out_dir, f"{today_str}-{args.session}.html")

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
        html = gsb.render_html(data, args.session, now_kst)
        os.makedirs(out_dir, exist_ok=True)
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"Generated from Drive JSON: {out_file}")
        return

    if os.path.exists(out_file):
        print(f"Brief already published today at {out_file}; skipping.")
        return

    if is_within_grace_window(args.session, now_kst):
        print(f"Waiting for Gemini App upload ({expected_name}) within 1h 25m grace window...")
        return

    raise FileNotFoundError(
        f"Missing Gemini App output: {expected_name} not found in dashboard_incoming/daily-stock-brief."
    )


if __name__ == '__main__':
    main()

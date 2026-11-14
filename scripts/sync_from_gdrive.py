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
FOLDER_ID = '1jQIwN8b4SG536ClUWPAg3qUjo4GTNrJz'
KST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', choices=['morning', 'evening'], default='morning')
    args = parser.parse_args()

    sa_json = os.environ.get('GDRIVE_SA_KEY')
    if not sa_json:
        raise ValueError("GDRIVE_SA_KEY environment variable is missing.")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=['https://www.googleapis.com/auth/drive.readonly'],
    )
    service = build('drive', 'v3', credentials=creds)

    suffix = f"-stock-{args.session}.json"
    res = service.files().list(
        q=f"'{FOLDER_ID}' in parents and name contains '{suffix}' and trashed = false",
        orderBy='modifiedTime desc',
        pageSize=5,
        fields='files(id, name, modifiedTime)',
    ).execute()
    files = res.get('files', [])
    if not files:
        raise FileNotFoundError(f"No *{suffix} found in dashboard_incoming.")

    target = files[0]
    print(f"Fetching {target['name']} (ID: {target['id']})...")
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, service.files().get_media(fileId=target['id']))
    done = False
    while not done:
        _, done = downloader.next_chunk()

    data = json.loads(buf.getvalue().decode('utf-8'))
    now_kst = datetime.now(KST)
    date_part = target['name'].replace(suffix, '')
    month_part = date_part[:7]

    html = gsb.render_html(data, args.session, now_kst)
    out_dir = os.path.join(DOCS_DIR, month_part)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"{date_part}-{args.session}.html")
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Generated: {out_file}")


if __name__ == '__main__':
    main()

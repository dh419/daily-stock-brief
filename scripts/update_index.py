import os
import re
import json
import glob
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, 'docs')
HUB_DATA_PATH = os.path.join(REPO_ROOT, 'hub-data.json')
ROBOTS_TXT_PATH = os.path.join(REPO_ROOT, 'robots.txt')
ROBOTS_META_TAG = '<meta name="robots" content="noindex, nofollow" />'


def ensure_noindex_in_file(html_path):
    if not os.path.exists(html_path):
        return
    with open(html_path, 'r', encoding='utf-8') as f:
        text = f.read()
    if re.search(r'<meta\s+name=["\']robots["\']', text, re.IGNORECASE):
        return
    updated, n = re.subn(
        r'(<head[^>]*>)',
        rf'\1\n  {ROBOTS_META_TAG}',
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if n > 0:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(updated)


def ensure_search_blocking():
    desired = "User-agent: *\nDisallow: /\n"
    if not os.path.exists(ROBOTS_TXT_PATH):
        with open(ROBOTS_TXT_PATH, 'w', encoding='utf-8') as f:
            f.write(desired)
    ensure_noindex_in_file(os.path.join(REPO_ROOT, 'index.html'))
    for filepath in glob.glob(os.path.join(DOCS_DIR, '**', '*.html'), recursive=True):
        ensure_noindex_in_file(filepath)

def update_hub_index():
    reports = []
    
    # Search all html files in docs/YYYY-MM/YYYY-MM-DD-(morning|evening).html
    pattern = os.path.join(DOCS_DIR, '**', '*.html')
    for filepath in glob.glob(pattern, recursive=True):
        filename = os.path.basename(filepath)
        # Match YYYY-MM-DD-(morning|evening).html
        match = re.match(r'^(\d{4}-\d{2}-\d{2})-(morning|evening)\.html$', filename)
        if not match:
            continue
            
        date_str = match.group(1)
        session = match.group(2)
        rel_path = os.path.relpath(filepath, REPO_ROOT).replace('\\', '/')
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Extract title from <h1> or <title>
        title_match = re.search(r'<h1>(.*?)</h1>', content)
        title = title_match.group(1).strip() if title_match else f"{date_str} {session.capitalize()} Brief"
        
        # Clean tags if any
        title = re.sub(r'<.*?>', '', title)
        
        reports.append({
            'id': f"{date_str}-{session}",
            'date': date_str,
            'session': session,
            'title': title,
            'path': rel_path,
            'timestamp': f"{date_str} {'07:30' if session == 'morning' else '18:00'}"
        })
        
    # Sort descending by id (e.g. 2026-11-13-morning, 2026-11-12-evening, etc.)
    reports.sort(key=lambda x: (x['date'], 1 if x['session'] == 'evening' else 0), reverse=True)
    
    with open(HUB_DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)
        
    print(f"Successfully indexed {len(reports)} reports into hub-data.json")
    ensure_search_blocking()

if __name__ == '__main__':
    update_hub_index()

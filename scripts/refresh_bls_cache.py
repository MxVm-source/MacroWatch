"""Mirror only public official BLS inputs for the worker's network fallback."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.modules import jj_sources as src


def main():
    path = Path('data/bls-official-cache.json')
    payload = json.loads(path.read_text()) if path.exists() else {'sources': {}}
    now = datetime.now(timezone.utc)
    failures = []
    sources = {'BLS schedule': src.BLS_CALENDAR,
               **{name: url for name, url in src.FEEDS.items() if name.startswith('BLS ')}}
    for name, url in sources.items():
        for attempt in range(3):
            try:
                response = requests.get(url, timeout=15, headers={'User-Agent': 'MacroWatch-JJ/1.0'})
                response.raise_for_status()
                response.encoding = 'utf-8'
                body = response.text
                parsed = src.parse_bls_calendar(body) if name == 'BLS schedule' else src.parse_feed(body, name)
                if not parsed:
                    raise ValueError('Empty/unusable official input')
                previous = payload['sources'].get(name, {})
                last = datetime.fromisoformat(previous['fetched_at']) if previous.get('fetched_at') else None
                # Commit changed publications immediately; keep unchanged bodies freshly
                # verified hourly rather than creating a commit every five minutes.
                if previous.get('body') != body or not last or now - last >= timedelta(hours=1):
                    payload['sources'][name] = {'url': url, 'fetched_at': now.isoformat(), 'body': body}
                print(f'{name}: official input verified, items={len(parsed)}')
                break
            except Exception as exc:
                if attempt == 2:
                    failures.append(name)
                    print(f'{name}: unavailable ({type(exc).__name__}); old data NOT re-timestamped')
                else:
                    time.sleep(2)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    if len(failures) == len(sources):
        raise SystemExit('All official BLS fetches failed')


if __name__ == '__main__':
    main()

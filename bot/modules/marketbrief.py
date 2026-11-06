"""Small traditional-market closing recap; never invent market moves or causes."""
import logging
import math
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bot.modules import jj_sources as src

log = logging.getLogger('marketbrief')
SYMBOLS = {'Nasdaq Composite': '^IXIC', 'S&P 500': '^GSPC'}


def parse_close(payload, day, now):
    closing = src.session_close(day)
    if not closing or now < closing:
        return None
    try:
        result = payload['chart']['result'][0]
        times = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
        bars = [(datetime.fromtimestamp(ts, timezone.utc).astimezone(src.NEW_YORK).date(), value)
                for ts, value in zip(times, closes)]
        matches = [(d, v) for d, v in bars if d <= day]
        if len(matches) < 2 or matches[-1][0] != day:
            return None
        value, prior = float(matches[-1][1]), float(matches[-2][1])
        if not (math.isfinite(value) and math.isfinite(prior) and value > 0 and prior > 0):
            return None
        # Prior must be the previous exchange session, not a missing week-old bar.
        previous = src._exchange().previous_session(str(day)).date()
        if matches[-2][0] != previous:
            return None
        return value, round((value / prior - 1) * 100, 2)
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


def fetch_close(symbol, day, now):
    try:
        response = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe=''),
            params={'interval': '1d', 'range': '5d'}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        response.raise_for_status()
        return parse_close(response.json(), day, now)
    except Exception as exc:
        log.warning('Close quote unavailable symbol=%s error_type=%s', symbol, type(exc).__name__)
        return None


def build_close(worker, day, now):
    closing = src.session_close(day)
    if not closing:
        return f'Marché US fermé le {day} : aucun bilan de séance.'
    lines = [f'🌙 MacroWatch — bilan US du {day}',
             'Clôture cash : ' + closing.astimezone(src.BRUSSELS).strftime('%H:%M %Z')]
    for name, symbol in SYMBOLS.items():
        data = fetch_close(symbol, day, now)
        lines.append(f'{name} : {data[0]:,.2f} ({data[1]:+.2f} %)' if data else f'{name} : indisponible')
    items = [item for item in worker.releases
             if item['published'] <= now and item['published'].astimezone(src.NEW_YORK).date() == day]
    for item in sorted(items, key=lambda x: x['published'], reverse=True)[:2]:
        lines += ['Publication officielle : ' + item['title'][:160], item['url']]
    if not items:
        lines.append('Publications officielles : aucune disponible pour cette séance dans les flux suivis.')
    unavailable = [name for name, ok in worker.health.items() if not ok]
    if unavailable:
        lines.append('Sources indisponibles : ' + ', '.join(unavailable))
    lines += ['Cours indicatifs Yahoo Finance, variation contre la clôture précédente.',
              'https://finance.yahoo.com/quote/%5EIXIC/ | https://finance.yahoo.com/quote/%5EGSPC/',
              'Relevé : ' + now.astimezone(src.BRUSSELS).strftime('%d/%m %H:%M %Z'),
              'Aucun lien de causalité automatique entre une nouvelle et la variation des indices.']
    return '\n'.join(lines)[:4000]

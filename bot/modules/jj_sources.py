"""Public, attributed inputs for JJ. No AI numbers or inferred actuals."""
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

UTC = timezone.utc
NEW_YORK = ZoneInfo('America/New_York')
BRUSSELS = ZoneInfo('Europe/Brussels')
CALENDAR_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
BLS_CALENDAR = 'https://www.bls.gov/schedule/news_release/bls.ics'
FEEDS = {
    'BLS employment': 'https://www.bls.gov/feed/empsit.rss',
    'BLS CPI': 'https://www.bls.gov/feed/cpi.rss',
    'BLS PPI': 'https://www.bls.gov/feed/ppi.rss',
    'BEA': 'https://apps.bea.gov/rss/rss.xml',
    'Fed': 'https://www.federalreserve.gov/feeds/press_monetary.xml',
}


def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', str(value or '')))).strip()


def group(title):
    title = title.lower()
    if re.search(r'\badp\b', title):
        return 'jobs'
    for name, pattern in [
        ('employment', r'non.?farm|payroll|unemployment rate|hourly earnings|employment situation'),
        ('cpi', r'\bcpi\b|consumer price index'),
        ('ppi', r'\bppi\b|producer price index'),
        ('pce', r'\bpce\b|personal (?:income|consumption|spending)'),
        ('gdp', r'\bgdp\b|gross domestic product'),
        ('fed', r'fomc|fed chair|federal funds|federal reserve|powell'),
        ('jobs', r'job openings|jolts|jobless|unemployment claims|adp'),
        ('activity', r'retail sales|\bism\b|\bpmi\b|consumer confidence|consumer sentiment|inflation expectations'),
    ]:
        if re.search(pattern, title):
            return name
    return None


def normalize_calendar(rows):
    if not isinstance(rows, list):
        raise ValueError('Calendar must be a list')
    events = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('country') != 'USD':
            continue
        title = clean(row.get('title'))[:160]
        category = group(title)
        impact = row.get('impact')
        if impact not in ('High', 'Medium') or not category:
            continue
        try:
            start = datetime.fromisoformat(row['date'].replace('Z', '+00:00'))
            if start.tzinfo is None:
                continue
            start = start.astimezone(UTC)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        key = f'{title}|{start.isoformat()}'
        events[key] = dict(id=key, title=title, start=start, group=category,
                           priority='essentielle' if category in ('cpi', 'employment', 'fed', 'pce')
                           else 'importante', forecast=clean(row.get('forecast'))[:40],
                           previous=clean(row.get('previous'))[:40],
                           consensus_source=CALENDAR_URL, schedule_source=CALENDAR_URL)
    return sorted(events.values(), key=lambda e: (e['start'], e['title']))


def parse_bls_calendar(text):
    if 'BEGIN:VCALENDAR' not in text:
        raise ValueError('Invalid official calendar')
    text = re.sub(r'\r?\n[ \t]', '', text)
    events = []
    for block in text.split('BEGIN:VEVENT')[1:]:
        title = re.search(r'^SUMMARY:(.+)', block, re.M)
        start = re.search(r'^DTSTART(?:;TZID=([^:\r\n]+))?:(\d{8}T\d{6}Z?)', block, re.M)
        if not title or not start:
            continue
        title = title.group(1).strip()
        category = group(title)
        if category not in ('employment', 'cpi', 'ppi', 'jobs'):
            continue
        try:
            tz = UTC if start.group(2).endswith('Z') else NEW_YORK
            when = datetime.strptime(start.group(2).rstrip('Z'), '%Y%m%dT%H%M%S').replace(tzinfo=tz)
        except ValueError:
            continue
        events.append(dict(id=f'BLS:{title}|{when.isoformat()}', title=title,
                           start=when.astimezone(UTC), group=category, forecast='', previous='',
                           consensus_source='', schedule_source=BLS_CALENDAR,
                           priority='essentielle' if category in ('employment', 'cpi') else 'importante'))
    return events


def merge_official(events, official):
    """Official dates win. Do not move a forecast onto a different release date."""
    result = []
    used = set()
    for event in events:
        candidates = [e for e in official if e['group'] == event['group']
                      and abs(e['start'] - event['start']) <= timedelta(days=7)
                      and (event['group'] != 'jobs' or re.search(r'jolts|job openings', event['title'], re.I))]
        if not candidates:
            result.append(event)
            continue
        candidate = min(candidates, key=lambda e: abs(e['start'] - event['start']))
        if candidate['start'] == event['start']:
            result.append({**event, 'schedule_source': BLS_CALENDAR})
            used.add(candidate['id'])
        # A conflicting row is dropped; its official event is added below.
    for event in official:
        if event['id'] not in used:
            result.append(event)
    return sorted(result, key=lambda e: (e['start'], e['title']))


def _item_date(raw):
    try:
        result = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try:
            result = parsedate_to_datetime(raw)
        except (ValueError, TypeError, IndexError):
            return None
    return result.astimezone(UTC) if result.tzinfo else None


def parse_feed(text, source):
    text = text.removeprefix('ï»¿').lstrip('\ufeff')
    root = ET.fromstring(text)
    items = []
    nodes = root.findall('.//item') + root.findall('{*}entry')
    for node in nodes:
        fields = {child.tag.rsplit('}', 1)[-1]: child for child in node}
        def value(key):
            child = fields.get(key)
            return ''.join(child.itertext()).strip() if child is not None else ''
        link = fields.get('link')
        url = (link.get('href') or value('link')) if link is not None else ''
        published = _item_date(value('published') or value('pubDate') or value('updated'))
        if not published or urlparse(url).scheme != 'https':
            continue
        items.append(dict(title=clean(value('title')), body=clean(value('content') or value('description')),
                          published=published, url=url, source=source))
    return items


def _number(value):
    match = re.fullmatch(r'([+-]?[\d,]+(?:\.\d+)?)\s*(K|M|%|)', value.strip())
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(',', '')), match.group(2)
    except InvalidOperation:
        return None


def compare_values(actual, forecast):
    a, f = _number(actual), _number(forecast)
    if not a or not f or a[1] != f[1]:
        return None
    diff = a[0] - f[0]
    unit = ' point(s)' if a[1] == '%' else a[1]
    return f'{diff:+f}'.rstrip('0').rstrip('.') + unit if '.' in f'{diff:+f}' else f'{diff:+f}{unit}'


def _bls_actual(event, item):
    """Narrow, unit-specific extraction. Unrecognized wording stays unavailable."""
    title = event['title'].lower()
    text = item['body'] or item['title']
    if re.search(r'non.?farm|employment situation', title):
        match = re.search(r'(?:nonfarm payroll employment|payroll employment)\s+'
                          r'(rose|increased|fell|declined|decreased)(?:\s+by)?\s+([\d,]+)', text, re.I)
        if match:
            value = Decimal(match.group(2).replace(',', '')) / 1000
            if match.group(1).lower() in ('fell', 'declined', 'decreased'):
                value = -value
            return f'{value:g}K', 'K'
    if title == 'unemployment rate':
        match = re.search(r'unemployment rate[^.;]{0,65}?\b(?:at|was|to)\s+([\d.]+)\s*(?:percent|%)', text, re.I)
        if match:
            return match.group(1) + '%', '%'
    # Only headline CPI m/m or y/y: core and wage metrics must not reuse headline values.
    if title in ('cpi m/m', 'cpi y/y'):
        text = re.split(r'(?:the )?index for all items less|all items less food|excluding food and energy|\bcore\b',
                        text, maxsplit=1, flags=re.I)[0]
        pattern = (r'(rose|increased|fell|declined|decreased)\s+([\d.]+)\s+percent,?\s+seasonally'
                   if title.endswith('m/m') else
                   r'(rose|increased|fell|declined|decreased)\s+([\d.]+)\s+percent\s+over the year')
        match = re.search(pattern, text, re.I)
        if match:
            sign = '-' if match.group(1).lower() in ('fell', 'declined', 'decreased') else ''
            return sign + match.group(2) + '%', '%'
    return '', ''


def match_release(event, items, now=None):
    hosts = {'employment': 'bls.gov', 'cpi': 'bls.gov', 'ppi': 'bls.gov',
             'gdp': 'bea.gov', 'pce': 'bea.gov', 'fed': 'federalreserve.gov'}
    expected = hosts.get(event['group'])
    if not expected:
        return None
    for item in items:
        host = (urlparse(item['url']).hostname or '').lower()
        if host != expected and not host.endswith('.' + expected):
            continue
        if item['published'].astimezone(NEW_YORK).date() != event['start'].astimezone(NEW_YORK).date():
            continue
        if now is not None and item['published'] > now:
            continue
        if expected == 'bls.gov':
            archive = re.search(r'/(empsit|cpi|ppi)_(\d{8})\.htm', item['url'])
            if not archive:
                continue
            expected_prefix = {'employment': 'empsit', 'cpi': 'cpi', 'ppi': 'ppi'}[event['group']]
            if (archive.group(1) != expected_prefix or archive.group(2) !=
                    event['start'].astimezone(NEW_YORK).strftime('%m%d%Y')):
                continue
        if group(item['title'] + ' ' + item['body'][:300]) != event['group']:
            continue
        if event['group'] == 'gdp':
            headline = item['title'].lower()
            if re.search(r'annual update|by state|by county|by industry|regional', headline):
                continue
            stages = {'advance': r'advance estimate|first estimate',
                      'prelim': r'second estimate', 'final': r'third estimate'}
            expected_stage = next((pattern for word, pattern in stages.items()
                                   if word in event['title'].lower()), None)
            if expected_stage and not re.search(expected_stage, headline):
                continue
        # A policy statement does not confirm a press conference, speech or minutes.
        if event['group'] == 'fed' and not (
            re.search(r'fomc statement|federal funds rate', event['title'], re.I)
            and 'statement' in item['title'].lower()
        ):
            continue
        actual, unit = _bls_actual(event, item) if expected == 'bls.gov' else ('', '')
        return {**item, 'actual': actual, 'unit': unit}
    return None


@lru_cache(maxsize=1)
def _exchange():
    return xcals.get_calendar('XNYS')


def session_open(day):
    calendar = _exchange()
    if not calendar.is_session(str(day)):
        return None
    return calendar.session_open(str(day)).to_pydatetime().astimezone(UTC)


def session_close(day):
    calendar = _exchange()
    if not calendar.is_session(str(day)):
        return None
    return calendar.session_close(str(day)).to_pydatetime().astimezone(UTC)


def relevance_note(event):
    return {
        'employment': 'Emploi, salaires et chômage : surveiller ensemble les composantes et les révisions.',
        'cpi': 'Inflation : distinguer global et sous-jacent ; surveiller la réaction des taux US.',
        'ppi': 'Prix à la production : surveiller les composantes, sans les assimiler au CPI.',
        'pce': 'Inflation et consommation : surveiller le sous-jacent et les attentes de taux.',
        'gdp': 'Croissance : surveiller la composition et les révisions.',
        'fed': 'Politique monétaire : distinguer décision, communiqué et propos du président.',
        'jobs': 'Marché du travail : contexte pour les attentes de croissance et de taux.',
        'activity': 'Activité et demande : examiner les détails et les autres publications simultanées.',
    }[event['group']]

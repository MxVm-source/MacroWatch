"""Private US-session macro briefings. Deterministic, sourced and restart-safe."""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bot.modules import jj_sources as src
from bot.utils import send_text

log = logging.getLogger('jjwatch')
UTC = timezone.utc
VERSION = '2026-11-06-jj-v1'


def enabled():
    return os.getenv('ENABLE_JJWATCH', 'true').lower() in ('true', '1', 'yes', 'on')


def send_private(text):
    chat = os.getenv('CHAT_ID', '').strip()
    if not chat or chat == os.getenv('PUBLIC_CHAT_ID', '').strip():
        log.error('JJ delivery blocked: private destination missing or identical to public destination')
        return False
    return send_text(text, parse_mode=None)


class Engine:
    def __init__(self, directory, sender=None):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.db = str(Path(directory) / 'jjwatch.sqlite3')
        self.sender = sender or send_private
        self.lock = threading.RLock()
        self.events = []
        self.releases = []
        self.health = {}
        self.calendar_checked = self.calendar_success = None
        self.coverage = None
        self.attempts = {}
        self.baseline = None
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS cache (name TEXT PRIMARY KEY, body TEXT NOT NULL, at TEXT NOT NULL)')
        log.info('JJ ready version=%s persistent_dedup=true private_only=true', VERSION)

    def _source(self, name, url, parser, now, ttl, max_age=timedelta(hours=12)):
        with sqlite3.connect(self.db) as conn:
            saved = conn.execute('SELECT body, at FROM cache WHERE name=?', (name,)).fetchone()
        fetched = datetime.fromisoformat(saved[1]) if saved else None
        body = saved[0] if saved else None
        retry_delay = min(ttl, timedelta(minutes=5)) if self.health.get(name, True) else timedelta(minutes=5)
        retry_due = now - self.attempts.get(name, datetime.min.replace(tzinfo=UTC)) >= retry_delay
        if (not fetched or now - fetched >= ttl) and retry_due:
            self.attempts[name] = now
            try:
                response = requests.get(url, timeout=10, headers={'User-Agent': 'MacroWatch-JJ/1.0'})
                response.raise_for_status()
                response.encoding = 'utf-8'
                parsed = parser(response.text)
                body, fetched = response.text, now
                with sqlite3.connect(self.db) as conn:
                    conn.execute('INSERT OR REPLACE INTO cache VALUES (?, ?, ?)', (name, body, now.isoformat()))
                self.health[name] = True
                log.info('JJ source=%s ok=true items=%d', name, len(parsed))
                return parsed, fetched
            except Exception as exc:
                # Avoid URLs/errors containing user credentials or message bodies.
                log.warning('JJ source=%s ok=false error_type=%s', name, type(exc).__name__)
                self.health[name] = False
        elif name not in self.health:
            self.health[name] = bool(fetched and now - fetched < ttl)
        if not fetched or now - fetched > max_age:
            return [], None
        try:
            return parser(body), fetched
        except Exception:
            self.health[name] = False
            return [], None

    def refresh(self, now):
        def calendar_parser(text):
            rows = json.loads(text)
            events = src.normalize_calendar(rows)
            # A valid JSON error/empty response must not imply a quiet session.
            if not rows or not events:
                raise ValueError('Empty/unusable calendar')
            return events
        events, fetched = self._source('calendar', src.CALENDAR_URL, calendar_parser, now,
                                      timedelta(hours=1))
        self.calendar_checked = now
        self.calendar_success = fetched
        self.coverage = None
        if events:
            # The export contains one Sunday–Saturday week. Never assume next-week coverage.
            dates = [e['start'].astimezone(src.NEW_YORK).date() for e in events]
            anchor = max(dates)
            sunday = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
            self.coverage = (sunday, sunday + timedelta(days=6))
        official, _ = self._source('BLS schedule', src.BLS_CALENDAR, src.parse_bls_calendar,
                                   now, timedelta(hours=6), timedelta(days=2))
        merged = src.merge_official(events, official)
        self.events = [e for e in merged if now - timedelta(hours=12) <= e['start'] <= now + timedelta(days=8)]
        # Preserve the calendar interface consumed by existing morning/weekly briefs.
        from bot.modules import fedwatch
        categories = {'employment': 'NFP', 'cpi': 'CPI', 'ppi': 'PPI', 'fed': 'FOMC'}
        fedwatch.STATE.update(events=[{**e, 'category': categories.get(e['group'], 'OTHER')}
                                     for e in self.events],
                              source_ok=bool(fetched and self.health.get('calendar')),
                              last_refresh=fetched, alert_queue=[])
        active = any(timedelta(minutes=-2) <= now - e['start'] <= timedelta(minutes=90)
                     for e in self.events)
        # Release polling accelerates around events; no load on calendar publisher every minute.
        ttl = timedelta(minutes=1 if active else 15)
        self.releases = []
        for name, url in src.FEEDS.items():
            feed_ttl = timedelta(minutes=3) if name == 'Fed' and 6 <= now.astimezone(src.NEW_YORK).hour < 10 else ttl
            items, _ = self._source(name, url, lambda text, n=name: src.parse_feed(text, n), now, feed_ttl,
                                    timedelta(hours=2))
            self.releases.extend(items)
        with sqlite3.connect(self.db) as conn:
            conn.execute('DELETE FROM sent WHERE at < ?', ((now - timedelta(days=30)).isoformat(),))

    def deliver(self, key, text, now):
        with self.lock:
            with sqlite3.connect(self.db) as conn:
                if conn.execute('SELECT 1 FROM sent WHERE key=?', (key,)).fetchone():
                    return False
                if not self.sender(text):
                    log.warning('JJ delivery failed kind=%s', key.split(':', 1)[0])
                    return False
                conn.execute('INSERT INTO sent VALUES (?, ?)', (key, now.isoformat()))
            log.info('JJ delivered kind=%s', key.split(':', 1)[0])
            return True

    def complete_calendar(self, day, now):
        return bool(self.calendar_success and now - self.calendar_success <= timedelta(hours=6)
                    and self.health.get('calendar') and self.coverage
                    and self.coverage[0] <= day <= self.coverage[1])

    def _event_lines(self, event, now, include_note=True):
        when = event['start'].astimezone(src.BRUSSELS).strftime('%H:%M %Z')
        forecast = self._forecast(event, now) or 'consensus indisponible'
        previous = event.get('previous') or 'indisponible'
        lines = [f"• {when} — {event['title']} ({event['priority']})",
                 f'  Attendu : {forecast} | Précédent : {previous}']
        if event['start'] <= now:
            result = src.match_release(event, self.releases, now=now)
            lines.append('  Réel : ' + (result['actual'] if result and result['actual'] else 'indisponible'))
        if include_note:
            lines.append('  JJ : ' + src.relevance_note(event))
        return lines

    def _forecast(self, event, now):
        day = event['start'].astimezone(src.NEW_YORK).date()
        return event.get('forecast', '') if self.complete_calendar(day, now) else ''

    def brief(self, day, label, now):
        opening = src.session_open(day)
        lines = [f'🚦 JJ — {label} — {day}',
                 'Ouverture US : ' + (opening.astimezone(src.BRUSSELS).strftime('%H:%M %Z')
                                     if opening else 'marché cash fermé'), '']
        events = [e for e in self.events if e['start'].astimezone(src.NEW_YORK).date() == day]
        complete = self.complete_calendar(day, now)
        if not complete:
            lines.append('⚠️ Couverture incomplète : calendrier absent, périmé ou hors de la semaine disponible.')
        if not self.health.get('BLS schedule'):
            lines.append('⚠️ Horaires BLS non revérifiés ; consulter la source officielle.')
        unavailable = [name for name in src.FEEDS if not self.health.get(name, True)]
        if unavailable:
            lines.append('⚠️ Sources de résultats indisponibles : ' + ', '.join(unavailable))
        if events:
            lines.append('Vigilance : publication(s) à surveiller ; vérifier les annonces simultanées.')
            for event in events[:7]:
                lines.extend(self._event_lines(event, now, include_note=False))
            if len(events) > 7:
                lines.append(f'… {len(events) - 7} autre(s) publication(s) : voir le calendrier source.')
        elif complete:
            lines.append('Aucune annonce majeure identifiée dans le calendrier suivi. Surveiller les nouvelles imprévues.')
        else:
            lines.append('Impossible de conclure que la séance sera calme.')
        recent = [r for r in self.releases if r['published'].astimezone(src.NEW_YORK).date() == day
                  and timedelta(0) <= now - r['published'] <= timedelta(hours=12)
                  and src.group(r['title']) in ('fed', 'gdp', 'pce')]
        for item in recent[:2]:
            lines.extend(['', 'Publication officielle : ' + item['title'][:150], item['url']])
        lines += ['', 'Attentes : Forex Factory / Fair Economy (source secondaire).',
                  src.CALENDAR_URL, 'Horaires BLS : ' + src.BLS_CALENDAR,
                  'Résultats : BLS / BEA / Fed selon disponibilité. Révisions : consulter la publication.',
                  'Couverture limitée : pas de veille exhaustive des entreprises ou de la géopolitique.',
                  'Réaction Nasdaq/taux/dollar : non mesurée par ce suivi. Aucun signal automatique.']
        if self.calendar_success:
            lines.append('Calendrier consulté : ' + self.calendar_success.astimezone(src.BRUSSELS).strftime('%d/%m %H:%M %Z'))
        return '\n'.join(lines)[:4000]

    def tick(self, now):
        if self.baseline is None:
            self.baseline = now
        local = now.astimezone(src.BRUSSELS)
        us_day = now.astimezone(src.NEW_YORK).date()
        # Five-minute grace for scheduled reports, no replay of hours-old alerts on startup.
        if local.weekday() in (6, 0, 1, 2, 3) and local.hour == 19 and local.minute < 5:
            target = local.date() + timedelta(days=1)
            self.deliver('evening:' + str(target), self.brief(target, 'Veille', now), now)
        opening = src.session_open(us_day)
        if opening and timedelta(0) <= now - (opening - timedelta(minutes=15)) < timedelta(minutes=5):
            self.deliver('preopen:' + str(us_day), self.brief(us_day, 'Préouverture', now), now)
        groups = {}
        for event in self.events:
            groups.setdefault((event['group'], event['start']), []).append(event)
        for (category, start), events in groups.items():
            key = f'{category}|{start.isoformat()}'
            until = start - now
            if timedelta(minutes=27) < until <= timedelta(minutes=30):
                lines = ['⏳ JJ — annonce dans environ 30 min']
                for event in events:
                    lines.extend(self._event_lines(event, now))
                lines += ['Attentes : ' + src.CALENDAR_URL,
                          'Horaire : ' + events[0]['schedule_source'],
                          'Une surprise ne détermine pas automatiquement le sens du Nasdaq.']
                self.deliver('reminder:' + key, '\n'.join(lines)[:4000], now)
            elif timedelta(minutes=-90) <= until <= timedelta(0):
                if start < self.baseline - timedelta(minutes=2):
                    continue
                results = [(e, src.match_release(e, self.releases, now=now)) for e in events]
                confirmed = [(e, r) for e, r in results if r]
                if not confirmed:
                    continue
                lines = ['📣 JJ — publication officielle confirmée', start.astimezone(src.BRUSSELS).strftime('%d/%m %H:%M %Z')]
                for event, result in confirmed:
                    actual = result['actual'] or 'indisponible dans le flux'
                    forecast = self._forecast(event, now)
                    lines += [event['title'], f"Réel : {actual} | Attendu : {forecast or 'consensus indisponible'}"]
                    diff = src.compare_values(result['actual'], forecast)
                    if diff:
                        lines.append('Écart au consensus : ' + diff)
                first = confirmed[0][1]
                lines += ['Extrait officiel (EN) : ' + first['body'][:350], 'Source : ' + first['url'],
                          'Précédents et révisions : vérifier le texte et les tableaux de la publication.',
                          'Lecture directionnelle : indéterminée ; réaction Nasdaq/taux/dollar non mesurée.']
                self.deliver('release:' + key, '\n'.join(lines)[:4000], now)
        # Conservative exceptional-news scope: new monetary-policy statements only.
        if opening and 6 <= now.astimezone(src.NEW_YORK).hour < 10:
            for item in self.releases:
                if (item['source'] == 'Fed' and 'statement' in item['title'].lower()
                        and timedelta(0) <= now - item['published'] <= timedelta(minutes=5)
                        and item['published'] >= self.baseline
                        and not any(e['group'] == 'fed' and abs(e['start'] - item['published']) < timedelta(minutes=60)
                                    for e in self.events)):
                    self.deliver('news:' + item['url'], '⚡ JJ — Actualité monétaire officielle\n'
                                 + item['title'] + '\n' + item['url']
                                 + '\nImpact à vérifier sur les taux et le Nasdaq ; aucune direction automatique.', now)

    def poll(self, now=None):
        live = now is None
        now = now or datetime.now(UTC)
        with self.lock:
            self.refresh(now)
            now = datetime.now(UTC) if live else now
            self.tick(now)
            self.deliver('activation:' + VERSION,
                         '🚦 JJ — suivi activé dans ce groupe privé.\n'
                         'Veille à 19h Bruxelles, rappels T−30 min, publication officielle confirmée, '
                         'synthèse 15 min avant l’ouverture US.\n'
                         'Attentes sourcées ; chiffres manquants signalés. '
                         'Résultats live limités aux flux officiels disponibles ; '
                         'pas de mesure du Nasdaq/taux/dollar ni de couverture exhaustive des entreprises/géopolitique.\n'
                         'Commandes : /jj et /jj_diag.\n' + self.diagnostics(), now)

    def diagnostics(self):
        lines = [f'JJ {VERSION}', f'Événements chargés : {len(self.events)}',
                 'Destination : groupe privé configuré uniquement',
                 '19h Bruxelles veille | T−30m | publication confirmée | ouverture US−15m',
                 'Contrôle : 60s ; publication selon délai de la source ; pas de garantie instantanée.']
        lines.extend(f"{name} : {'OK' if ok else 'INDISPONIBLE / cache'}" for name, ok in self.health.items())
        return '\n'.join(lines)


_ENGINE = None
_LOCK = threading.Lock()


def engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            directory = os.getenv('JJ_STATE_DIR') or ('/var/data' if Path('/var/data').is_dir() else '/tmp/macrowatch-jj')
            _ENGINE = Engine(directory)
    return _ENGINE


def poll_once():
    if enabled():
        engine().poll()


def show_brief(diagnostic=False):
    worker = engine()
    now = datetime.now(UTC)
    with worker.lock:
        worker.refresh(now)
        text = worker.diagnostics() if diagnostic else worker.brief(now.astimezone(src.NEW_YORK).date(), 'À la demande', now)
        return send_private(text)

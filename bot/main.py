"""MacroWatch: private Nasdaq/JJ information, public strategy sharing stays manual.

Only three jobs remain: JJ session cycle, relevant macro statements, Monday
macro calendar. Retired crypto implementation files remain in git for reference;
none of the old position/challenge/report jobs is scheduled or command-accessible.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

from bot.utils import get_updates
from bot.modules import jjwatch, trumpwatch_live
from bot.modules.jjwatch import send_private

logging.getLogger('apscheduler').setLevel(logging.WARNING)
log = logging.getLogger('main')
STARTED_AT_UTC = datetime.now(timezone.utc)
SCHED = BackgroundScheduler(timezone='Europe/Brussels')


def _job_trumpwatch():
    try:
        trumpwatch_live.poll_once()
    except Exception as exc:
        log.error('TrumpWatch failed error_type=%s', type(exc).__name__)


def _job_jjwatch():
    try:
        jjwatch.poll_once()
    except Exception as exc:
        log.exception('JJ poll failed error_type=%s', type(exc).__name__)


def _job_weekly_brief():
    try:
        jjwatch.show_weekly()
    except Exception as exc:
        log.error('Weekly macro brief failed error_type=%s', type(exc).__name__)


def _on_job_error(event):
    log.error('Scheduler job=%s failed error_type=%s', event.job_id, type(event.exception).__name__)


def _cleanup_group_descriptions():
    from bot.group_maintenance import update_descriptions
    update_descriptions(jjwatch.engine())
    from bot.modules import jj_sources as src, marketbrief
    now = datetime.now(timezone.utc)
    day = now.astimezone(src.NEW_YORK).date()
    while not src.session_close(day) or src.session_close(day) > now:
        day -= timedelta(days=1)
    for name, symbol in marketbrief.SYMBOLS.items():
        available = marketbrief.fetch_close(symbol, day, now) is not None
        log.warning('Close source probe index=%s available=%s session=%s', name, available, day)


def start_scheduler():
    SCHED.add_listener(_on_job_error, EVENT_JOB_ERROR)
    if os.getenv('ENABLE_TRUMPWATCH_LIVE', 'true').lower() in ('1', 'true', 'yes', 'on'):
        SCHED.add_job(_job_trumpwatch, 'interval', seconds=60, id='trumpwatch',
                      max_instances=1, coalesce=True, misfire_grace_time=30)
    if jjwatch.enabled():
        SCHED.add_job(_job_jjwatch, 'interval', seconds=60, id='jjwatch',
                      max_instances=1, coalesce=True, misfire_grace_time=30)
        SCHED.add_job(_job_weekly_brief, 'cron', day_of_week='mon', hour=9, minute=0,
                      timezone='Europe/Brussels', id='weekly_brief', max_instances=1,
                      misfire_grace_time=300)
    SCHED.start()
    log.warning('MacroWatch cleanup-v2 running jobs=%s crypto_jobs=off public_posts=manual_only',
                ','.join(job.id for job in SCHED.get_jobs()))


def _build_health_msg():
    worker = jjwatch.engine()
    age = int((datetime.now(timezone.utc) - STARTED_AT_UTC).total_seconds())
    lines = ['🩺 MacroWatch — Nasdaq/JJ', f'En service depuis {age // 60} min',
             'Crypto : anciens envois et surveillance arrêtés',
             'INFINEX CAPITAL : publications manuelles uniquement', '', worker.diagnostics(), '',
             'Tâches programmées :']
    for job in SCHED.get_jobs():
        when = getattr(job, 'next_run_time', None)
        lines.append(job.id + ' : ' + (when.isoformat() if when else 'en attente'))
    return '\n'.join(lines)


def _handle_command(text, text_raw):
    command = text.split(None, 1)[0].split('@', 1)[0]
    if command in ('/help', '/start'):
        return send_private('🚦 MacroWatch — Nasdaq/JJ\n'
            '/jj — préparation de la session US\n'
            '/jj_diag — état des sources\n'
            '/evening — dernier bilan de clôture US\n'
            '/weekly — calendrier macro de la semaine\n'
            '/health — état du service\n'
            '/trumpwatch — vérifier les déclarations macro récentes\n'
            'Les anciennes fonctions crypto sont en pause. INFINEX CAPITAL reste manuel.')
    if command in ('/jj', '/fedwatch', '/morning'):
        return jjwatch.show_brief()
    if command in ('/jj_diag', '/fed_diag'):
        return jjwatch.show_brief(diagnostic=True)
    if command == '/weekly':
        return jjwatch.show_weekly()
    if command == '/evening':
        return jjwatch.show_close()
    if command == '/health':
        return send_private(_build_health_msg())
    if command == '/trumpwatch':
        _job_trumpwatch()
        return send_private('Vérification des déclarations terminée. Seules les nouveautés pertinentes sont envoyées.')
    if command in ('/status', '/bot_challenge', '/live_challenge', '/challenge',
                    '/report', '/intel', '/monthly_update', '/pulse', '/challenge_diag'):
        return send_private('Cette ancienne fonction crypto est en pause. Utilise /jj, /evening ou /weekly.')


def command_loop():
    offset = None
    while True:
        try:
            chat_allow = os.getenv('CHAT_ID', '').strip()
            for upd in get_updates(offset=offset, timeout=20).get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message') or {}
                chat = str((msg.get('chat') or {}).get('id') or '')
                # Commands are private-only and fail closed if no destination is configured.
                if not chat_allow or chat != chat_allow:
                    continue
                raw = (msg.get('text') or '').strip()
                if raw.startswith('/'):
                    _handle_command(raw.lower(), raw)
        except Exception as exc:
            log.error('Command loop failed error_type=%s', type(exc).__name__)
            time.sleep(3)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
    start_scheduler()
    threading.Thread(target=_cleanup_group_descriptions, daemon=True).start()
    threading.Thread(target=command_loop, daemon=True).start()
    while True:
        time.sleep(60)

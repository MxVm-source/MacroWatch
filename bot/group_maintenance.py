"""One-time description cleanup for the two user-identified Telegram groups."""
import logging
import os
import sqlite3
from datetime import datetime, timezone

import requests
from bot.utils import API_BASE, CHAT_ID

log = logging.getLogger('groups')
VERSION = 'cleanup-v2'
DESCRIPTIONS = {
    'private': 'Préparation Nasdaq/JJ : calendrier économique, annonces importantes et bilans de séance US. '
               'Les sources et données indisponibles sont signalées. Groupe privé de travail.',
    'public': 'INFINEX CAPITAL — Publications sélectionnées sur les stratégies de trading. '
              'Partages manuels ; aucun suivi crypto ou challenge automatique actif.',
}


def update_descriptions(worker):
    if not API_BASE or not CHAT_ID:
        return
    public_id = os.getenv('PUBLIC_CHAT_ID', '').strip()
    if not public_id:
        return
    with worker.lock, sqlite3.connect(worker.db) as conn:
        todo = [role for role in DESCRIPTIONS if not conn.execute(
            'SELECT 1 FROM sent WHERE key=?', ('description:' + VERSION + ':' + role,)).fetchone()]
        if not todo:
            return
        def call(method, data, role):
            response = requests.post(API_BASE + '/' + method, json=data, timeout=10)
            result = response.json()
            if not response.ok or not result.get('ok'):
                description = str(result.get('description', '')).lower()
                reason = next((tag.replace(' ', '_') for tag in (
                    'chat not found', 'not enough rights', 'not a member', 'was kicked', 'was blocked')
                    if tag in description), 'telegram_refused')
                log.warning('Group request refused method=%s role=%s status=%s reason=%s',
                            method, role, response.status_code, reason)
                return {'ok': False}
            return result
        try:
            resolved = {}
            for role, target in [('private', CHAT_ID), ('public', public_id)]:
                result = call('getChat', {'chat_id': target}, role)
                if result.get('ok'):
                    resolved[role] = result['result']
            if len(resolved) == 2 and resolved['private']['id'] == resolved['public']['id']:
                log.warning('Group description cleanup skipped: destinations identical')
                return
            private_title = resolved.get('private', {}).get('title', '').casefold()
            verified = {'private': 'infinex' in private_title and 'macro' in private_title,
                        'public': resolved.get('public', {}).get('username', '').casefold() == 'macrowatchalpha'}
            for role in todo:
                if not verified[role]:
                    log.warning('Group description cleanup skipped role=%s identity not verified', role)
                    continue
                chat = resolved[role]
                if chat.get('description') == DESCRIPTIONS[role]:
                    ok = True
                else:
                    ok = call('setChatDescription', {'chat_id': chat['id'], 'description': DESCRIPTIONS[role]}, role).get('ok')
                if ok:
                    conn.execute('INSERT OR REPLACE INTO sent VALUES (?, ?)',
                                 ('description:' + VERSION + ':' + role, datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                    log.warning('Group description updated role=%s identity_verified=true', role)
                else:
                    log.warning('Group description not updated role=%s Telegram refused', role)
        except Exception as exc:
            # Never expose token-bearing URLs in exception strings.
            log.warning('Group description cleanup pending error_type=%s', type(exc).__name__)

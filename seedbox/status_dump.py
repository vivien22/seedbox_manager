#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Exporte l'etat des torrents "vivien" dans status/seedbox.json pour le site web.
Lance a chaque tour de seedbox-loop.sh.
"""

import os
import sys
import json
import time
import glob
import tempfile

BASE = '/media/sdt1/bibien/private/seedbox-manager'
QUEUE = BASE + '/queue'
ORGANIZER_QUEUE = BASE + '/organizer_queue'
ORGANIZER_FAILED = BASE + '/organizer_failed'
SYNC_READY = BASE + '/sync/ready'
SYNC_PROCESSED = BASE + '/sync/processed'
STATUS_DIR = BASE + '/status'
STATUS_FILE = STATUS_DIR + '/seedbox.json'

AUTHFILE = '/media/sdt1/bibien/.config/deluge/auth'
HOST = '127.0.0.1'
PORT = 52940
LABEL = 'vivien'

MAX_FINISHED_SHOWN = 15
WATCHDOG_SECONDS = 60

KEYS = [
    'name', 'label', 'state', 'progress', 'is_finished', 'total_wanted',
    'total_done', 'download_payload_rate', 'upload_payload_rate', 'eta',
    'ratio', 'time_added', 'num_seeds', 'num_peers'
]

try:
    unicode
except NameError:
    unicode = str


def to_unicode(value):
    if isinstance(value, unicode):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return unicode(value)


def write_status(data):
    if not os.path.isdir(STATUS_DIR):
        os.makedirs(STATUS_DIR)
    data['updated_at'] = int(time.time())
    content = json.dumps(data, ensure_ascii=True, sort_keys=True)
    if not isinstance(content, bytes):
        content = content.encode('ascii')

    fd, temporary = tempfile.mkstemp(prefix='.seedbox-', dir=STATUS_DIR)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        os.rename(temporary, STATUS_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        with open(path, 'r') as stream:
            return json.load(stream)
    except (IOError, OSError, ValueError):
        return None


def read_text(path):
    try:
        with open(path, 'rb') as stream:
            return to_unicode(stream.read()).strip()
    except (IOError, OSError):
        return None


def stage_of(torrent_id, torrent):
    """Etape du torrent dans la chaine seedbox -> Pi."""
    if os.path.exists(os.path.join(ORGANIZER_FAILED, torrent_id + '.job')):
        reason = read_text(os.path.join(ORGANIZER_FAILED, torrent_id + '.reason'))
        return 'failed', reason

    if os.path.exists(os.path.join(SYNC_READY, torrent_id + '.json')):
        return 'ready_for_pi', None

    processed = os.path.join(SYNC_PROCESSED, torrent_id + '.json')
    if os.path.exists(processed):
        return 'on_pi', None

    if not torrent.get('is_finished'):
        return 'downloading', None

    if os.path.exists(os.path.join(ORGANIZER_QUEUE, torrent_id + '.job')):
        state = read_json(os.path.join(ORGANIZER_QUEUE, torrent_id + '.state')) or {}
        if state.get('errors'):
            return 'organizing', u'Erreur : ' + to_unicode(state['errors'].get('last', ''))
        if state.get('move'):
            return 'organizing', u'Déplacement en cours'
        if state.get('rename'):
            return 'organizing', u'Renommage en cours'
        return 'organizing', None

    return 'seeding', None


def manifest_name(torrent_id):
    for directory in (SYNC_READY, SYNC_PROCESSED):
        manifest = read_json(os.path.join(directory, torrent_id + '.json'))
        if manifest and manifest.get('name'):
            return manifest['name'], manifest.get('category')
    return None, None


def on_status(status):
    torrents = []

    for torrent_id, torrent in status.items():
        torrent_id = to_unicode(torrent_id).lower()
        if torrent.get('label') != LABEL:
            continue

        stage, detail = stage_of(torrent_id, torrent)
        title, category = manifest_name(torrent_id)

        processed = os.path.join(SYNC_PROCESSED, torrent_id + '.json')
        finished_at = int(os.path.getmtime(processed)) if os.path.exists(processed) else None

        torrents.append({
            'id': torrent_id,
            'name': to_unicode(torrent.get('name', '')),
            'title': title,
            'category': category,
            'state': to_unicode(torrent.get('state', '')),
            'stage': stage,
            'detail': detail,
            'progress': round(float(torrent.get('progress', 0)), 1),
            'size': int(torrent.get('total_wanted', 0)),
            'done': int(torrent.get('total_done', 0)),
            'down_rate': int(torrent.get('download_payload_rate', 0)),
            'up_rate': int(torrent.get('upload_payload_rate', 0)),
            'eta': int(torrent.get('eta', 0)),
            'ratio': round(float(torrent.get('ratio', 0)), 2),
            'seeds': int(torrent.get('num_seeds', 0)),
            'peers': int(torrent.get('num_peers', 0)),
            'added_at': int(torrent.get('time_added', 0)),
            'finished_at': finished_at,
        })

    active = [t for t in torrents if t['stage'] not in ('on_pi', 'seeding')]
    finished = [t for t in torrents if t['stage'] in ('on_pi', 'seeding')]

    active.sort(key=lambda t: t['added_at'], reverse=True)
    finished.sort(key=lambda t: t['finished_at'] or t['added_at'], reverse=True)

    write_status({
        'ok': True,
        'queue_pending': len(glob.glob(os.path.join(QUEUE, '*.torrent'))),
        'torrents': active + finished[:MAX_FINISHED_SHOWN],
    })
    stop()


def on_error(failure):
    write_status({
        'ok': False,
        'error': to_unicode(str(failure.getErrorMessage()
                                if hasattr(failure, 'getErrorMessage') else failure)),
        'queue_pending': len(glob.glob(os.path.join(QUEUE, '*.torrent'))),
        'torrents': [],
    })
    stop()


def on_connected(result):
    _, client = deluge()
    d = client.core.get_torrents_status({}, KEYS)
    d.addCallback(on_status)
    d.addErrback(on_error)


def deluge():
    from twisted.internet import reactor
    from deluge.ui.client import client
    return reactor, client


def stop():
    reactor, client = deluge()
    try:
        client.disconnect()
    except Exception:
        pass
    if reactor.running:
        reactor.callLater(0, reactor.stop)


def read_credentials():
    with open(AUTHFILE, 'r') as stream:
        for line in stream:
            parts = line.strip().split(':', 2)
            if len(parts) == 3 and parts[0] == 'localclient':
                return parts[0], parts[1]
    raise Exception('localclient credentials not found')


def watchdog():
    on_error(Exception('Deluge ne repond pas'))


def main():
    reactor, client = deluge()
    user, password = read_credentials()

    d = client.connect(HOST, PORT, user, password)
    d.addCallback(on_connected)
    d.addErrback(on_error)

    reactor.callLater(WATCHDOG_SECONDS, watchdog)
    reactor.run()


if __name__ == '__main__':
    main()

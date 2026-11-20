#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Execute les demandes deposees par le site web dans commands/ :
  <id>.remove  -> supprime le torrent ET ses donnees de la seedbox (Deluge).

La Raspberry Pi n'est jamais touchee : les fichiers deja transferes y restent.
Seuls les torrents portant le label "vivien" peuvent etre supprimes.
"""

import os
import sys
import json
import time
import tempfile

BASE = '/media/sdt1/bibien/private/seedbox-manager'
COMMANDS = BASE + '/commands'
REJECTED = COMMANDS + '/rejected'
ORGANIZER_QUEUE = BASE + '/organizer_queue'
ORGANIZER_FAILED = BASE + '/organizer_failed'
SYNC_READY = BASE + '/sync/ready'
SYNC_CANCELLED = BASE + '/sync/cancelled'

AUTHFILE = '/media/sdt1/bibien/.config/deluge/auth'
HOST = '127.0.0.1'
PORT = 52940
LABEL = 'vivien'

MAX_ATTEMPTS = 5
WATCHDOG_SECONDS = 120

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


def output(value):
    text = time.strftime('%Y-%m-%d %H:%M:%S ') + to_unicode(value) + u'\n'
    if sys.version_info[0] < 3:
        sys.stdout.write(text.encode('utf-8'))
    else:
        sys.stdout.write(text)
    sys.stdout.flush()


def valid_id(torrent_id):
    return len(torrent_id) == 40 and all(c in '0123456789abcdef' for c in torrent_id)


def pending_removals():
    if not os.path.isdir(COMMANDS):
        return []
    ids = []
    for name in sorted(os.listdir(COMMANDS)):
        if name.endswith('.remove') and valid_id(name[:-7]):
            ids.append(name[:-7])
    return ids


def command_path(torrent_id):
    return os.path.join(COMMANDS, torrent_id + '.remove')


def read_command(torrent_id):
    try:
        with open(command_path(torrent_id), 'r') as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def write_command(torrent_id, data):
    fd, temporary = tempfile.mkstemp(prefix='.cmd-', dir=COMMANDS)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.rename(temporary, command_path(torrent_id))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reject(torrent_id, reason):
    if not os.path.isdir(REJECTED):
        os.makedirs(REJECTED)
    data = read_command(torrent_id)
    data['rejected'] = to_unicode(reason)
    data['rejected_at'] = int(time.time())
    write_command(torrent_id, data)
    os.rename(command_path(torrent_id), os.path.join(REJECTED, torrent_id + '.remove'))
    output(u'[REFUSE] ' + torrent_id + u' : ' + to_unicode(reason))


def remove_if_exists(path):
    if os.path.exists(path):
        os.unlink(path)


def cleanup_pipeline(torrent_id):
    """Retire le torrent de la chaine d'organisation et de synchro.
    Un manifeste pas encore recupere par la Pi est mis de cote dans
    sync/cancelled (la Pi ne cherchera pas des fichiers supprimes)."""
    for directory in (ORGANIZER_QUEUE, ORGANIZER_FAILED):
        remove_if_exists(os.path.join(directory, torrent_id + '.job'))
        remove_if_exists(os.path.join(directory, torrent_id + '.state'))
        remove_if_exists(os.path.join(directory, torrent_id + '.reason'))

    manifest = os.path.join(SYNC_READY, torrent_id + '.json')
    if os.path.exists(manifest):
        if not os.path.isdir(SYNC_CANCELLED):
            os.makedirs(SYNC_CANCELLED)
        os.rename(manifest, os.path.join(SYNC_CANCELLED, torrent_id + '.json'))


# ------------------------------------------------------------------ Deluge

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


def process_next(status, ids):
    if not ids:
        stop()
        return

    _, client = deluge()
    torrent_id = ids[0]
    rest = ids[1:]
    torrent = status.get(torrent_id)

    if torrent is None:
        cleanup_pipeline(torrent_id)
        remove_if_exists(command_path(torrent_id))
        output(u'[DEJA ABSENT] ' + torrent_id)
        return process_next(status, rest)

    if torrent.get('label') != LABEL:
        reject(torrent_id, u'label différent de ' + LABEL)
        return process_next(status, rest)

    name = to_unicode(torrent.get('name', ''))
    cleanup_pipeline(torrent_id)

    def done(result):
        remove_if_exists(command_path(torrent_id))
        output(u'[SUPPRIME] ' + name + u' (' + torrent_id + u')')
        process_next(status, rest)

    def failed(failure):
        data = read_command(torrent_id)
        data['attempts'] = int(data.get('attempts', 0)) + 1
        data['last_error'] = to_unicode(str(failure.getErrorMessage()))
        write_command(torrent_id, data)
        output(u'[ERREUR] ' + name + u' : ' + data['last_error'])
        if data['attempts'] >= MAX_ATTEMPTS:
            reject(torrent_id, u'Deluge refuse la suppression : ' + data['last_error'])
        process_next(status, rest)

    # True = supprimer aussi les donnees telechargees
    d = client.core.remove_torrent(torrent_id, True)
    d.addCallbacks(done, failed)


def on_status(status):
    status = dict((to_unicode(k).lower(), v) for k, v in status.items())
    process_next(status, pending_removals())


def on_error(failure):
    output(u'ERREUR RPC : ' + to_unicode(str(failure)))
    stop()


def on_connected(result):
    _, client = deluge()
    d = client.core.get_torrents_status({}, ['name', 'label'])
    d.addCallback(on_status)
    d.addErrback(on_error)


def read_credentials():
    with open(AUTHFILE, 'r') as stream:
        for line in stream:
            parts = line.strip().split(':', 2)
            if len(parts) == 3 and parts[0] == 'localclient':
                return parts[0], parts[1]
    raise Exception('localclient credentials not found')


def watchdog():
    output(u'[ALERTE] Deluge ne repond pas')
    stop()


def main():
    if not pending_removals():
        return

    reactor, client = deluge()
    user, password = read_credentials()
    d = client.connect(HOST, PORT, user, password)
    d.addCallback(on_connected)
    d.addErrback(on_error)

    reactor.callLater(WATCHDOG_SECONDS, watchdog)
    reactor.run()


if __name__ == '__main__':
    main()

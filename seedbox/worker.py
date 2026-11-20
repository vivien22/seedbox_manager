#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Worker seedbox (v3) : prend le plus ancien .torrent de queue/, l'ajoute a
Deluge avec le label "vivien", puis cree la tache pour l'organiseur.
"""

import os
import sys
import glob
import base64
import logging
import fcntl
import tempfile

BASE = '/media/sdt1/bibien/private/seedbox-manager'
QUEUE = BASE + '/queue'
PROCESSED = BASE + '/processed'
FAILED = BASE + '/failed'
ORGANIZER_QUEUE = BASE + '/organizer_queue'
LOGFILE = BASE + '/logs/worker.log'
AUTHFILE = '/media/sdt1/bibien/.config/deluge/auth'

VIDEOS_DIR = '/media/sdt1/bibien/private/deluge/data/Vivien/Videos'

HOST = '127.0.0.1'
PORT = 52940
LABEL = 'vivien'
WATCHDOG_SECONDS = 120

logging.basicConfig(
    filename=LOGFILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

current_file = None
current_hash = None


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


def fail(error):
    logging.error('Worker error: %s', error)
    stop()
    return None


def move_file(source, destination_dir):
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)
    destination = os.path.join(destination_dir, os.path.basename(source))
    os.rename(source, destination)
    logging.info('Moved %s to %s', os.path.basename(source), destination_dir)


def enqueue_for_organizer(torrent_id):
    torrent_id = torrent_id.lower()
    if len(torrent_id) != 40 or not all(c in '0123456789abcdef' for c in torrent_id):
        raise Exception('Invalid torrent ID')

    if not os.path.isdir(ORGANIZER_QUEUE):
        os.makedirs(ORGANIZER_QUEUE)

    job_path = os.path.join(ORGANIZER_QUEUE, torrent_id + '.job')
    if os.path.exists(job_path):
        return

    fd, temporary = tempfile.mkstemp(prefix='.organizer-', dir=ORGANIZER_QUEUE)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(torrent_id + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, job_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def on_labeled(result):
    enqueue_for_organizer(current_hash)
    move_file(current_file, PROCESSED)
    stop()
    return result


def apply_label(torrent_id):
    _, client = deluge()
    logging.info('Applying label %s to %s', LABEL, torrent_id)
    d = client.label.set_torrent(torrent_id, LABEL)
    d.addCallback(on_labeled)
    d.addErrback(fail)
    return d


def on_added(torrent_id):
    if not torrent_id:
        # Deluge renvoie None si le torrent existe deja (ajout concurrent) :
        # on reutilise le hash calcule localement.
        logging.info('Deluge returned no ID, using local hash %s', current_hash)
        torrent_id = current_hash
    logging.info('Torrent added: %s', torrent_id)
    return apply_label(torrent_id)


def on_status(status):
    _, client = deluge()

    if current_hash in status:
        logging.info('Torrent already exists: %s', current_hash)
        return apply_label(current_hash)

    logging.info('Adding torrent: %s', os.path.basename(current_file))
    with open(current_file, 'rb') as stream:
        data = stream.read()

    options = {
        'download_location': VIDEOS_DIR,
        'add_paused': False,
    }

    d = client.core.add_torrent_file(
        os.path.basename(current_file),
        base64.b64encode(data),
        options
    )
    d.addCallback(on_added)
    d.addErrback(fail)
    return d


def on_labels(labels):
    _, client = deluge()
    if LABEL not in labels:
        raise Exception('Required Deluge label does not exist: ' + LABEL)

    d = client.core.get_torrents_status({}, ['name'])
    d.addCallback(on_status)
    d.addErrback(fail)
    return d


def on_connected(result):
    _, client = deluge()
    logging.info('Connected to Deluge RPC')
    d = client.label.get_labels()
    d.addCallback(on_labels)
    d.addErrback(fail)
    return result


def read_credentials():
    with open(AUTHFILE, 'r') as stream:
        for line in stream:
            parts = line.strip().split(':', 2)
            if len(parts) == 3 and parts[0] == 'localclient':
                return parts[0], parts[1]
    raise Exception('localclient credentials not found')


def watchdog():
    logging.error('Deluge did not answer within %d s, giving up', WATCHDOG_SECONDS)
    stop()


def main():
    global current_file, current_hash

    lockfile = open(BASE + '/worker.lock', 'w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logging.info('Another worker is already running')
        return

    files = sorted(glob.glob(os.path.join(QUEUE, '*.torrent')))
    if not files:
        return  # file vide : rien a journaliser toutes les 15 s

    if not os.path.isdir(VIDEOS_DIR):
        raise Exception('Videos directory does not exist')

    current_file = files[0]
    with open(current_file, 'rb') as stream:
        data = stream.read()

    try:
        import deluge.core.torrentmanager as torrentmanager
        torrent_info = torrentmanager.lt.torrent_info(torrentmanager.lt.bdecode(data))
        current_hash = str(torrent_info.info_hash()).lower()
    except Exception as exc:
        logging.exception('Invalid or unsupported torrent: %s', exc)
        move_file(current_file, FAILED)
        return

    username, password = read_credentials()
    logging.info('Processing %s', os.path.basename(current_file))

    reactor, client = deluge()
    d = client.connect(HOST, PORT, username, password)
    d.addCallback(on_connected)
    d.addErrback(fail)

    reactor.callLater(WATCHDOG_SECONDS, watchdog)
    reactor.run()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.exception('Fatal worker error: %s', exc)
        sys.exit(1)

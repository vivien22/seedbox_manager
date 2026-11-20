#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
Organiseur seedbox (v3).

Pour chaque tache de organizer_queue/ dont le torrent est termine :
  1. deplace le stockage Deluge vers Videos/Films/<Titre> ou Videos/Series/<Serie>
  2. (series) renomme les fichiers en <racine>/Saison XX/... dans Deluge
  3. ecrit le manifeste sync/ready/<id>.json pour la Raspberry Pi

Une seule operation Deluge asynchrone est lancee par execution : la boucle
seedbox-loop.sh relance le script toutes les 15 s et l'etat est conserve dans
organizer_queue/<id>.state.
"""

import os
import sys
import re
import json
import time
import tempfile

try:
    unicode
except NameError:  # Python 3 (tests uniquement)
    unicode = str

BASE = '/media/sdt1/bibien/private/seedbox-manager'
ORGANIZER_QUEUE = BASE + '/organizer_queue'
ORGANIZER_PROCESSED = BASE + '/organizer_processed'
ORGANIZER_FAILED = BASE + '/organizer_failed'
SYNC_READY = BASE + '/sync/ready'

AUTHFILE = '/media/sdt1/bibien/.config/deluge/auth'
DELUGE_HOST = '127.0.0.1'
DELUGE_PORT = 52940
LABEL = 'vivien'

VIDEOS_ROOT = u'/media/sdt1/bibien/private/deluge/data/Vivien/Videos'

# Un deplacement / renommage Deluge non constate apres ce delai est relance.
OPERATION_TIMEOUT = 15 * 60
MAX_OPERATION_ATTEMPTS = 3

# Une tache en erreur est abandonnee (organizer_failed/) quand les deux
# conditions sont reunies : assez d'echecs ET assez de temps ecoule.
FAIL_AFTER_ERRORS = 20
FAIL_AFTER_SECONDS = 30 * 60

# Garde-fou : si Deluge ne repond plus, on arrete le script.
WATCHDOG_SECONDS = 120

VIDEO_EXTENSIONS = (
    '.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv', '.mpg', '.mpeg', '.ts'
)
SUBTITLE_EXTENSIONS = (
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup', '.vtt'
)
IGNORED_EXTENSIONS = (
    '.log', '.part', '.tmp', '.!qb'
)

SEP = r'[ ._\-]'

EPISODE_PATTERNS = [
    # Serie.S01E02, S01E02 - Titre, Serie S1E102
    re.compile(r'^(?:(.*?)' + SEP + r'+)?S(\d{1,2})' + SEP + r'?E(\d{1,3})(?=$|[ ._\-E])',
               re.I | re.U),
    # Serie.1x02
    re.compile(r'^(?:(.*?)' + SEP + r'+)?(\d{1,2})x(\d{2,3})(?=$|[ ._\-])', re.I | re.U),
]

YEAR_RE = re.compile(r'(?:^|[ ._\-\(\[])((?:19|20)\d{2})(?=$|[ ._\-\)\]])', re.U)

MARKER_RE = re.compile(
    r'[ ._\-\[\(](?:2160p|1080p|720p|576p|480p|4K|UHD|WEB[-. ]?DL|WEB|WEBRip|'
    r'BluRay|BRRip|BDRip|HDRip|DVDRip|HDTV|FRENCH|TRUEFRENCH|MULTI|VOSTFR|VFF|'
    r'VFQ|VFI|VF2|HEVC|AV1|x264|x265|H[ .]?264|H[ .]?265|10bit|HDR|HDR10|DV|'
    r'REPACK|PROPER|iNTERNAL|COMPLETE|INTEGRALE)(?=$|[ ._\-\]\)])',
    re.I | re.U
)

SEASON_DIR_RE = re.compile(r'(?:season|saison)' + SEP + r'*(\d{1,2})', re.I | re.U)
SEASON_TAG_RE = re.compile(r'(?:^|[ ._/\-])S(\d{1,2})(?:E\d{1,3})?(?=$|[ ._/\-])', re.I | re.U)
SAMPLE_RE = re.compile(r'(?:^|[ ._/\-\[\(])sample(?=$|[ ._/\-\]\)])', re.I | re.U)


class PermanentError(Exception):
    """Erreur qui ne se corrigera pas toute seule : tache abandonnee."""


# ---------------------------------------------------------------- utilitaires

def to_unicode(value):
    if isinstance(value, unicode):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return unicode(value)


def error_text(exc):
    """Message d'exception en unicode, sans planter sur les accents (Python 2)."""
    if getattr(exc, 'args', None):
        parts = []
        for arg in exc.args:
            try:
                parts.append(to_unicode(arg))
            except Exception:
                parts.append(to_unicode(repr(arg)))
        return u' '.join(parts)
    return to_unicode(repr(exc))


def output(value):
    text = to_unicode(value) + u'\n'
    if sys.version_info[0] < 3:
        sys.stdout.write(text.encode('utf-8'))
    else:
        sys.stdout.write(text)
    sys.stdout.flush()


def ext_of(filename):
    return os.path.splitext(to_unicode(filename).lower())[1]


def is_video(filename):
    return ext_of(filename) in VIDEO_EXTENSIONS


def is_subtitle(filename):
    return ext_of(filename) in SUBTITLE_EXTENSIONS


def is_ignored(filename):
    lower = to_unicode(filename).lower()
    return any(lower.endswith(ext) for ext in IGNORED_EXTENSIONS)


def is_sample(relative_path):
    return SAMPLE_RE.search(to_unicode(relative_path)) is not None


def should_sync(item):
    """Seuls les videos (hors samples) et les sous-titres partent sur la Pi."""
    filename = item['filename']
    if is_ignored(filename):
        return False
    if is_video(filename):
        return not is_sample(item['path'])
    return is_subtitle(filename)


# ------------------------------------------------------------ noms et titres

def clean_name(value):
    value = to_unicode(value)
    value = re.sub(r'[<>:"/\\|?*]', u' ', value)
    value = re.sub(r'[._]+', u' ', value)
    value = re.sub(r'-+', u' ', value)
    value = re.sub(r'\s+', u' ', value, flags=re.U)
    return value.strip(u' .-_')


def normalize_title(title):
    title = clean_name(title)
    # "L affaire" -> "L'affaire", "D argent" -> "D'argent", "C est" -> "C'est"...
    title = re.sub(u"\\b([LDC]|Qu) (?=[AEIOUYHaeiouyhÀ-ÿ])",
                   u"\\1'", title, flags=re.U)
    return title


def cut_at_markers(value):
    value = to_unicode(value)
    match = MARKER_RE.search(value)
    if match:
        value = value[:match.start()]
    return value


def remove_trailing_year(value):
    return re.sub(r'[ ._\-\(\[]+(?:19|20)\d{2}[\)\]]?\s*$', u'', to_unicode(value)).strip()


def episode_info(filename):
    """(serie, saison, episode) ou None. La serie peut etre vide."""
    name = os.path.splitext(to_unicode(filename))[0] if (
        is_video(filename) or is_subtitle(filename)) else to_unicode(filename)

    for pattern in EPISODE_PATTERNS:
        match = pattern.search(name)
        if match:
            show = match.group(1) or u''
            return (
                normalize_title(remove_trailing_year(cut_at_markers(show))),
                int(match.group(2)),
                int(match.group(3))
            )
    return None


def show_from_torrent_name(name):
    name = to_unicode(name)
    match = re.match(r'^(.*?)' + SEP + r'+(?:S\d{1,2}|Saison|Season|Integrale|Complete)',
                     name, re.I | re.U)
    if match:
        name = match.group(1)
    return normalize_title(remove_trailing_year(cut_at_markers(name)))


def film_title(filename):
    """Titre (Annee). On retient la DERNIERE annee plausible :
    Blade.Runner.2049.2017 -> Blade Runner 2049 (2017), 1917.2019 -> 1917 (2019)."""
    stem = os.path.splitext(to_unicode(filename))[0]
    stem = cut_at_markers(stem) or stem

    best = None
    for match in YEAR_RE.finditer(stem):
        title = normalize_title(stem[:match.start(1)].rstrip(u' ._-(['))
        if title:
            best = (title, match.group(1))

    if best:
        return best[0] + u' (' + best[1] + u')'

    return normalize_title(stem)


def get_path_season(relative, has_root):
    """Saison deduite des SOUS-dossiers uniquement (jamais du dossier racine,
    sinon les bonus d'un pack "Serie.S01" finiraient dans Saison 01)."""
    parts = to_unicode(relative).split(u'/')
    dirs = parts[1:-1] if has_root else parts[:-1]
    if not dirs:
        return None

    path = u'/'.join(dirs)
    match = SEASON_DIR_RE.search(path)
    if match:
        return int(match.group(1))
    match = SEASON_TAG_RE.search(path)
    if match:
        return int(match.group(1))
    return None


def canonical_folder(parent, title):
    """Reutilise un dossier existant au nom identique a la casse pres
    ("the office" -> "The Office" s'il existe deja)."""
    try:
        names = os.listdir(to_unicode(parent))
    except OSError:
        return title

    key = title.lower()
    for name in sorted(names):
        name = to_unicode(name)
        if name.lower() == key and os.path.isdir(os.path.join(to_unicode(parent), name)):
            return name
    return title


# ------------------------------------------------------------------ chemins

def safe_source(save_path, relative_path):
    relative_path = to_unicode(relative_path)

    if os.path.isabs(relative_path):
        raise PermanentError(u'Chemin absolu interdit : ' + relative_path)

    parts = relative_path.replace(u'\\', u'/').split(u'/')
    if any(part in (u'', u'.', u'..') for part in parts):
        raise PermanentError(u'Chemin relatif invalide : ' + relative_path)

    root = os.path.realpath(to_unicode(save_path))
    source = os.path.realpath(os.path.join(root, *parts))

    if not source.startswith(root.rstrip(os.sep) + os.sep):
        raise PermanentError(u'Chemin source hors Videos : ' + source)

    return source


def safe_component(value):
    value = clean_name(value) if value else u''
    if value in (u'', u'.', u'..'):
        raise PermanentError(u'Nom de dossier invalide')
    return value


# --------------------------------------------------------------- classement

def read_entries(torrent):
    entries = []
    for item in torrent.get('files', []):
        relative = to_unicode(item.get('path', '')).replace(u'\\', u'/')
        parts = relative.split(u'/')
        if (not relative or relative.startswith(u'/')
                or any(p in (u'', u'.', u'..') for p in parts)):
            raise PermanentError(u'Chemin torrent invalide : ' + relative)

        entries.append({
            'index': int(item['index']),
            'path': relative,
            'filename': parts[-1],
            'size': int(item.get('size', 0))
        })
    return entries


def build_classification_plan(torrent, videos_root=VIDEOS_ROOT):
    save_path = to_unicode(torrent.get('save_path', ''))
    if not save_path:
        raise PermanentError(u'Dossier de telechargement absent')

    entries = read_entries(torrent)
    if not entries:
        raise PermanentError(u'Torrent sans fichier')

    videos = [e for e in entries if is_video(e['filename'])
              and not is_ignored(e['filename']) and not is_sample(e['path'])]
    if not videos:
        raise PermanentError(u'Aucun fichier video valide dans le torrent')

    # Torrent multi-fichiers = un dossier racine commun. Torrent mono-fichier = pas de racine.
    roots = set(e['path'].split(u'/')[0] for e in entries)
    has_root = all(u'/' in e['path'] for e in entries) and len(roots) == 1
    root = roots.pop() if has_root else None

    episodes = [info for info in (episode_info(v['filename']) for v in videos) if info]

    renames = []
    files = []
    targets = set()
    seedbox_paths = set()
    skipped = []

    if episodes:
        category = u'Series'
        fallback = show_from_torrent_name(torrent.get('name', u''))
        main_show = episodes[0][0] or fallback
        main_show = canonical_folder(
            os.path.join(videos_root, u'Series'), safe_component(main_show))

        for item in entries:
            if not should_sync(item):
                continue

            filename = item['filename']
            name = filename
            info = episode_info(filename)

            # Sous-titres "Subs/Serie.S01E02/2_French.srt" : on rattache au nom
            # du dossier pour que Kodi les associe a la video et pour eviter
            # les doublons de noms entre episodes.
            if info is None and is_subtitle(filename) and u'/' in item['path']:
                parent = item['path'].split(u'/')[-2]
                parent_info = episode_info(parent)
                if parent_info:
                    info = parent_info
                    name = parent + u'.' + filename

            show = main_show
            if info and info[0]:
                show = canonical_folder(
                    os.path.join(videos_root, u'Series'), safe_component(info[0]))
            season = info[1] if info else get_path_season(item['path'], has_root)
            folder = (u'Saison %02d' % season) if season is not None else u'Extras'

            target = u'Vidéos/Séries/' + show + u'/' + folder + u'/' + name

            if has_root:
                new_relative = root + u'/' + folder + u'/' + name
            else:
                new_relative = item['path']  # fichier seul : rien a renommer

            if target in targets or new_relative in seedbox_paths:
                if is_video(filename):
                    raise PermanentError(u'Destination en doublon : ' + target)
                skipped.append(item['path'])
                continue

            targets.add(target)
            seedbox_paths.add(new_relative)

            if new_relative != item['path']:
                renames.append((item['index'], new_relative))

            files.append({
                'index': item['index'],
                'source_relative': new_relative,
                'target': target,
                'size': item['size']
            })

        title = main_show

    else:
        category = u'Films'
        main_video = sorted(videos, key=lambda e: e['size'], reverse=True)[0]
        title = film_title(main_video['filename'])
        if not title:
            title = film_title(to_unicode(torrent.get('name', u'')) + u'.mkv')
        title = canonical_folder(
            os.path.join(videos_root, u'Films'), safe_component(title))

        for item in entries:
            if not should_sync(item):
                continue

            filename = item['filename']
            # Les videos secondaires (bonus) vont dans Extras/ pour ne pas
            # etre prises pour d'autres films par Kodi.
            if is_video(filename) and item is not main_video:
                target = u'Vidéos/Films/' + title + u'/Extras/' + filename
            else:
                target = u'Vidéos/Films/' + title + u'/' + filename

            if target in targets:
                if item is main_video:
                    raise PermanentError(u'Destination en doublon : ' + target)
                skipped.append(item['path'])
                continue
            targets.add(target)

            files.append({
                'index': item['index'],
                'source_relative': item['path'],
                'target': target,
                'size': item['size']
            })

    if not files:
        raise PermanentError(u'Aucun fichier a synchroniser')

    files.sort(key=lambda f: f['target'])

    return {
        'category': category,
        'title': title,
        'root': root,
        'storage_path': os.path.join(videos_root, category, title),
        'renames': renames,
        'files': files,
        'skipped': skipped
    }


# ---------------------------------------------------------------- file / etat

def valid_torrent_id(torrent_id):
    return (len(torrent_id) == 40
            and all(c in '0123456789abcdef' for c in torrent_id))


def read_organizer_queue():
    jobs = []
    if not os.path.isdir(ORGANIZER_QUEUE):
        return jobs

    for filename in sorted(os.listdir(ORGANIZER_QUEUE)):
        if not filename.endswith('.job'):
            continue
        torrent_id = filename[:-4].lower()
        if not valid_torrent_id(torrent_id):
            continue

        job_path = os.path.join(ORGANIZER_QUEUE, filename)
        if not os.path.isfile(job_path):
            continue

        with open(job_path, 'r') as job:
            stored_id = job.read().strip().lower()
        if stored_id == torrent_id:
            jobs.append(torrent_id)

    return jobs


def state_path(torrent_id):
    return os.path.join(ORGANIZER_QUEUE, torrent_id + '.state')


def read_organizer_state(torrent_id):
    path = state_path(torrent_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r') as stream:
            state = json.load(stream)
        return state if isinstance(state, dict) else {}
    except ValueError:
        return {}


def atomic_write(directory, final_path, content, prefix):
    if not isinstance(content, bytes):
        content = content.encode('utf-8')

    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, final_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_organizer_state(torrent_id, state):
    path = state_path(torrent_id)
    if not state:
        if os.path.exists(path):
            os.unlink(path)
        return
    atomic_write(ORGANIZER_QUEUE, path, json.dumps(state, sort_keys=True), '.state-')


def record_error(torrent_id, message):
    """Compte les erreurs ; abandonne la tache si elles persistent."""
    state = read_organizer_state(torrent_id)
    errors = state.get('errors') or {}
    now = time.time()

    errors['count'] = int(errors.get('count', 0)) + 1
    errors.setdefault('first_at', now)
    errors['last_at'] = now
    errors['last'] = message
    state['errors'] = errors
    write_organizer_state(torrent_id, state)

    if (errors['count'] >= FAIL_AFTER_ERRORS
            and now - float(errors['first_at']) >= FAIL_AFTER_SECONDS):
        move_to_failed(torrent_id, message)
        return True
    return False


def clear_errors(torrent_id, state):
    if 'errors' in state:
        del state['errors']
        write_organizer_state(torrent_id, state)


def archive_job(torrent_id, destination_dir):
    if not os.path.isdir(destination_dir):
        os.makedirs(destination_dir)

    source = os.path.join(ORGANIZER_QUEUE, torrent_id + '.job')
    destination = os.path.join(destination_dir, torrent_id + '.job')
    if os.path.exists(source):
        os.rename(source, destination)


def move_to_failed(torrent_id, reason):
    archive_job(torrent_id, ORGANIZER_FAILED)
    atomic_write(
        ORGANIZER_FAILED,
        os.path.join(ORGANIZER_FAILED, torrent_id + '.reason'),
        to_unicode(reason) + u'\n',
        '.reason-'
    )
    path = state_path(torrent_id)
    if os.path.exists(path):
        os.unlink(path)
    output(u'[ABANDON] ' + torrent_id + u' : ' + to_unicode(reason))


# --------------------------------------------------------------- manifeste

def build_manifest_files(torrent, plan):
    save_path = to_unicode(torrent.get('save_path', ''))

    if os.path.realpath(save_path) != os.path.realpath(plan['storage_path']):
        raise Exception(u'Deplacement du stockage non termine')

    manifest_files = []
    for item in plan['files']:
        source = safe_source(save_path, item['source_relative'])

        if not os.path.isfile(source):
            raise Exception(u'Fichier absent : ' + source)

        actual_size = os.path.getsize(source)
        expected_size = int(item['size'])
        if actual_size != expected_size:
            raise Exception(u'Taille incorrecte pour %s : %d au lieu de %d' % (
                os.path.basename(source), actual_size, expected_size))

        manifest_files.append({
            'source': source,
            'target': item['target'],
            'size': actual_size
        })

    if not manifest_files:
        raise PermanentError(u'Aucun fichier a synchroniser')
    return manifest_files


def write_manifest(torrent_id, plan, files):
    if not valid_torrent_id(torrent_id):
        raise PermanentError(u'Identifiant torrent invalide')
    if not files:
        raise PermanentError(u'Manifeste vide refuse')

    manifest = {
        'version': 1,
        'torrent_id': torrent_id,
        'name': plan['title'],
        'category': plan['category'],
        'files': files
    }
    content = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + '\n'
    if not isinstance(content, bytes):
        content = content.encode('ascii')

    destination = os.path.join(SYNC_READY, torrent_id + '.json')
    if not os.path.isdir(SYNC_READY):
        os.makedirs(SYNC_READY)

    if os.path.exists(destination):
        with open(destination, 'rb') as existing:
            if existing.read() == content:
                output(u'[MANIFESTE DEJA PRESENT] ' + to_unicode(destination))
                return
        raise PermanentError(u'Manifeste existant different : ' + to_unicode(destination))

    atomic_write(SYNC_READY, destination, content, '.manifest-')
    output(u'[MANIFESTE PRET] ' + to_unicode(destination))


# ------------------------------------------------------- operations Deluge
# Importes ici pour que les fonctions de classement restent testables sans Deluge.

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


def operation_done(result, torrent_id, description):
    output(u'[OPERATION ENVOYEE] ' + description + u' : ' + torrent_id)
    stop()
    return result


def operation_failed(failure, torrent_id, description, key):
    message = description + u' refuse par Deluge : ' + to_unicode(str(failure))
    output(u'[ERREUR RPC] ' + message)
    try:
        state = read_organizer_state(torrent_id)
        state.pop(key, None)
        write_organizer_state(torrent_id, state)
        record_error(torrent_id, message)
    except Exception as exc:
        output(u'[ERREUR ETAT] ' + error_text(exc))
    stop()
    return None


def launch_move(torrent_id, plan, state, attempts):
    _, client = deluge()
    destination = plan['storage_path']

    if attempts == 1:
        for item in plan['files']:
            original = item['source_relative']
            destination_file = safe_source(destination, original)
            if os.path.exists(destination_file):
                raise PermanentError(u'Destination deja existante : ' + destination_file)

    state['move'] = {
        'destination': destination,
        'requested_at': time.time(),
        'attempts': attempts
    }
    write_organizer_state(torrent_id, state)

    d = client.core.move_storage([torrent_id], destination)
    d.addCallbacks(
        lambda result: operation_done(result, torrent_id, u'Deplacement'),
        lambda failure: operation_failed(failure, torrent_id, u'Deplacement', 'move')
    )


def launch_renames(torrent_id, plan, state, attempts):
    _, client = deluge()
    renames = plan['renames']

    state['rename'] = {
        'files': [[int(index), path] for index, path in renames],
        'requested_at': time.time(),
        'attempts': attempts
    }
    write_organizer_state(torrent_id, state)

    d = client.core.rename_files(torrent_id, renames)
    d.addCallbacks(
        lambda result: operation_done(result, torrent_id, u'Renommage'),
        lambda failure: operation_failed(failure, torrent_id, u'Renommage', 'rename')
    )


def pending_attempts(pending, label, torrent_id):
    """None = encore en attente ; sinon numero de la prochaine tentative."""
    age = time.time() - float(pending.get('requested_at', 0))
    attempts = int(pending.get('attempts', 1))

    if age < OPERATION_TIMEOUT:
        output(u'[EN ATTENTE] ' + label + u' Deluge en cours : ' + torrent_id)
        return None
    if attempts >= MAX_OPERATION_ATTEMPTS:
        raise PermanentError(u'%s toujours pas constate apres %d tentatives' % (label, attempts))

    output(u'[RELANCE] ' + label + u' non constate apres %d min : %s' % (
        OPERATION_TIMEOUT // 60, torrent_id))
    return attempts + 1


def process_job(torrent_id, torrent):
    """Retourne 'done', 'waiting' ou 'launched' (operation asynchrone envoyee)."""
    if torrent is None:
        raise Exception(u'Torrent absent de Deluge')
    if torrent.get('label') != LABEL:
        raise Exception(u'Label incorrect : ' + to_unicode(torrent.get('label', '')))
    if not torrent.get('is_finished', False):
        output(u'[EN ATTENTE] Telechargement en cours : ' + torrent_id)
        return 'waiting'

    plan = build_classification_plan(torrent)
    state = read_organizer_state(torrent_id)

    current_path = os.path.realpath(to_unicode(torrent.get('save_path', '')))
    destination = os.path.realpath(plan['storage_path'])

    # 1. Deplacement
    if current_path != destination:
        pending = state.get('move')
        attempts = 1
        if pending and pending.get('destination') == plan['storage_path']:
            attempts = pending_attempts(pending, u'Deplacement', torrent_id)
            if attempts is None:
                return 'waiting'
        launch_move(torrent_id, plan, state, attempts)
        return 'launched'

    if state.pop('move', None) is not None:
        write_organizer_state(torrent_id, state)

    # 2. Renommages (series)
    pending = state.get('rename')
    if pending:
        current_files = dict(
            (int(item['index']), to_unicode(item['path']).replace(u'\\', u'/'))
            for item in torrent.get('files', [])
        )
        completed = all(
            current_files.get(int(index)) == to_unicode(path)
            for index, path in pending.get('files', [])
        )
        if completed:
            del state['rename']
            write_organizer_state(torrent_id, state)
        else:
            attempts = pending_attempts(pending, u'Renommage', torrent_id)
            if attempts is None:
                return 'waiting'
            launch_renames(torrent_id, plan, state, attempts)
            return 'launched'

    if plan['renames']:
        launch_renames(torrent_id, plan, state, 1)
        return 'launched'

    # 3. Manifeste
    for skipped in plan['skipped']:
        output(u'[IGNORE] Doublon non video : ' + skipped)

    manifest_files = build_manifest_files(torrent, plan)
    write_manifest(torrent_id, plan, manifest_files)
    archive_job(torrent_id, ORGANIZER_PROCESSED)
    write_organizer_state(torrent_id, {})
    output(u'[TERMINE] ' + torrent_id + u' -> ' + plan['category'] + u'/' + plan['title'])
    return 'done'


def on_status(status):
    jobs = read_organizer_queue()
    if not jobs:
        stop()
        return

    output(u'\n%s - taches dans la file : %d' % (time.strftime('%Y-%m-%d %H:%M:%S'), len(jobs)))

    waiting = 0
    succeeded = 0

    for torrent_id in jobs:
        try:
            result = process_job(torrent_id, status.get(torrent_id))
            if result == 'launched':
                return  # stop() sera appele par le callback RPC
            if result == 'done':
                succeeded += 1
            else:
                waiting += 1
                if result == 'waiting':
                    state = read_organizer_state(torrent_id)
                    clear_errors(torrent_id, state)

        except PermanentError as exc:
            move_to_failed(torrent_id, error_text(exc))
        except Exception as exc:
            message = error_text(exc)
            output(u'[ERREUR - TACHE CONSERVEE] ' + torrent_id + u' : ' + message)
            try:
                record_error(torrent_id, message)
            except Exception as state_exc:
                output(u'[ERREUR ETAT] ' + error_text(state_exc))
            waiting += 1

    output(u'Bilan : %d termine(s), %d en attente / erreur(s)' % (succeeded, waiting))
    stop()


def on_connected(result):
    _, client = deluge()
    keys = ['name', 'label', 'files', 'save_path', 'progress', 'is_finished']
    d = client.core.get_torrents_status({}, keys)
    d.addCallback(on_status)
    d.addErrback(on_error)


def on_error(failure):
    output(u'ERREUR RPC : ' + to_unicode(str(failure)))
    stop()


def read_credentials():
    with open(AUTHFILE, 'r') as stream:
        for line in stream:
            parts = line.strip().split(':', 2)
            if len(parts) == 3 and parts[0] == 'localclient':
                return parts[0], parts[1]
    raise Exception('localclient credentials not found')


def watchdog():
    output(u'[ALERTE] Deluge ne repond pas, arret apres %d s' % WATCHDOG_SECONDS)
    stop()


def main():
    if not read_organizer_queue():
        return  # rien a faire : pas de connexion Deluge inutile

    reactor, client = deluge()
    user, password = read_credentials()

    d = client.connect(DELUGE_HOST, DELUGE_PORT, user, password)
    d.addCallback(on_connected)
    d.addErrback(on_error)

    reactor.callLater(WATCHDOG_SECONDS, watchdog)
    reactor.run()


if __name__ == '__main__':
    main()

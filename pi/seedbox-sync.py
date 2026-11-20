#!/usr/bin/python3
"""
Synchro seedbox -> Raspberry Pi (v3).

- lit les manifestes de sync/ready sur la seedbox
- telecharge chaque fichier DIRECTEMENT sous son nom final (reprise via reget)
- des que le debut d'une video est arrive, demande a Kodi de scanner son
  dossier : la video apparait dans la mediatheque et peut etre lue pendant
  que la suite se telecharge
- publie son etat sur la seedbox (status/pi.json) pour le site web
"""

import os
import re
import json
import time
import fcntl
import shutil
import socket
import tempfile
import subprocess

HOST = "bibien@clymene.feralhosting.com"
KEY = "/storage/.ssh/feral_transfer"

REMOTE_BASE = "/media/sdt1/bibien/private/seedbox-manager"
REMOTE_READY = REMOTE_BASE + "/sync/ready"
REMOTE_PROCESSED = REMOTE_BASE + "/sync/processed"
REMOTE_STATUS = REMOTE_BASE + "/status"
SOURCE_ROOT = "/media/sdt1/bibien/private/deluge/data/Vivien/Videos"

DEST_ROOT = "/var/media/RaspPi"

SSH_BIN = "/usr/bin/ssh"
SFTP_BIN = "/usr/bin/sftp"
KODI_SEND = "/usr/bin/kodi-send"

CACHE_DIR = "/storage/.cache/seedbox-sync"
LOCK_FILE = CACHE_DIR + "/sync.lock"
IN_PROGRESS_FILE = CACHE_DIR + "/in-progress.json"
LOCAL_STATUS_FILE = CACHE_DIR + "/status.json"

SSH_OPTIONS = [
    "-i", KEY,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
]

# Scan Kodi quand ce volume d'une video est arrive (ou la video entiere si plus petite)
SCAN_THRESHOLD = 64 * 1024 * 1024
# True = scanner toute la mediatheque au lieu du seul dossier du film / de la serie.
# A activer si les fichiers n'apparaissent pas (source Kodi declaree sous un autre chemin).
SCAN_WHOLE_LIBRARY = False

# A la reprise on recule un peu : sftp ecrit par blocs paralleles et une
# coupure brutale peut laisser des trous juste avant la fin du fichier.
RESUME_BACKOFF = 8 * 1024 * 1024

FREE_SPACE_MARGIN = 2 * 1024 ** 3
POLL_INTERVAL = 2
STATUS_INTERVAL = 10
MAX_RECENT = 10
MAX_ERRORS = 10

TORRENT_RE = re.compile(r"^[0-9a-f]{40}\.json$")
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts")


def log(message):
    print(time.strftime("%H:%M:%S ") + message, flush=True)


def is_video(path):
    return os.path.splitext(path.lower())[1] in VIDEO_EXTENSIONS


# ------------------------------------------------------------------ outils

def run(args, input_data=None, timeout=None):
    result = subprocess.run(
        args, input=input_data, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout.decode("utf-8", "replace").strip()
                           or "code retour %d" % result.returncode)
    return result.stdout.decode("utf-8", "replace")


def ssh(command, input_data=None, timeout=120):
    return run([SSH_BIN] + SSH_OPTIONS + [HOST, command], input_data, timeout)


def sftp_quote(path):
    if "\n" in path or "\r" in path or "\x00" in path:
        raise ValueError("Caractère interdit dans un chemin")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def atomic_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def human(size):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return "%.1f %s" % (size, unit) if unit != "o" else "%d o" % size
        size /= 1024.0


# ------------------------------------------------------------------- Kodi

def kodi_scan(folder=None):
    action = "UpdateLibrary(video)"
    label = "toute la médiathèque"
    if folder and not SCAN_WHOLE_LIBRARY and '"' not in folder and "\\" not in folder:
        action = 'UpdateLibrary(video,"%s/")' % folder.rstrip("/")
        label = folder
    elif folder:
        label = "toute la médiathèque (nouveau : %s)" % os.path.basename(folder)

    result = subprocess.run(
        [KODI_SEND, "--action=" + action],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    if result.returncode == 0:
        log("[KODI] Scan demandé : " + label)
    else:
        log("[ATTENTION] Échec de kodi-send")


def library_folder(destination):
    """Dossier du film ou de la serie : DEST_ROOT/Vidéos/<Films|Séries>/<Titre>."""
    relative = os.path.relpath(destination, DEST_ROOT).split(os.sep)
    depth = 3 if len(relative) > 3 else len(relative) - 1
    return os.path.join(DEST_ROOT, *relative[:depth])


# ------------------------------------------------------------------- etat

class Status:
    """Etat publie pour le site web (seedbox:status/pi.json)."""

    def __init__(self):
        previous = read_json(LOCAL_STATUS_FILE, {})
        self.data = {
            "version": 1,
            "host": socket.gethostname(),
            "state": "starting",
            "message": "",
            "current": None,
            "manifest": None,
            "recent": previous.get("recent", [])[:MAX_RECENT],
            "errors": previous.get("errors", [])[:MAX_ERRORS],
        }
        self.last_push = 0

    def set(self, state, message=""):
        self.data["state"] = state
        self.data["message"] = message

    def error(self, message):
        log("[ERREUR] " + message)
        self.data["errors"].insert(0, {"at": int(time.time()), "message": message[:500]})
        del self.data["errors"][MAX_ERRORS:]

    def recent(self, torrent_id, name, ok):
        self.data["recent"] = [r for r in self.data["recent"] if r.get("torrent_id") != torrent_id]
        self.data["recent"].insert(0, {
            "torrent_id": torrent_id, "name": name, "ok": ok,
            "finished_at": int(time.time())
        })
        del self.data["recent"][MAX_RECENT:]

    def push(self, force=False):
        now = time.time()
        if not force and now - self.last_push < STATUS_INTERVAL:
            return
        self.last_push = now

        self.data["updated_at"] = int(now)
        try:
            usage = shutil.disk_usage(DEST_ROOT)
            self.data["disk"] = {"free": usage.free, "total": usage.total}
        except OSError:
            self.data["disk"] = None

        try:
            atomic_json(LOCAL_STATUS_FILE, self.data)
        except OSError as error:
            log("[ATTENTION] État local non écrit : " + str(error))

        payload = json.dumps(self.data, ensure_ascii=True).encode("ascii")
        try:
            ssh(
                "mkdir -p " + REMOTE_STATUS + " && cat > " + REMOTE_STATUS
                + "/.pi.json.tmp && mv -f " + REMOTE_STATUS + "/.pi.json.tmp "
                + REMOTE_STATUS + "/pi.json",
                payload, timeout=30
            )
        except Exception as error:
            log("[ATTENTION] État non publié sur la seedbox : " + str(error)[:200])


# ------------------------------------------------------------ validation

def validate_file(item):
    source = item.get("source")
    target = item.get("target")
    size = item.get("size")

    if not isinstance(source, str):
        raise ValueError("Chemin source invalide")
    source = os.path.normpath(source)
    if not source.startswith(SOURCE_ROOT + os.sep):
        raise ValueError("Chemin source non autorisé : " + repr(source))

    if not isinstance(target, str) or not target or target.startswith("/"):
        raise ValueError("Chemin cible invalide")
    parts = target.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Chemin cible dangereux")

    if not isinstance(size, int) or size < 0:
        raise ValueError("Taille invalide")

    destination = os.path.realpath(os.path.join(DEST_ROOT, *parts))
    root = os.path.realpath(DEST_ROOT) + os.sep
    if not destination.startswith(root):
        raise ValueError("La cible sort du disque de destination")

    return source, destination, size


# --------------------------------------------------------------- transfert

def transfer_file(source, destination, size, status, on_playable):
    """Telecharge source vers destination (nom final). Retourne 'present' ou 'done'."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    in_progress = read_json(IN_PROGRESS_FILE, {})
    registered = destination in in_progress

    # Reprise d'un .partial laisse par l'ancienne version du script
    legacy = destination + ".partial"
    if not os.path.exists(destination) and os.path.exists(legacy):
        os.rename(legacy, destination)
        registered = True

    current = os.path.getsize(destination) if os.path.exists(destination) else 0

    if os.path.exists(destination) and not registered:
        if current == size:
            log("[DÉJÀ PRÉSENT] " + destination)
            return "present"
        raise RuntimeError("Fichier existant inconnu de la synchro, taille différente : "
                           + destination)

    if registered and current > size:
        os.truncate(destination, 0)
        current = 0

    in_progress[destination] = {"source": source, "size": size, "since": int(time.time())}
    atomic_json(IN_PROGRESS_FILE, in_progress)

    if current < size:
        if current > 0:
            current = max(0, current - RESUME_BACKOFF)
            os.truncate(destination, current)

        free = shutil.disk_usage(DEST_ROOT).free
        if free - (size - current) < FREE_SPACE_MARGIN:
            raise RuntimeError("Espace disque insuffisant : %s libres, %s nécessaires"
                               % (human(free), human(size - current)))

        relative = os.path.relpath(destination, DEST_ROOT)
        log("[SFTP] " + relative + (" (reprise à %s)" % human(current) if current else ""))

        batch = "reget " + sftp_quote(source) + " " + sftp_quote(destination) + "\n"
        with tempfile.TemporaryFile() as sftp_output:
            process = subprocess.Popen(
                [SFTP_BIN, "-q", "-b", "-"] + SSH_OPTIONS + [HOST],
                stdin=subprocess.PIPE, stdout=sftp_output, stderr=subprocess.STDOUT
            )
            process.stdin.write(batch.encode("utf-8"))
            process.stdin.close()

            playable_sent = False
            last_size, last_time, speed = current, time.time(), 0.0
            threshold = min(size, SCAN_THRESHOLD)

            while True:
                finished = process.poll() is not None
                done = os.path.getsize(destination) if os.path.exists(destination) else 0
                now = time.time()
                if now > last_time:
                    instant = max(0, done - last_size) / (now - last_time)
                    speed = instant if speed == 0 else 0.7 * speed + 0.3 * instant
                last_size, last_time = done, now

                status.data["current"] = {
                    "target": relative, "size": size, "done": min(done, size),
                    "speed": int(speed),
                    "eta": int((size - done) / speed) if speed > 0 else None,
                    "playable": playable_sent,
                }

                if not playable_sent and is_video(destination) and done >= threshold:
                    on_playable(destination)
                    playable_sent = True
                    status.data["current"]["playable"] = True
                    status.push(force=True)

                status.push()

                if finished:
                    break
                time.sleep(POLL_INTERVAL)

            if process.returncode != 0:
                sftp_output.seek(0)
                message = sftp_output.read().decode("utf-8", "replace").strip()
                raise RuntimeError("sftp : " + (message or "code %d" % process.returncode))

    final = os.path.getsize(destination)
    if final != size:
        raise RuntimeError("Taille incorrecte : %d octets attendus, %d reçus" % (size, final))

    in_progress = read_json(IN_PROGRESS_FILE, {})
    in_progress.pop(destination, None)
    atomic_json(IN_PROGRESS_FILE, in_progress)

    status.data["current"] = None
    log("[TERMINÉ] " + destination)
    return "done"


def process_manifest(filename, status, touched_folders):
    torrent_id = filename[:-5]
    log("=== MANIFESTE " + torrent_id + " ===")

    raw = ssh("cat " + REMOTE_READY + "/" + filename)
    manifest = json.loads(raw)

    if manifest.get("version") != 1:
        raise RuntimeError("Version de manifeste non prise en charge")
    if manifest.get("torrent_id") != torrent_id:
        raise RuntimeError("Identifiant incohérent")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("Manifeste vide ou invalide")

    items = [validate_file(item) for item in files]
    # Videos d'abord (dans l'ordre des episodes), puis sous-titres et le reste
    items.sort(key=lambda item: (not is_video(item[1]), item[1]))

    name = manifest.get("name") or os.path.basename(os.path.dirname(items[0][1]))
    states = [{"target": os.path.relpath(d, DEST_ROOT), "size": s, "state": "waiting"}
              for _, d, s in items]
    status.data["manifest"] = {"torrent_id": torrent_id, "name": name, "files": states}
    status.set("transferring", name)
    status.push(force=True)

    def on_playable(destination):
        folder = library_folder(destination)
        touched_folders.add(folder)
        kodi_scan(folder)

    for index, (source, destination, size) in enumerate(items):
        states[index]["state"] = "downloading"
        try:
            result = transfer_file(source, destination, size, status, on_playable)
        except Exception:
            states[index]["state"] = "error"
            raise
        states[index]["state"] = result
        touched_folders.add(library_folder(destination))
        status.push(force=True)

    ssh("mv " + REMOTE_READY + "/" + filename + " " + REMOTE_PROCESSED + "/" + filename)
    log("[MANIFESTE ARCHIVÉ] " + torrent_id)
    status.recent(torrent_id, name, True)
    status.data["manifest"] = None


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Une synchronisation est déjà en cours.")
        return

    status = Status()

    if not os.path.ismount(DEST_ROOT):
        status.set("error", "Disque USB non monté")
        status.error("Disque USB non monté, synchronisation suspendue")
        status.push(force=True)
        return

    try:
        listing = ssh("mkdir -p " + REMOTE_PROCESSED + " && find " + REMOTE_READY
                      + " -maxdepth 1 -type f -name '*.json' -printf '%f\\n' | sort")
    except Exception as error:
        status.set("error", "Seedbox injoignable")
        status.error("Seedbox injoignable : " + str(error)[:300])
        status.push(force=True)
        return

    manifests = [line.strip() for line in listing.splitlines()
                 if TORRENT_RE.match(line.strip())]

    touched_folders = set()
    failures = 0

    for filename in manifests:
        try:
            process_manifest(filename, status, touched_folders)
        except Exception as error:
            failures += 1
            status.error(filename[:-5][:8] + "… : " + str(error))
            if status.data.get("manifest"):
                status.recent(filename[:-5], status.data["manifest"]["name"], False)
            status.data["current"] = None
            status.data["manifest"] = None

    # Scan final : fichiers termines, sous-titres, et videos dont le scan
    # initial a pu etre ignore par Kodi (scan deja en cours).
    if touched_folders:
        if SCAN_WHOLE_LIBRARY or len(touched_folders) > 3:
            kodi_scan(None)
        else:
            for folder in sorted(touched_folders):
                kodi_scan(folder)

    if failures:
        status.set("error", "%d manifeste(s) en erreur, nouvel essai au prochain passage"
                   % failures)
    else:
        status.set("idle", "")
    status.push(force=True)

    if manifests:
        log("Bilan : {} manifeste(s), {} erreur(s).".format(len(manifests), failures))


if __name__ == "__main__":
    main()

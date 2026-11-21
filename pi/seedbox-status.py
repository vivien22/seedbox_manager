#!/usr/bin/python3
"""
Affiche les transferts seedbox -> Pi en cours, avec la vitesse en direct.
  python3 seedbox-status.py          # une fois
  python3 seedbox-status.py --watch  # rafraichi chaque seconde (Ctrl+C pour quitter)
"""

import os
import sys
import json
import time

CACHE_DIR = "/storage/.cache/seedbox-sync"
IN_PROGRESS_FILE = CACHE_DIR + "/in-progress.json"
STATUS_FILE = CACHE_DIR + "/status.json"
DEST_ROOT = "/var/media/RaspPi"
INTERVAL = 1.0


def read_json(path, default):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def human(size):
    size = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return ("%d %s" % (size, unit)) if unit == "o" else ("%.1f %s" % (size, unit))
        size /= 1024


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min %02d s" % (seconds // 60, seconds % 60)
    return "%d h %02d min" % (seconds // 3600, (seconds % 3600) // 60)


def bar(fraction, width=30):
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class Speed:
    """Vitesse lissee calculee a partir de la taille des fichiers sur le disque."""

    def __init__(self):
        self.previous = {}
        self.speed = {}

    def update(self, path, size_now):
        now = time.time()
        previous = self.previous.get(path)
        self.previous[path] = (now, size_now)
        if previous is None or now <= previous[0]:
            return self.speed.get(path, 0.0)
        instant = max(0, size_now - previous[1]) / (now - previous[0])
        old = self.speed.get(path)
        self.speed[path] = instant if old is None else 0.6 * old + 0.4 * instant
        return self.speed[path]


def render(speeds):
    lines = []
    status = read_json(STATUS_FILE, {})
    in_progress = read_json(IN_PROGRESS_FILE, {})

    state = {"idle": "En veille", "transferring": "Transfert en cours",
             "error": "Erreur", "starting": "Démarrage"}.get(status.get("state"), status.get("state", "?"))
    lines.append("Raspberry Pi : %s%s" % (state, (" - " + status["message"]) if status.get("message") else ""))

    try:
        free = os.statvfs(DEST_ROOT)
        lines.append("Disque       : %s libres" % human(free.f_bavail * free.f_frsize))
    except OSError:
        lines.append("Disque       : non monté")
    lines.append("")

    active = []
    for destination, info in sorted(in_progress.items()):
        size = int(info.get("size", 0))
        done = os.path.getsize(destination) if os.path.exists(destination) else 0
        speed = speeds.update(destination, done)
        active.append((destination, size, done, speed))

    if not active:
        lines.append("Aucun transfert en cours.")
    for destination, size, done, speed in active:
        fraction = float(done) / size if size else 1.0
        name = os.path.relpath(destination, DEST_ROOT)
        lines.append(name)
        details = "%s %5.1f %%  %s / %s" % (bar(fraction), 100 * fraction, human(done), human(size))
        if speed > 0:
            details += "  %s/s  reste %s" % (human(speed), duration((size - done) / speed))
        elif status.get("state") != "transferring":
            details += "  (en pause, reprise au prochain passage)"
        lines.append("  " + details)

    manifest = status.get("manifest")
    if manifest:
        waiting = [f for f in manifest.get("files", []) if f.get("state") == "waiting"]
        if waiting:
            lines.append("")
            lines.append("Ensuite (%s) : %d fichier(s), %s" % (
                manifest.get("name", ""), len(waiting), human(sum(f["size"] for f in waiting))))

    errors = status.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Dernière erreur (%s) : %s" % (
            time.strftime("%d/%m %H:%M", time.localtime(errors[0]["at"])), errors[0]["message"][:200]))

    return "\n".join(lines)


def main():
    speeds = Speed()
    if "--watch" not in sys.argv:
        render(speeds)
        time.sleep(INTERVAL)
        print(render(speeds))
        return

    try:
        while True:
            screen = render(speeds)
            sys.stdout.write("\033[H\033[2J" + screen + "\n\n" + time.strftime("%H:%M:%S")
                             + " - Ctrl+C pour quitter\n")
            sys.stdout.flush()
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

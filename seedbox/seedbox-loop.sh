#!/bin/sh
# Boucle seedbox (v3) : ajout des torrents, organisation, etat pour le site web.

BASE="/media/sdt1/bibien/private/seedbox-manager"

# Python 2 a besoin d'une locale UTF-8 pour manipuler des chemins accentues.
# Verifie avec "locale -a" que C.UTF-8 existe (sinon en_US.UTF-8).
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8

cd "$BASE" || exit 1
mkdir -p "$BASE/logs" "$BASE/status" "$BASE/organizer_queue" \
         "$BASE/organizer_failed" "$BASE/sync/ready" "$BASE/sync/processed" \
         "$BASE/commands"

rotate() {
    # Garde chaque journal sous 5 Mo (une archive .1)
    if [ -f "$1" ] && [ "$(wc -c < "$1")" -gt 5242880 ]; then
        mv -f "$1" "$1.1"
    fi
}

while true; do
    rotate "$BASE/logs/organizer.log"
    rotate "$BASE/logs/status.log"
    rotate "$BASE/logs/worker.log"
    rotate "$BASE/logs/commands.log"

    /usr/bin/python "$BASE/worker.py"

    /usr/bin/flock -n "$BASE/organizer.lock" \
        /usr/bin/python "$BASE/organizer_v2.py" >> "$BASE/logs/organizer.log" 2>&1

    /usr/bin/flock -n "$BASE/commands.lock" \
        /usr/bin/python "$BASE/commands.py" >> "$BASE/logs/commands.log" 2>&1

    /usr/bin/flock -n "$BASE/status.lock" \
        /usr/bin/python "$BASE/status_dump.py" >> "$BASE/logs/status.log" 2>&1

    sleep 15
done

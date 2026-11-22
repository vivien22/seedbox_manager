#!/bin/sh
# Autorise un appareil (telephone, PC...) a utiliser le site web.
#   sh pair-device.sh [Nom]          -> code a usage unique (10 min) pour l'appareil "Nom"
#                                       (defaut : Telephone). Un appareil deja autorise
#                                       sous le meme nom est remplace.
#   sh pair-device.sh --list         -> appareils autorises
#   sh pair-device.sh --revoke Nom   -> retire l'acces a l'appareil "Nom"
#   sh pair-device.sh --revoke-all   -> retire l'acces a tous les appareils

set -eu

BASE="/media/sdt1/bibien/private/seedbox-manager"
STATE="$BASE/state"
URL="https://bibien.clymene.feralhosting.com/"
TTL=600
PYTHON=/usr/bin/python

mkdir -p "$STATE"
chmod 700 "$STATE"

case "${1:-}" in
    --list)
        "$PYTHON" - "$STATE/devices.json" <<'EOF'
# -*- coding: utf-8 -*-
import json, time
import sys
def u(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
def out(x):
    sys.stdout.write(x.encode("utf-8") if sys.version_info[0] < 3 else x)
    sys.stdout.write("\n")
try:
    devices = json.load(open(sys.argv[1])).get("devices", [])
except (IOError, ValueError):
    devices = []
if not devices:
    out(u"Aucun appareil autorisé.")
for d in sorted(devices, key=lambda d: d["created"]):
    out(u"%-15s autorisé le %s, vu le %s" % (
        d.get("name", u"Téléphone"),
        time.strftime("%d/%m/%Y %H:%M", time.localtime(d["created"])),
        time.strftime("%d/%m/%Y %H:%M", time.localtime(d["last_seen"]))))
EOF
        exit 0
        ;;
    --revoke-all)
        rm -f "$STATE/devices.json" "$STATE/pairing.json"
        echo "Accès retiré à tous les appareils."
        exit 0
        ;;
    --revoke)
        [ -n "${2:-}" ] || { echo "Usage : sh pair-device.sh --revoke Nom" >&2; exit 1; }
        "$PYTHON" - "$STATE/devices.json" "$2" <<'EOF'
# -*- coding: utf-8 -*-
import json, os
import sys
def u(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
def out(x):
    sys.stdout.write(x.encode("utf-8") if sys.version_info[0] < 3 else x)
    sys.stdout.write("\n")
path, name = sys.argv[1], u(sys.argv[2]).lower()
if name == u"telephone":
    name = u"téléphone"
try:
    devices = json.load(open(path)).get("devices", [])
except (IOError, ValueError):
    devices = []
kept = [d for d in devices if d.get("name", u"Téléphone").lower() != name]
if len(kept) == len(devices):
    out(u"Aucun appareil de ce nom.")
    sys.exit(1)
tmp = path + ".tmp"
with open(tmp, "w") as stream:
    json.dump({"devices": kept}, stream)
os.chmod(tmp, 0o600)
os.rename(tmp, path)
out(u"Accès retiré.")
EOF
        exit $?
        ;;
esac

NAME="${1:-Telephone}"

# 10 caracteres sans ambiguite (pas de 0/O, 1/I/L) : ~50 bits, usage unique,
# valable 10 min, 5 essais par quart d'heure -> impossible a deviner.
CODE=$(LC_ALL=C tr -dc 'ABCDEFGHJKMNPQRSTUVWXYZ23456789' < /dev/urandom | head -c 10)
HASH=$(printf '%s' "$CODE" | sha256sum | cut -d' ' -f1)
EXPIRES=$(( $(date +%s) + TTL ))

"$PYTHON" - "$STATE/pairing.json" "$HASH" "$EXPIRES" "$NAME" <<'EOF'
import json, os
import sys
def u(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
def out(x):
    sys.stdout.write(x.encode("utf-8") if sys.version_info[0] < 3 else x)
    sys.stdout.write("\n")
path, digest, expires, name = sys.argv[1:5]
tmp = path + ".tmp"
with open(tmp, "w") as stream:
    json.dump({"hash": digest, "expires": int(expires),
               "name": u(name)}, stream)
os.chmod(tmp, 0o600)
os.rename(tmp, path)
EOF

PRETTY=$(printf '%s' "$CODE" | sed 's/^\(.....\)\(.....\)$/\1-\2/')

cat <<EOF

  Appareil            : $NAME
  Code d'autorisation : $PRETTY
  (valable 10 minutes, utilisable une seule fois)

  Sur l'appareil à autoriser, ouvre :
  ${URL}?pair=$PRETTY
  puis appuie sur « Autoriser cet appareil ».

EOF

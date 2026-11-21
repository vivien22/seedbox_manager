#!/bin/sh
# Autorise un appareil (ton telephone) a utiliser le site web.
#   sh pair-device.sh           -> affiche un code a usage unique (valable 10 min)
#   sh pair-device.sh --revoke  -> retire l'acces a tous les appareils
#   sh pair-device.sh --list    -> affiche l'appareil autorise

set -eu

BASE="/media/sdt1/bibien/private/seedbox-manager"
STATE="$BASE/state"
URL="https://bibien.clymene.feralhosting.com/"
TTL=600

mkdir -p "$STATE"
chmod 700 "$STATE"

case "${1:-}" in
    --revoke)
        rm -f "$STATE/devices.json" "$STATE/pairing.json"
        echo "Accès retiré à tous les appareils."
        exit 0
        ;;
    --list)
        if [ -f "$STATE/devices.json" ]; then
            /usr/bin/python -c '
import json, time
for d in json.load(open("'"$STATE"'/devices.json")).get("devices", []):
    print("Autorisé le %s, vu le %s" % (
        time.strftime("%d/%m/%Y %H:%M", time.localtime(d["created"])),
        time.strftime("%d/%m/%Y %H:%M", time.localtime(d["last_seen"]))))
    print("  " + d.get("user_agent", ""))
'
        else
            echo "Aucun appareil autorisé."
        fi
        exit 0
        ;;
esac

# 10 caracteres sans ambiguite (pas de 0/O, 1/I/L) : ~50 bits, usage unique,
# valable 10 min, 5 essais par quart d'heure -> impossible a deviner.
CODE=$(LC_ALL=C tr -dc 'ABCDEFGHJKMNPQRSTUVWXYZ23456789' < /dev/urandom | head -c 10)
HASH=$(printf '%s' "$CODE" | sha256sum | cut -d' ' -f1)
EXPIRES=$(( $(date +%s) + TTL ))

TMP=$(mktemp "$STATE/.pairing-XXXXXX")
printf '{"hash":"%s","expires":%s}\n' "$HASH" "$EXPIRES" > "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$STATE/pairing.json"

PRETTY=$(printf '%s' "$CODE" | sed 's/^\(.....\)\(.....\)$/\1-\2/')

cat <<EOF

  Code d'autorisation : $PRETTY
  (valable 10 minutes, utilisable une seule fois)

  Sur ton téléphone, ouvre :
  ${URL}?pair=$PRETTY
  puis appuie sur « Autoriser cet appareil ».

  Les appareils autorisés auparavant perdront l'accès.

EOF

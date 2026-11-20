#!/bin/sh
# Installation de la synchro seedbox v3 sur la Raspberry Pi (LibreELEC).
# Usage (en root sur la Pi) : sh /storage/seedbox-v3/install-kodi.sh

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="/storage/.config/seedbox-sync.py"
UNIT_DIR="/storage/.config/system.d"
KEY="/storage/.ssh/feral_transfer"
HOST="bibien@clymene.feralhosting.com"
DEST_ROOT="/var/media/RaspPi"
SOURCES_XML="/storage/.kodi/userdata/sources.xml"
INTERVAL="2min"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/storage/.config/seedbox-sync-backup-$STAMP"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m %s\n' "$*"; }
warn() { printf '    \033[33mATTENTION\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- verifications
say "Vérifications"

[ -f "$HERE/pi/seedbox-sync.py" ] || die "pi/seedbox-sync.py manquant dans le paquet"
sed -i 's/\r$//' "$HERE/pi/seedbox-sync.py"

for bin in /usr/bin/python3 /usr/bin/ssh /usr/bin/sftp /usr/bin/kodi-send; do
    [ -x "$bin" ] || die "$bin introuvable"
done
ok "python3, ssh, sftp, kodi-send"

[ -f "$KEY" ] || die "clé SSH $KEY introuvable"
/usr/bin/ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 "$HOST" true \
    || die "connexion SSH à la seedbox impossible avec $KEY"
ok "connexion SSH à la seedbox"

if mountpoint -q "$DEST_ROOT" 2>/dev/null || grep -q " $DEST_ROOT " /proc/mounts; then
    ok "disque $DEST_ROOT monté"
else
    warn "$DEST_ROOT n'est pas monté : la synchro attendra le disque"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cp "$HERE/pi/seedbox-sync.py" "$TMP/seedbox-sync.py"
/usr/bin/python3 -m py_compile "$TMP/seedbox-sync.py" || die "erreur de syntaxe dans seedbox-sync.py"
ok "syntaxe du script"

# ----------------------------------------------------------------- sauvegarde
say "Sauvegarde dans $BACKUP"
mkdir -p "$BACKUP"
if [ -f "$SCRIPT" ]; then cp -p "$SCRIPT" "$BACKUP/"; fi
for unit in seedbox-sync.service seedbox-sync.timer; do
    if [ -f "$UNIT_DIR/$unit" ]; then cp -p "$UNIT_DIR/$unit" "$BACKUP/"; fi
done
crontab -l > "$BACKUP/crontab.txt" 2>/dev/null || true
ok "ancienne version sauvegardée"

# Arret de l'ancienne synchro (l'ancien service etait une boucle permanente).
# Un transfert interrompu reprendra automatiquement avec la nouvelle version.
if systemctl is-active -q seedbox-sync.service 2>/dev/null; then
    systemctl stop seedbox-sync.service
    ok "ancien service seedbox-sync arrêté"
fi
if [ -f /storage/.cache/seedbox-sync.lock ]; then
    i=0
    while ! flock -n /storage/.cache/seedbox-sync.lock true 2>/dev/null; do
        i=$((i + 1))
        if [ $i -ge 60 ]; then warn "ancienne synchro toujours en cours après 5 min"; break; fi
        sleep 5
    done
fi

# --------------------------------------------------------------- installation
say "Installation"
cp "$HERE/pi/seedbox-sync.py" "$SCRIPT"
chmod 700 "$SCRIPT"
mkdir -p /storage/.cache/seedbox-sync

# Si les sources Kodi ne pointent pas vers le disque sous ce chemin, le scan
# cible d'un dossier ne trouverait rien : on scanne alors toute la mediatheque.
if [ -f "$SOURCES_XML" ] && grep -q "$DEST_ROOT" "$SOURCES_XML"; then
    ok "sources Kodi sous $DEST_ROOT : scan ciblé du film / de la série"
else
    sed -i 's/^SCAN_WHOLE_LIBRARY = False/SCAN_WHOLE_LIBRARY = True/' "$SCRIPT"
    warn "sources Kodi déclarées sous un autre chemin : scan de toute la médiathèque"
fi
ok "script installé dans $SCRIPT"

# Ancienne planification par cron : on la retire (copie dans la sauvegarde)
if crontab -l 2>/dev/null | grep -q "seedbox-sync"; then
    crontab -l | grep -v "seedbox-sync" | crontab -
    ok "ancienne entrée cron supprimée"
fi

mkdir -p "$UNIT_DIR"
for other in $(grep -l "seedbox-sync.py" "$UNIT_DIR"/* 2>/dev/null || true); do
    case "$(basename "$other")" in
        seedbox-sync.service|seedbox-sync.timer|*.bak|*.old|*.orig) ;;
        *) warn "autre unité systemd qui lance déjà la synchro : $other (à désactiver)";;
    esac
done
cat > "$UNIT_DIR/seedbox-sync.service" <<EOF
[Unit]
Description=Synchronisation seedbox vers le disque USB
After=network-online.target kodi.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $SCRIPT
Nice=10
IOSchedulingClass=idle
EOF

cat > "$UNIT_DIR/seedbox-sync.timer" <<EOF
[Unit]
Description=Synchronisation seedbox toutes les $INTERVAL

[Timer]
OnBootSec=2min
OnUnitInactiveSec=$INTERVAL

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable seedbox-sync.timer >/dev/null 2>&1
systemctl restart seedbox-sync.timer
ok "minuteur systemd actif (toutes les $INTERVAL après la fin de la synchro précédente)"

# ------------------------------------------------------------- premier passage
say "Premier passage"
systemctl start --no-block seedbox-sync.service
sleep 20
journalctl -u seedbox-sync.service -n 15 --no-pager || true

cat <<EOF

Installation terminée.
  Journal en direct : journalctl -fu seedbox-sync
  Lancer à la main  : systemctl start seedbox-sync
  Retour arrière    : systemctl disable --now seedbox-sync.timer \\
                      && cp $BACKUP/seedbox-sync.py $SCRIPT \\
                      && crontab $BACKUP/crontab.txt
EOF

#!/bin/sh
# Installation de Seedbox Manager v3 sur la seedbox Feral.
# Usage : sh ~/seedbox-v3/install-seedbox.sh
# Sauvegarde l'existant, installe les scripts et la page web, relance la boucle.

set -eu

BASE="/media/sdt1/bibien/private/seedbox-manager"
WEB="$HOME/www/bibien.clymene/public_html"
HERE=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BASE/backup-v3-$STAMP"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m %s\n' "$*"; }
warn() { printf '    \033[33mATTENTION\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- verifications
say "Vérifications"

for f in seedbox/worker.py seedbox/organizer_v2.py seedbox/status_dump.py seedbox/commands.py seedbox/pair-device.sh \
         seedbox/seedbox-loop.sh web/index.php; do
    [ -f "$HERE/$f" ] || die "fichier manquant dans le paquet : $f"
done
# Fichiers passes par Windows : on retire d'eventuels retours chariot
find "$HERE" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.php' \) \
    -exec sed -i 's/\r$//' {} +
ok "paquet complet ($HERE)"

[ -d "$WEB" ] || die "$WEB introuvable (dossier web de Feral)"
[ -f "/media/sdt1/bibien/.config/deluge/auth" ] || die "fichier d'authentification Deluge introuvable"
[ -d "/media/sdt1/bibien/private/deluge/data/Vivien/Videos" ] \
    || die "dossier de téléchargement Deluge introuvable : ~/private/deluge/data/Vivien/Videos"
if [ -d "$BASE" ] && [ -f "$BASE/seedbox-loop.sh" ]; then
    ok "mise à jour de $BASE"
    FRESH=0
else
    ok "installation neuve dans $BASE"
    FRESH=1
fi

/usr/bin/python -c 'import twisted, deluge.ui.client' 2>/dev/null \
    || die "Python 2 / Deluge non importables avec /usr/bin/python"
ok "Python 2 + Deluge"

command -v flock >/dev/null || die "flock introuvable"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for f in worker.py organizer_v2.py status_dump.py commands.py; do
    cp "$HERE/seedbox/$f" "$TMP/$f"
    /usr/bin/python -m py_compile "$TMP/$f" || die "erreur de syntaxe dans $f"
done
sh -n "$HERE/seedbox/seedbox-loop.sh" || die "erreur de syntaxe dans seedbox-loop.sh"
sh -n "$HERE/seedbox/pair-device.sh" || die "erreur de syntaxe dans pair-device.sh"
command -v sha256sum >/dev/null || die "sha256sum introuvable"
if command -v php >/dev/null; then
    php -l "$HERE/web/index.php" >/dev/null || die "erreur de syntaxe dans index.php"
fi
ok "syntaxe des scripts"

# Locale UTF-8 indispensable a Python 2 pour les chemins accentues
LOCALE=""
for candidate in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8 fr_FR.UTF-8 fr_FR.utf8; do
    if locale -a 2>/dev/null | grep -qx "$candidate"; then LOCALE="$candidate"; break; fi
done
if [ -z "$LOCALE" ]; then
    warn "aucune locale UTF-8 trouvée, C.UTF-8 utilisée par défaut"
    LOCALE="C.UTF-8"
fi
ok "locale $LOCALE"

# ----------------------------------------------------------------- sauvegarde
say "Sauvegarde dans $BACKUP"
mkdir -p "$BACKUP"
for f in worker.py organizer_v2.py status_dump.py commands.py pair-device.sh seedbox-loop.sh; do
    if [ -f "$BASE/$f" ]; then cp -p "$BASE/$f" "$BACKUP/"; fi
done
if [ -f "$WEB/index.php" ]; then cp -p "$WEB/index.php" "$BACKUP/index.php"; fi
crontab -l > "$BACKUP/crontab.txt" 2>/dev/null || true
ok "anciens fichiers et crontab sauvegardés"

# ------------------------------------------------------- arret de la boucle
say "Arrêt de la boucle en cours"
if pkill -f "$BASE/seedbox-loop.sh" 2>/dev/null; then ok "boucle arrêtée"; else ok "aucune boucle active"; fi

# On attend la fin d'un worker / organiseur en cours d'execution
flock -w 180 "$BASE/worker.lock" true    || warn "worker.lock toujours pris"
flock -w 180 "$BASE/organizer.lock" true || warn "organizer.lock toujours pris"
ok "plus aucun traitement en cours"

# --------------------------------------------------------------- installation
say "Installation"
mkdir -p "$BASE"
chmod 700 "$BASE"

# config.php : lu par la page web (hors de public_html)
if [ ! -f "$BASE/config.php" ]; then
    cat > "$BASE/config.php" <<PHP
<?php
return array(
    'queue_dir' => '$BASE/queue',
    'max_upload_bytes' => 52428800,
);
PHP
    chmod 600 "$BASE/config.php"
    ok "config.php créé"
fi
mkdir -p "$BASE/logs" "$BASE/status" "$BASE/state" "$BASE/organizer_queue" \
         "$BASE/organizer_processed" "$BASE/organizer_failed" \
         "$BASE/sync/ready" "$BASE/sync/processed" "$BASE/queue" \
         "$BASE/processed" "$BASE/failed" "$BASE/commands"
chmod 700 "$BASE/status" "$BASE/state" "$BASE/commands"

for f in worker.py organizer_v2.py status_dump.py commands.py pair-device.sh seedbox-loop.sh; do
    cp "$HERE/seedbox/$f" "$BASE/$f"
    chmod 700 "$BASE/$f"
done
sed -i "s/^export LANG=.*/export LANG=$LOCALE/; s/^export LC_ALL=.*/export LC_ALL=$LOCALE/" \
    "$BASE/seedbox-loop.sh"
rm -f "$BASE"/*.pyc
ok "scripts installés dans $BASE"

cp "$HERE/web/index.php" "$WEB/index.php"
chmod 644 "$WEB/index.php"
ok "page web installée dans $WEB"

# ---------------------------------------------------------------------- cron
say "Démarrage automatique (cron)"
LOOP_CMD="/usr/bin/flock -n $BASE/seedbox-loop.lock $BASE/seedbox-loop.sh >> $BASE/logs/seedbox-loop.log 2>&1"
if crontab -l 2>/dev/null | grep -q "seedbox-loop.sh"; then
    ok "une entrée cron existe déjà pour seedbox-loop.sh (conservée) :"
    crontab -l | grep "seedbox-loop.sh" | sed 's/^/      /'
else
    { crontab -l 2>/dev/null || true
      echo "@reboot $LOOP_CMD"
      echo "*/5 * * * * $LOOP_CMD"
    } | crontab -
    ok "entrées cron ajoutées (@reboot + relance toutes les 5 min si arrêtée)"
fi

# ------------------------------------------------------------------ demarrage
say "Démarrage de la boucle"
# Le "sleep 15" de l'ancienne boucle herite du verrou : on attend qu'il le
# libere (ou que la cron ait deja relance la nouvelle boucle).
if pgrep -f "$BASE/seedbox-loop.sh" >/dev/null; then
    ok "boucle déjà relancée par la cron"
else
    flock -w 60 "$BASE/seedbox-loop.lock" true 2>/dev/null || true
    nohup sh -c "$LOOP_CMD" >/dev/null 2>&1 &
fi

i=0
while [ $i -lt 12 ]; do
    if pgrep -f "$BASE/seedbox-loop.sh" >/dev/null && [ -f "$BASE/status/seedbox.json" ]; then
        break
    fi
    sleep 5
    i=$((i + 1))
done

if pgrep -f "$BASE/seedbox-loop.sh" >/dev/null; then
    ok "boucle active"
else
    warn "la boucle ne tourne pas encore (la cron la relancera dans la minute), voir $BASE/logs/seedbox-loop.log"
fi

if [ -f "$BASE/status/seedbox.json" ]; then
    ok "état exporté pour le site web (status/seedbox.json)"
else
    warn "status/seedbox.json absent, voir $BASE/logs/status.log"
fi

say "Derniers journaux"
for log in organizer.log status.log commands.log; do
    if [ -f "$BASE/logs/$log" ]; then echo "--- $log"; tail -n 8 "$BASE/logs/$log"; fi
done

if [ "$FRESH" = 1 ]; then
    cat <<EOF

Installation neuve : pense à
  - autoriser tes appareils sur le site : .\\deploy.ps1 -Pair Phone puis .\\deploy.ps1 -Pair PC
  - vérifier que le label Deluge « vivien » existe (journal : $BASE/logs/worker.log)
EOF
fi

cat <<EOF

Installation terminée.
  Site web        : https://bibien.clymene.feralhosting.com/
  Autoriser un appareil : .\\deploy.ps1 -Pair Phone  (ou sh $BASE/pair-device.sh Phone)
  Journaux        : $BASE/logs/
  Retour arrière  : cp $BACKUP/*.py $BACKUP/seedbox-loop.sh $BASE/ \\
                    && cp $BACKUP/index.php $WEB/ \\
                    && pkill -f $BASE/seedbox-loop.sh
EOF

# ------------------------------------------------------------------ menage
# Dossier de transit depose par deploy.ps1 : inutile une fois installe.
# (on ne supprime que ce dossier precis, jamais un clone du depot)
if [ "$HERE" = "$HOME/seedbox-v3" ]; then
    rm -rf "$HERE"
fi
# On garde les 5 sauvegardes les plus recentes
ls -1d "$BASE"/backup-v3-* 2>/dev/null | sort -r | tail -n +6 | while read -r old; do
    rm -rf "$old"
done

# Seedbox Manager v3

## Contenu

| Fichier | Où il va | Rôle |
|---|---|---|
| `seedbox/worker.py` | seedbox | ajoute les .torrent déposés à Deluge (label `vivien`) |
| `seedbox/organizer_v2.py` | seedbox | range Films / Séries, écrit le manifeste pour la Pi |
| `seedbox/commands.py` | seedbox | supprime de Deluge (avec les données) les torrents demandés depuis le site |
| `seedbox/pair-device.sh` | seedbox | autorise ton téléphone sur le site (code à usage unique) |
| `seedbox/status_dump.py` | seedbox | **nouveau** : exporte l'état des téléchargements pour le site |
| `seedbox/seedbox-loop.sh` | seedbox | boucle toutes les 15 s (worker → organiseur → état) |
| `web/index.php` | seedbox (public_html) | envoi de torrents + **suivi seedbox et Pi en direct** |
| `pi/seedbox-sync.py` | Raspberry Pi | rapatrie les fichiers, **visionnage pendant le téléchargement** |
| `install-seedbox.sh` | seedbox | installe tout côté seedbox |
| `install-kodi.sh` | Raspberry Pi | installe la synchro + minuteur systemd |
| `deploy.ps1` / `deploy.bat` | PC Windows | envoie tout et lance les deux installations |

## Installation depuis le PC (Windows)

**Le plus simple** : double-clic sur `deploy.bat`. L'adresse IP de la
Raspberry Pi est déjà réglée (192.168.1.100) ; réponds simplement aux
demandes de mot de passe (Feral, puis `libreelec` pour la Pi).

Ou dans PowerShell, depuis ce dossier :

```powershell
.\deploy.ps1 -PiHost 192.168.1.50              # seedbox puis Pi
.\deploy.ps1 -SeedboxOnly                      # seedbox seulement
.\deploy.ps1 -PiHost 192.168.1.50 -PiOnly      # Pi seulement
.\deploy.ps1 -PiHost 192.168.1.50 -SetupKeys   # installe ta clé SSH : plus de mot de passe ensuite
```

Si PowerShell refuse d'exécuter le script :
`powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -PiHost 192.168.1.50`

`deploy.ps1` copie les fichiers dans `~/seedbox-v3` (seedbox) et
`/storage/seedbox-v3` (Pi), puis lance `install-seedbox.sh` et `install-kodi.sh`
par SSH ; tu vois leur sortie en direct.

Chaque script vérifie les prérequis, sauvegarde l'ancienne version
(`backup-v3-<date>` sur la seedbox, `/storage/.config/seedbox-sync-backup-<date>`
sur la Pi), installe, relance, puis affiche la commande de retour arrière.

## Visionnage pendant le téléchargement

La Pi télécharge désormais directement sous le nom final (plus de `.partial`),
fichier par fichier, vidéos d'abord. Dès que 64 Mo d'une vidéo sont arrivés,
elle demande à Kodi de scanner le dossier du film ou de la série : le titre
apparaît dans la médiathèque et peut être lancé.

À savoir :
- La lecture **s'arrête** si elle rattrape le téléchargement : relance-la
  (Kodi propose de reprendre là où tu étais). Avec la fibre et une seedbox
  Feral, le transfert va en général bien plus vite que la lecture.
- Les **.mkv** se lisent sans problème. Certains **.mp4** ont leur index à la
  fin du fichier et ne démarrent qu'une fois complets.
- L'avance rapide au-delà de la partie déjà reçue ne fonctionne pas.
- Les fichiers en cours sont listés dans `/storage/.cache/seedbox-sync/in-progress.json` ;
  une coupure (redémarrage, réseau) reprend automatiquement au passage suivant.

## Accès au site : ton téléphone uniquement

Il n'y a plus de mot de passe. Le site n'est accessible qu'à **un seul
appareil autorisé** ; tout autre navigateur voit « Appareil non autorisé ».

1. Sur le PC : `.\deploy.ps1 -PairPhone` affiche un code (valable 10 min,
   utilisable une seule fois) et un lien.
2. Sur le téléphone : ouvre le lien (ou le site, puis tape le code) et
   appuie sur **Autoriser cet appareil**.

Le téléphone reçoit un jeton aléatoire de 256 bits dans un cookie protégé
(HttpOnly, valable 10 ans et prolongé à chaque visite) ; la seedbox n'en
garde que l'empreinte. Autoriser un nouvel appareil retire l'accès à
l'ancien. Si tu vides les cookies du navigateur ou changes de téléphone,
refais l'étape 1.

Sur la seedbox :
```sh
sh ~/private/seedbox-manager/pair-device.sh --list     # appareil autorisé
sh ~/private/seedbox-manager/pair-device.sh --revoke   # retirer tout accès
```

## Suivre les transferts sur la Pi

```powershell
.\deploy.ps1 -PiStatus
```
ou directement : `ssh -t root@192.168.1.100 python3 /storage/.config/seedbox-status.py --watch`

## Suppression depuis le site

Le bouton **Supprimer de la seedbox** retire le torrent de Deluge **avec ses
fichiers**, uniquement sur la seedbox : rien n'est supprimé sur la Raspberry Pi.

- Seuls les torrents du label `vivien` peuvent être supprimés.
- Si le torrent n'est pas encore sur la Pi, une confirmation prévient qu'il ne
  sera jamais transféré (son manifeste est mis de côté dans `sync/cancelled/`).
- Pendant que la Pi le récupère, la suppression est refusée.
- Journal : `logs/commands.log`.

## Commandes utiles

Seedbox :
```sh
tail -f ~/private/seedbox-manager/logs/organizer.log
ls ~/private/seedbox-manager/organizer_failed/     # tâches abandonnées + .reason
```

Une tâche abandonnée se relance en remettant son `.job` dans `organizer_queue/` :
```sh
cd ~/private/seedbox-manager && mv organizer_failed/<id>.job organizer_queue/ && rm organizer_failed/<id>.reason
```

Raspberry Pi :
```sh
journalctl -fu seedbox-sync        # journal en direct
systemctl start seedbox-sync       # synchro immédiate
systemctl list-timers seedbox-sync # prochain passage
```

Si les nouveaux fichiers n'apparaissent pas dans Kodi, mets
`SCAN_WHOLE_LIBRARY = True` dans `/storage/.config/seedbox-sync.py`.

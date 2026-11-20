# Seedbox Manager v3

## Contenu

| Fichier | Où il va | Rôle |
|---|---|---|
| `seedbox/worker.py` | seedbox | ajoute les .torrent déposés à Deluge (label `vivien`) |
| `seedbox/organizer_v2.py` | seedbox | range Films / Séries, écrit le manifeste pour la Pi |
| `seedbox/status_dump.py` | seedbox | **nouveau** : exporte l'état des téléchargements pour le site |
| `seedbox/seedbox-loop.sh` | seedbox | boucle toutes les 15 s (worker → organiseur → état) |
| `web/index.php` | seedbox (public_html) | envoi de torrents + **suivi seedbox et Pi en direct** |
| `pi/seedbox-sync.py` | Raspberry Pi | rapatrie les fichiers, **visionnage pendant le téléchargement** |
| `install-seedbox.sh` | seedbox | installe tout côté seedbox |
| `install-kodi.sh` | Raspberry Pi | installe la synchro + minuteur systemd |

## Installation (depuis PowerShell sur le PC)

```powershell
cd $HOME\Desktop\seedbox-manager

# 1. Seedbox (en premier)
scp -r seedbox-v3 bibien@clymene.feralhosting.com:~/
ssh bibien@clymene.feralhosting.com "sh ~/seedbox-v3/install-seedbox.sh"

# 2. Raspberry Pi (remplace 192.168.1.50 par son adresse)
scp -r seedbox-v3 root@192.168.1.50:/storage/
ssh root@192.168.1.50 "sh /storage/seedbox-v3/install-kodi.sh"
```

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

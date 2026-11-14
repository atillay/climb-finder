#!/bin/bash
# Met en ligne le site (lecture seule) et la base des côtes sur le serveur (voir deploy.env).
# À relancer après chaque calcul local pour publier les nouvelles côtes :
#   ./deployer.sh
# Seuls le site et la base partent : aucun téléchargement ni calcul ne tourne sur le serveur.
set -euo pipefail
cd "$(dirname "$0")"

# serveur, port SSH et adresse publique : deploy.env (non versionné, modèle : deploy.env.example)
[ -f deploy.env ] || { echo "deploy.env manquant (copier deploy.env.example)"; exit 1; }
source deploy.env
SSH=(ssh -p "$PORT_SSH" "$HOTE")

echo "1/4 Tests"
python3 test_scanner.py > /dev/null 2>&1 || { echo "Tests en échec : déploiement annulé (python3 test_scanner.py)"; exit 1; }

echo "2/4 Copie cohérente de la base (même si precompute.py tourne)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 - "$TMP/cotes.sqlite" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("data/cotes.sqlite")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.execute("PRAGMA journal_mode=DELETE")
dst.execute("VACUUM")
n = dst.execute("select count(*) from climbs").fetchone()[0]
t = dst.execute("select count(*) from tiles").fetchone()[0]
dst.close()
print(f"    {n} côtes, {t} cases")
EOF
mkdir -p "$TMP/app/static"
cp server.py store.py scanner.py "$TMP/app/"
cp static/index.html "$TMP/app/static/"

echo "3/4 Envoi"
rsync -az --delete -e "ssh -p $PORT_SSH" "$TMP/app/" "$HOTE:climb-finder-envoi/app/"
rsync -az -e "ssh -p $PORT_SSH" "$TMP/cotes.sqlite" "$HOTE:climb-finder-envoi/cotes.sqlite"

echo "4/4 Installation et redémarrage"
"${SSH[@]}" 'set -e
sudo rsync -a --delete --chown=root:root --chmod=D755,F644 ~/climb-finder-envoi/app/ /opt/climb-finder/
sudo systemctl stop climb-finder
sudo rm -f /var/lib/climb-finder/cotes.sqlite-wal /var/lib/climb-finder/cotes.sqlite-shm
sudo install -o climbfinder -g climbfinder -m 0640 ~/climb-finder-envoi/cotes.sqlite /var/lib/climb-finder/cotes.sqlite
sudo systemctl start climb-finder
rm -f ~/climb-finder-envoi/cotes.sqlite
for k in $(seq 1 20); do curl -fsS -o /dev/null http://127.0.0.1:8765/api/status 2>/dev/null && break; sleep 0.5; done
curl -fsS http://127.0.0.1:8765/api/status; echo'

curl -fsS -o /dev/null "$URL" && echo "En ligne : $URL"

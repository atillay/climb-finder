# Côtes autour de moi — notes pour Claude Code

Projet perso, non lucratif : trouver les côtes et les « murs » à vélo (routes revêtues uniquement)
autour d'un point, classés par difficulté. L'utilisateur parle français : réponses, messages et
interface en français.

## Architecture

- `scanner.py` : téléchargements (Overpass par case de 0,04° × 0,06° ≈ 4,5 km, cache `cache/osm/` ;
  MNT RGE ALTI de l'IGN via WMS `image/x-bil;bits=32`, cache `cache/dem/`), filtre des routes revêtues,
  recollage des tronçons aux carrefours, détection des côtes, métriques, `process_tile()`.
- `store.py` : base SQLite `data/cotes.sqlite` (tables `climbs`, `tiles`), recherche par rayon
  (`tile IN (…)` des cases du cercle), filtres, tri, pagination. Tracés et profils stockés au format
  compact « polyline » (préfixe `!`, ancien JSON encore lisible ; `python3 store.py --compact`
  convertit une vieille base).
- `precompute.py` : calcul d'une grande zone, autonome (une case en échec est journalisée et
  retentée plus tard, pause si le réseau tombe, filet de sécurité global). Journal :
  `logs/precompute.log`. État en direct : `data/precompute_status.json` (affiché sur le site).
- `lancer-nuit.sh` : `caffeinate` + relance automatique de `precompute.py` en cas d'arrêt inattendu.
- `france.py` + `lancer-france.sh` : indexation de toute la France sans Overpass, à partir de
  l'extrait Geofabrik (`cache/geofabrik/france-latest.osm.pbf`, 5 Go) découpé avec `osmium`
  (Homebrew `osmium-tool`). Par blocs de 25 × 25 cases dans l'ordre de priorité (`PRIORITY`),
  calcul sur tous les cœurs moins un, cartes d'altitude supprimées dès qu'aucun bloc restant n'en a
  besoin (`data/france_state.json`, champ `owned_maps` ; celles d'avant sont gardées). Journal
  `logs/france.log`, avancement `data/france_status.json`. Reprenable à tout moment.
  ⚠ Un changement d'`ALGO_VERSION` oblige alors à retélécharger les cartes d'altitude des blocs
  déjà faits (~4 par case).
- `server.py` : site + API sur le port 8765 (ou le suivant libre), calcul de zone en tâche de fond.
- `static/index.html` : interface Leaflet, sans build.
- `deployer.sh` : met en ligne le site (`server.py --readonly`) et une copie de la base, via SSH
  (paramètres dans `deploy.env`, non versionné). Service systemd : `deploy/climb-finder.service`.
  Détails du serveur (hôte, Caddy, DNS, autres services à vérifier) : `CLAUDE.local.md`, non versionné.
- `test_scanner.py` : tests hors-ligne.
- `EVOLUTIONS.md` : évolutions demandées par l'utilisateur (les variantes / enchaînements de rues
  sont faites, avec les choix retenus). À lire avant de toucher à la détection.
- Zones en base : Paris (défaut de `precompute.py`), Nantes (`--lat 47.2184 --lon -1.5536`),
  Grasse 06130 (`--lat 43.6589 --lon 6.9237`), La Rochelle (`--lat 46.1603 --lon -1.1511`),
  50 km chacune. `--status` avec les mêmes
  coordonnées pour suivre une zone.

Contraintes : **bibliothèque standard Python uniquement** (pas de pip). Garder les tests verts.
Si la détection change, incrémenter `ALGO_VERSION` dans `scanner.py` : les cases sont alors
recalculées depuis le cache, sans retéléchargement.

## Commandes

```bash
python3 test_scanner.py                 # tests (à lancer après toute modification)
python3 scanner.py --check              # services OSM / IGN joignables ?
python3 precompute.py --status          # avancement de la base (zone Paris 50 km par défaut)
nohup ./lancer-nuit.sh > logs/nuit.out 2>&1 &    # calcul de fond, survit à la fermeture du terminal
tail -n 50 logs/precompute.log          # suivre le calcul
grep -A30 ERROR logs/precompute.log | tail -80   # dernières erreurs avec leur traceback
pgrep -fl precompute.py                 # le calcul tourne-t-il ?
pkill -TERM -f lancer-nuit; pkill -TERM -f "caffeinate -i bash"; pkill -TERM -f precompute.py
                                        # arrêter le calcul lancé avec nohup … & (il ignore SIGINT)
python3 server.py                       # le site
nohup ./lancer-france.sh > logs/france.out 2>&1 &   # toute la France (reprenable)
python3 france.py --status              # avancement France
tail -n 30 logs/france.log              # suivre (une ligne par bloc : extraction, altitudes, calcul)
pkill -TERM -f lancer-france; pkill -TERM -f "caffeinate -i bash"; pkill -TERM -f france.py
```

## Surveiller et réparer le calcul de fond

Objectif de l'utilisateur : que le remplissage de la base (50 km autour de Paris, ~470 cases)
avance en autonomie, et que les problèmes soient corrigés sans lui.

1. Vérifier qu'il tourne (`pgrep -fl precompute.py`) et qu'il avance (`precompute.py --status`,
   fin de `logs/precompute.log`, champ `updated` de `data/precompute_status.json`).
2. En cas d'erreurs répétées, lire les tracebacks dans `logs/precompute.log` :
   - une erreur réseau ou de service (Overpass 429/504, timeout IGN) est normale : le script
     retente tout seul, il n'y a rien à corriger ;
   - une erreur de code (exception Python récurrente sur certaines cases) : reproduire sur la case
     concernée (`scanner.process_tile((i, j), scanner.DemService())`), corriger, ajouter un test
     dans `test_scanner.py`, lancer les tests, puis relancer `lancer-nuit.sh`. Les cases déjà
     en base sont conservées.
3. Le calcul d'une case avec les variantes prend 5 à 20 s (plus en ville dense) : c'est normal.
4. Ne jamais supprimer `data/cotes.sqlite`. `cache/` peut être supprimé (il se reconstruit, mais
   le retéléchargement est long).

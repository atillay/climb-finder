# Climb Finder — les côtes à vélo autour de moi

Trouver les côtes et les « murs » à vélo autour d'un point, classés par difficulté, sur des routes
revêtues uniquement. Toute la France métropolitaine est calculée (plus d'un million de côtes et
variantes) à partir d'OpenStreetMap et du modèle de terrain de l'IGN.

**En ligne : https://climb-finder.alty.fr/**

- Recherche autour d'une adresse, de ta position, ou de la zone affichée sur la carte (jusqu'à 30 km).
- Pour chaque côte : longueur, dénivelé, pente moyenne et maximale (sur 100 m et 300 m), score de
  difficulté, profil coloré, revêtement (pavés repérés), nombre de carrefours et de feux/stops.
- Les **variantes** : les autres façons d'arriver au même sommet en enchaînant plusieurs rues.
- Export GPX, Street View au pied de la côte, carte des segments Strava, lien de partage.

Le code est en **Python 3, bibliothèque standard uniquement** (aucune dépendance à installer) ;
l'interface est une seule page HTML avec [Leaflet](https://leafletjs.com/).

## Démarrage

```bash
python3 test_scanner.py           # tests hors ligne
python3 scanner.py --check        # vérifie qu'OpenStreetMap (Overpass) et l'IGN répondent

# Calculer une zone (à laisser tourner, reprenable à tout moment)
./lancer-nuit.sh                                         # 50 km autour de Paris
./lancer-nuit.sh --lat 45.764 --lon 4.835 --radius 30    # une autre zone
python3 precompute.py --status                           # avancement

# Le site
python3 server.py                                        # http://localhost:8765
```

`lancer-nuit.sh` empêche la mise en veille du Mac (`caffeinate`) et relance le calcul s'il
s'arrête. Le calcul ne s'arrête pas sur une erreur : une case en échec (réseau, service saturé) est
journalisée dans `logs/precompute.log` puis retentée plus tard. Depuis le site lancé en local, le
bouton « Calculer la zone » calcule aussi en tâche de fond les cases manquantes du cercle affiché.

### Toute la France

```bash
brew install osmium-tool                               # découpe des extraits OpenStreetMap
nohup ./lancer-france.sh > logs/france.out 2>&1 &      # reprenable ; met le site à jour à la fin
python3 france.py --status
```

Overpass n'est pas fait pour aspirer tout un pays : `france.py` part de l'extrait OpenStreetMap de
la France ([Geofabrik](https://download.geofabrik.de/europe/france.html), ~5 Go, téléchargé une
fois), qu'`osmium` réduit aux routes puis découpe par blocs de 25 × 25 cases (~110 km). Les blocs
sont traités par ordre de priorité (grandes villes, montagnes, puis le reste), avec un processus de
calcul par cœur. Les cartes d'altitude du bloc suivant se téléchargent pendant le calcul et sont
supprimées dès qu'aucun bloc restant n'en a besoin. Compter une dizaine d'heures pour la France
entière, surtout pour télécharger les altitudes.

### Mise en ligne

Le serveur de production ne télécharge et ne calcule rien : il sert le site en lecture seule
(`server.py --readonly`) avec une copie de la base calculée en local.

```bash
cp deploy.env.example deploy.env   # serveur SSH, port, adresse publique (fichier non versionné)
./deployer.sh                      # tests, copie cohérente de la base, envoi, redémarrage du service
```

Unité systemd d'exemple : `deploy/climb-finder.service` (utilisateur dédié, mémoire bornée,
système de fichiers en lecture seule sauf la base). Le service écoute en local, derrière un proxy
HTTPS (Caddy, nginx…).

## Utilisation du site

- **Centre** : le champ d'adresse (suggestions au fil de la frappe, API Adresse de l'État),
  « Ma position », ou simplement en déplaçant la carte : la recherche suit son centre.
  **Rayon** : le curseur (30 km au plus).
- **Tableau** : tri par colonne, filtres (longueur, pente, D+, carrefours, nom), pages de 300.
  Une ligne par sommet ; « + N variantes » déplie les autres façons d'y arriver.
- **Fiche d'une côte** : clic sur la ligne ou sur le tracé (↑ ↓ pour passer à la suivante). ✕ ferme
  la fiche en restant sur la côte ; Échap ou un clic sur la carte désélectionne.
- **Lien** : chaque côte a son adresse (`?c=<id>`), que le bouton « Partager » envoie ou copie.
- **Carte** : couleur = pente moyenne ; zones non calculées hachurées en rouge.

## Comment c'est calculé

La carte est découpée en **cases fixes de 0,04° × 0,06° (~4,5 × 4,5 km)**, calculées une fois pour
toutes et stockées dans une base SQLite (`data/cotes.sqlite`).

1. **Routes** : voies OpenStreetMap de la case et des 3 km autour (pour suivre une côte qui sort de
   la case). Seules les routes revêtues sont gardées, les pavés étant marqués à part ; sont exclus
   les sentiers, chemins non revêtus, voies privées ou interdites aux vélos. Une route sans
   information de revêtement est considérée revêtue (« supposé »).
2. **Altitude** : le MNT RGE ALTI de l'IGN (~5,5 m par pixel), lu en local après téléchargement.
3. **Routes continues** : les tronçons sont recollés aux carrefours en suivant d'abord la rue du même
   nom (même si elle tourne), sinon la plus droite. Un point tous les 10 m. Sur les ponts et dans
   les tunnels, l'altitude est interpolée entre les deux extrémités. Les pentes impossibles sont
   des erreurs du MNT (route en corniche) et sont écartées : plus de 30 % sur 100 m sur une route
   classée, plus de 50 % sur 50 m sur n'importe quelle route.
4. **Détection** dans les deux sens, sens uniques respectés : une côte s'arrête si l'on redescend de
   plus de 10 m ou après un replat de 500 m ; on garde les côtes d'au moins 100 m, 12 m de D+ et 3 %.
5. **Difficulté** = Σ pente(%)² × longueur(km), par tronçons de 50 m.
   Repères : mur de 300 m à 20 % ≈ 120 ; 1 km à 8 % ≈ 64 ; 5 km à 6 % ≈ 180.
6. **Variantes** : depuis le sommet de chaque côte, on redescend le réseau par toutes les rues qui y
   montent (virage de moins de 135°, jamais deux fois le même nœud, 3 changements de rue au plus).
   Au plus 5 variantes par sommet, les plus directes ; la route classique est toujours en tête.
7. **Carrefours** : carrefours traversés entre le pied et le sommet (dont grands axes), feux et stops.

Chaque côte est rattachée à la case qui contient son pied (pas de doublon). Tracés et profils sont
stockés au format compact « polyline ». Si la détection change (`ALGO_VERSION` dans `scanner.py`),
les cases sont recalculées à partir du cache.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `scanner.py` | téléchargements (Overpass, MNT IGN), réseau routier, détection, variantes, métriques |
| `store.py` | base SQLite : enregistrement, recherche par rayon, filtres, tri, pagination |
| `precompute.py`, `lancer-nuit.sh` | calcul d'une zone via Overpass, autonome et reprenable |
| `france.py`, `lancer-france.sh` | calcul de toute la France à partir de l'extrait Geofabrik |
| `server.py` | le site et son API (et le calcul à la demande en local) |
| `static/index.html` | l'interface (Leaflet, sans build) |
| `deployer.sh`, `deploy/` | mise en ligne (paramètres dans `deploy.env`, non versionné) |
| `test_scanner.py` | tests hors ligne |
| `EVOLUTIONS.md` | évolutions réalisées et à venir, avec les choix retenus |
| `CLAUDE.md` | notes pour Claude Code (surveiller et réparer le calcul) |

`data/` (la base), `cache/` (téléchargements) et `logs/` ne sont pas versionnés.

## Données et licences

- Routes : © les contributeurs [OpenStreetMap](https://www.openstreetmap.org/copyright), sous
  licence ODbL ; extraits fournis par [Geofabrik](https://www.geofabrik.de/).
- Altitudes : RGE ALTI® de l'[IGN](https://geoservices.ign.fr/), Licence Ouverte Etalab 2.0.
- Adresses : [API Adresse](https://adresse.data.gouv.fr/) (Géoplateforme).
- Fonds de carte : OpenStreetMap, OpenTopoMap, Plan IGN.

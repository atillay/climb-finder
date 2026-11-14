# Évolutions à prévoir

## 1. Variantes d'une côte : enchaînements de rues

> **Fait (ALGO_VERSION 4, 12/11/2026).** Implémenté dans `scanner.py` (`find_variants`,
> `select_variants`, `junction_stats`), `store.py` (colonnes `family`, `variant`, `streets`,
> `junctions`, `main_junctions`, `signals`, recherche regroupée par sommet) et `static/index.html`
> (lignes dépliables, colonne et filtre « Carrefours », variantes de la famille sur la carte et dans
> la fiche). Tests : `VariantTests` (dont le cas réel de la Rue de la Garenne, depuis le cache).
>
> Choix et écarts par rapport à ce qui suit :
> - en tête de famille : la **route classique** (si plusieurs routes classiques arrivent au même
>   sommet, la plus pentue) ; étiquettes « départ plus bas » (`longue` : contient la route
>   classique) et « autre départ » ;
> - la règle des 50 m ne s'applique qu'entre une variante et celle qu'elle prolonge (90 % de la
>   plus courte en commun) : deux trajets différents de même longueur sont gardés tous les deux ;
> - un départ est aussi proposé à chaque carrefour où l'on change de rue (sinon « Rue des Bois →
>   Rue de la Garenne » n'existait que prolongée par la rue d'avant) ;
> - carrefour compté = nœud traversé où arrive une autre route (hors desserte, piste cyclable,
>   chemin) ; « grand axe » = tertiaire ou plus. Feux/stops : seulement pour les cases téléchargées
>   après le 12/11/2026 (`null` sinon, « non relevés » sur le site) — Paris : à retélécharger ;
> - non fait : les interdictions de tourner (relations `restriction`, optionnel) ;
> - seuil de 70 % : à ajuster. Sur la Garenne, « Rue des Pommerets → Rue des Bois → Garenne »
>   (69 %) et « Rue Léon Cladel → … » (67 %) restent à côté de « Rue des Bois → Garenne ».

### Le besoin

Aujourd'hui, une côte suit une seule « route continue ». Aux carrefours, on prend la rue qui va le
plus droit, de préférence celle qui porte le même nom. Dès qu'il faut tourner, la côte s'arrête,
même si la montée continue dans la rue suivante. On rate donc les montées longues qui enchaînent
plusieurs rues, et les différentes façons d'arriver au même sommet.

**Cas de référence : Rue de la Garenne** (case `1220_36`)

| Variante | Enchaînement | État actuel |
|---|---|---|
| Courte (indexée aujourd'hui) | Rue de la Garenne | ✅ 680 m, 75 m D+, 11 % (id `4ef0c94ea2`) |
| Longue | Rue du Parc Cheviron → Rue Victor Pauchet → Rue de la Garenne | ❌ non trouvée |
| Autre départ | Rue des Bois → Rue de la Garenne | ❌ non trouvée (Rue des Bois est indexée seule : 229 m à 9,2 %, son sommet est à ~50 m du pied de la Rue de la Garenne) |

Rue Victor Pauchet n'apparaît pas du tout dans la base : seule, elle est trop courte pour
passer les seuils (100 m, 12 m D+). Il ne faut donc **pas** recoller des côtes déjà détectées, mais
chercher les enchaînements directement sur le réseau routier.

On veut indexer **toutes les variantes intéressantes** comme des côtes à part entière (score,
pentes max, profil, GPX), regroupées avec la côte principale.

### Conditions pour enchaîner deux rues

- Les rues se touchent vraiment : elles partagent un nœud OSM. Deux rues qui se croisent sans
  nœud commun (pont, tunnel, `layer` différent) ne s'enchaînent pas.
- Tout le trajet est roulable : même filtre que maintenant (revêtu, pas de `steps`, `path`,
  `footway`, voie privée ou interdite aux vélos), sens uniques respectés.
- Ça monte sur tout l'enchaînement : même règle qu'aujourd'hui (une redescente de plus de 10 m
  ou un replat de 500 m coupe la côte).
- Pas de demi-tour ni de virage absurde au carrefour (par ex. angle de virage < 135°), et on ne
  repasse jamais par le même nœud.
- Optionnel : respecter les interdictions de tourner (relations OSM `restriction`).

### Piste d'algorithme

1. Construire un graphe orienté « montant » : chaque tronçon entre deux carrefours, dans chaque
   sens autorisé, avec son profil d'altitude (déjà calculé).
2. Pour chaque sommet de côte (le haut de la Rue de la Garenne, par exemple), remonter le graphe
   vers l'aval : à chaque carrefour, essayer toutes les rues qui arrivent et qui montent vers
   ce carrefour, pas seulement la plus droite. C'est un parcours en profondeur, borné en
   longueur (~10 km) et en nombre de branches.
3. Chaque chemin trouvé est une variante candidate, avec un pied et un sommet. On calcule les
   mêmes métriques qu'aujourd'hui, avec le rognage des débuts plats.
4. Ne garder que les variantes intéressantes. **Règles décidées avec l'utilisateur :**
   - une variante doit ajouter **au moins 50 m de longueur** par rapport à la variante plus
     courte qu'elle prolonge. Sinon on garde la plus courte. Une variante qui part d'un autre pied
     (Rue des Bois) est comparée aux autres sur le même critère ;
   - **au plus 5 variantes par sommet**, en prenant **les plus directes, donc les plus courtes**.
     L'objectif reste de proposer le chemin le plus pentu, pas le plus long. En cas d'égalité
     de longueur, garder la plus pentue (pente moyenne) ;
   - la variante « principale » d'un sommet est la plus pentue.
   **Point d'attention : les cols à lacets** (soulevé par l'utilisateur). La route classique et
   la limite d'intersections sont **validées** par l'utilisateur ; le seuil de 70 % reste à ajuster. Sur un long col, d'anciennes routes coupent souvent tout droit
   entre les lacets de la départementale. Avec la seule règle « les plus courtes », on obtiendrait
   des patchworks « route + raccourci 1 + route + raccourci 3… » : jusqu'à 2^n combinaisons
   presque identiques, qui chasseraient la route classique du top 5. Garde-fous proposés :
   - **la route classique est toujours gardée** : la variante qui suit une seule et même route
     (même nom ou même `ref`, ce que détecte l'algorithme actuel) ne compte pas dans la limite
     de 5, et c'est elle qu'on affiche en tête de la famille ;
   - **des variantes vraiment différentes** : si deux variantes ont plus de 70 % de leur
     longueur en commun, on n'en garde qu'une, celle qui change le moins souvent de rue.
     « Route + un raccourci de 300 m » n'est donc pas une variante du col ;
   - **limiter les intersections** (validé, facteur important pour l'utilisateur) :
     - pas plus de **3 changements de rue** par variante ;
     - à longueur proche, préférer la variante qui **croise le moins de carrefours** (chaque
       carrefour = un arrêt possible, surtout en ville). Compter les nœuds de jonction
       traversés et les feux / stops OSM (`highway=traffic_signals`, `highway=stop`), avec un
       poids plus fort pour les croisements de routes principales ;
       ⚠ les feux et stops sont des nœuds OSM tagués : la requête Overpass actuelle ne récupère
       que les voies. Il faudra ajouter `node(...)["highway"~"^(traffic_signals|stop)$"]` à la
       requête, ce qui implique de **retélécharger les cases** (contrairement au reste de
       l'évolution, qui se recalcule depuis le cache). Les carrefours, eux, sont déjà connus ;
     - afficher ce nombre de carrefours dans le tableau et le détail d'une côte, pour qu'on
       puisse aussi trier ou filtrer dessus (utile pour trouver un mur « répétable » sans arrêt) ;
   - les raccourcis n'en restent pas moins visibles : un raccourci raide est déjà indexé comme
     une côte (un mur) à part entière.
5. Rattacher chaque variante à la case qui contient son pied, comme aujourd'hui. Pour ne pas
   couper les variantes longues, il faudra peut-être élargir la marge de 3 km.

### Modèle de données et interface

- Table `climbs` :
  - `family` : identifiant du sommet commun, pour regrouper les variantes ;
  - `variant` : `principale` | `longue` | `autre départ` ;
  - `streets` : la liste des rues traversées, par ex. « Rue du Parc Cheviron → Rue Victor
    Pauchet → Rue de la Garenne ».
- Tableau : **regroupement par sommet** (décidé). Une ligne par sommet, celle de la variante
  principale, avec un indicateur « + N variantes ». Elle se déplie pour montrer les variantes,
  de la plus courte à la plus longue. Le nom affiché est l'enchaînement des rues. Le tri et les
  filtres s'appliquent aux variantes : un sommet apparaît dès qu'une de ses variantes correspond.
- Carte : à la sélection d'une variante, afficher les autres variantes de la famille en plus
  léger.
- Incrémenter `ALGO_VERSION` : toutes les cases seront recalculées depuis le cache.

### Test de non-régression

Ajouter un test sur ce cas réel, à partir des fichiers de `cache/` des cases autour de
`1220_36`. Il doit trouver les trois variantes ci-dessus, Rue de la Garenne seule comprise, et
ne pas en produire de doublons.

## 2. Indexer toute la France

> **En place (13/11/2026)** : `france.py` + `lancer-france.sh` (extrait Geofabrik France découpé
> avec osmium, blocs de 25 × 25 cases par priorité, calcul sur 7 cœurs, cartes d'altitude
> supprimées au fil de l'eau, recherche par `tile IN (…)`). Essai sur le bloc d'Ajaccio : 60 cases,
> 3 709 côtes, 287 cartes d'altitude téléchargées en 83 s puis supprimées, calcul en 88 s.
> Toute la France calculée le 14/11/2026 : 1 052 421 côtes et variantes.
> Stockage compact (point 4) fait : tracés et profils en « polyline » (store.py, `encode_pairs`),
> tracé allégé recalculé à la volée ; base 4,1 Go -> 723 Mo, recherche plus rapide.
> Reste ouvert : mise à jour annuelle des extraits.

En cours : zones prioritaires lancées avec Claude Code (Nantes, Grasse), après Paris (50 km).

### Ordres de grandeur

- France métropolitaine ≈ 544 000 km², une case ≈ 20,5 km², donc **≈ 27 000 cases**
  (Paris 50 km = 470 cases).
- Avec la méthode actuelle :
  - Overpass : 10 à 60 s par case, donc **une à deux semaines** de téléchargement ;
  - cartes d'altitude : 4 par case ≈ 110 000 requêtes et ~50 Go ;
  - calcul : ~2 s par case, soit environ 15 h sur un seul cœur.

### Problème n° 1 : Overpass n'est pas fait pour ça

La politique d'usage d'Overpass vise ~10 000 requêtes et ~1 Go par jour pour un usage ponctuel
(à diviser par 100 pour un usage régulier). Le manuel cite explicitement comme abus le fait de
« recoller des rectangles » pour aspirer toutes les données d'une zone : c'est exactement ce
qu'on ferait sur 27 000 cases.

**Solution : les extraits OSM de Geofabrik** (fichiers `.osm.pbf` par région, mis à jour
quotidiennement, par ex. Île-de-France ≈ 312 Mo, Bretagne ≈ 306 Mo, Corse ≈ 32 Mo) :
1. télécharger l'extrait d'une région (un seul fichier, téléchargement autorisé et prévu pour ça) ;
2. ne garder que les routes, avec `osmium tags-filter` (outil `osmium-tool` via Homebrew : ce
   n'est pas une dépendance Python, la contrainte « bibliothèque standard » reste respectée côté
   code). Alternative sans outil externe : un lecteur PBF en Python pur (protobuf + zlib),
   faisable mais plus lent ;
3. répartir les voies dans les cases **au même format que `cache/osm/<i>_<j>.json`** : tout le reste
   du pipeline (`process_tile`, base, site) ne change pas ;
4. les cases sans route (mer, hors France) sont repérées tout de suite, et on n'y télécharge
   aucune carte d'altitude.

Overpass reste utile pour le calcul « à la demande » d'une petite zone depuis le site.

### Problème n° 2 : le volume des cartes d'altitude

- Court terme : garder le WMS de l'IGN, mais **supprimer les cartes d'altitude d'une case dès
  qu'elle et ses voisines sont calculées** (cache tournant). Le disque reste petit. Le
  téléchargement total (~50 Go) s'étale sur la durée, avec une limite de débit raisonnable.
- Plus tard, si besoin : RGE ALTI 5 m téléchargé en bloc par département (fichiers en
  Lambert-93, donc à reprojeter : la conversion Lambert-93 → GPS s'écrit en Python pur).

### Problème n° 3 : le temps de calcul

Paralléliser `process_tile` avec `multiprocessing` (bibliothèque standard), un processus par
cœur. Sur un Mac à 8 cœurs, on passe d'environ 15 h à 2-3 h pour toute la France, hors
téléchargements. Les cases d'une même région sont traitées par le même processus, pour profiter
du cache.

### Problème n° 4 : la taille de la base

- Estimation : 20 à 40 côtes par case, soit **0,5 à 1 million de côtes**, plus les variantes
  (évolution n° 1).
- Stocker tracé et profil de façon compacte (polyline encodée, entiers) : ~1 Ko par côte au lieu
  de 5 à 8, soit ~1 à 2 Go au total.
- Recherche par rayon : calculer les cases du cercle et faire `WHERE tile IN (…)` plutôt que
  d'utiliser l'index sur la latitude. Ça reste rapide même avec un million de lignes.
- Le site ne renvoie déjà au plus que 2 000 côtes, triées et filtrées côté serveur.

### Organisation proposée

1. **File de priorités** des cases à calculer :
   - zones demandées (Nantes, Grasse…) et clics « Calculer la zone » depuis le site ;
   - grandes villes ;
   - régions vélo et montagneuses ;
   - puis tout le reste.
2. **Région par région** : extrait Geofabrik → découpage en cases → calcul en parallèle →
   copie de la base sur le VPS (`deployer.sh`).
3. **Où faire tourner** : le Mac la nuit (lancer-nuit.sh) ou le VPS 24 h/24. Sur le VPS, le faire
   en priorité basse (`nice`, `ionice`), surveiller l'espace disque et vérifier que les autres
   services du serveur ne sont pas ralentis.
4. **Mise à jour** : retélécharger les extraits une à deux fois par an. Un changement
   d'algorithme se recalcule depuis le cache (`ALGO_VERSION`).

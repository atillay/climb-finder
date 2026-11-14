"""
Moteur de scan des côtes : routes revêtues OpenStreetMap + altitude IGN (RGE ALTI)
-> liste de côtes et de murs avec leurs métriques et un score de difficulté.

Aucune dépendance externe : uniquement la bibliothèque standard Python 3.9+.

Utilisation :
    python3 scanner.py --check     # vérifie que les services OSM / IGN répondent
Le calcul par cases et la base de données sont dans precompute.py et store.py.
"""

import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from array import array
import struct
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
WMS_URL = "https://data.geopf.fr/wms-r"
WMS_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"  # MNT issu du RGE ALTI
WMS_MIN_INTERVAL = 0.15
WMS_WORKERS = 4

# Découpage en cases (grille fixe en degrés, donc réutilisable d'un scan à l'autre)
OSM_TILE_DLAT, OSM_TILE_DLON = 0.04, 0.06    # ~4,4 x 4,5 km : une requête Overpass par case
DEM_TILE_DLAT, DEM_TILE_DLON = 0.02, 0.03    # 2 x 2 cartes d'altitude par case
DEM_MIN_Z = -100.0                           # en dessous : artefact (bord de mer), pas une altitude
DEM_PX = 400                                 # ~5,5 m par pixel
SCAN_MARGIN_M = 1500                         # routes chargées au-delà du rayon (côtes qui en sortent)
OSM_TILE_MAX_AGE_DAYS = 60

IGN_URL = "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json"
IGN_RESOURCE = "ign_rge_alti_wld"   # RGE ALTI (1 m / 5 m)
IGN_MAX_POINTS = 1000               # (limite documentée : 5000, mais les gros lots expirent souvent)
IGN_MIN_INTERVAL = 0.25             # 4 req/s (limite IGN : 5 req/s par IP)
IGN_WORKERS = 4
USER_AGENT = "cotes-finder-prototype/0.1 (projet perso non lucratif)"

# --- Paramètres de détection --------------------------------------------------
STEP_M = 10.0            # échantillonnage le long des routes
REVERSAL_M = 10.0        # une descente de plus de 10 m termine une côte
FLAT_SPLIT_M = 500.0     # un replat de 500 m ...
FLAT_SPLIT_GAIN_M = 5.0  # ... qui gagne moins de 5 m coupe la côte en deux
TRIM_WINDOW_M = 50.0     # on rogne les débuts/fins de côte ...
TRIM_GRADE = 0.02        # ... tant que la pente sur 50 m est < 2 %
MIN_LENGTH_M = 100.0
MIN_GAIN_M = 12.0
MIN_AVG_GRADE = 3.0      # %
SCORE_CHUNK_M = 50.0     # tronçons pour le score Σ pente² × longueur
MAX_DEFLECTION_DEG = 50  # angle max pour considérer qu'une route "continue" à un carrefour
MAX_DEFLECTION_SAME_NAME_DEG = 100  # idem quand les deux tronçons portent le même nom ou numéro (rue qui tourne)
MAX_RADIUS_KM = 50
# variantes (enchaînements de rues jusqu'au même sommet, voir EVOLUTIONS.md)
MAX_TURN_DEG = 135          # virage max au carrefour (au-delà : demi-tour, épingle absurde)
MAX_STREET_CHANGES = 3      # changements de rue max par variante
SHORT_PIECE_M = 25          # un morceau de rue plus court ne compte pas comme un changement de rue
MAX_VARIANT_M = 10000       # longueur max explorée vers l'aval
VARIANT_MIN_EXTRA_M = 50    # une variante doit différer d'au moins 50 m de longueur des autres
MAX_VARIANTS = 5            # variantes max par sommet (route classique non comprise)
VARIANT_OVERLAP = 0.7       # au-delà de 70 % de longueur commune, deux variantes sont « la même »
SUMMIT_MATCH_M = 25         # tolérance sur la position du sommet commun
VARIANT_BUDGET = 3000       # tronçons explorés au plus par côte de départ
VARIANT_MAX_EVAL = 150      # chemins candidats évalués au plus par côte de départ
MAJOR_ROADS = {"trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
               "tertiary", "tertiary_link"}
JUNCTION_IGNORE = {"service", "cycleway", "track"}
# pentes impossibles = erreur d'altitude : (fenêtre m, pente max, routes classées seulement)
IMPLAUSIBLE_GRADES = [(100.0, 0.30, True), (50.0, 0.50, False)]   # ces voies ne font pas un carrefour « arrêt »

EARTH_R = 6371008.8

# --- Filtre "route revêtue" ---------------------------------------------------
ROAD_CLASSES = {
    "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
}
PAVED = {
    "asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "chipseal",
    "paving_stones", "metal",
}
COBBLES = {"sett", "cobblestone", "unhewn_cobblestone", "cobblestone:flattened"}
EXCLUDED_SERVICE = {"driveway", "parking_aisle", "drive-through", "emergency_access"}

HIGHWAY_LABEL = {
    "residential": "Rue", "living_street": "Rue", "unclassified": "Petite route",
    "tertiary": "Route", "tertiary_link": "Route", "secondary": "Route",
    "secondary_link": "Route", "primary": "Route", "primary_link": "Route",
    "trunk": "Route", "trunk_link": "Route", "service": "Voie de desserte",
    "track": "Chemin revêtu", "cycleway": "Piste cyclable",
}


MINOR_STREETS = {"residential", "living_street", "service"}


def classify_way(tags, include_cobbles=False, skip_minor=False):
    """Renvoie None si la voie est exclue, sinon 'confirmed' | 'assumed' | 'cobbles'."""
    hw = tags.get("highway")
    if hw not in ROAD_CLASSES and hw not in ("service", "track", "cycleway"):
        return None
    if skip_minor and hw in MINOR_STREETS:
        return None
    if tags.get("area") == "yes" or tags.get("motorroad") == "yes":
        return None
    if tags.get("bicycle") in ("no", "dismount"):
        return None
    if tags.get("access") in ("private", "no") and tags.get("bicycle") not in (
        "yes", "designated", "permissive"
    ):
        return None
    if hw == "service" and tags.get("service") in EXCLUDED_SERVICE:
        return None

    surface = tags.get("surface")
    if surface:
        surface = surface.split(";")[0].strip()
        if surface in PAVED:
            return "confirmed"
        if surface in COBBLES:
            return "cobbles" if include_cobbles else None
        return None  # gravel, compacted, dirt, ground, grass, valeur inconnue...

    # Pas d'info de revêtement
    if hw == "track":
        return "assumed" if tags.get("tracktype") == "grade1" else None
    return "assumed"


def parse_oneway(tags):
    """+1 : sens de la way uniquement, -1 : sens inverse uniquement, 0 : double sens (vélo)."""
    ow = tags.get("oneway")
    if ow is None and tags.get("junction") in ("roundabout", "circular"):
        ow = "yes"
    if tags.get("oneway:bicycle") == "no" or tags.get("cycleway") in (
        "opposite", "opposite_lane", "opposite_track"
    ):
        return 0
    if ow in ("yes", "1", "true"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    return 0


def structure(tags):
    if tags.get("bridge") not in (None, "no"):
        return "bridge"
    if tags.get("tunnel") not in (None, "no") or tags.get("covered") == "yes":
        return "tunnel"
    return None


# --- Géométrie -----------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


class LocalProj:
    """Projection équirectangulaire locale (précise à quelques mm près sur 50 km)."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.ky = EARTH_R * math.pi / 180
        self.kx = self.ky * math.cos(math.radians(lat0))

    def xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)


# --- Cases ---------------------------------------------------------------------
def osm_tile_of(lat, lon):
    return (math.floor(lat / OSM_TILE_DLAT), math.floor(lon / OSM_TILE_DLON))


def osm_tile_bbox(t):
    i, j = t
    return (i * OSM_TILE_DLAT, j * OSM_TILE_DLON, (i + 1) * OSM_TILE_DLAT, (j + 1) * OSM_TILE_DLON)


def tiles_for_scan(lat, lon, radius_m):
    """Cases qui touchent le cercle (+ marge), triées de la plus proche à la plus lointaine."""
    reach = radius_m + SCAN_MARGIN_M
    proj = LocalProj(lat, lon)
    dlat = reach / proj.ky
    dlon = reach / proj.kx
    i0, j0 = osm_tile_of(lat - dlat, lon - dlon)
    i1, j1 = osm_tile_of(lat + dlat, lon + dlon)
    out = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            s, w, n, e = osm_tile_bbox((i, j))
            x0, y0 = proj.xy(s, w)
            x1, y1 = proj.xy(n, e)
            dx = max(x0, 0, -x1) if not (x0 <= 0 <= x1) else 0
            dy = max(y0, 0, -y1) if not (y0 <= 0 <= y1) else 0
            if math.hypot(dx, dy) <= reach:
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                out.append((math.hypot(cx, cy), (i, j)))
    return [t for _, t in sorted(out)]


# --- Overpass (une requête par case, mise en cache) ------------------------------
def build_overpass_query(bbox):
    a = "(%.5f,%.5f,%.5f,%.5f)" % bbox
    roads = "|".join(sorted(ROAD_CLASSES | {"service", "cycleway"}))
    hard = "asphalt|paved|concrete|concrete:plates|concrete:lanes|chipseal|paving_stones|sett|cobblestone|unhewn_cobblestone"
    return f"""[out:json][timeout:180];
(
  way{a}["highway"~"^({roads})$"]["area"!="yes"];
  way{a}["highway"="track"]["surface"~"^({hard})$"];
  way{a}["highway"="track"]["tracktype"="grade1"];
  node{a}["highway"~"^(traffic_signals|stop)$"];
);
out body geom;"""


def http_post(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_osm_tile(tile):
    """Voies OSM d'une case. Cache disque : cache/osm/<i>_<j>.json (60 jours)."""
    d = os.path.join(CACHE_DIR, "osm")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{tile[0]}_{tile[1]}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < OSM_TILE_MAX_AGE_DAYS * 86400:
        with open(path, "rb") as f:
            return json.loads(f.read())

    body = urllib.parse.urlencode({"data": build_overpass_query(osm_tile_bbox(tile))}).encode()
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    last_err = None
    for attempt in range(6):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            data = json.loads(http_post(url, body, headers, timeout=240))
            remark = data.get("remark") or ""
            if "error" in remark.lower():  # requête interrompue côté serveur : résultat incomplet
                raise RuntimeError(remark)
            elements = [
                {"id": e["id"], "nodes": e.get("nodes"), "geometry": e.get("geometry"), "tags": e.get("tags", {})}
                for e in data.get("elements", []) if e.get("type") == "way"
            ]
            # feux et stops (nœuds), puis un repère : les anciens fichiers du cache n'en ont pas
            elements += [{"type": "node", "id": e["id"], "tags": e.get("tags", {})}
                         for e in data.get("elements", []) if e.get("type") == "node"]
            elements.append({"type": "meta", "signals": 1})
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(elements, f, separators=(",", ":"))
            os.replace(tmp, path)
            return elements
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError, OSError) as e:
            last_err = e
            busy = isinstance(e, urllib.error.HTTPError) and e.code in (429, 504)
            time.sleep(10 if busy else 3)
    raise RuntimeError(f"Overpass indisponible pour la case {tile} : {last_err}")


# --- Altitude : cartes MNT (WMS IGN, float32) ------------------------------------
class DemService:
    """Altitudes lues dans des petites cartes MNT RGE ALTI téléchargées une fois pour toutes
    (cache/dem/<i>_<j>.f32z). Lecture locale avec interpolation bilinéaire."""

    def __init__(self):
        self.dir = os.path.join(CACHE_DIR, "dem")
        os.makedirs(self.dir, exist_ok=True)
        self.tiles = {}
        self.lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.last_call = 0.0

    @staticmethod
    def tile_of(lat, lon):
        return (math.floor(lat / DEM_TILE_DLAT), math.floor(lon / DEM_TILE_DLON))

    def _throttle(self):
        with self.rate_lock:
            wait = self.last_call + WMS_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()

    @staticmethod
    def _decode(raw):
        n = DEM_PX * DEM_PX
        if len(raw) != 4 * n:
            raise ValueError(f"réponse inattendue ({len(raw)} octets) : {raw[:200]!r}")
        best = None
        for order in ("<", ">"):
            vals = struct.unpack(f"{order}{n}f", raw)
            ok = [v for v in vals[:: max(1, n // 2000)] if DEM_MIN_Z < v < 9000 and (v == 0 or abs(v) > 1e-3)]
            if best is None or len(ok) > best[0]:
                best = (len(ok), vals)
        # aucune altitude valide : carte en pleine mer ou hors de France (Monaco, Italie…), pas une
        # erreur — les points y sont simplement sans altitude, comme hors de la couverture du MNT
        return array("f", [v if DEM_MIN_Z < v < 9000 else float("nan") for v in best[1]])

    def _fetch(self, t):
        i, j = t
        s, w = i * DEM_TILE_DLAT, j * DEM_TILE_DLON
        n, e = s + DEM_TILE_DLAT, w + DEM_TILE_DLON
        qs = urllib.parse.urlencode({
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": WMS_LAYER,
            "STYLES": "", "CRS": "EPSG:4326", "BBOX": f"{s},{w},{n},{e}",
            "WIDTH": DEM_PX, "HEIGHT": DEM_PX, "FORMAT": "image/x-bil;bits=32",
        })
        last = None
        for attempt in range(5):
            self._throttle()
            try:
                req = urllib.request.Request(f"{WMS_URL}?{qs}", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return self._decode(resp.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
            time.sleep(min(20, 2 ** attempt))
        raise RuntimeError(f"carte d'altitude {t} indisponible : {last}")

    def _path(self, t):
        return os.path.join(self.dir, f"{t[0]}_{t[1]}.f32z")

    def _load(self, t):
        path = self._path(t)
        if os.path.exists(path):
            with open(path, "rb") as f:
                arr = array("f")
                arr.frombytes(zlib.decompress(f.read()))
                return arr
        return self._download(t)

    def _download(self, t):
        arr = self._fetch(t)
        path = self._path(t)
        tmp = f"{path}.{threading.get_ident()}.tmp"   # préchargement et calcul peuvent se croiser
        with open(tmp, "wb") as f:
            f.write(zlib.compress(arr.tobytes(), 6))
        os.replace(tmp, path)
        return arr

    def download(self, t):
        """Met la carte `t` dans le cache disque si elle n'y est pas (préchargement : rien en mémoire)."""
        if not os.path.exists(self._path(t)):
            self._download(t)

    def ensure(self, tiles, progress=None):
        todo = [t for t in set(tiles) if t not in self.tiles]
        if not todo:
            return
        done = [0]

        def work(t):
            arr = self._load(t)
            with self.lock:
                self.tiles[t] = arr
                done[0] += 1
                if progress:
                    progress(done[0] / len(todo), f"cartes d'altitude {done[0]}/{len(todo)}")

        with ThreadPoolExecutor(WMS_WORKERS) as ex:
            list(ex.map(work, todo))

    def z(self, lat, lon):
        t = self.tile_of(lat, lon)
        arr = self.tiles.get(t)
        if arr is None:
            return None
        # position en pixels (centres de pixels), ligne 0 = nord
        fx = (lon - t[1] * DEM_TILE_DLON) / DEM_TILE_DLON * DEM_PX - 0.5
        fy = ((t[0] + 1) * DEM_TILE_DLAT - lat) / DEM_TILE_DLAT * DEM_PX - 0.5
        x0 = min(DEM_PX - 2, max(0, int(math.floor(fx))))
        y0 = min(DEM_PX - 2, max(0, int(math.floor(fy))))
        tx = min(1.0, max(0.0, fx - x0))
        ty = min(1.0, max(0.0, fy - y0))
        k = y0 * DEM_PX + x0
        a, b, c, d = arr[k], arr[k + 1], arr[k + DEM_PX], arr[k + DEM_PX + 1]
        v = (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty
        return None if v != v else v  # NaN -> None

    def lookup(self, points, progress=None):
        self.ensure([self.tile_of(la, lo) for la, lo in points], progress)
        return [self.z(la, lo) for la, lo in points]


# --- Altitude IGN, point par point (repli si les cartes MNT ne sont pas disponibles) -------------------------------------------------------------
class ElevationService:
    """Altitudes via l'API Géoplateforme, avec cache SQLite et limite de débit."""

    def __init__(self, db_path=None):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.db = sqlite3.connect(db_path or os.path.join(CACHE_DIR, "elevation.sqlite"),
                                  check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS z (k TEXT PRIMARY KEY, v REAL)")
        self.lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.last_call = 0.0
        self.mode = None  # mode POST qui fonctionne, détecté au premier appel

    @staticmethod
    def key(lat, lon):
        return f"{lat:.5f},{lon:.5f}"

    def _cached(self, keys):
        out = {}
        with self.lock:
            for i in range(0, len(keys), 900):
                chunk = keys[i:i + 900]
                q = "SELECT k, v FROM z WHERE k IN (%s)" % ",".join("?" * len(chunk))
                out.update(self.db.execute(q, chunk).fetchall())
        return out

    def _store(self, pairs):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO z VALUES (?, ?)", pairs)
            self.db.commit()

    def _throttle(self):
        with self.rate_lock:
            wait = self.last_call + IGN_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()

    @staticmethod
    def _parse(raw, n):
        data = json.loads(raw)
        elev = data.get("elevations", data)
        vals = [e["z"] if isinstance(e, dict) else e for e in elev]
        if len(vals) != n:
            raise ValueError(f"{len(vals)} altitudes reçues pour {n} points")
        return [None if v is None or v <= -9999 else float(v) for v in vals]

    def _request(self, pts, mode):
        lons = [f"{p[1]:.6f}" for p in pts]
        lats = [f"{p[0]:.6f}" for p in pts]
        headers = {"User-Agent": USER_AGENT}
        if mode == "json_str":
            body = {"lon": "|".join(lons), "lat": "|".join(lats), "resource": IGN_RESOURCE,
                    "delimiter": "|", "zonly": True}
            headers["Content-Type"] = "application/json"
            return self._parse(http_post(IGN_URL, json.dumps(body).encode(), headers, 120), len(pts))
        if mode == "json_list":
            body = {"lon": [float(x) for x in lons], "lat": [float(x) for x in lats],
                    "resource": IGN_RESOURCE, "zonly": True}
            headers["Content-Type"] = "application/json"
            return self._parse(http_post(IGN_URL, json.dumps(body).encode(), headers, 120), len(pts))
        # mode "get" : petites requêtes GET (repli si le POST n'est pas accepté)
        out = []
        for i in range(0, len(pts), 100):
            sub_lon, sub_lat = lons[i:i + 100], lats[i:i + 100]
            qs = urllib.parse.urlencode({"lon": "|".join(sub_lon), "lat": "|".join(sub_lat),
                                         "resource": IGN_RESOURCE, "delimiter": "|", "zonly": "true"})
            if i:
                self._throttle()
            req = urllib.request.Request(f"{IGN_URL}?{qs}", headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                out.extend(self._parse(resp.read(), len(sub_lon)))
        return out

    def _fetch_batch(self, pts):
        modes = [self.mode] if self.mode else ["json_str", "json_list", "get"]
        last = None
        for mode in modes:
            for attempt in range(5):
                self._throttle()
                try:
                    vals = self._request(pts, mode)
                    self.mode = mode
                    return vals
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code in (429, 500, 502, 503, 504):
                        time.sleep(min(30, 2 ** attempt))
                        continue
                    break  # 400 & co : on essaie le mode suivant
                except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
                    last = e
                    time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"API altitude IGN indisponible : {last}")

    def get(self, points, progress=None):
        """points : liste de (lat, lon). Renvoie un dict clé -> altitude (ou None)."""
        keys = list({self.key(la, lo) for la, lo in points})
        result = self._cached(keys)
        missing = [k for k in keys if k not in result]
        batches = [missing[i:i + IGN_MAX_POINTS] for i in range(0, len(missing), IGN_MAX_POINTS)]
        done = [0]

        def work(batch):
            pts = [tuple(map(float, k.split(","))) for k in batch]
            vals = self._fetch_batch(pts)
            pairs = list(zip(batch, vals))
            self._store(pairs)
            done[0] += 1
            if progress:
                progress(done[0] / max(1, len(batches)),
                         f"Altitudes IGN : lot {done[0]}/{len(batches)} ({len(missing)} points à récupérer)")
            return pairs

        if batches:
            with ThreadPoolExecutor(IGN_WORKERS) as ex:
                for pairs in ex.map(work, batches):
                    result.update(pairs)
        return result

    def lookup(self, points, progress=None):
        zmap = self.get(points, progress)
        return [zmap.get(self.key(la, lo)) for la, lo in points]


# --- Réseau routier ------------------------------------------------------------
class Edge:
    __slots__ = ("nodes", "attrs")

    def __init__(self, nodes, attrs):
        self.nodes = nodes
        self.attrs = attrs


def prepare_way(el, include_cobbles=False, skip_minor=False):
    """Élément OSM -> (nodes, attrs) ou None si la voie est exclue."""
    tags = el.get("tags", {})
    surf = classify_way(tags, include_cobbles, skip_minor)
    if not surf:
        return None
    nodes, geom = el.get("nodes") or [], el.get("geometry") or []
    if len(nodes) < 2 or len(nodes) != len(geom) or any(g is None for g in geom):
        return None
    return (nodes, [(g["lat"], g["lon"]) for g in geom], {
        "name": tags.get("name") or "", "ref": tags.get("ref") or "",
        "highway": tags.get("highway"), "surface": surf,
        "oneway": parse_oneway(tags), "struct": structure(tags), "osm_id": el.get("id"),
    })


def build_edges(prepared):
    """prepared : liste de (nodes, coords, attrs). Coupe les voies à chaque carrefour."""
    coords, ways = {}, []
    for nodes, geom, attrs in prepared:
        for nid, c in zip(nodes, geom):
            coords[nid] = c
        ways.append((nodes, attrs))

    count = Counter()
    for nodes, _ in ways:
        count.update(nodes)
        count[nodes[0]] += 1
        count[nodes[-1]] += 1

    edges = []
    for nodes, attrs in ways:
        cur = [nodes[0]]
        for nid in nodes[1:]:
            cur.append(nid)
            if count[nid] >= 2:
                if len(cur) >= 2:
                    edges.append(Edge(cur, attrs))
                cur = [nid]
        if len(cur) >= 2:
            edges.append(Edge(cur, attrs))
    return edges, coords


def _end_bearing(edge, end, coords, proj, look_m=30.0):
    """Cap (radians) de l'arête quand on la quitte par l'extrémité `end` (0 = début, 1 = fin)."""
    seq = edge.nodes if end == 0 else edge.nodes[::-1]
    x0, y0 = proj.xy(*coords[seq[0]])
    x, y = x0, y0
    for nid in seq[1:]:
        x, y = proj.xy(*coords[nid])
        if math.hypot(x - x0, y - y0) >= look_m:
            break
    return math.atan2(x - x0, y - y0)


def build_chains(edges, coords, proj):
    """Recolle les arêtes en "routes continues" à travers les carrefours (méthode des strokes)."""
    ends = defaultdict(list)
    for i, e in enumerate(edges):
        ends[e.nodes[0]].append((i, 0))
        ends[e.nodes[-1]].append((i, 1))

    link = {}
    for node, lst in ends.items():
        if len(lst) < 2:
            continue
        bearings = [_end_bearing(edges[i], end, coords, proj) for i, end in lst]
        cands = []
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                if lst[a][0] == lst[b][0]:
                    continue
                diff = abs((bearings[a] - bearings[b] + math.pi) % (2 * math.pi) - math.pi)
                deflection = 180 - math.degrees(diff)
                aa, ab = edges[lst[a][0]].attrs, edges[lst[b][0]].attrs
                same = (aa["name"] and aa["name"] == ab["name"]) or (aa["ref"] and aa["ref"] == ab["ref"])
                if deflection > (MAX_DEFLECTION_SAME_NAME_DEG if same else MAX_DEFLECTION_DEG):
                    continue
                # on suit d'abord la rue du même nom (même si elle tourne), puis la plus droite
                cands.append((0 if same else 1, deflection, a, b))
        cands.sort()
        used = set()
        for _, _, a, b in cands:
            if a in used or b in used:
                continue
            used.update((a, b))
            link[lst[a]] = lst[b]
            link[lst[b]] = lst[a]

    visited = [False] * len(edges)
    chains = []
    for seed in range(len(edges)):
        if visited[seed]:
            continue
        visited[seed] = True
        chain = [(seed, False)]
        # vers l'avant
        while True:
            e, rev = chain[-1]
            nxt = link.get((e, 0 if rev else 1))
            if not nxt or visited[nxt[0]]:
                break
            visited[nxt[0]] = True
            chain.append((nxt[0], nxt[1] == 1))
        # vers l'arrière
        while True:
            e, rev = chain[0]
            prv = link.get((e, 1 if rev else 0))
            if not prv or visited[prv[0]]:
                break
            visited[prv[0]] = True
            chain.insert(0, (prv[0], prv[1] == 0))
        chains.append(chain)
    return chains


def sample_edges(edges, coords, proj, step=STEP_M):
    """Chaque arête échantillonnée tous les 10 m depuis son début, extrémités comprises. Le profil d'une route
    (ou d'un enchaînement de rues) est la mise bout à bout de ces échantillons : deux chemins qui
    passent par la même arête ont exactement les mêmes points."""
    out = []
    for e in edges:
        pts = [coords[n] for n in e.nodes]
        xy = [proj.xy(*p) for p in pts]
        cum = [0.0]
        for i in range(1, len(xy)):
            cum.append(cum[-1] + math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]))
        total = cum[-1]
        positions = [q * step for q in range(int(total // step) + 1)]
        if total - positions[-1] > 0.5 or len(positions) == 1:
            positions.append(total)
        else:
            positions[-1] = total
        lat, lon, d = [], [], []
        j = 0
        for s in positions:
            while j < len(cum) - 2 and cum[j + 1] < s:
                j += 1
            seglen = cum[j + 1] - cum[j]
            t = 0 if seglen == 0 else min(1.0, max(0.0, (s - cum[j]) / seglen))
            lat.append(pts[j][0] + t * (pts[j + 1][0] - pts[j][0]))
            lon.append(pts[j][1] + t * (pts[j + 1][1] - pts[j][1]))
            d.append(s)
        lat[0], lon[0], lat[-1], lon[-1] = pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]
        out.append({"lat": lat, "lon": lon, "d": d, "node_d": cum, "len": total, "z": None})
    return out


def path_profile(path, es, edges):
    """Profil brut d'un enchaînement d'arêtes [(arête, à l'envers)] : points, distances, altitudes
    brutes, seg[i] = arête du segment i -> i+1 (dans le sens du parcours), nœuds OSM traversés."""
    lat, lon, d, z, seg, nodes = [], [], [], [], [], []
    off = 0.0
    for ei, rev in path:
        s = es[ei]
        L = s["len"]
        order = range(len(s["d"]) - 1, -1, -1) if rev else range(len(s["d"]))
        for k, q in enumerate(order):
            if k == 0 and lat:
                seg[-1] = (ei, rev)  # point de jonction déjà présent : le segment suivant est sur cette arête
                continue
            lat.append(s["lat"][q])
            lon.append(s["lon"][q])
            d.append(off + (L - s["d"][q] if rev else s["d"][q]))
            z.append(s["z"][q])
            seg.append((ei, rev))
        nids, nd = edges[ei].nodes, s["node_d"]
        order = range(len(nids) - 1, -1, -1) if rev else range(len(nids))
        for k, q in enumerate(order):
            if k == 0 and nodes:
                continue
            nodes.append((off + (L - nd[q] if rev else nd[q]), nids[q]))
        off += L
    return {"lat": lat, "lon": lon, "d": d, "z": z, "seg": seg, "nodes": nodes}


# --- Profils et détection -----------------------------------------------------
def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


def smooth(z):
    n = len(z)
    med = [_median(z[max(0, i - 2):i + 3]) for i in range(n)]
    return [sum(med[max(0, i - 1):i + 2]) / len(med[max(0, i - 1):i + 2]) for i in range(n)]


def fix_structures(z, dist, structs):
    """Ponts/tunnels : le MNT donne le fond de vallée ou le dessus de la colline -> on interpole."""
    n = len(z)
    i = 0
    while i < n:
        if structs[i] is None:
            i += 1
            continue
        j = i
        while j < n and structs[j] is not None:
            j += 1
        a, b = i - 1, j  # échantillons hors ouvrage de part et d'autre
        if a >= 0 and b < n and z[a] is not None and z[b] is not None:
            for k in range(i, j):
                t = (dist[k] - dist[a]) / (dist[b] - dist[a])
                z[k] = z[a] + t * (z[b] - z[a])
        else:
            # une extrémité inconnue (ouvrage en bout de profil, ou sans altitude) : on ne sait pas
            # interpoler, et le MNT donnerait le dessus de la colline ou le fond de la vallée
            for k in range(i, j):
                z[k] = None
        i = j
    return z


def zigzag(z, h):
    """Points de retournement (minima/maxima) d'amplitude >= h. Renvoie [(index, +1|-1)]."""
    n = len(z)
    piv, trend, imin, imax, cand = [], 0, 0, 0, 0
    for i in range(1, n):
        if trend == 0:
            if z[i] > z[imax]:
                imax = i
            if z[i] < z[imin]:
                imin = i
            if z[imax] - z[imin] >= h:
                if imin < imax:
                    piv.append((imin, -1)); trend, cand = 1, imax
                else:
                    piv.append((imax, 1)); trend, cand = -1, imin
        elif trend == 1:
            if z[i] > z[cand]:
                cand = i
            elif z[cand] - z[i] >= h:
                piv.append((cand, 1)); trend, cand = -1, i
        else:
            if z[i] < z[cand]:
                cand = i
            elif z[i] - z[cand] >= h:
                piv.append((cand, -1)); trend, cand = 1, i
    if trend:
        piv.append((cand, trend))
    return piv


def _index_at(dist, i, meters, direction=1):
    """Premier index j (après/avant i) tel que |dist[j] - dist[i]| >= meters."""
    j = i
    n = len(dist)
    while 0 <= j + direction < n and abs(dist[j] - dist[i]) < meters:
        j += direction
    return j


def split_on_flats(d, z, a, b):
    """Coupe une montée [a, b] là où un replat de FLAT_SPLIT_M gagne moins de FLAT_SPLIT_GAIN_M."""
    out, start, i, j, last = [], a, a, a, len(d) - 1
    while i < b:
        # j = premier point à FLAT_SPLIT_M de i (comme _index_at, mais en avançant j avec i)
        j = max(j, i)
        while j < last and d[j] - d[i] < FLAT_SPLIT_M:
            j += 1
        if j > b or d[j] - d[i] < FLAT_SPLIT_M:
            break
        if max(z[i:j + 1]) - min(z[i:j + 1]) < FLAT_SPLIT_GAIN_M:
            # sommet = point le plus haut du replat, reprise = point le plus bas après ce sommet
            top = max(range(i, j + 1), key=lambda k: (z[k], -k))
            if top > start:
                out.append((start, top))
            start = min(range(top, j + 1), key=lambda k: (z[k], -k))
            i = j
            continue
        i += 1
    if b > start:
        out.append((start, b))
    return out


def trim(d, z, a, b):
    while a < b:
        j = _index_at(d, a, TRIM_WINDOW_M)
        if j >= b or (z[j] - z[a]) / max(1e-6, d[j] - d[a]) >= TRIM_GRADE:
            break
        a += 1
    while b > a:
        j = _index_at(d, b, TRIM_WINDOW_M, -1)
        if j <= a or (z[b] - z[j]) / max(1e-6, d[b] - d[j]) >= TRIM_GRADE:
            break
        b -= 1
    return a, b


def max_grade(d, z, a, b, window):
    """Meilleure pente moyenne sur `window` m. Si la côte est plus courte que la fenêtre,
    on l'élargit sur la route autour (ex. mur de 280 m -> pente sur les 300 m qui l'englobent),
    sans descendre sous D+ / fenêtre (cas d'une côte entourée de descentes)."""
    floor = (z[b] - z[a]) / window * 100 if d[b] - d[a] < window else None
    v = _max_grade(d, z, a, b, window)
    if floor is not None:
        v = floor if v is None else max(v, floor)
    return v


def _max_grade(d, z, a, b, window):
    n, toggle = len(d), True
    while d[b] - d[a] < window and (a > 0 or b < n - 1):
        if (toggle and b < n - 1) or a == 0:
            b += 1
        else:
            a -= 1
        toggle = not toggle
    if d[b] - d[a] < window * 0.98:
        return None
    best, j = -1e9, a
    for i in range(a, b + 1):
        while j <= b and d[j] - d[i] < window:
            j += 1
        if j > b:
            break
        best = max(best, (z[j] - z[i]) / (d[j] - d[i]))
    return None if best == -1e9 else best * 100


def difficulty(d, z, a, b):
    """Σ pente(%)² × longueur(km) sur des tronçons de ~50 m (seuls les tronçons qui montent)."""
    score, i = 0.0, a
    while i < b:
        j = min(b, _index_at(d, i, SCORE_CHUNK_M))
        if j == i:
            break
        g = (z[j] - z[i]) / (d[j] - d[i]) * 100
        if g > 0:
            score += g * g * (d[j] - d[i]) / 1000
        i = j
    return score


def detect_climbs(d, z, allowed):
    """Côtes dans le sens de la liste. allowed[i] : le vélo peut rouler dans ce sens au point i."""
    found = []
    piv = zigzag(z, REVERSAL_M)
    for k in range(len(piv) - 1):
        (a, ta), (b, tb) = piv[k], piv[k + 1]
        if ta != -1 or tb != 1:
            continue
        for a2, b2 in split_on_flats(d, z, a, b):
            a3, b3 = trim(d, z, a2, b2)
            length = d[b3] - d[a3]
            gain = z[b3] - z[a3]
            if length < MIN_LENGTH_M or gain < MIN_GAIN_M or gain / length * 100 < MIN_AVG_GRADE:
                continue
            if not all(allowed[a3:b3]):
                continue
            found.append((a3, b3))
    return found


def _downsample(idx, max_pts):
    if len(idx) <= max_pts:
        return idx
    step = (len(idx) - 1) / (max_pts - 1)
    return [idx[round(k * step)] for k in range(max_pts)]


def _street_label(attrs):
    return attrs["name"] or attrs["ref"] or f"{HIGHWAY_LABEL.get(attrs['highway'], 'Route')} sans nom"


def same_street(x, y):
    """Même rue : même nom, ou même numéro de route (deux voies sans nom ni numéro : même rue)."""
    if x["ref"] and x["ref"] == y["ref"]:
        return True
    return x["name"] == y["name"] and (bool(x["name"]) or not (x["ref"] or y["ref"]))


def street_names(prof, a, b, edges):
    """Rues traversées entre a et b, dans l'ordre (les morceaux de moins de 25 m sont ignorés :
    traverser un carrefour décalé n'est pas un changement de rue)."""
    pieces = []
    for i in range(a, b):
        at = edges[prof["seg"][i][0]].attrs
        L = prof["d"][i + 1] - prof["d"][i]
        if pieces and same_street(pieces[-1][0], at):
            pieces[-1][1] += L
            pieces[-1][2][_street_label(at)] += L
        else:
            pieces.append([at, L, Counter({_street_label(at): L})])
    if len(pieces) > 1:
        kept = [p for p in pieces if p[1] >= SHORT_PIECE_M] or [max(pieces, key=lambda p: p[1])]
        pieces = []
        for p in kept:
            if pieces and same_street(pieces[-1][0], p[0]):
                pieces[-1][1] += p[1]
                pieces[-1][2].update(p[2])
            else:
                pieces.append(p)
    return [p[2].most_common(1)[0][0] for p in pieces]


def junction_stats(prof, a, b, net):
    """(carrefours traversés, dont avec une route principale, feux/stops). Les feux/stops valent
    None si la case a été téléchargée avant qu'on les récupère."""
    da, db = prof["d"][a], prof["d"][b]
    on_path = {e for e, _ in prof["seg"][a:b]}
    junctions = main = 0
    sig = None if net.signals is None else 0
    seen = set()
    for x, nid in prof["nodes"]:
        if x < da - 1 or x > db + 1 or nid in seen:
            continue
        seen.add(nid)
        if sig is not None and nid in net.signals:
            sig += 1
        if x <= da + 1 or x >= db - 1:
            continue  # pied et sommet : pas un carrefour traversé
        roads = [net.edges[e].attrs["highway"] for e, _ in net.ends.get(nid, ()) if e not in on_path]
        roads = [h for h in roads if h not in JUNCTION_IGNORE]
        if roads:
            junctions += 1
            if any(h in MAJOR_ROADS for h in roads):
                main += 1
    return junctions, main, sig


def describe_climb(prof, a, b, edges, center, net=None, extra=None):
    d, z = prof["d"], prof["z"]
    length = d[b] - d[a]
    ascent = sum(max(0.0, z[i + 1] - z[i]) for i in range(a, b))
    names, refs, hws, surf_len = Counter(), Counter(), Counter(), Counter()
    for i in range(a, b):
        attrs = edges[prof["seg"][i][0]].attrs
        seglen = d[i + 1] - d[i]
        if attrs["name"]:
            names[attrs["name"]] += seglen
        if attrs["ref"]:
            refs[attrs["ref"]] += seglen
        hws[attrs["highway"]] += seglen
        surf_len[attrs["surface"]] += seglen
    hw = hws.most_common(1)[0][0]
    name = names.most_common(1)[0][0] if names else ""
    ref = refs.most_common(1)[0][0] if refs else ""
    if not name:
        name = ref or f"{HIGHWAY_LABEL.get(hw, 'Route')} sans nom"
    elif ref and ref not in name:
        name = f"{name} ({ref})"

    if surf_len["cobbles"] > 0.1 * length:
        surface = "pavés"
    elif surf_len["confirmed"] >= 0.8 * length:
        surface = "confirmé"
    else:
        surface = "supposé"

    idx = _downsample(list(range(a, b + 1)), 150)
    out = {
        "id": climb_id(prof, a, b),
        "name": name,
        "highway": hw,
        "length_m": round(length),
        "gain_m": round(z[b] - z[a], 1),
        "ascent_m": round(ascent, 1),
        "avg_grade": round((z[b] - z[a]) / length * 100, 1),
        "max_grade_100": _r(max_grade(d, z, a, b, 100)),
        "max_grade_300": _r(max_grade(d, z, a, b, 300)),
        "score": round(difficulty(d, z, a, b), 1),
        "surface": surface,
        "start": [round(prof["lat"][a], 6), round(prof["lon"][a], 6)],
        "end": [round(prof["lat"][b], 6), round(prof["lon"][b], 6)],
        "alt_start": round(z[a]), "alt_end": round(z[b]),
        "distance_km": round(haversine(center[0], center[1], prof["lat"][a], prof["lon"][a]) / 1000, 1),
        "geometry": [[round(prof["lat"][i], 5), round(prof["lon"][i], 5)] for i in idx],
        "profile": [[round(d[i] - d[a]), round(z[i], 1)] for i in idx],
        "streets": " → ".join(street_names(prof, a, b, edges)),
    }
    if net is not None:
        out["junctions"], out["main_junctions"], out["signals"] = junction_stats(prof, a, b, net)
    out.update(extra or {})
    return out


def climb_id(prof, a, b):
    key = "%.4f,%.4f>%.4f,%.4f" % (prof["lat"][a], prof["lon"][a], prof["lat"][b], prof["lon"][b])
    return hashlib.md5(key.encode()).hexdigest()[:10]


def _r(v):
    return None if v is None else round(v, 1)


def implausible_grades(d, z, seg, edges):
    """Points pris dans une pente impossible, donc une erreur d'altitude (route en corniche dont le
    MNT mélange le haut et le bas de la paroi) : plus de 30 % sur 100 m sur une route classée
    (D, N, M… ou numérotée), plus de 50 % sur 50 m sur n'importe quelle route. Montée ou descente."""
    n = len(d)
    bad = [False] * n
    for window, limit, classified_only in IMPLAUSIBLE_GRADES:
        j = 0
        for i in range(n):
            j = max(j, i)
            while j < n - 1 and d[j] - d[i] < window:
                j += 1
            if d[j] - d[i] < window * 0.98:
                break
            if abs(z[j] - z[i]) <= limit * (d[j] - d[i]):
                continue
            if classified_only:
                at = edges[seg[i][0]].attrs
                if at["highway"] not in MAJOR_ROADS and not at["ref"]:
                    continue
            for k in range(i, j + 1):
                bad[k] = True
    return bad


def detect_in_profile(p, edges, both=True):
    """Côtes d'un profil brut (path_profile), dans les deux sens (ou seulement dans le sens du
    profil). Renvoie [(prof, a, b)] : prof est orienté dans le sens de la montée, lissé, et
    prof["seg"][i] = (arête, à l'envers) dans ce sens."""
    zr = fix_structures(list(p["z"]), p["d"], [edges[e].attrs["struct"] for e, _ in p["seg"]])
    runs, cur = [], []
    for i, v in enumerate(zr):  # morceaux continus d'altitudes valides
        if v is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(i)
    if cur:
        runs.append(cur)

    # pentes impossibles (erreur du MNT le long d'une paroi) : ces points sont écartés aussi
    pieces = []
    for run in runs:
        i0, i1 = run[0], run[-1]
        if i1 - i0 < 2 or p["d"][i1] - p["d"][i0] < MIN_LENGTH_M:
            continue
        z = smooth(zr[i0:i1 + 1])
        bad = implausible_grades(p["d"][i0:i1 + 1], z, p["seg"][i0:i1 + 1], edges)
        k = 0
        while k < len(z):
            if bad[k]:
                k += 1
                continue
            m = k
            while m + 1 < len(z) and not bad[m + 1]:
                m += 1
            pieces.append((i0 + k, i0 + m, z[k:m + 1]))
            k = m + 1

    out = []
    for i0, i1, z in pieces:
        if i1 - i0 < 2 or p["d"][i1] - p["d"][i0] < MIN_LENGTH_M:
            continue
        lat, lon, d = p["lat"][i0:i1 + 1], p["lon"][i0:i1 + 1], p["d"][i0:i1 + 1]
        seg = p["seg"][i0:i1 + 1]
        nodes = [(x, nid) for x, nid in p["nodes"] if d[0] - 1e-6 <= x <= d[-1] + 1e-6]
        for direction in ((1, -1) if both else (1,)):
            if direction == 1:
                prof = {"lat": lat, "lon": lon, "d": d, "z": z, "seg": seg, "nodes": nodes}
            else:
                n, top = len(d), d[-1]
                rseg = [(seg[n - 2 - j][0], not seg[n - 2 - j][1]) for j in range(n - 1)]
                rseg.append((seg[0][0], not seg[0][1]))
                prof = {"lat": lat[::-1], "lon": lon[::-1], "d": [top - x for x in d[::-1]], "z": z[::-1],
                        "seg": rseg, "nodes": [(top - x, nid) for x, nid in nodes[::-1]]}
            # sens autorisé : +1 sens de la way OSM, inversé si l'arête est parcourue à l'envers
            allowed = [edges[e].attrs["oneway"] * (-1 if r else 1) >= 0 for e, r in prof["seg"]]
            for a, b in detect_climbs(prof["d"], prof["z"], allowed):
                out.append((prof, a, b))
    return out


# --- Variantes : enchaînements de rues jusqu'au même sommet (voir EVOLUTIONS.md) ---------------
class Network:
    """Réseau routier d'un calcul : arêtes, échantillons, extrémités par nœud, caps aux carrefours."""

    def __init__(self, edges, coords, proj, es, signals):
        self.edges, self.coords, self.proj, self.es, self.signals = edges, coords, proj, es, signals
        self.ends = defaultdict(list)
        for i, e in enumerate(edges):
            self.ends[e.nodes[0]].append((i, 0))
            self.ends[e.nodes[-1]].append((i, 1))
        self._bearing = {}

    def bearing(self, e, end):
        k = (e, end)
        if k not in self._bearing:
            self._bearing[k] = _end_bearing(self.edges[e], end, self.coords, self.proj)
        return self._bearing[k]


def _summary(prof, a, b, net, src=None):
    """Ce qu'il faut pour choisir entre variantes (la description complète vient après)."""
    d, z = prof["d"], prof["z"]
    em = Counter()
    for i in range(a, b):
        em[prof["seg"][i][0]] += d[i + 1] - d[i]
    j, m, s = junction_stats(prof, a, b, net)
    return {"prof": prof, "a": a, "b": b, "len": d[b] - d[a], "avg": (z[b] - z[a]) / max(1e-6, d[b] - d[a]),
            "edges": em, "changes": len(street_names(prof, a, b, net.edges)) - 1,
            "cost": j + 2 * m + 2 * (s or 0), "id": climb_id(prof, a, b), "src": src}


def _common(x, y):
    """Longueur commune à deux côtes (m)."""
    return sum(min(v, y["edges"].get(e, 0.0)) for e, v in x["edges"].items())


def _overlap(x, y):
    """Part de longueur commune, rapportée à la plus longue des deux."""
    return _common(x, y) / max(x["len"], y["len"], 1e-6)


def _climb_edges(prof, a, b):
    out = []
    for i in range(a, b):
        if not out or prof["seg"][i] != out[-1]:
            out.append(prof["seg"][i])
    return out


def find_variants(prof, a, b, net):
    """Autres façons d'arriver au sommet de la côte (prof, a, b) : à chaque carrefour de la côte,
    on remonte le réseau vers l'aval par toutes les rues qui y montent (parcours en profondeur
    borné), puis chaque chemin trouvé est passé à la détection habituelle."""
    edges, es = net.edges, net.es
    d = prof["d"]
    # la côte + la route au-delà du sommet (pour que la détection voie la même fin de côte)
    b_ext = b
    while b_ext < len(d) - 1 and d[b_ext] - d[b] < 300:
        b_ext += 1
    full = _climb_edges(prof, a, b_ext)
    n_climb = len(_climb_edges(prof, a, b))
    before = None  # arête de la même route juste avant le pied : c'est la côte elle-même
    for i in range(a, -1, -1):
        if prof["seg"][i] != full[0]:
            before = prof["seg"][i][0]
            break
    top = (prof["lat"][b], prof["lon"][b])
    out, budget, n_eval = [], [VARIANT_BUDGET], 0

    for i in range(n_climb):
        e, rev = full[i]
        node = edges[e].nodes[-1] if rev else edges[e].nodes[0]
        q0 = len(es[e]["d"]) - 1 if rev else 0
        z0 = es[e]["z"][q0]
        if z0 is None:
            continue
        visited = set()
        for ee, _ in full[i:]:
            visited.update(edges[ee].nodes)
        excluded = full[i - 1][0] if i > 0 else before
        cands = {}
        st = (0.0, z0, 0.0, None, z0, 0.0, 0, edges[e].attrs, z0)
        _explore(net, node, (e, rev), [], visited, st, cands, budget, excluded)
        for pre in cands.values():
            if n_eval >= VARIANT_MAX_EVAL:
                break
            n_eval += 1
            raw = path_profile(pre + full[i:], es, edges)
            d_node = sum(es[f]["len"] for f, _ in pre)
            for p2, a2, b2 in detect_in_profile(raw, edges, both=False):
                if p2["d"][a2] < d_node - 10 and \
                        haversine(p2["lat"][b2], p2["lon"][b2], top[0], top[1]) < SUMMIT_MATCH_M:
                    out.append(_summary(p2, a2, b2, net))
                    break
        if budget[0] <= 0 or n_eval >= VARIANT_MAX_EVAL:
            break
    return out


def _explore(net, node, nxt, stack, visited, st, cands, budget, excluded):
    """Parcours en profondeur vers l'aval depuis `node`, où le chemin continue par l'arête `nxt`.
    st = (distance parcourue, z min, distance du z min, position du z min, z repère du replat,
    distance du repère, changements de rue, rue courante, z au nœud). Renvoie True si une rue a
    été suivie."""
    edges, es = net.edges, net.es
    bd0, zmin0, zmin_bd0, zmin_pos0, az0, abd0, changes0, cur0, znode0 = st
    nxt_end = 1 if nxt[1] else 0
    walked = False
    for f, end in net.ends.get(node, ()):
        if f == nxt[0] or f == excluded or budget[0] <= 0:
            continue
        rev = end == 0  # on arrive au nœud par le début de f : f est parcourue à l'envers
        at = edges[f].attrs
        if at["oneway"] * (-1 if rev else 1) < 0:
            continue
        nodes = edges[f].nodes
        far = nodes[-1] if rev else nodes[0]
        if far in visited or any(x in visited for x in nodes[1:-1]):
            continue  # on ne repasse jamais par le même nœud
        diff = abs((net.bearing(f, end) - net.bearing(nxt[0], nxt_end) + math.pi) % (2 * math.pi) - math.pi)
        if 180 - math.degrees(diff) > MAX_TURN_DEG:
            continue  # demi-tour ou virage en épingle au carrefour
        changes, cur = changes0, cur0
        if not same_street(at, cur) and es[f]["len"] >= SHORT_PIECE_M:
            changes, cur = changes + 1, at
        if changes > MAX_STREET_CHANGES:
            continue
        if changes > changes0 and stack and znode0 - zmin0 < REVERSAL_M:
            # on change de rue : partir de ce carrefour est aussi une variante (ex. Rue des Bois ->
            # Rue de la Garenne, sans la rue d'avant), si la côte n'y est pas coupée par une descente
            cands.setdefault(("départ", tuple(stack)), list(reversed(stack)))
        budget[0] -= 1
        walked = True
        s = es[f]
        L, ds, zs = s["len"], s["d"], s["z"]
        bd, zmin, zmin_bd, zmin_pos, az, abd, zq = bd0, zmin0, zmin_bd0, zmin_pos0, az0, abd0, znode0
        leaf = False
        for q in (range(len(ds)) if rev else range(len(ds) - 1, -1, -1)):
            bd = bd0 + (ds[q] if rev else L - ds[q])
            zq = zs[q]
            if zq is None:
                leaf = True
                break
            if zq < zmin:
                zmin, zmin_bd, zmin_pos = zq, bd, (len(stack), q)
            if zq < az - FLAT_SPLIT_GAIN_M:
                az, abd = zq, bd
            # au-delà, le pied ne peut plus changer : remontée (= descente dans le sens de la côte),
            # long replat, ou chemin trop long
            if zq > zmin + REVERSAL_M + 3 or bd - abd > FLAT_SPLIT_M + 100 or bd > MAX_VARIANT_M:
                leaf = True
                break
        stack.append((f, rev))
        if leaf:
            _emit(stack, zmin_bd, zmin_pos, cands)
        else:
            added = [x for x in nodes if x not in visited]
            visited.update(added)
            st2 = (bd, zmin, zmin_bd, zmin_pos, az, abd, changes, cur, zq)
            if not _explore(net, far, (f, rev), stack, visited, st2, cands, budget, excluded=None):
                _emit(stack, zmin_bd, zmin_pos, cands)  # pas de suite possible : le pied est ici
            visited.difference_update(added)
        stack.pop()
    return walked


def _emit(stack, zmin_bd, zmin_pos, cands):
    if zmin_pos is None or zmin_bd < 15:
        return  # le point bas est au carrefour lui-même : c'est la côte de départ
    depth, q = zmin_pos
    key = (tuple(stack[:depth + 1]), q)
    if key not in cands:
        cands[key] = list(reversed(stack))


def select_variants(classics, cands):
    """Règles d'EVOLUTIONS.md : les routes classiques sont toujours gardées (hors limite) ; une
    variante doit ajouter au moins 50 m à celle qu'elle prolonge (à longueur proche, on garde
    celle qui croise le moins de carrefours), avoir au plus 3 changements de rue et
    moins de 70 % de longueur commune avec chacune (sinon celle qui change le moins de rue) ;
    au plus 5, les plus directes (les plus courtes, puis les plus pentues)."""
    kept, chosen = list(classics), []
    for v in sorted(cands, key=lambda v: (v["len"], -v["avg"])):
        if v["changes"] > MAX_STREET_CHANGES:
            continue
        if len(chosen) >= MAX_VARIANTS:
            break
        rival, reject = None, False
        for k in kept:
            # la règle des 50 m vaut entre une variante et celle qu'elle prolonge (90 % de la plus
            # courte en commun) ; deux trajets différents de même longueur sont gardés tous les deux
            common = _common(v, k)
            close = abs(v["len"] - k["len"]) < VARIANT_MIN_EXTRA_M and \
                common >= 0.9 * min(v["len"], k["len"])
            if not close and common / max(v["len"], k["len"], 1e-6) <= VARIANT_OVERLAP:
                continue
            better = v["cost"] < k["cost"] if close else v["changes"] < k["changes"]
            if better and rival is None and any(k is c for c in chosen):
                rival = k
            else:
                reject = True
                break
        if reject:
            continue
        if rival is not None:
            chosen = [c for c in chosen if c is not rival]
            kept = [c for c in kept if c is not rival]
        chosen.append(v)
        kept.append(v)
    return chosen


# --- Point d'entrée -----------------------------------------------------------
def compute_climbs(prepared, elevation, proj, center, radius_m, progress=None, start_ok=None, signals=None):
    """Réseau -> routes continues -> côtes classiques -> variantes par sommet -> côtes."""
    edges, coords = build_edges(prepared)
    es = sample_edges(edges, coords, proj)
    points = [(la, lo) for s in es for la, lo in zip(s["lat"], s["lon"])]
    zs = elevation.lookup(points, progress)
    k = 0
    for s in es:
        s["z"] = zs[k:k + len(s["lat"])]
        k += len(s["lat"])
    net = Network(edges, coords, proj, es, signals)

    road_m, n_roads, families = 0.0, 0, defaultdict(list)
    for chain in build_chains(edges, coords, proj):
        raw = path_profile(chain, es, edges)
        if len(raw["d"]) < 2 or raw["d"][-1] < MIN_LENGTH_M:
            continue
        road_m += raw["d"][-1]
        n_roads += 1
        for prof, a, b in detect_in_profile(raw, edges):
            # famille = sommet commun (deux routes qui arrivent au même carrefour au sommet)
            families["%.6f,%.6f" % (prof["lat"][b], prof["lon"][b])].append((prof, a, b))

    if start_ok is None:
        def start_ok(la, lo):
            return haversine(center[0], center[1], la, lo) <= radius_m

    found = {}
    for members in families.values():
        classics = {}
        for prof, a, b in members:
            s = _summary(prof, a, b, net)
            classics.setdefault(s["id"], s)
        classics = list(classics.values())
        head = max(classics, key=lambda s: (s["avg"], s["len"]))  # la route classique la plus pentue
        cands = {}
        for c in classics:
            for v in find_variants(c["prof"], c["a"], c["b"], net):
                if v["id"] not in cands:
                    v["src"] = c
                    cands[v["id"]] = v
        ids = {c["id"] for c in classics}
        chosen = select_variants(classics, [v for v in cands.values() if v["id"] not in ids])
        for s in classics + chosen:
            if s["id"] in found or not start_ok(s["prof"]["lat"][s["a"]], s["prof"]["lon"][s["a"]]):
                continue
            if s is head:
                kind = "principale"
            elif s["src"] is not None and _overlap(s, s["src"]) * s["len"] >= 0.95 * s["src"]["len"]:
                kind = "longue"
            else:
                kind = "autre départ"
            c = describe_climb(s["prof"], s["a"], s["b"], edges, center, net,
                               {"family": head["id"], "variant": kind})
            if kind != "principale" and s["src"] is not None:
                c["name"] = c["streets"]  # variante : le nom est l'enchaînement des rues
            found[c["id"]] = c

    climbs = sorted(found.values(), key=lambda c: -c["score"])
    for r, c in enumerate(climbs, 1):
        c["rank"] = r
    return climbs, {"road_km": round(road_m / 1000), "roads": n_roads, "points": len(points)}


def _duration(sec):
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 5400:
        return f"{sec / 60:.0f} min"
    return f"{sec // 3600:.0f} h {sec % 3600 / 60:02.0f}"


TILE_MARGIN_M = 3000   # routes chargées autour d'une case : une côte qui démarre dans la case
                       # est suivie jusqu'à 3 km au-delà de son bord
ALGO_VERSION = 6       # à incrémenter quand la détection change : les cases sont alors recalculées


def neighbor_tiles(tile, margin_m=TILE_MARGIN_M):
    """Cases OSM nécessaires pour calculer `tile` : la case + celles qui touchent sa marge."""
    s, w, n, e = osm_tile_bbox(tile)
    ky = EARTH_R * math.pi / 180
    dlat = margin_m / ky
    dlon = margin_m / (ky * math.cos(math.radians((s + n) / 2)))
    i0, j0 = osm_tile_of(s - dlat, w - dlon)
    i1, j1 = osm_tile_of(n + dlat - 1e-9, e + dlon - 1e-9)
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def scan_elements(elements, elevation, center, radius_m, include_cobbles=False, skip_minor=False,
                  start_ok=None, progress=None, signals=None):
    """Détecte les côtes d'une liste de voies OSM (sans téléchargement). signals : nœuds OSM des
    feux et stops (None : inconnus)."""
    prepared, seen = [], set()
    for el in elements:
        if el.get("type") in ("node", "meta"):
            continue
        if el["id"] in seen:
            continue
        seen.add(el["id"])
        pw = prepare_way(el, include_cobbles, skip_minor)
        if pw:
            prepared.append(pw)
    return compute_climbs(prepared, elevation, LocalProj(*center), center, radius_m,
                          progress=progress, start_ok=start_ok, signals=signals)


def tile_bounds_with_margin(tile, margin_m=TILE_MARGIN_M):
    """(S, W, N, E) de la case élargie de la marge : les routes chargées pour la calculer."""
    s, w, n, e = osm_tile_bbox(tile)
    ky = EARTH_R * math.pi / 180
    mlat = margin_m / ky
    mlon = margin_m / (ky * math.cos(math.radians((s + n) / 2)))
    return s - mlat, w - mlon, n + mlat, e + mlon


def dem_maps_for_tile(tile):
    """Cartes d'altitude dont le calcul de la case peut avoir besoin (case + marge)."""
    S, W, N, E = tile_bounds_with_margin(tile)
    i0, j0 = DemService.tile_of(S, W)
    i1, j1 = DemService.tile_of(N, E)
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def process_tile(tile, elevation, fetch=None, progress=None):
    """Toutes les côtes dont le PIED est dans la case `tile` (une côte appartient à une seule case).
    On stocke tout (pavés et petites rues compris) ; le filtrage se fait à la recherche."""
    fetch = fetch or fetch_osm_tile
    s, w, n, e = osm_tile_bbox(tile)
    S, W, N, E = tile_bounds_with_margin(tile)
    elements, seen, signals = [], set(), set()
    for t in neighbor_tiles(tile):
        els = fetch(t)
        if signals is not None and not any(el.get("type") == "meta" for el in els[-1:]):
            signals = None  # case téléchargée avant la récupération des feux/stops
        for el in els:
            if el.get("type") == "node":
                if signals is not None:
                    signals.add(el["id"])
                continue
            if el.get("type") == "meta" or el["id"] in seen:
                continue
            seen.add(el["id"])
            geom = el.get("geometry") or []
            if any(g and S <= g["lat"] <= N and W <= g["lon"] <= E for g in geom):
                elements.append(el)
    center = ((s + n) / 2, (w + e) / 2)
    climbs, stats = scan_elements(elements, elevation, center, float("inf"), include_cobbles=True,
                                  start_ok=lambda la, lo: s <= la < n and w <= lo < e, progress=progress,
                                  signals=signals)
    for c in climbs:
        c["minor"] = c["highway"] in MINOR_STREETS
    return climbs, stats


def check():
    """Vérifie que les services répondent (à lancer une fois avant le premier scan)."""
    ok = True
    try:
        q = '[out:json][timeout:25];way(around:300,45.9237,6.8694)["highway"];out ids;'
        raw = http_post(OVERPASS_URLS[0], urllib.parse.urlencode({"data": q}).encode(),
                        {"User-Agent": USER_AGENT}, 60)
        print(f"Overpass : OK ({len(json.loads(raw)['elements'])} voies)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Overpass : ÉCHEC -> {e}")
    pts = {"Chamonix": (45.9237, 6.8694), "Tour Eiffel": (48.8584, 2.2945), "Vieux-Port": (43.2965, 5.3698)}
    dem_vals = {}
    try:
        dem = DemService()
        for name, p in pts.items():
            dem_vals[name] = dem.lookup([p])[0]
        print("Cartes MNT IGN : OK -> " + ", ".join(f"{k} {v:.1f} m" for k, v in dem_vals.items()))
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Cartes MNT IGN : ÉCHEC -> {e}")
    try:
        api = ElevationService()
        vals = api.lookup(list(pts.values()))
        print(f"API altitude IGN : OK (mode {api.mode}) -> " + ", ".join(
            f"{k} {v:.1f} m" for k, v in zip(pts, vals)))
        for (k, v) in zip(pts, vals):
            if k in dem_vals and dem_vals[k] is not None and abs(dem_vals[k] - v) > 8:
                print(f"  ⚠ écart MNT/API à {k} : {dem_vals[k]:.1f} vs {v:.1f} m")
    except Exception as e:  # noqa: BLE001
        print(f"API altitude IGN (secours) : ÉCHEC -> {e}")
    return ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    print("Usage : python3 scanner.py --check   (le calcul se lance avec precompute.py ou depuis le site)")

"""
Indexation de toute la France métropolitaine à partir des extraits OpenStreetMap de Geofabrik
(voir EVOLUTIONS.md, « Indexer toute la France »).

    ./lancer-france.sh                       # recommandé : anti-veille + relance auto
    python3 france.py                        # tout, par blocs, dans l'ordre de priorité (reprenable)
    python3 france.py --status               # avancement
    python3 france.py --max-blocks 1         # un seul bloc (essai)
    python3 france.py --pbf extrait.osm.pbf  # autre extrait (ex. Corse, pour un essai rapide)

Étapes :
1. télécharge france-latest.osm.pbf (Geofabrik, ~5 Go, une seule fois, reprise si coupure) ;
2. osmium ne garde que les routes, les feux et les stops (cache/geofabrik/<nom>-routes.osm.pbf) ;
3. par bloc de 25 × 25 cases (~110 km) : osmium extrait les routes du bloc et de la couronne de
   cases autour, lues ici et rangées dans cache/osm/<i>_<j>.json, au même format qu'Overpass
   (feux/stops compris) ; les cases sans route (mer, étranger) sont ignorées ;
4. les cartes d'altitude des seules zones avec des routes sont téléchargées (celles du bloc suivant
   pendant le calcul du bloc courant), puis les cases sont calculées en parallèle (un processus
   par cœur, moins un) ;
5. une carte d'altitude téléchargée ici est supprimée dès qu'aucun bloc restant n'en a besoin
   (le disque reste petit) ; celles qui existaient avant (Paris, Nantes…) sont gardées.

Overpass n'est plus utilisé ici (sa politique d'usage interdit d'aspirer toute une région). Il reste
utilisé pour le calcul à la demande d'une petite zone (precompute.py, bouton du site).

Journal : logs/france.log — état : data/france_state.json (blocs, cartes d'altitude à supprimer)
et data/france_status.json (avancement en direct).
"""

import argparse
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait

import scanner
from store import Store, tile_key

HERE = os.path.dirname(os.path.abspath(__file__))
GEO_DIR = os.path.join(scanner.CACHE_DIR, "geofabrik")
OSM_DIR = os.path.join(scanner.CACHE_DIR, "osm")
DEM_DIR = os.path.join(scanner.CACHE_DIR, "dem")
LOG_PATH = os.path.join(HERE, "logs", "france.log")
STATE_PATH = os.path.join(HERE, "data", "france_state.json")
STATUS_PATH = os.path.join(HERE, "data", "france_status.json")
FRANCE_URL = "https://download.geofabrik.de/europe/france-latest.osm.pbf"

FRANCE_BBOX = (41.3, -5.2, 51.15, 9.6)   # métropole et Corse (s, w, n, e)
BLOCK = 25                               # bloc de 25 × 25 cases
WORKERS = max(1, (os.cpu_count() or 2) - 1)
STALL_TIMEOUT_S = 900                    # aucune case terminée depuis 15 min : bloc abandonné (retenté)
DEM_THREADS = 6                          # la limite de débit IGN (scanner.WMS_MIN_INTERVAL) reste la même
SIGNALS = {"traffic_signals", "stop"}
HARD_TRACK = {"asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "chipseal",
              "paving_stones", "sett", "cobblestone", "unhewn_cobblestone"}
ROADS = scanner.ROAD_CLASSES | {"service", "cycleway"}

# Ordre de priorité : les blocs qui contiennent ces points d'abord (dans cet ordre), puis le reste
# du plus proche au plus lointain de Paris.
PRIORITY = [
    # zones demandées
    ("Paris", 48.8566, 2.3522), ("Nantes", 47.2184, -1.5536), ("Grasse", 43.6589, 6.9237),
    ("La Rochelle", 46.1603, -1.1511),
    # grandes villes
    ("Lyon", 45.764, 4.8357), ("Marseille", 43.2965, 5.3698), ("Toulouse", 43.6047, 1.4442),
    ("Nice", 43.7102, 7.262), ("Bordeaux", 44.8378, -0.5792), ("Lille", 50.6292, 3.0573),
    ("Strasbourg", 48.5734, 7.7521), ("Montpellier", 43.6108, 3.8767), ("Rennes", 48.1173, -1.6778),
    ("Grenoble", 45.1885, 5.7245), ("Clermont-Ferrand", 45.7772, 3.087), ("Dijon", 47.322, 5.0415),
    # montagnes et régions vélo
    ("Annecy", 45.8992, 6.1294), ("Chamonix", 45.9237, 6.8694), ("Briançon", 44.8986, 6.6436),
    ("Alpe d'Huez", 45.0907, 6.0698), ("Mont Ventoux", 44.1741, 5.2789), ("Pau", 43.2951, -0.3708),
    ("Luchon", 42.7898, 0.5934), ("Perpignan", 42.6887, 2.8948), ("Tarbes", 43.2328, 0.0781),
    ("Gérardmer", 48.0727, 6.8779), ("Besançon", 47.238, 6.0244), ("Ajaccio", 41.9192, 8.7386),
    ("Bastia", 42.6977, 9.4508), ("Le Puy-en-Velay", 45.0434, 3.885), ("Millau", 44.0981, 3.0779),
]

log = logging.getLogger("france")


# --- Lecture des routes (format OPL d'osmium) -------------------------------------------------
_ESC = re.compile(r"%([0-9a-fA-F]+)%")


def _unescape(s):
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def parse_tags(field):
    """'Thighway=residential,name=Rue%20%X' -> {'highway': 'residential', 'name': 'Rue X'}"""
    tags = {}
    if len(field) > 1:
        for kv in field[1:].split(","):
            k, _, v = kv.partition("=")
            tags[_unescape(k)] = _unescape(v)
    return tags


def parse_opl_line(line):
    """Une ligne OPL -> ('way', élément au format du cache Overpass) | ('node', élément) | None."""
    parts = line.rstrip("\n").split(" ")
    if not parts or not parts[0]:
        return None
    kind, oid = parts[0][0], int(parts[0][1:])
    fields = {p[0]: p for p in parts[1:] if p}
    if kind == "w":
        nodes, geom = [], []
        for ref in fields.get("N", "N")[1:].split(","):
            if not ref:
                continue
            m = re.match(r"n(\d+)x([-0-9.]*)y([-0-9.]*)", ref)
            if not m:
                continue
            nodes.append(int(m.group(1)))
            geom.append({"lat": float(m.group(3)), "lon": float(m.group(2))} if m.group(2) and m.group(3) else None)
        return "way", {"id": oid, "nodes": nodes, "geometry": geom, "tags": parse_tags(fields.get("T", "T"))}
    if kind == "n":
        tags = parse_tags(fields.get("T", "T"))
        if tags.get("highway") not in SIGNALS or "x" not in fields or "y" not in fields:
            return None
        return "node", {"type": "node", "id": oid, "tags": tags,
                        "lat": float(fields["y"][1:]), "lon": float(fields["x"][1:])}
    return None


def keep_way(tags):
    """Même sélection que la requête Overpass (scanner.build_overpass_query)."""
    hw = tags.get("highway")
    if hw in ROADS:
        return tags.get("area") != "yes"
    if hw == "track":
        return tags.get("surface") in HARD_TRACK or tags.get("tracktype") == "grade1"
    return False


def split_into_tiles(lines, tiles):
    """Range les voies et feux/stops d'un flux OPL dans les cases `tiles` (ensemble de (i, j)).
    Renvoie {case: [éléments JSON déjà sérialisés]} ; une voie va dans chaque case où elle a un nœud."""
    out = {t: [] for t in tiles}
    for line in lines:
        rec = parse_opl_line(line)
        if rec is None:
            continue
        kind, el = rec
        if kind == "way":
            if not keep_way(el["tags"]) or len(el["nodes"]) < 2 or any(g is None for g in el["geometry"]):
                continue
            hit = {scanner.osm_tile_of(g["lat"], g["lon"]) for g in el["geometry"]} & tiles
            if hit:
                s = json.dumps(el, separators=(",", ":"), ensure_ascii=False)
                for t in hit:
                    out[t].append(s)
        else:
            t = scanner.osm_tile_of(el["lat"], el["lon"])
            if t in tiles:
                out[t].append(json.dumps(el, separators=(",", ":"), ensure_ascii=False))
    return out


META = json.dumps({"type": "meta", "signals": 1, "source": "geofabrik"}, separators=(",", ":"))


def write_tile(tile, items):
    path = os.path.join(OSM_DIR, f"{tile[0]}_{tile[1]}.json")
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("[" + ",".join(items + [META]) + "]")
    os.replace(tmp, path)


def read_tile(tile):
    """Lecture du cache, sans jamais appeler Overpass ; une case absente = zone sans route."""
    path = os.path.join(OSM_DIR, f"{tile[0]}_{tile[1]}.json")
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return [{"type": "meta", "signals": 1}]


def has_way(items):
    return any(not s.startswith('{"type"') for s in items)


# --- Blocs ------------------------------------------------------------------------------------
def all_blocks():
    s, w, n, e = FRANCE_BBOX
    i0, j0 = scanner.osm_tile_of(s, w)
    i1, j1 = scanner.osm_tile_of(n, e)
    return [(bi, bj) for bi in range(i0 // BLOCK, i1 // BLOCK + 1) for bj in range(j0 // BLOCK, j1 // BLOCK + 1)]


def block_tiles(block, ring=0):
    bi, bj = block
    return {(i, j) for i in range(bi * BLOCK - ring, (bi + 1) * BLOCK + ring)
            for j in range(bj * BLOCK - ring, (bj + 1) * BLOCK + ring)}


def block_bbox(block, ring=0):
    """(s, w, n, e) des cases du bloc (plus `ring` cases autour)."""
    bi, bj = block
    s, w, _, _ = scanner.osm_tile_bbox((bi * BLOCK - ring, bj * BLOCK - ring))
    _, _, n, e = scanner.osm_tile_bbox(((bi + 1) * BLOCK - 1 + ring, (bj + 1) * BLOCK - 1 + ring))
    return s, w, n, e


def block_key(block):
    return f"{block[0]}_{block[1]}"


def pbf_bbox(pbf):
    """Emprise (s, w, n, e) déclarée dans l'en-tête de l'extrait (None si absente)."""
    out = subprocess.run(["osmium", "fileinfo", "-g", "header.boxes", pbf], capture_output=True, text=True).stdout
    m = re.search(r"\(([-0-9.]+),([-0-9.]+),([-0-9.]+),([-0-9.]+)\)", out)
    return (float(m.group(2)), float(m.group(1)), float(m.group(4)), float(m.group(3))) if m else None


def ordered_blocks(bbox=None):
    """Blocs dans l'ordre de priorité (PRIORITY), puis du plus proche au plus lointain de Paris.
    `bbox` (s, w, n, e) : seulement les blocs qui touchent cette emprise (extrait régional)."""
    def contains(block, la, lo):
        s, w, n, e = block_bbox(block)
        return s <= la < n and w <= lo < e

    blocks = all_blocks()
    if bbox:
        bs, bw, bn, be = bbox
        blocks = [b for b in blocks if (lambda s, w, n, e: s < bn and n > bs and w < be and e > bw)(*block_bbox(b))]
    rank = {}
    for k, (_, la, lo) in enumerate(PRIORITY):
        for b in blocks:
            if b not in rank and contains(b, la, lo):
                rank[b] = k
    paris = PRIORITY[0][1:]

    def key(b):
        s, w, n, e = block_bbox(b)
        return (rank.get(b, len(PRIORITY)), scanner.haversine(paris[0], paris[1], (s + n) / 2, (w + e) / 2))
    return sorted(blocks, key=key)


# --- Cartes d'altitude -------------------------------------------------------------------------
def dem_maps_for_items(tile_items):
    """Cartes d'altitude touchées par les routes (nœuds) de ces cases : on ne télécharge rien en mer."""
    maps = set()
    for items in tile_items:
        for s in items:
            if s.startswith('{"type"'):
                continue
            for g in json.loads(s)["geometry"]:
                maps.add(scanner.DemService.tile_of(g["lat"], g["lon"]))
    return maps


def dem_path(m):
    return os.path.join(DEM_DIR, f"{m[0]}_{m[1]}.f32z")


def map_bounds(m):
    s, w = m[0] * scanner.DEM_TILE_DLAT, m[1] * scanner.DEM_TILE_DLON
    return s, w, s + scanner.DEM_TILE_DLAT, w + scanner.DEM_TILE_DLON


def still_needed(m, pending_blocks):
    """Une carte reste utile si elle touche un bloc restant (élargi de la marge d'une case)."""
    s, w, n, e = map_bounds(m)
    for b in pending_blocks:
        bs, bw, bn, be = block_bbox(b, ring=1)
        if s < bn and n > bs and w < be and e > bw:
            return True
    return False


# --- Calcul en parallèle ----------------------------------------------------------------------
_W = {}


def _init_worker():
    _W["dem"] = scanner.DemService()


def _work(tile):
    dem = _W["dem"]
    keep = set(scanner.dem_maps_for_tile(tile))
    dem.tiles = {m: a for m, a in dem.tiles.items() if m in keep}   # mémoire bornée par processus
    t0 = time.time()
    climbs, stats = scanner.process_tile(tile, dem, fetch=read_tile)
    return tile, climbs, stats, time.time() - t0


# --- État, journal ----------------------------------------------------------------------------
def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(ch)


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("blocks", {})       # bloc -> {"road_tiles": [[i, j]...], "source": pbf, "date": …}
    st.setdefault("owned_maps", [])   # cartes d'altitude téléchargées ici (à supprimer quand inutiles)
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def write_status(**kw):
    kw["updated"] = time.time()
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(kw, f, ensure_ascii=False)
    os.replace(tmp, STATUS_PATH)


# --- Téléchargement et filtrage ---------------------------------------------------------------
def download(url, path):
    """Téléchargement avec reprise (Range) : un fichier de 5 Go survit à une coupure réseau."""
    if os.path.exists(path):
        return path
    part = path + ".part"
    for attempt in range(20):
        done = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": scanner.USER_AGENT, "Range": f"bytes={done}-"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "ab" if done else "wb") as f:
                total = done + int(r.headers.get("Content-Length") or 0)
                if r.status == 200 and done:          # le serveur ignore la reprise : on repart de zéro
                    f.seek(0)
                    f.truncate()
                    done = 0
                next_log = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total and done >= next_log:
                        log.info(f"Téléchargement {os.path.basename(path)} : {done / 1e9:.2f}/{total / 1e9:.2f} Go")
                        write_status(running=True, message=f"Téléchargement de l'extrait OSM : {100 * done / total:.0f} %")
                        next_log = done + total // 20
            os.replace(part, path)
            return path
        except (OSError, urllib.error.URLError) as e:
            log.warning(f"Téléchargement interrompu ({e}), reprise dans 30 s")
            time.sleep(30)
    raise RuntimeError(f"téléchargement impossible : {url}")


def roads_pbf(pbf):
    """Ne garde que les routes, feux et stops (fait une fois par extrait)."""
    out = pbf.replace(".osm.pbf", "-routes.osm.pbf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(pbf):
        return out
    classes = ",".join(sorted(ROADS | {"track"}))
    log.info(f"Filtrage des routes de {os.path.basename(pbf)} (osmium)…")
    write_status(running=True, message="Filtrage des routes (osmium)…")
    subprocess.run(["osmium", "tags-filter", pbf, f"w/highway={classes}", "n/highway=traffic_signals,stop",
                    "-o", out + ".tmp.osm.pbf", "--overwrite"], check=True)
    os.replace(out + ".tmp.osm.pbf", out)
    return out


def extract_block(roads, block):
    """Routes du bloc et de sa couronne -> cache/osm. Renvoie (cases avec routes, cartes d'altitude)."""
    s, w, n, e = block_bbox(block, ring=1)
    tmp = os.path.join(GEO_DIR, f"bloc-{block_key(block)}.osm.pbf")
    subprocess.run(["osmium", "extract", "-b", f"{w},{s},{e},{n}", "--strategy", "complete_ways",
                    roads, "-o", tmp, "--overwrite"], check=True, capture_output=True)
    proc = subprocess.Popen(["osmium", "add-locations-to-ways", tmp, "-f", "opl,add_metadata=false", "-o", "-"],
                            stdout=subprocess.PIPE, text=True, encoding="utf-8")
    tiles = block_tiles(block, ring=1)
    per_tile = split_into_tiles(proc.stdout, tiles)
    if proc.wait() != 0:
        raise RuntimeError(f"osmium add-locations-to-ways a échoué (bloc {block_key(block)})")
    os.remove(tmp)
    os.makedirs(OSM_DIR, exist_ok=True)
    core = block_tiles(block)
    for t, items in per_tile.items():
        if has_way(items) or t in core:   # une case de bloc sans route est écrite vide (= « rien ici »)
            write_tile(t, items)
    road_tiles = sorted(t for t in core if has_way(per_tile[t]))
    # cartes d'altitude utiles : routes du bloc et de la marge de 3 km autour (couronne d'une case)
    maps = dem_maps_for_items(per_tile.values())
    return road_tiles, maps


def download_maps(maps, dem):
    todo = [m for m in maps if not os.path.exists(dem_path(m))]
    failed = []

    def one(m):
        try:
            dem.download(m)
        except Exception as e:  # noqa: BLE001 — la case concernée retentera elle-même
            failed.append((m, e))
    with ThreadPoolExecutor(DEM_THREADS) as ex:
        list(ex.map(one, todo))
    if failed:
        log.warning(f"{len(failed)} carte(s) d'altitude en échec (retentées au calcul) : {failed[0][1]}")
    return todo


# --- Boucle principale ------------------------------------------------------------------------
class Run:
    def __init__(self, pbf, store, max_blocks=None):
        self.pbf, self.store, self.max_blocks = pbf, store, max_blocks
        self.state = load_state()
        self.dem = scanner.DemService()
        self.lock = threading.Lock()
        # un pool neuf par bloc : mémoire des processus libérée entre deux blocs. Pas de
        # max_tasks_per_child : avec lui, le pool de Python se fige quand les processus atteignent
        # leur limite (blocage du bloc de Lyon, 13/11/2026)
        self.make_pool = lambda: ProcessPoolExecutor(WORKERS, initializer=_init_worker)

    def prepare(self, roads, block):
        """Extraction du bloc + téléchargement de ses cartes d'altitude (en tâche de fond)."""
        key = block_key(block)
        info = self.state["blocks"].get(key)
        if info is None or info.get("source") != os.path.basename(self.pbf):
            t0 = time.time()
            road_tiles, maps = extract_block(roads, block)
            info = {"road_tiles": [list(t) for t in road_tiles], "source": os.path.basename(self.pbf),
                    "maps": sorted([list(m) for m in maps])}
            with self.lock:
                self.state["blocks"][key] = info
                save_state(self.state)
            log.info(f"Bloc {key} : {len(road_tiles)} cases avec routes, {len(maps)} cartes d'altitude "
                     f"utiles (extraction {time.time() - t0:.0f} s)")
        todo = [tuple(t) for t in info["road_tiles"]]
        todo = [t for t in todo if t not in self.store.done_tiles(todo)]
        if todo:
            t0 = time.time()
            got = download_maps([tuple(m) for m in info["maps"]], self.dem)
            with self.lock:
                owned = set(map(tuple, self.state["owned_maps"])) | set(got)
                self.state["owned_maps"] = sorted([list(m) for m in owned])
                save_state(self.state)
            if got:
                log.info(f"Bloc {key} : {len(got)} cartes d'altitude téléchargées en {time.time() - t0:.0f} s")
        return todo

    def rotate_dem(self, pending):
        """Supprime les cartes téléchargées ici dont plus aucun bloc restant n'a besoin."""
        with self.lock:
            keep, gone = [], 0
            for m in map(tuple, self.state["owned_maps"]):
                if still_needed(m, pending):
                    keep.append(list(m))
                else:
                    try:
                        os.remove(dem_path(m))
                        gone += 1
                    except FileNotFoundError:
                        pass
            self.state["owned_maps"] = keep
            save_state(self.state)
        if gone:
            log.info(f"{gone} cartes d'altitude supprimées (plus aucun bloc restant n'en a besoin)")

    def run(self):
        roads = roads_pbf(self.pbf)
        blocks = ordered_blocks(pbf_bbox(self.pbf))
        if self.max_blocks:
            blocks = blocks[:self.max_blocks]
        # blocs déjà faits (toutes leurs cases avec routes en base)
        def block_done(b):
            info = self.state["blocks"].get(block_key(b))
            if not info or info.get("source") != os.path.basename(self.pbf):
                return False
            tiles = [tuple(t) for t in info["road_tiles"]]
            return len(self.store.done_tiles(tiles)) == len(tiles)
        pending = [b for b in blocks if not block_done(b)]
        log.info(f"Démarrage : {len(blocks)} blocs, {len(pending)} à faire, {WORKERS} processus de calcul")
        self.rotate_dem(pending)
        if not pending:
            write_status(running=False, message="France : tous les blocs sont calculés")
            return 0

        prep = ThreadPoolExecutor(1)
        failed_total = 0
        nxt = prep.submit(self.prepare, roads, pending[0])
        for k, block in enumerate(pending):
            key = block_key(block)
            write_status(running=True, block=key, blocks_done=k, blocks_total=len(pending),
                         message=f"Bloc {key} ({k + 1}/{len(pending)}) : extraction et cartes d'altitude…")
            try:
                todo = nxt.result()
            except Exception as e:  # noqa: BLE001 — un bloc en échec ne bloque pas les suivants
                log.error(f"Bloc {key} : préparation en échec : {e}\n{traceback.format_exc()}")
                todo = None
            if k + 1 < len(pending):   # le bloc suivant se prépare pendant le calcul de celui-ci
                nxt = prep.submit(self.prepare, roads, pending[k + 1])
            if not todo:
                if todo is not None:
                    self.rotate_dem(pending[k + 1:])
                continue
            failed = self.compute(key, todo, k, len(pending))
            failed_total += failed
            # cases en échec : on garde les cartes de ce bloc pour la prochaine tentative
            self.rotate_dem(pending[k + 1:] + ([block] if failed else []))
        prep.shutdown()
        msg = (f"France : {len(pending)} blocs traités" + (f", {failed_total} case(s) en échec (relancer pour "
               f"les retenter)" if failed_total else "") + f". Base : {self.store.stats()['climbs']} côtes.")
        log.info(msg)
        write_status(running=False, message=msg)
        return 2 if failed_total else 0

    def compute(self, key, todo, k, n_blocks, stall_timeout=STALL_TIMEOUT_S):
        """Calcule les cases `todo` en parallèle. Renvoie le nombre de cases en échec. Si plus
        aucune case ne se termine pendant `stall_timeout` s, le bloc est abandonné (journalisé,
        processus arrêtés) : ses cases restantes seront retentées plus tard."""
        t0, done, failed, n_climbs = time.time(), 0, 0, 0
        pool = self.make_pool()
        try:
            futures = {pool.submit(_work, t): t for t in todo}
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=stall_timeout, return_when=FIRST_COMPLETED)
                if not finished:
                    failed += len(pending)
                    log.error(f"Bloc {key} : aucune case terminée depuis {stall_timeout / 60:.0f} min, "
                              f"{len(pending)} case(s) abandonnée(s), retentées plus tard")
                    for fut in pending:
                        fut.cancel()
                    for proc in list(getattr(pool, "_processes", {}).values()):
                        proc.terminate()
                    break
                for fut in finished:
                    tile = futures[fut]
                    try:
                        tile, climbs, stats, dt = fut.result()
                        for c in climbs:
                            c["minor"] = c["highway"] in scanner.MINOR_STREETS
                        self.store.save_tile(tile, climbs, stats)
                        done += 1
                        n_climbs += len(climbs)
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        log.error(f"Case {tile_key(tile)} en échec : {e}")
                    if (done + failed) % 25 == 0 or done + failed == len(todo):
                        rate = (time.time() - t0) / max(1, done + failed)
                        write_status(running=True, block=key, blocks_done=k, blocks_total=n_blocks,
                                     tiles_done=done, tiles_total=len(todo),
                                     message=f"Bloc {key} ({k + 1}/{n_blocks}) : {done}/{len(todo)} cases, "
                                             f"{n_climbs} côtes, encore ~{scanner._duration(rate * (len(todo) - done - failed))}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        log.info(f"Bloc {key} : {done} cases, {n_climbs} côtes en {scanner._duration(time.time() - t0)}"
                 + (f", {failed} en échec" if failed else ""))
        return failed


def status(pbf, store):
    st = load_state()
    blocks = ordered_blocks(pbf_bbox(pbf) if os.path.exists(pbf) else None)
    src = os.path.basename(pbf)
    n_done = n_tiles = 0
    for b in blocks:
        info = st["blocks"].get(block_key(b))
        if info and info.get("source") == src:
            tiles = [tuple(t) for t in info["road_tiles"]]
            n_tiles += len(tiles)
            n_done += len(store.done_tiles(tiles))
    seen = sum(1 for b in blocks if (st["blocks"].get(block_key(b)) or {}).get("source") == src)
    print(f"Blocs : {len(blocks)} au total, {seen} extraits ; cases avec routes extraites : {n_done}/{n_tiles} "
          f"calculées. Base : {store.stats()['climbs']} côtes. Cartes d'altitude en sursis : {len(st['owned_maps'])}.")


def main():
    ap = argparse.ArgumentParser(description="Indexation de toute la France (extraits Geofabrik)")
    ap.add_argument("--pbf", help="extrait .osm.pbf à utiliser (défaut : France entière, téléchargée)")
    ap.add_argument("--max-blocks", type=int, help="ne traiter que les N premiers blocs (essai)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    setup_logging()
    if shutil.which("osmium") is None:
        sys.exit("osmium introuvable : brew install osmium-tool")
    os.makedirs(GEO_DIR, exist_ok=True)
    pbf = args.pbf or os.path.join(GEO_DIR, "france-latest.osm.pbf")
    store = Store()
    if args.status:
        return status(pbf, store)
    if not args.pbf:
        download(FRANCE_URL, pbf)
    try:
        sys.exit(Run(pbf, store, args.max_blocks).run())
    except KeyboardInterrupt:
        write_status(running=False, message="Arrêté manuellement")
        sys.exit(130)


if __name__ == "__main__":
    main()

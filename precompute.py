"""
Pré-calcul des côtes d'une grande zone, à laisser tourner (par ex. toute la nuit).

    ./lancer-nuit.sh                                       # recommandé : anti-veille + relance auto
    python3 precompute.py                                  # 50 km autour de Paris
    python3 precompute.py --lat 45.764 --lon 4.835 --radius 30
    python3 precompute.py --status                         # état de la base

Fonctionne en autonomie :
- une case qui plante (réseau, service IGN/Overpass, donnée bizarre) est notée dans le journal
  et sautée ; les cases en échec sont retentées automatiquement plus tard (2 min, 5 min, 15 min…) ;
- si plusieurs cases d'affilée échouent (réseau coupé), le script fait une pause avant de continuer ;
- toute erreur inattendue est journalisée et le calcul reprend après 1 min ;
- reprenable : les cases déjà en base sont sautées. Ctrl+C arrête proprement.

Journal : logs/precompute.log — état en direct : data/precompute_status.json (affiché sur le site).
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor

import scanner
from store import Store, tile_key

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "logs", "precompute.log")
STATUS_PATH = os.path.join(HERE, "data", "precompute_status.json")
PARIS = (48.8566, 2.3522)
RETRY_DELAYS_MIN = [2, 5, 15, 30, 60, 60, 120, 120, 240]   # pauses avant de retenter les cases en échec
MAX_CONSECUTIVE_FAILURES = 3

log = logging.getLogger("precompute")


class _OneLine(logging.Formatter):
    """Console : seulement la première ligne (le détail complet est dans le journal)."""

    def format(self, record):
        return super().format(record).split("\n", 1)[0]


def setup_logging(console=True):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
        if console:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(_OneLine("%(asctime)s %(message)s", "%H:%M:%S"))
            log.addHandler(ch)


def _prefetch(fetch, tile):
    try:
        fetch(tile)  # met la case en cache disque ; le résultat est relu au moment du calcul
        return None
    except Exception as e:  # noqa: BLE001
        return e


def _done_future(result=None):
    f = Future()
    f.set_result(result)
    return f


def run_tiles(tiles, store, dem=None, report=None, stop=None, fetch=None, on_tile=None, on_error=None):
    """Calcule les cases `tiles` (dans l'ordre donné) qui ne sont pas encore en base.
    Une case qui échoue est sautée (on_error(tile, exception, traceback)) sans arrêter les autres.
    report(done, total, message) est appelé pendant le calcul. Renvoie (faites, cases en échec)."""
    fetch = fetch or scanner.fetch_osm_tile
    dem = dem or scanner.DemService()
    report = report or (lambda d, t, m: None)
    on_error = on_error or (lambda t, e, tb: None)
    already = store.done_tiles(tiles)
    todo = [t for t in tiles if t not in already]
    total = len(todo)
    if not total:
        report(0, 0, "Zone déjà entièrement calculée")
        return 0, []

    # Téléchargements OSM en avance (2 en parallèle), dans l'ordre de traitement
    order, seen = [], set()
    for t in todo:
        for nb in [t] + scanner.neighbor_tiles(t):
            if nb not in seen:
                seen.add(nb)
                order.append(nb)
    ex = ThreadPoolExecutor(2)
    futures = {nb: ex.submit(_prefetch, fetch, nb) for nb in order}
    # Cartes d'altitude en avance aussi (2 en parallèle, sur disque) : sinon chaque case attend
    # les siennes au moment du calcul. Un échec ici est sans conséquence : la case retentera.
    dem_ex = None
    if hasattr(dem, "download"):
        dem_ex, maps = ThreadPoolExecutor(2), {}
        for t in todo:
            for m in scanner.dem_maps_for_tile(t):
                if m not in maps:
                    maps[m] = dem_ex.submit(_prefetch, dem.download, m)

    t0, done, failed, n_climbs, consecutive = time.time(), 0, [], 0, 0
    try:
        for k, tile in enumerate(todo):
            if stop is not None and stop.is_set():
                break
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                pause = min(600, 60 * consecutive)
                report(done, total, f"{consecutive} cases d'affilée en échec (réseau ?) : pause de "
                                    f"{pause // 60} min avant de continuer")
                if _sleep(pause, stop):
                    break
            try:
                report(done, total, f"Case {k + 1}/{total} : téléchargement des routes…")
                for nb in [tile] + scanner.neighbor_tiles(tile):
                    err = futures[nb].result()
                    if err is not None:
                        # échec du préchargement, peut-être ancien (lancé pendant une panne) :
                        # nouvel essai maintenant plutôt que d'abandonner la case sur cet échec
                        err = _prefetch(fetch, nb)
                        if err is None:
                            futures[nb] = _done_future()
                    if err is not None:
                        raise RuntimeError(f"Overpass : {err}")
                c0 = time.time()
                climbs, stats = scanner.process_tile(
                    tile, dem, fetch=fetch,
                    progress=lambda f, m, k=k: report(done, total, f"Case {k + 1}/{total} : {m}"))
                store.save_tile(tile, climbs, stats)
            except Exception as e:  # noqa: BLE001 — une case ne doit jamais arrêter tout le calcul
                failed.append(tile)
                consecutive += 1
                on_error(tile, e, traceback.format_exc())
                report(done, total, f"Case {tile_key(tile)} en échec ({e}) : elle sera retentée plus tard")
                # pas de nouveau préchargement ici : il passerait en fin de file et bloquerait les
                # cases voisines suivantes ; un préchargement en échec est retenté par la case qui en a besoin
                continue
            consecutive = 0
            done += 1
            n_climbs += len(climbs)
            if on_tile:
                on_tile(tile)
            rate = (time.time() - t0) / done
            rest = total - done - len(failed)
            eta = f" · encore ~{scanner._duration(rate * rest)}" if rest else ""
            report(done, total, f"Case {k + 1}/{total} ({tile_key(tile)}) : {len(climbs)} côtes en "
                                f"{time.time() - c0:.1f} s · {n_climbs} côtes ajoutées{eta}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        if dem_ex is not None:
            dem_ex.shutdown(wait=False, cancel_futures=True)
    return done, failed


def _sleep(seconds, stop=None):
    """Pause interruptible. Renvoie True si un arrêt a été demandé."""
    if stop is None:
        time.sleep(seconds)
        return False
    return stop.wait(seconds)


class StatusFile:
    """État en direct, lu par le site (data/precompute_status.json)."""

    def __init__(self, zone):
        self.zone = zone
        self.last = 0

    def due(self):
        return time.time() - self.last >= 2

    def write(self, force=False, **kw):
        if not force and not self.due():
            return
        self.last = time.time()
        data = dict(self.zone, pid=os.getpid(), updated=time.time(), **kw)
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATUS_PATH)


def main():
    ap = argparse.ArgumentParser(description="Pré-calcul des côtes d'une zone")
    ap.add_argument("--lat", type=float, default=PARIS[0])
    ap.add_argument("--lon", type=float, default=PARIS[1])
    ap.add_argument("--radius", type=float, default=50, help="rayon en km (défaut 50)")
    ap.add_argument("--status", action="store_true", help="affiche l'état de la base et quitte")
    args = ap.parse_args()

    setup_logging()
    store = Store()
    tiles = scanner.tiles_for_scan(args.lat, args.lon, args.radius * 1000)
    done = store.done_tiles(tiles)
    st = store.stats()
    print(f"Base : {st['climbs']} côtes, {st['tiles']} cases calculées au total.")
    print(f"Zone {args.radius:g} km autour de ({args.lat}, {args.lon}) : {len(tiles)} cases, "
          f"{len(done)} déjà faites, {len(tiles) - len(done)} à faire.")
    if args.status:
        return

    zone = {"center": [args.lat, args.lon], "radius_km": args.radius, "tiles_total": len(tiles)}
    status = StatusFile(zone)
    stop = threading.Event()
    tty = sys.stdout.isatty()

    def report(d, total, msg):
        if status.due():
            status.write(running=True, message=msg, tiles_done=len(store.done_tiles(tiles)))
        final = " côtes en " in msg or "échec" in msg or "pause" in msg or "déjà" in msg
        if final:
            if tty:
                print("\r" + " " * 120 + "\r", end="")
            log.info(f"[{d:>4}/{total}] {msg}")
        elif tty:
            print("\r" + f"[{d:>4}/{total}] {msg}"[:118].ljust(118), end="", flush=True)

    def on_error(tile, e, tb):
        log.error(f"Case {tile_key(tile)} en échec : {e}\n{tb}")

    log.info(f"Démarrage : zone {args.radius:g} km autour de ({args.lat}, {args.lon}), "
             f"{len(tiles) - len(done)} cases à faire")
    retry = 0
    try:
        while True:
            try:
                ok, failed = run_tiles(tiles, store, report=report, stop=stop, on_error=on_error)
            except Exception as e:  # noqa: BLE001 — filet de sécurité : on journalise et on reprend
                log.error(f"Erreur inattendue : {e}\n{traceback.format_exc()}")
                status.write(force=True, running=True, message=f"Erreur inattendue, reprise dans 1 min : {e}")
                time.sleep(60)
                continue
            remaining = len(tiles) - len(store.done_tiles(tiles))
            if remaining == 0:
                msg = f"Zone terminée : {store.stats()['climbs']} côtes en base."
                log.info(msg)
                status.write(force=True, running=False, message=msg, tiles_done=len(tiles))
                return
            if retry >= len(RETRY_DELAYS_MIN):
                msg = f"Arrêt après {retry} séries de nouvelles tentatives : {remaining} case(s) toujours en échec."
                log.warning(msg)
                status.write(force=True, running=False, message=msg)
                sys.exit(2)
            wait = RETRY_DELAYS_MIN[retry]
            retry += 1
            msg = f"{remaining} case(s) en échec, nouvel essai dans {wait} min (tentative {retry}/{len(RETRY_DELAYS_MIN)})"
            log.info(msg)
            status.write(force=True, running=True, message=msg)
            time.sleep(wait * 60)
    except KeyboardInterrupt:
        stop.set()
        log.info("Arrêt demandé (Ctrl+C). Relance la même commande pour reprendre.")
        status.write(force=True, running=False, message="Arrêté manuellement")
        sys.exit(130)


if __name__ == "__main__":
    main()

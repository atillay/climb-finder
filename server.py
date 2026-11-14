"""
Serveur du site : sert la page et interroge la base des côtes (data/cotes.sqlite).

    python3 server.py            # puis ouvrir l'adresse affichée (port 8765 par défaut)
    python3 server.py --port 9000 --host 0.0.0.0

Si le port est déjà pris, le serveur essaie automatiquement les 10 ports suivants.

API :
    GET  /api/climbs?lat=&lon=&radius_km=&cobbles=0&minor=1&len_min=&len_max=&avg_min=
                    &max300_min=&gain_min=&q=&sort=score&dir=desc&limit=2000
         -> {"climbs": [...], "total": n, "coverage": {"done": d, "total": t}}
    GET  /api/climbs/<id>          -> côte complète (tracé + profil)
    GET  /api/coverage?s=&w=&n=&e= -> cases déjà calculées dans ce rectangle
    POST /api/scan {"lat", "lon", "radius_km"} -> lance le calcul des cases manquantes en tâche de fond
    GET  /api/status               -> avancement du calcul + taille de la base

Mode lecture seule (site en ligne, la base est calculée en local puis envoyée par deployer.sh) :
    python3 server.py --readonly --db /var/lib/climb-finder/cotes.sqlite
    -> pas de calcul en tâche de fond, POST /api/scan refusé, bouton « Calculer la zone » masqué.
    Les URL de la page sont relatives : le site peut être servi à la racine (https://climb-finder.alty.fr/)
    comme sous un sous-dossier.
"""

import argparse
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import scanner
from store import Store

SEARCH_LOCK = threading.Lock()
BOX_MAX_DEG = 1.4    # zone affichée au plus ~150 km de haut (au-delà : zoomer)
SEARCH_MAX_KM = 30   # rayon max d'une recherche (au-delà : réponses lourdes pour peu d'intérêt)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Worker:
    """Calcule en tâche de fond les cases manquantes de la dernière zone demandée."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.pending = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.status = {"running": False, "message": "", "done": 0, "total": 0, "version": 0,
                       "center": None, "radius_km": None}
        self.dem = None
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, lat, lon, radius_km):
        tiles = scanner.tiles_for_scan(lat, lon, radius_km * 1000)
        todo = len(tiles) - len(self.store.done_tiles(tiles))
        with self.lock:
            self.pending = (lat, lon, radius_km, tiles)
            self.stop.set()   # la zone précédente s'arrête après sa case en cours
            self.wake.set()
        return len(tiles), todo

    def _loop(self):
        while True:
            self.wake.wait()
            with self.lock:
                req, self.pending = self.pending, None
                self.wake.clear()
                self.stop.clear()
            if not req:
                continue
            lat, lon, radius_km, tiles = req
            st = self.status
            st.update(running=True, center=[lat, lon], radius_km=radius_km, done=0, total=0,
                      message="Démarrage du calcul…")

            def report(d, total, msg):
                st.update(done=d, total=total, message=msg)

            def on_tile(_):
                st["version"] += 1

            from precompute import run_tiles  # import ici : inutile en lecture seule

            try:
                if self.dem is None:
                    self.dem = scanner.DemService()
                ok, failed = run_tiles(tiles, self.store, dem=self.dem, report=report, stop=self.stop,
                                       on_tile=on_tile,
                                       on_error=lambda t, e, tb: print(f"Case {t} en échec : {e}\n{tb}"))
                if not self.stop.is_set():
                    st["message"] = (f"Zone calculée ({ok} nouvelles cases)" if ok else "Zone déjà calculée") + (
                        f", {len(failed)} case(s) en échec : reclique pour les retenter" if failed else "")
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                st["message"] = f"Erreur : {e}"
            with self.lock:
                if self.pending is None:
                    st["running"] = False


def share_meta(c):
    """<title> et balises Open Graph d'une côte, pour l'aperçu d'un lien partagé."""
    from html import escape

    def fr(v, d=1):   # nombre à la française : 6,4
        return f"{v:.{d}f}".replace(".", ",")
    name = c["name"] if c.get("variant") in (None, "principale") else (c.get("streets") or c["name"])
    length = f"{fr(c['length_m'] / 1000, 2)} km" if c["length_m"] >= 1000 else f"{c['length_m']:.0f} m"
    title = f"{name} — {length} à {fr(c['avg_grade'])} %"
    desc = (f"D+ {c['gain_m']:.0f} m, jusqu'à {fr(c['max_grade_100'] or 0)} % sur 100 m, difficulté "
            f"{c['score']:.0f}. Côte à vélo trouvée par Climb Finder.")
    t, d = escape(title), escape(desc)
    return (f'<title>{t}</title>\n<meta property="og:title" content="{t}">\n'
            f'<meta property="og:description" content="{d}">\n<meta name="description" content="{d}">\n'
            f'<meta property="og:type" content="website">')


def _num(qs, key):
    v = qs.get(key, [""])[0]
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "CotesFinder/0.3"
    store = None
    worker = None   # None en lecture seule

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/api/status" not in line and "/api/climbs" not in line and "/api/coverage" not in line:
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # site en ligne : les données ne changent qu'au déploiement, le navigateur peut les garder
        # 10 min (déplacements de carte, retours en arrière) ; en local, le calcul les modifie
        self.send_header("Cache-Control", "public, max-age=600" if cache and self.worker is None else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    page = f.read()
                if self.worker is None:
                    page = page.replace(b"<body>", b'<body class="readonly">', 1)
                cid = qs.get("c", [""])[0]
                c = self.store.get(cid) if cid else None
                if c:   # lien partagé : titre et aperçu (Messages, WhatsApp, Slack…) de la côte
                    page = page.replace(b"<title>C\xc3\xb4tes autour de moi</title>", share_meta(c).encode(), 1)
                return self._send(200, page, "text/html; charset=utf-8")

            if url.path == "/api/climbs":
                box = [_num(qs, k) for k in ("s", "w", "n", "e")]
                if None not in box:   # zone affichée sur la carte (emprise bornée à BOX_MAX_DEG)
                    if box[2] - box[0] > BOX_MAX_DEG or box[3] - box[1] > BOX_MAX_DEG * 1.5:
                        return self._send(400, {"error": "zone trop grande : zoomer"})
                    lat, lon, radius = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, None
                else:
                    box = None
                    lat, lon = _num(qs, "lat"), _num(qs, "lon")
                    radius = max(0.5, min(SEARCH_MAX_KM, _num(qs, "radius_km") or 10))
                    if lat is None or lon is None:
                        return self._send(400, {"error": "lat/lon ou s/w/n/e manquants"})
                with SEARCH_LOCK:   # une grosse recherche à la fois : borne la mémoire du serveur en ligne
                    res = self.store.search(
                        lat, lon, radius,
                        cobbles=qs.get("cobbles", ["0"])[0] == "1", minor=qs.get("minor", ["1"])[0] == "1",
                        len_min=_num(qs, "len_min"), len_max=_num(qs, "len_max"), avg_min=_num(qs, "avg_min"),
                        max300_min=_num(qs, "max300_min"), gain_min=_num(qs, "gain_min"),
                        junc_max=_num(qs, "junc_max"),
                        q=qs.get("q", [""])[0].strip() or None,
                        sort=qs.get("sort", ["score"])[0], desc=qs.get("dir", ["desc"])[0] != "asc",
                        limit=int(_num(qs, "limit") or 2000), offset=int(_num(qs, "offset") or 0), box=box)
                    if box:
                        i0, j0 = scanner.osm_tile_of(box[0], box[1])
                        i1, j1 = scanner.osm_tile_of(box[2], box[3])
                        tiles = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
                    else:
                        tiles = scanner.tiles_for_scan(lat, lon, radius * 1000)
                    res["coverage"] = {"done": len(self.store.done_tiles(tiles)), "total": len(tiles)}
                    body = json.dumps(res, ensure_ascii=False, separators=(",", ":")).encode()
                    del res
                return self._send(200, body, cache=True)

            if url.path == "/api/map":   # côtes de la zone visible, pour la carte (indépendant du tableau)
                box = [_num(qs, k) for k in ("s", "w", "n", "e")]
                if None in box:
                    return self._send(400, {"error": "s/w/n/e manquants"})
                with SEARCH_LOCK:
                    res = self.store.map_climbs(
                        *box, cobbles=qs.get("cobbles", ["0"])[0] == "1", minor=qs.get("minor", ["1"])[0] == "1",
                        len_min=_num(qs, "len_min"), len_max=_num(qs, "len_max"), avg_min=_num(qs, "avg_min"),
                        max300_min=_num(qs, "max300_min"), gain_min=_num(qs, "gain_min"),
                        junc_max=_num(qs, "junc_max"), q=qs.get("q", [""])[0].strip() or None)
                return self._send(200, res, cache=True)

            if url.path.startswith("/api/climbs/"):
                c = self.store.get(url.path.rsplit("/", 1)[-1])
                return self._send(200, c, cache=True) if c else self._send(404, {"error": "côte inconnue"})

            if url.path == "/api/coverage":
                box = [_num(qs, k) for k in ("s", "w", "n", "e")]
                if None in box:
                    return self._send(400, {"error": "s/w/n/e manquants"})
                return self._send(200, {"dlat": scanner.OSM_TILE_DLAT, "dlon": scanner.OSM_TILE_DLON,
                                        "tiles": self.store.coverage(*box)})

            if url.path == "/api/status":
                if self.worker is None:
                    return self._send(200, {"readonly": True, "running": False, "message": "",
                                            "db": self.store.stats(), "night": None})
                out = dict(self.worker.status, db=self.store.stats(), night=None)
                from precompute import STATUS_PATH
                try:  # calcul lancé par precompute.py (fichier d'état mis à jour toutes les ~2 s)
                    with open(STATUS_PATH) as f:
                        night = json.load(f)
                    if night.get("running") and time.time() - night.get("updated", 0) < 300:
                        out["night"] = night
                except (OSError, ValueError):
                    pass
                return self._send(200, out)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "introuvable"})

    def do_POST(self):
        if self.path != "/api/scan":
            return self._send(404, {"error": "introuvable"})
        if self.worker is None:
            return self._send(403, {"error": "site en lecture seule : le calcul se fait en local"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            lat, lon = float(data["lat"]), float(data["lon"])
            radius = max(0.5, min(scanner.MAX_RADIUS_KM, float(data.get("radius_km", 10))))
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": f"paramètres invalides : {e}"})
        total, todo = self.worker.request(lat, lon, radius)
        self._send(202, {"tiles_total": total, "tiles_todo": todo})


def main():
    ap = argparse.ArgumentParser(description="Site de recherche de côtes")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=None, help="chemin de la base (défaut : data/cotes.sqlite)")
    ap.add_argument("--readonly", action="store_true",
                    help="site en ligne : consultation seule, aucun calcul ni téléchargement")
    ap.add_argument("--strict-port", action="store_true", help="échoue si le port est pris (pas d'essai du suivant)")
    args = ap.parse_args()
    Handler.store = Store(args.db) if args.db else Store()
    Handler.worker = None if args.readonly else Worker(Handler.store)
    httpd = None
    for port in range(args.port, args.port + (1 if args.strict_port else 11)):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            print(f"Port {port} déjà utilisé, essai du suivant…")
    if httpd is None:
        raise SystemExit(f"Aucun port libre entre {args.port} et {args.port + 10}")
    st = Handler.store.stats()
    print(f"Base : {st['climbs']} côtes sur {st['tiles']} cases" + (" (lecture seule)." if args.readonly else "."))
    print(f"Ouvre http://localhost:{port} dans ton navigateur (Ctrl+C pour arrêter)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

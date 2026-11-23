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
    GET  /api/regions              -> régions et départements, avec leur nombre de côtes (menu)
    GET  /api/region/<nom>         -> un département ou une région : contour, parents, enfants
    GET  /api/climbs?region=<nom>&… -> top des côtes du département / de la région (mêmes filtres)

Pages : /region (toutes les régions), /region/<nom> (top des côtes d'une région ou d'un département,
même page que l'accueil), /sitemap.xml, /robots.txt.

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

import regions
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


def _fr(v, d=1):   # nombre à la française : 6,4
    return f"{v:.{d}f}".replace(".", ",")


def _fmt_len(m):
    return f"{_fr(m / 1000, 2)} km" if m >= 1000 else f"{m:.0f} m"


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


def region_label(kind, x):
    return f"{x['name']} ({x['code']})" if kind == "departement" else x["name"]


def region_head(kind, x, total, top, root=""):
    """<title>, description et lien canonique d'une page /region/<nom>."""
    from html import escape
    label = region_label(kind, x)
    title = f"Côtes à vélo : {label}, les plus difficiles — Climb Finder"
    desc = f"Les {total} côtes à vélo {'du département' if kind == 'departement' else 'de la région'} {label}, classées par difficulté"
    if top:
        h = top[0]
        desc += f" ; la plus dure : {h['name']} ({_fmt_len(h['length_m'])} à {_fr(h['avg_grade'])} %)"
    desc += ". Profils, pentes max, carrefours, GPX."
    t, d = escape(title), escape(desc)
    return (f'<title>{t}</title>\n<meta name="description" content="{d}">\n'
            f'<meta property="og:title" content="{t}">\n<meta property="og:description" content="{d}">\n'
            f'<meta property="og:type" content="website">\n<link rel="canonical" href="{root}region/{x["slug"]}">')


def region_seo(kind, x, reg, counts, top):
    """Contenu de la page lisible sans JavaScript (moteurs de recherche) : remplacé par la liste
    interactive au chargement."""
    from html import escape as e
    parts = [f'<div id="seo"><h2>Les côtes les plus difficiles : {e(region_label(kind, x))}</h2>']
    if kind == "departement":
        r = reg.region_by_code[x["region"]]
        parts.append(f'<p><a href="region">France</a> › <a href="region/{r["slug"]}">{e(r["name"])}</a></p>')
    else:
        parts.append('<p><a href="region">France</a></p><ul>' + "".join(
            f'<li><a href="region/{d["slug"]}">{e(d["name"])}</a> ({counts.get(d["code"], 0)})</li>'
            for d in (reg.dept_by_code[c] for c in x["depts"])) + "</ul>")
    parts.append("<ol>" + "".join(
        f'<li><a href="?c={e(c["id"])}">{e(c["name"])}</a> — {_fmt_len(c["length_m"])} à {_fr(c["avg_grade"])} %, '
        f'D+ {c["gain_m"]:.0f} m, difficulté {c["score"]:.0f}</li>' for c in top) + "</ol></div>")
    return "".join(parts)


def regions_index_seo(reg, counts):
    from html import escape as e
    out = ['<div id="seo"><h2>Côtes à vélo par région et département</h2><ul>']
    for r in reg.regions:
        n = sum(counts.get(c, 0) for c in r["depts"])
        out.append(f'<li><a href="region/{r["slug"]}">{e(r["name"])}</a> ({n})<ul>' + "".join(
            f'<li><a href="region/{d["slug"]}">{e(d["name"])}</a> ({counts.get(d["code"], 0)})</li>'
            for d in (reg.dept_by_code[c] for c in r["depts"])) + "</ul></li>")
    return "".join(out) + "</ul></div>"


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
    _counts = (None, {})   # (tampon de la base, côtes par département)

    def dept_counts(self):
        stamp = self.store._stamp()
        if Handler._counts[0] != stamp:
            Handler._counts = (stamp, self.store.dept_counts())
        return Handler._counts[1]

    def region_info(self, kind, x):
        reg, counts = regions.shared(), self.dept_counts()
        out = {"kind": kind, "slug": x["slug"], "name": x["name"], "code": x["code"], "bbox": x["bbox"],
               "outline": x["polys"]}
        if kind == "departement":
            r = reg.region_by_code[x["region"]]
            out["count"] = counts.get(x["code"], 0)
            out["parent"] = {"slug": r["slug"], "name": r["name"]}
            out["children"] = []
        else:
            out["count"] = sum(counts.get(c, 0) for c in x["depts"])
            out["parent"] = None
            out["children"] = [{"slug": d["slug"], "name": d["name"], "code": d["code"],
                                "count": counts.get(d["code"], 0)} for d in (reg.dept_by_code[c] for c in x["depts"])]
        return out

    def _page(self, extra_head=b"", seo=b"", title=None, base=None, code=200):
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            page = f.read()
        if self.worker is None:
            page = page.replace(b"<body>", b'<body class="readonly">', 1)
        if base:   # /region/<nom> : les adresses relatives (api/…, region/…) partent de la racine du site
            page = page.replace(b"<head>", b'<head>\n<base href="' + base + b'">', 1)
        if title:
            page = page.replace(b"<title>C\xc3\xb4tes autour de moi</title>", title, 1)
        page = page.replace(b"</head>", extra_head + b"</head>", 1)
        page = page.replace(b'<div id="filters">', seo + b'<div id="filters">', 1)
        return self._send(code, page, "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        line = str(args[0]) if args else ""   # args[0] peut être un HTTPStatus (erreurs)
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
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):   # sondes de disponibilité (curl -I, supervision)
        self.do_GET()

    def _root(self):
        """Adresse publique du site (sitemap) : derrière Caddy, l'hôte et le protocole d'origine."""
        proto = self.headers.get("X-Forwarded-Proto") or ("http" if self.headers.get("Host", "").startswith(("localhost", "127.")) else "https")
        return f"{proto}://{self.headers.get('Host', 'localhost')}/"

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html", "/region", "/region/") or url.path.startswith("/region/"):
                cid = qs.get("c", [""])[0]
                c = self.store.get(cid) if cid else None
                share = share_meta(c).encode() if c else None   # lien partagé : aperçu de la côte
                reg = regions.shared()
                slug = url.path[len("/region/"):].strip("/") if url.path.startswith("/region/") else ""
                if not slug and url.path.startswith("/region") and reg:
                    from html import escape
                    t = "Côtes à vélo par région et département — Climb Finder"
                    head = (f'<title>{t}</title>\n<meta name="description" content="{escape("Toutes les côtes à vélo de France, région par région et département par département, classées par difficulté.")}">'
                            f'\n<link rel="canonical" href="{self._root()}region">')
                    return self._page(seo=regions_index_seo(reg, self.dept_counts()).encode(),
                                      title=share or head.encode(), base=b"../" if url.path.endswith("/") else None)
                if slug:
                    hit = reg.resolve(slug) if reg else None
                    if not hit:
                        return self._page(base=b"../", code=404, seo='<div id="seo"><h2>Région inconnue</h2>'
                                          '<p><a href="region">Voir toutes les régions</a></p></div>'.encode())
                    kind, x, depts = hit
                    res = self.store.region_search(depts, x["bbox"], cobbles=True, limit=30)
                    seo = region_seo(kind, x, reg, self.dept_counts(), res["climbs"])
                    return self._page(base=b"../", seo=seo.encode(),
                                      title=share or region_head(kind, x, res["total"], res["climbs"], self._root()).encode())
                return self._page(title=share)

            if url.path == "/robots.txt":
                return self._send(200, b"User-agent: *\nAllow: /\nSitemap: " + self._root().encode() + b"sitemap.xml\n",
                                  "text/plain; charset=utf-8", cache=True)

            if url.path == "/sitemap.xml":
                reg = regions.shared()
                root = self._root()
                urls = [root, root + "region"] + [root + "region/" + x["slug"] for x in (reg.regions + reg.depts if reg else [])]
                body = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                        + "".join(f"<url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n")
                return self._send(200, body.encode(), "application/xml; charset=utf-8", cache=True)

            if url.path == "/api/regions":
                reg, counts = regions.shared(), self.dept_counts()
                if not reg:
                    return self._send(200, {"regions": []})
                out = []
                for r in reg.regions:
                    ds = [{"slug": d["slug"], "name": d["name"], "code": d["code"], "count": counts.get(d["code"], 0)}
                          for d in (reg.dept_by_code[c] for c in r["depts"])]
                    out.append({"slug": r["slug"], "name": r["name"], "code": r["code"],
                                "count": sum(d["count"] for d in ds), "depts": sorted(ds, key=lambda d: regions.slugify(d["name"]))})
                return self._send(200, {"regions": out}, cache=True)

            if url.path.startswith("/api/region/"):
                reg = regions.shared()
                hit = reg.resolve(url.path.rsplit("/", 1)[-1]) if reg else None
                if not hit:
                    return self._send(404, {"error": "région inconnue"})
                return self._send(200, self.region_info(hit[0], hit[1]), cache=True)

            if url.path == "/api/climbs" and qs.get("region"):
                reg = regions.shared()
                hit = reg.resolve(qs["region"][0]) if reg else None
                if not hit:
                    return self._send(404, {"error": "région inconnue"})
                with SEARCH_LOCK:
                    res = self.store.region_search(
                        hit[2], hit[1]["bbox"],
                        cobbles=qs.get("cobbles", ["0"])[0] == "1", minor=qs.get("minor", ["1"])[0] == "1",
                        len_min=_num(qs, "len_min"), len_max=_num(qs, "len_max"), avg_min=_num(qs, "avg_min"),
                        max300_min=_num(qs, "max300_min"), gain_min=_num(qs, "gain_min"),
                        junc_max=_num(qs, "junc_max"), q=qs.get("q", [""])[0].strip() or None,
                        sort=qs.get("sort", ["score"])[0], desc=qs.get("dir", ["desc"])[0] != "asc",
                        limit=min(1000, int(_num(qs, "limit") or 300)), offset=int(_num(qs, "offset") or 0))
                res["coverage"] = {"done": 1, "total": 1}
                return self._send(200, res, cache=True)

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

"""
Régions et départements (métropole) : contours, noms d'URL, rattachement des côtes.

Contours : Admin Express COG CARTO petite échelle de l'IGN (licence ouverte Etalab 2.0), au format
de `regions.json` (versionné, ~1 Mo) :
    {"departements": [{"code", "name", "slug", "region", "bbox", "polys"}],
     "regions":      [{"code", "name", "slug", "depts", "bbox", "polys"}]}
`polys` : liste de polygones, chacun liste d'anneaux [[lat, lon], …] (le premier = l'extérieur,
les suivants = trous) ; `bbox` = [sud, ouest, nord, est].

    python3 regions.py --build     # retélécharge les contours (une fois par an suffit)
    python3 regions.py --assign    # rattache les côtes de la base à leur département (colonne dept)

Une côte est rattachée au département qui contient son pied. Les nouvelles cases le sont à
l'enregistrement (store.save_tile).
"""

import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REGIONS_PATH = os.path.join(HERE, "regions.json")
WFS = ("https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
       "&TYPENAMES=ADMINEXPRESS-COG-CARTO-PE.LATEST:{layer}&OUTPUTFORMAT=application/json&SRSNAME=EPSG:4326")
BAND = 0.02         # bandes de latitude de l'index des côtés (degrés)
NEAR_DEG = 0.006    # pied hors de tout contour (côte simplifiée) : département le plus proche à ~600 m ; au-delà, étranger


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# --- construction de regions.json --------------------------------------------------------------
def _simplify(ring, tol):
    """Douglas-Peucker (degrés), anneau fermé conservé."""
    if len(ring) < 5:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (y1, x1), (y2, x2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        n2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for k in range(a + 1, b):
            y, x = ring[k]
            if n2 == 0:
                d = (x - x1) ** 2 + (y - y1) ** 2
            else:
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / n2))
                d = (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2
            if d > best:
                best, idx = d, k
        if idx > 0 and best > tol * tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(ring, keep) if k]


def _polys(geom, tol=0.0008):
    parts = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in parts:
        rings = []
        for ring in poly:
            r = _simplify([[round(la, 4), round(lo, 4)] for lo, la in ring], tol)
            if len(r) >= 4:
                rings.append(r)
        if rings:
            out.append(rings)
    return out


def _bbox(polys):
    pts = [p for poly in polys for p in poly[0]]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


def build(path=REGIONS_PATH):
    from urllib.request import Request, urlopen

    def get(layer):
        req = Request(WFS.format(layer=layer), headers={"User-Agent": "climb-finder"})
        with urlopen(req, timeout=120) as r:
            return json.load(r)["features"]

    depts, regs = [], []
    for f in get("departement"):
        p = f["properties"]
        if p["code_insee"].startswith("97"):   # outre-mer : pas dans la base
            continue
        polys = _polys(f["geometry"])
        depts.append({"code": p["code_insee"], "name": p["nom_officiel"], "slug": slugify(p["nom_officiel"]),
                      "region": p["code_insee_de_la_region"], "bbox": _bbox(polys), "polys": polys})
    codes = {d["region"] for d in depts}
    for f in get("region"):
        p = f["properties"]
        if p["code_insee"] not in codes:
            continue
        polys = _polys(f["geometry"], tol=0.002)
        regs.append({"code": p["code_insee"], "name": p["nom_officiel"], "slug": slugify(p["nom_officiel"]),
                     "depts": sorted(d["code"] for d in depts if d["region"] == p["code_insee"]),
                     "bbox": _bbox(polys), "polys": polys})
    depts.sort(key=lambda d: d["code"])
    regs.sort(key=lambda r: slugify(r["name"]))
    slugs = [x["slug"] for x in depts + regs]
    dup = {s for s in slugs if slugs.count(s) > 1}
    assert not dup, f"noms d'URL en double : {dup}"
    with open(path, "w") as f:
        json.dump({"source": "IGN Admin Express COG CARTO PE (licence ouverte Etalab 2.0)",
                   "departements": depts, "regions": regs}, f, ensure_ascii=False, separators=(",", ":"))
    return len(depts), len(regs)


# --- lecture et rattachement --------------------------------------------------------------------
class Regions:
    def __init__(self, path=REGIONS_PATH):
        with open(path) as f:
            data = json.load(f)
        self.depts = data["departements"]
        self.regions = data["regions"]
        self.dept_by_code = {d["code"]: d for d in self.depts}
        self.region_by_code = {r["code"]: r for r in self.regions}
        self.by_slug = {d["slug"]: ("departement", d) for d in self.depts}
        self.by_slug.update({r["slug"]: ("region", r) for r in self.regions})
        self._index = {}   # code -> {bande: [(la1, lo1, la2, lo2)]}
        self._last = None

    def _segments(self, d):
        idx = self._index.get(d["code"])
        if idx is None:
            idx = {}
            for poly in d["polys"]:
                for ring in poly:
                    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:] + ring[:1]):
                        for b in range(int((min(la1, la2)) // BAND), int(max(la1, la2) // BAND) + 1):
                            idx.setdefault(b, []).append((la1, lo1, la2, lo2))
            self._index[d["code"]] = idx
        return idx

    def contains(self, d, lat, lon):
        s, w, n, e = d["bbox"]
        if not (s <= lat <= n and w <= lon <= e):
            return False
        inside = False
        for la1, lo1, la2, lo2 in self._segments(d).get(int(lat // BAND), ()):
            if (la1 > lat) != (la2 > lat) and lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1) > lon:
                inside = not inside
        return inside

    def _near(self, d, lat, lon):
        """Distance² (degrés, approchée) du point au contour, ou None si loin."""
        idx = self._segments(d)
        best = None
        b0 = int(lat // BAND)
        for b in range(b0 - 2, b0 + 3):
            for la1, lo1, la2, lo2 in idx.get(b, ()):
                dx, dy = lo2 - lo1, la2 - la1
                n2 = dx * dx + dy * dy
                t = 0 if n2 == 0 else max(0.0, min(1.0, ((lon - lo1) * dx + (lat - la1) * dy) / n2))
                dd = (lon - lo1 - t * dx) ** 2 * 0.5 + (lat - la1 - t * dy) ** 2   # cos(46°)² ≈ 0,5
                if best is None or dd < best:
                    best = dd
        return best

    def dept_of(self, lat, lon):
        """Code du département qui contient le point (ou le plus proche à moins de ~600 m)."""
        if self._last and self.contains(self._last, lat, lon):
            return self._last["code"]
        for d in self.depts:
            if self.contains(d, lat, lon):
                self._last = d
                return d["code"]
        best = None
        for d in self.depts:
            s, w, n, e = d["bbox"]
            if s - NEAR_DEG <= lat <= n + NEAR_DEG and w - NEAR_DEG <= lon <= e + NEAR_DEG:
                dd = self._near(d, lat, lon)
                if dd is not None and dd <= NEAR_DEG ** 2 and (best is None or dd < best[0]):
                    best = (dd, d["code"])
        return best[1] if best else None

    def resolve(self, slug):
        """(type, entrée, codes des départements) pour un nom d'URL, ou None."""
        hit = self.by_slug.get(slug)
        if not hit:
            return None
        kind, x = hit
        return kind, x, (x["depts"] if kind == "region" else [x["code"]])


_shared = None


def shared():
    """Instance commune (chargée au premier usage), ou None si regions.json manque."""
    global _shared
    if _shared is None:
        _shared = Regions() if os.path.exists(REGIONS_PATH) else False
    return _shared or None


def assign(db=None, progress=print):
    """Remplit climbs.dept pour toutes les côtes (une passe sur la base)."""
    import sqlite3
    from store import Store
    st = Store(db) if db else Store()
    reg = shared()
    con = sqlite3.connect(st.path, timeout=60)
    rows = con.execute("SELECT id, lat, lon FROM climbs ORDER BY tile").fetchall()
    out, done, missing = [], 0, 0
    for cid, la, lo in rows:
        code = reg.dept_of(la, lo)
        missing += code is None
        out.append((code, cid))
        if len(out) >= 50000:
            con.executemany("UPDATE climbs SET dept = ? WHERE id = ?", out)
            con.commit()
            done += len(out)
            out = []
            progress(f"{done}/{len(rows)} côtes rattachées")
    con.executemany("UPDATE climbs SET dept = ? WHERE id = ?", out)
    con.commit()
    con.close()
    progress(f"{len(rows)} côtes rattachées, {missing} hors de France")
    return len(rows), missing


if __name__ == "__main__":
    if "--build" in sys.argv:
        print("%d départements, %d régions" % build())
    if "--assign" in sys.argv:
        assign()

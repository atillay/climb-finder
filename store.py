"""
Base SQLite des côtes (data/cotes.sqlite).

- table `climbs` : une ligne par côte (route classique ou variante), rattachée à la case qui
  contient son pied ; `family` regroupe les variantes qui arrivent au même sommet ;
- table `tiles`  : cases déjà calculées (avec la version de l'algorithme).

La recherche par rayon lit la base : elle est instantanée, même sur des dizaines de milliers de côtes.
"""

import json
import math
import os
import sqlite3
import time

import scanner

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "cotes.sqlite")

NUM_COLS = ["length_m", "gain_m", "ascent_m", "avg_grade", "max_grade_100", "max_grade_300",
            "score", "alt_start", "alt_end", "junctions", "main_junctions", "signals"]
SORTABLE = {"rank", "name", "length_m", "gain_m", "avg_grade", "max_grade_100", "max_grade_300",
            "score", "distance_km", "surface", "junctions"}
# colonnes ajoutées avec les variantes (bases plus anciennes : ajoutées au démarrage)
NEW_COLS = {"family": "TEXT", "variant": "TEXT", "streets": "TEXT", "junctions": "INTEGER",
            "main_junctions": "INTEGER", "signals": "INTEGER"}
INSERT_COLS = ["id", "tile", "lat", "lon", "end_lat", "end_lon", "name", "highway", "minor", "surface",
               "length_m", "gain_m", "ascent_m", "avg_grade", "max_grade_100", "max_grade_300", "score",
               "alt_start", "alt_end", "geom_light", "geometry", "profile",
               "family", "variant", "streets", "junctions", "main_junctions", "signals"]
LIGHT_GEOM_POINTS = 40
MAP_LIMIT = 1500           # côtes au plus sur la carte (vue très large : les plus difficiles)
MAP_GEOM_POINTS = 12       # tracé simplifié pour la carte
SEARCH_CACHE_SIZE = 3      # recherches classées gardées en mémoire (pages « Afficher plus »)

SCHEMA = """
CREATE TABLE IF NOT EXISTS climbs (
    id TEXT PRIMARY KEY, tile TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, end_lat REAL, end_lon REAL,
    name TEXT, highway TEXT, minor INTEGER, surface TEXT,
    length_m REAL, gain_m REAL, ascent_m REAL, avg_grade REAL,
    max_grade_100 REAL, max_grade_300 REAL, score REAL, alt_start REAL, alt_end REAL,
    geom_light TEXT, geometry TEXT, profile TEXT
);
CREATE INDEX IF NOT EXISTS climbs_lat ON climbs(lat);
CREATE INDEX IF NOT EXISTS climbs_tile ON climbs(tile);
CREATE TABLE IF NOT EXISTS tiles (
    tile TEXT PRIMARY KEY, i INTEGER, j INTEGER, n_climbs INTEGER, road_km REAL,
    algo INTEGER, updated REAL
);
"""


# --- Stockage compact des tracés et profils ---------------------------------------------------
# Format « polyline » (celui de Google) : différences entières entre points successifs, en base 64
# lisible. ~5 caractères par point au lieu de ~20 en JSON (base France : 4,1 -> ~1,5 Go). Préfixe
# « ! » (hors de l'alphabet polyline) ; les lignes plus anciennes, en JSON, restent lisibles.
GEOM_SCALES = (1e5, 1e5)    # lat, lon au 1/100 000 de degré (~1 m)
PROFILE_SCALES = (1, 10)    # distance au mètre, altitude au décimètre


def encode_pairs(pairs, scales):
    out, prev = ["!"], [0, 0]
    for pair in pairs:
        for k in (0, 1):
            v = round(pair[k] * scales[k])
            d, prev[k] = v - prev[k], v
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1f)) + 63))
                d >>= 5
            out.append(chr(d + 63))
    return "".join(out)


def decode_pairs(text, scales):
    if not text:
        return []
    if text[0] != "!":
        return json.loads(text)          # ancien format JSON
    vals, i, n, cur = [], 1, len(text), [0, 0]
    k = 0
    while i < n:
        shift = result = 0
        while True:
            b = ord(text[i]) - 63
            i += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        cur[k] += ~(result >> 1) if result & 1 else result >> 1
        vals.append(cur[k])
        k ^= 1
    digits = [5 if sc == 1e5 else (1 if sc == 10 else 0) for sc in scales]
    out = []
    for j in range(0, len(vals) - 1, 2):
        a, b = vals[j] / scales[0], vals[j + 1] / scales[1]
        out.append([round(a, digits[0]) if digits[0] else int(a), round(b, digits[1]) if digits[1] else int(b)])
    return out


def light_geometry(text):
    """Tracé allégé (carte) à partir du tracé stocké."""
    return scanner._downsample(decode_pairs(text, GEOM_SCALES), LIGHT_GEOM_POINTS)


def _filters(s, w, n, e, cobbles=False, minor=True, len_min=None, len_max=None, avg_min=None,
             max300_min=None, gain_min=None, junc_max=None, q=None):
    """Conditions SQL communes au tableau (search) et à la carte (map_climbs)."""
    where = ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
    args = [s, n, w, e]
    if not cobbles:
        where.append("surface != 'pavés'")
    if not minor:
        where.append("minor = 0")
    for col, op, v in (("length_m", ">=", len_min), ("length_m", "<=", len_max),
                       ("avg_grade", ">=", avg_min), ("max_grade_300", ">=", max300_min),
                       ("gain_m", ">=", gain_min), ("junctions", "<=", junc_max)):
        if v is not None:
            where.append(f"{col} {op} ?")
            args.append(v)
    if q:
        where.append("(name LIKE ? OR streets LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    return where, args


def tile_key(t):
    return f"{t[0]}_{t[1]}"


class Store:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._cache = {}   # dernières recherches classées (pagination) : clé -> (tampon base, résultat)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(climbs)")}
            for col, typ in NEW_COLS.items():
                if col not in have:
                    c.execute(f"ALTER TABLE climbs ADD COLUMN {col} {typ}")
            c.execute("CREATE INDEX IF NOT EXISTS climbs_family ON climbs(family)")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=60)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # --- écriture -----------------------------------------------------------
    def save_tile(self, tile, climbs, stats):
        key = tile_key(tile)
        rows = []
        for c in climbs:
            rows.append((
                c["id"], key, c["start"][0], c["start"][1], c["end"][0], c["end"][1],
                c["name"], c["highway"], int(bool(c.get("minor"))), c["surface"],
                *[c.get(k) for k in NUM_COLS[:9]],
                "",   # geom_light : recalculé à la volée depuis geometry (stockage compact)
                encode_pairs(c["geometry"], GEOM_SCALES),
                encode_pairs(c["profile"], PROFILE_SCALES),
                c.get("family") or c["id"], c.get("variant") or "principale", c.get("streets") or c["name"],
                c.get("junctions"), c.get("main_junctions"), c.get("signals"),
            ))
        with self._conn() as con:
            con.execute("DELETE FROM climbs WHERE tile = ?", (key,))
            con.executemany(f"INSERT OR REPLACE INTO climbs ({','.join(INSERT_COLS)}) "
                            f"VALUES ({','.join('?' * len(INSERT_COLS))})", rows)
            con.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?,?)",
                        (key, tile[0], tile[1], len(climbs), stats.get("road_km"),
                         scanner.ALGO_VERSION, time.time()))

    # --- lecture ------------------------------------------------------------
    def compact(self, batch=20000, progress=print):
        """Convertit les tracés et profils encore en JSON au format compact, puis VACUUM."""
        done = 0
        with self._conn() as con:
            while True:
                rows = con.execute("SELECT rowid, geometry, profile FROM climbs WHERE substr(geometry, 1, 1) = '[' "
                                   "LIMIT ?", (batch,)).fetchall()
                if not rows:
                    break
                con.executemany("UPDATE climbs SET geometry = ?, profile = ?, geom_light = '' WHERE rowid = ?", [
                    (encode_pairs(json.loads(g), GEOM_SCALES), encode_pairs(json.loads(p), PROFILE_SCALES), rid)
                    for rid, g, p in rows])
                con.commit()
                done += len(rows)
                progress(f"{done} côtes converties")
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.close()
        return done

    def _stamp(self):
        """Change dès qu'une case est (re)calculée : invalide le cache des recherches."""
        with self._conn() as con:
            return con.execute("SELECT COUNT(*), MAX(updated) FROM tiles").fetchone()

    def done_tiles(self, tiles=None):
        """Cases déjà calculées avec la version actuelle de l'algorithme."""
        with self._conn() as con:
            rows = con.execute("SELECT i, j FROM tiles WHERE algo = ?", (scanner.ALGO_VERSION,)).fetchall()
        done = {(i, j) for i, j in rows}
        return done if tiles is None else {t for t in tiles if t in done}

    def coverage(self, s, w, n, e):
        i0, j0 = scanner.osm_tile_of(s, w)
        i1, j1 = scanner.osm_tile_of(n, e)
        with self._conn() as con:
            rows = con.execute(
                "SELECT i, j FROM tiles WHERE algo = ? AND i BETWEEN ? AND ? AND j BETWEEN ? AND ?",
                (scanner.ALGO_VERSION, i0, i1, j0, j1)).fetchall()
        return [[i, j] for i, j in rows]

    def stats(self):
        with self._conn() as con:
            n_tiles = con.execute("SELECT COUNT(*) FROM tiles WHERE algo = ?", (scanner.ALGO_VERSION,)).fetchone()[0]
            n_climbs = con.execute("SELECT COUNT(*) FROM climbs").fetchone()[0]
        return {"tiles": n_tiles, "climbs": n_climbs}

    def search(self, lat, lon, radius_km, cobbles=False, minor=True, len_min=None, len_max=None,
               avg_min=None, max300_min=None, gain_min=None, junc_max=None, q=None, sort="score",
               desc=True, limit=2000, offset=0, box=None):
        """Côtes dont le pied est dans le cercle, regroupées par sommet (famille) : une entrée par
        famille, la variante principale en tête si elle correspond aux filtres (sinon la meilleure
        qui correspond), les autres variantes qui correspondent dans `variants`, de la plus courte à
        la plus longue (sans tracé). Les filtres portent sur les variantes (une famille apparaît dès
        qu'une variante correspond) ; le tri porte sur la tête affichée. Pagination : `limit` familles
        à partir de `offset` ; le classement d'une recherche est gardé en cache (pages suivantes).
        `box` = (s, w, n, e) : les côtes de cette emprise (zone affichée sur la carte) au lieu du
        cercle ; lat/lon servent alors seulement à la distance."""
        ky = scanner.EARTH_R * math.pi / 180
        if box:
            s_, w_, n_, e_ = box
            radius_m = float("inf")
        else:
            radius_m = radius_km * 1000
            dlat = radius_m / ky
            dlon = radius_m / (ky * math.cos(math.radians(lat)))
            s_, w_, n_, e_ = lat - dlat, lon - dlon, lat + dlat, lon + dlon
        where, args = _filters(s_, w_, n_, e_, cobbles, minor, len_min,
                               len_max, avg_min, max300_min, gain_min, junc_max, q)
        if sort not in SORTABLE or sort == "rank":
            sort, desc = "score", True if sort != "rank" else not desc
        key = (lat, lon, radius_km, tuple(box) if box else None, tuple(where), tuple(args), sort, desc)
        stamp = self._stamp()
        hit = self._cache.get(key)
        if hit and hit[0] == stamp:
            groups, rank, total, n_found = hit[1]
        else:
            # 1) tri et regroupement sur quelques colonnes seulement (tuples) : le serveur en ligne a
            #    peu de mémoire et un rayon de 50 km peut contenir des dizaines de milliers de lignes
            sort_col = {"distance_km": "NULL"}.get(sort, sort)
            # on part des cases du cercle (index sur la case) : avec toute la France en base, une
            # bande de latitude couvrirait tout le pays d'est en ouest
            if box:
                i0, j0 = scanner.osm_tile_of(s_, w_)
                i1, j1 = scanner.osm_tile_of(n_, e_)
                tiles = [f"{i}_{j}" for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
            else:
                tiles = [tile_key(t) for t in scanner.tiles_for_scan(lat, lon, radius_m)]
            sql = (f"SELECT id, lat, lon, family, variant, score, length_m, {sort_col} "
                   f"FROM climbs INDEXED BY climbs_tile WHERE tile IN ({','.join('?' * len(tiles))}) "
                   f"AND {' AND '.join(where)}")
            with self._conn() as con:
                rows = con.execute(sql, tiles + args).fetchall()
            found = []   # (id, distance m, famille, variante, score, longueur, valeur de tri)
            # distance en projection plane locale : quelques mètres d'écart sur 50 km, 3 fois plus rapide
            kx = ky * math.cos(math.radians(lat))
            hypot = math.hypot
            for cid, la, lo, fam, var, score, length, sv in rows:
                d = hypot((lo - lon) * kx, (la - lat) * ky)
                if d <= radius_m:
                    if sort == "distance_km":
                        sv = d
                    elif isinstance(sv, str):
                        sv = sv.lower()
                    found.append((cid, d, fam or cid, var or "principale", score, length, sv))
            del rows
            present = [c for c in found if c[6] is not None]
            present.sort(key=lambda c: c[6], reverse=desc)
            families = {}
            for c in present + [c for c in found if c[6] is None]:
                families.setdefault(c[2], []).append(c)
            groups = []   # (tête, [variantes])
            for members in families.values():
                head = next((c for c in members if c[3] == "principale"), None) or max(members, key=lambda c: c[4])
                groups.append((head, sorted((c for c in members if c[0] != head[0]), key=lambda c: c[5])))
            del families, present
            # familles classées selon la ligne affichée (leur tête), pas selon leur meilleure variante :
            # sinon le tableau paraît mal trié (une côte à 43 devant une à 718 grâce à une variante)
            with_v = [g for g in groups if g[0][6] is not None]
            with_v.sort(key=lambda g: g[0][6], reverse=desc)
            groups = with_v + [g for g in groups if g[0][6] is None]
            # rang = classement par difficulté (de la tête de chaque famille)
            rank = {g[0][0]: r for r, g in enumerate(sorted(groups, key=lambda g: -g[0][4]), 1)}
            total, n_found = len(groups), len(found)
            self._cache[key] = (stamp, (groups, rank, total, n_found))
            while len(self._cache) > SEARCH_CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))
        groups = groups[offset:offset + limit]

        # 2) détail des seules côtes renvoyées ; tracé des têtes seulement (la carte ne dessine
        #    qu'elles, et la fiche d'une variante, api/climbs/<id>, apporte son tracé complet)
        dist = {c[0]: c[1] for g in groups for c in [g[0]] + g[1]}
        heads = {g[0][0] for g in groups}
        cols = f"id, lat, lon, name, highway, surface, {', '.join(NUM_COLS)}, family, variant, streets"
        full, ids = {}, list(dist)
        k = 6 + len(NUM_COLS)
        with self._conn() as con:
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                for r in con.execute(f"SELECT {cols} FROM climbs WHERE id IN ({','.join('?' * len(part))})", part):
                    c = {"id": r[0], "start": [r[1], r[2]], "name": r[3], "highway": r[4], "surface": r[5],
                         "distance_km": round(dist[r[0]] / 1000, 1),
                         "family": r[k] or r[0], "variant": r[k + 1] or "principale", "streets": r[k + 2] or r[3]}
                    c.update(zip(NUM_COLS, r[6:k]))
                    full[r[0]] = c
            hl = list(heads)
            for i in range(0, len(hl), 500):
                part = hl[i:i + 500]
                for cid, g in con.execute(f"SELECT id, geometry FROM climbs WHERE id IN ({','.join('?' * len(part))})", part):
                    full[cid]["geometry"] = light_geometry(g)
        out = []
        for head, variants in groups:
            c = full[head[0]]
            c["rank"] = rank[head[0]]
            c["variants"] = [full[v[0]] for v in variants]
            out.append(c)
        return {"climbs": out, "total": total, "total_climbs": n_found, "offset": offset}

    def map_climbs(self, s, w, n, e, limit=MAP_LIMIT, **filters):
        """Côtes principales dont le pied est dans l'emprise (s, w, n, e), pour la carte, quels que
        soient le tri et la pagination du tableau : tracé simplifié (MAP_GEOM_POINTS points). Au-delà
        de `limit` (vue très large), les plus difficiles."""
        where, args = _filters(s, w, n, e, **filters)
        where.append("(variant = 'principale' OR variant IS NULL)")
        i0, j0 = scanner.osm_tile_of(s, w)
        i1, j1 = scanner.osm_tile_of(n, e)
        index = ""
        if (i1 - i0 + 1) * (j1 - j0 + 1) <= 3000:   # emprise raisonnable : par l'index des cases
            tiles = [f"{i}_{j}" for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]
            where.insert(0, f"tile IN ({','.join('?' * len(tiles))})")
            args = tiles + args
            index = "INDEXED BY climbs_tile "
        sql = (f"SELECT id, name, length_m, avg_grade, geometry FROM climbs {index}WHERE {' AND '.join(where)} "
               f"ORDER BY score DESC LIMIT ?")
        with self._conn() as con:
            rows = con.execute(sql, args + [limit + 1]).fetchall()
        out = [{"id": r[0], "name": r[1], "length_m": r[2], "avg_grade": r[3],
                "geometry": scanner._downsample(decode_pairs(r[4], GEOM_SCALES), MAP_GEOM_POINTS)}
               for r in rows[:limit]]
        return {"climbs": out, "truncated": len(rows) > limit}

    def family(self, family):
        """Toutes les variantes d'une famille (tracés allégés), de la plus courte à la plus longue."""
        with self._conn() as con:
            rows = con.execute("SELECT id, name, streets, variant, length_m, gain_m, avg_grade, score, "
                               "junctions, geometry FROM climbs WHERE family = ? OR id = ? "
                               "ORDER BY length_m", (family, family)).fetchall()
        keys = ["id", "name", "streets", "variant", "length_m", "gain_m", "avg_grade", "score", "junctions"]
        return [dict(zip(keys, r[:9]), geometry=light_geometry(r[9])) for r in rows]

    def get(self, climb_id):
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM climbs WHERE id = ?", (climb_id,)).fetchone()
        if not r:
            return None
        c = dict(r)
        c["geometry"] = decode_pairs(c["geometry"], GEOM_SCALES)
        c["profile"] = decode_pairs(c["profile"], PROFILE_SCALES)
        c["start"] = [c.pop("lat"), c.pop("lon")]
        c["end"] = [c.pop("end_lat"), c.pop("end_lon")]
        c.pop("geom_light")
        c["family"] = c.get("family") or c["id"]
        c["family_members"] = self.family(c["family"])
        return c


if __name__ == "__main__":
    import sys
    if "--compact" in sys.argv:   # python3 store.py --compact : convertit une base existante
        Store().compact()

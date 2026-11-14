"""Tests hors-ligne : Overpass et l'API IGN sont remplacés par des données simulées.

    python3 test_scanner.py
"""
import math
import unittest

import scanner

LAT0, LON0 = 48.0, 2.0
KY = scanner.EARTH_R * math.pi / 180
KX = KY * math.cos(math.radians(LAT0))


def ll(x, y):
    """mètres (x vers l'est, y vers le nord) -> (lat, lon)"""
    return LAT0 + y / KY, LON0 + x / KX


class FakeElevation:
    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def lookup(self, points, progress=None):
        self.calls += 1
        return [self.fn((lo - LON0) * KX, (la - LAT0) * KY) for la, lo in points]


_nid = [1000]


def way(points_xy, tags, wid, node_ids=None):
    if node_ids is None:
        node_ids = []
        for _ in points_xy:
            _nid[0] += 1
            node_ids.append(_nid[0])
    geom = [dict(zip(("lat", "lon"), ll(x, y))) for x, y in points_xy]
    return {"type": "way", "id": wid, "nodes": node_ids, "geometry": geom, "tags": tags}


def line(x0, y0, x1, y1, step=25):
    n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
    return [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)]


def profile_east(x, y):
    """Le long de y=0 vers l'est : plat, mur 300 m à 20 %, plat, 1 km à 6 %, plat."""
    if x < 500:
        return 100
    if x < 800:
        return 100 + (x - 500) * 0.20
    if x < 1500:
        return 160
    if x < 2500:
        return 160 + (x - 1500) * 0.06
    return 220


def run(elements, fn, radius_km=10, cobbles=False, skip_minor=False):
    climbs, stats = scanner.scan_elements(elements, FakeElevation(fn), (LAT0, LON0), radius_km * 1000,
                                          include_cobbles=cobbles, skip_minor=skip_minor)
    return {"climbs": climbs, "stats": stats}


def save_by_tile(st, climbs, stats=None):
    """Enregistre chaque côte dans la case de son pied, comme process_tile (la recherche part des
    cases du cercle). Renvoie les cases utilisées."""
    groups = {}
    for c in climbs:
        groups.setdefault(scanner.osm_tile_of(*c["start"]), []).append(c)
    for t, cs in groups.items():
        st.save_tile(t, cs, stats or {})
    return set(groups)


class DetectionTests(unittest.TestCase):
    def test_wall_and_climb_on_single_road(self):
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary", "name": "Route du Test"}, 1)]
        res = run(els, profile_east)
        climbs = res["climbs"]
        self.assertEqual(len(climbs), 2, climbs)
        wall = min(climbs, key=lambda c: c["length_m"])
        hill = max(climbs, key=lambda c: c["length_m"])
        self.assertAlmostEqual(wall["length_m"], 300, delta=40)
        self.assertAlmostEqual(wall["avg_grade"], 20, delta=1.5)
        self.assertAlmostEqual(wall["max_grade_300"], 20, delta=1.0)
        self.assertAlmostEqual(wall["score"], 120, delta=15)
        self.assertAlmostEqual(hill["length_m"], 1000, delta=60)
        self.assertAlmostEqual(hill["avg_grade"], 6, delta=0.5)
        self.assertAlmostEqual(hill["score"], 36, delta=5)
        self.assertEqual(wall["name"], "Route du Test")
        self.assertEqual(wall["surface"], "supposé")
        # le mur doit être classé premier (score le plus élevé)
        self.assertEqual(climbs[0]["length_m"], wall["length_m"])

    def test_reverse_direction_and_oneway(self):
        # Même route, profil inversé (montée vers l'ouest)
        fn = lambda x, y: profile_east(3000 - x, y)
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary"}, 1)]
        self.assertEqual(len(run(els, fn)["climbs"]), 2)
        # sens unique vers l'est : on ne peut pas monter vers l'ouest
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary", "oneway": "yes"}, 1)]
        self.assertEqual(len(run(els, fn)["climbs"]), 0)
        # ...sauf si les vélos sont autorisés à contresens
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary", "oneway": "yes", "oneway:bicycle": "no"}, 1)]
        self.assertEqual(len(run(els, fn)["climbs"]), 2)

    def test_unpaved_excluded_and_cobbles_option(self):
        for tags, expected in [
            ({"highway": "track"}, 0),
            ({"highway": "track", "surface": "gravel"}, 0),
            ({"highway": "path", "surface": "asphalt"}, 0),
            ({"highway": "track", "surface": "asphalt"}, 2),
            ({"highway": "residential", "surface": "compacted"}, 0),
            ({"highway": "residential", "access": "private"}, 0),
            ({"highway": "service", "service": "driveway"}, 0),
        ]:
            res = run([way(line(0, 0, 3000, 0), tags, 1)], profile_east)
            self.assertEqual(len(res["climbs"]), expected, tags)
        tags = {"highway": "residential", "surface": "sett"}
        self.assertEqual(len(run([way(line(0, 0, 3000, 0), tags, 1)], profile_east)["climbs"]), 0)
        res = run([way(line(0, 0, 3000, 0), tags, 1)], profile_east, cobbles=True)
        self.assertEqual(len(res["climbs"]), 2)
        self.assertEqual(res["climbs"][0]["surface"], "pavés")

    def test_bridge_over_valley_is_not_a_climb(self):
        # Route plate qui franchit une vallée de 40 m sur un viaduc de 400 m
        def fn(x, y):
            return 100 - 40 * max(0.0, 1 - abs(x - 1500) / 200)
        els = [
            way(line(0, 0, 1300, 0), {"highway": "secondary"}, 1, None),
        ]
        last = els[0]["nodes"][-1]
        b = way(line(1300, 0, 1700, 0), {"highway": "secondary", "bridge": "yes"}, 2)
        b["nodes"][0] = last
        els.append(b)
        c = way(line(1700, 0, 3000, 0), {"highway": "secondary"}, 3)
        c["nodes"][0] = b["nodes"][-1]
        els.append(c)
        self.assertEqual(run(els, fn)["climbs"], [])
        # sans le tag pont, on détecte (à tort) la remontée de vallée -> le tag est bien utile
        for e in els:
            e["tags"].pop("bridge", None)
        self.assertGreater(len(run(els, fn)["climbs"]), 0)

    def test_tunnel_at_end_of_road_is_not_a_climb(self):
        # Cas réel (Tunnel du Reveston, Nice) : la route commence dans un tunnel sous une colline.
        # Sans altitude connue à l'entrée, on ne peut pas interpoler : le MNT donne le dessus de la
        # colline (+120 m), qui ne doit pas devenir une côte à 60 %.
        def fn(x, y):
            return 100 + 120 * max(0.0, 1 - abs(x - 150) / 150) if x < 300 else 100
        t = way(line(0, 0, 300, 0), {"highway": "secondary", "tunnel": "yes", "name": "Tunnel"}, 1)
        r = way(line(300, 0, 1500, 0), {"highway": "secondary", "name": "Route"}, 2)
        r["nodes"][0] = t["nodes"][-1]
        self.assertEqual([(c["name"], c["avg_grade"]) for c in run([t, r], fn)["climbs"]], [])
        # même chose pour un pont en bout de route (fond de vallée sous le pont)
        b = dict(t, tags={"highway": "secondary", "bridge": "yes", "name": "Pont"})
        self.assertEqual(run([b, r], lambda x, y: 100 - fn(x, y) + 100)["climbs"], [])

    def test_implausible_grades_are_dem_artefacts(self):
        # Route en corniche (gorges du Verdon) : le MNT mélange le haut et le bas de la paroi. Une
        # route classée à plus de 30 % sur 100 m, ou n'importe quelle route à plus de 50 % sur 50 m,
        # c'est une erreur d'altitude : la côte est coupée là, pas comptée comme un mur.
        def fn(x, y):   # 1 km à 6 %, avec un faux saut de 35 m sur 100 m au milieu
            return 100 + 0.06 * x + (35 if x >= 500 else 35 * max(0.0, (x - 400) / 100))
        flat = lambda x, y: 0.0
        d952 = way(line(0, 0, 1000, 0), {"highway": "secondary", "ref": "D 952"}, 1)
        big = [c for c in run([d952], fn)["climbs"] if c["avg_grade"] > 9 or c["max_grade_100"] > 30]
        self.assertEqual([(c["name"], c["avg_grade"], c["max_grade_100"]) for c in big], [])
        # même profil sur une petite rue : 35 % sur 100 m reste possible (vrai mur), on garde
        rue = way(line(0, 0, 1000, 0), {"highway": "residential", "name": "Rue du Mur"}, 2)
        self.assertTrue([c for c in run([rue], fn)["climbs"] if c["max_grade_100"] > 30])
        # mais 60 % sur 50 m n'existe pas, même en ruelle
        def fn2(x, y):
            return 100 + 0.06 * x + (30 if x >= 500 else 30 * max(0.0, (x - 450) / 50))
        rue2 = way(line(0, 0, 1000, 0), {"highway": "residential", "name": "Rue du Mur"}, 3)
        steep = [c for c in run([rue2], fn2)["climbs"] if c["max_grade_100"] > 30]
        self.assertEqual([(c["name"], c["max_grade_100"]) for c in steep], [])

    def test_climb_through_intersection_is_one_climb(self):
        # Une côte de 1,2 km coupée par un carrefour à mi-pente (rue transversale)
        fn = lambda x, y: 100 + max(0, min(1200, x - 300)) * 0.08
        a = way(line(0, 0, 900, 0), {"highway": "unclassified", "name": "Côte A"}, 1)
        b = way(line(900, 0, 2000, 0), {"highway": "unclassified", "name": "Côte A"}, 2)
        b["nodes"][0] = a["nodes"][-1]
        cross = way(line(900, -400, 900, 400), {"highway": "residential", "name": "Rue X"}, 3)
        mid = len(cross["nodes"]) // 2
        cross["nodes"][mid] = a["nodes"][-1]
        cross["geometry"][mid] = a["geometry"][-1]
        res = run([a, b, cross], fn)
        big = [c for c in res["climbs"] if c["length_m"] > 500]
        self.assertEqual(len(big), 1, [(c["name"], c["length_m"]) for c in res["climbs"]])
        self.assertAlmostEqual(big[0]["length_m"], 1200, delta=60)
        self.assertEqual(big[0]["name"], "Côte A")

    def test_same_name_street_turning_at_junction_is_one_climb(self):
        # Comme la rue Lepic au carrefour des Abbesses : la rue tourne de 80° à un carrefour, et une
        # autre rue (« Rue X ») prolonge tout droit le tronçon d'après le virage. On suit le nom :
        # une seule côte de 700 m.
        a80 = math.radians(80)
        dx, dy = math.cos(a80), math.sin(a80)

        def fn(x, y):  # 6 % le long de la rue, en suivant le virage
            return 100 + 0.06 * (x if y < 0.5 else 300 + y / dy)

        def build(name_b):
            a = way(line(0, 0, 300, 0), {"highway": "residential", "name": "Rue L"}, 1)
            b = way(line(300, 0, 300 + 400 * dx, 400 * dy), {"highway": "residential", "name": name_b}, 2)
            b["nodes"][0] = a["nodes"][-1]
            cross = way(line(300, 0, 300 + 300 * dx, -300 * dy), {"highway": "residential", "name": "Rue X"}, 3)
            cross["nodes"][0] = a["nodes"][-1]
            return [a, b, cross]

        res = run(build("Rue L"), fn)
        big = [c for c in res["climbs"] if c["length_m"] > 500]
        self.assertEqual(len(big), 1, [(c["name"], c["length_m"]) for c in res["climbs"]])
        self.assertAlmostEqual(big[0]["length_m"], 700, delta=40)
        self.assertEqual(big[0]["name"], "Rue L")
        self.assertEqual(big[0]["variant"], "principale")
        # nom différent après le virage : ce n'est plus la même route, la route classique est
        # coupée au carrefour ; l'enchaînement Rue L -> Rue M est proposé comme variante longue
        res = run(build("Rue M"), fn)
        big = [c for c in res["climbs"] if c["length_m"] > 500]
        self.assertEqual([(c["streets"], c["variant"]) for c in big], [("Rue L → Rue M", "longue")])
        head = [c for c in res["climbs"] if c["id"] == big[0]["family"]]
        self.assertEqual([(c["name"], c["variant"]) for c in head], [("Rue M", "principale")])

    def test_small_dip_does_not_split_but_long_flat_does(self):
        def fn(x, y):  # 600 m à 8 %, petit creux de 4 m, 600 m à 8 %
            if x < 600:
                return 100 + x * 0.08
            if x < 700:
                return 148 - 4 * math.sin((x - 600) / 100 * math.pi)
            return 148 + min(600, x - 700) * 0.08
        els = [way(line(0, 0, 2000, 0), {"highway": "tertiary"}, 1)]
        climbs = run(els, fn)["climbs"]
        self.assertEqual(len(climbs), 1)
        self.assertAlmostEqual(climbs[0]["length_m"], 1300, delta=80)

        def fn2(x, y):  # 600 m à 8 %, 1 km de plat, 600 m à 8 %
            if x < 600:
                return 100 + x * 0.08
            if x < 1600:
                return 148
            return 148 + min(600, x - 1600) * 0.08
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary"}, 1)]
        self.assertEqual(len(run(els, fn2)["climbs"]), 2)

    def test_radius_filter(self):
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary"}, 1)]
        # rayon 1 km : seul le mur (pied à 500 m) est dans le cercle, pas la côte (pied à 1,5 km)
        res = run(els, profile_east, radius_km=1)
        self.assertEqual(len(res["climbs"]), 1)
        self.assertAlmostEqual(res["climbs"][0]["distance_km"], 0.5, delta=0.1)

    def test_irregular_climb_scores_higher(self):
        steady = lambda x, y: 100 + min(1000, max(0, x)) * 0.08
        def irregular(x, y):  # même D+ (80 m) mais 300 m à 15 %
            x = min(1000, max(0, x))
            if x < 350:
                return 100 + x * 0.05
            if x < 650:
                return 117.5 + (x - 350) * 0.15
            return 162.5 + (x - 650) * 0.05
        els = [way(line(0, 0, 1500, 0), {"highway": "tertiary"}, 1)]
        s1 = run(els, steady)["climbs"][0]
        s2 = run(els, irregular)["climbs"][0]
        self.assertAlmostEqual(s1["gain_m"], s2["gain_m"], delta=2)
        self.assertGreater(s2["score"], s1["score"] * 1.2)
        self.assertAlmostEqual(s2["max_grade_300"], 15, delta=1)


    def test_skip_minor_streets(self):
        els = [way(line(0, 0, 3000, 0), {"highway": "residential"}, 1)]
        self.assertEqual(len(run(els, profile_east)["climbs"]), 2)
        self.assertEqual(len(run(els, profile_east, skip_minor=True)["climbs"]), 0)
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary"}, 1)]
        self.assertEqual(len(run(els, profile_east, skip_minor=True)["climbs"]), 2)

    def test_stable_ids_and_rank(self):
        els = [way(line(0, 0, 3000, 0), {"highway": "tertiary"}, 1)]
        r1, r2 = run(els, profile_east), run(els, profile_east)
        self.assertEqual([c["id"] for c in r1["climbs"]], [c["id"] for c in r2["climbs"]])
        self.assertEqual([c["rank"] for c in r1["climbs"]], [1, 2])


class TileTests(unittest.TestCase):
    def test_tiles_cover_circle_sorted_by_distance(self):
        tiles = scanner.tiles_for_scan(48.85, 2.35, 10000)
        self.assertEqual(scanner.osm_tile_of(48.85, 2.35), tiles[0])
        # chaque point du cercle doit être dans une case listée
        for k in range(36):
            a = k * math.pi / 18
            la = 48.85 + 10000 * math.sin(a) / scanner.EARTH_R * 180 / math.pi
            lo = 2.35 + 10000 * math.cos(a) / (scanner.EARTH_R * math.cos(math.radians(48.85))) * 180 / math.pi
            self.assertIn(scanner.osm_tile_of(la, lo), tiles)
        self.assertLess(len(tiles), 40)

    def test_neighbor_tiles(self):
        nb = scanner.neighbor_tiles((1221, 37))
        self.assertEqual(len(nb), 9)
        self.assertIn((1220, 36), nb)
        self.assertIn((1222, 38), nb)

    def test_process_tile_keeps_only_climbs_starting_inside(self):
        # route est-ouest qui traverse 2 cases ; le mur démarre dans la case de gauche
        tile = scanner.osm_tile_of(LAT0 + 0.01, LON0 + 0.01)
        s, w, n, e = scanner.osm_tile_bbox(tile)
        lat = (s + n) / 2
        x_edge = (e - LON0) * KX            # bord est de la case, en mètres
        y = (lat - LAT0) * KY

        def fn(x, yy):  # mur de 300 m à 15 % qui démarre 200 m avant le bord est
            return 100 + 45 * min(1, max(0, (x - (x_edge - 200)) / 300))
        el = way(line(x_edge - 2000, y, x_edge + 2000, y), {"highway": "tertiary"}, 1)
        climbs, _ = scanner.process_tile(tile, FakeElevation(fn), fetch=lambda t: [el])
        self.assertEqual(len(climbs), 1)
        self.assertAlmostEqual(climbs[0]["gain_m"], 45, delta=3)  # suivi au-delà du bord de la case
        right = (tile[0], tile[1] + 1)
        climbs_r, _ = scanner.process_tile(right, FakeElevation(fn), fetch=lambda t: [el])
        self.assertEqual(climbs_r, [])                              # pas de doublon dans la case voisine


class StoreTests(unittest.TestCase):
    def test_save_search_filters_and_get(self):
        import os, tempfile
        from store import Store
        path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        st = Store(path)
        els = [way(line(0, 0, 3000, 0), {"highway": "residential", "name": "Rue A"}, 1),
               way(line(0, 500, 3000, 500), {"highway": "tertiary", "name": "Route B", "surface": "sett"}, 2)]
        climbs, stats = scanner.scan_elements(els, FakeElevation(profile_east), (LAT0, LON0), 1e9,
                                              include_cobbles=True)
        for c in climbs:
            c["minor"] = c["highway"] in scanner.MINOR_STREETS
        used = save_by_tile(st, climbs, stats)
        self.assertEqual(st.stats(), {"tiles": len(used), "climbs": 4})
        self.assertEqual(st.done_tiles(list(used) + [(0, 1)]), used)
        r = st.search(LAT0, LON0, 10)                          # pavés exclus par défaut
        self.assertEqual(r["total"], 2)
        self.assertEqual(st.search(LAT0, LON0, 10, cobbles=True)["total"], 4)
        self.assertEqual(st.search(LAT0, LON0, 10, cobbles=True, minor=False)["total"], 2)
        self.assertEqual(st.search(LAT0, LON0, 10, len_max=500)["total"], 1)
        self.assertEqual(st.search(LAT0, LON0, 1)["total"], 1)  # rayon 1 km : seul le mur
        r = st.search(LAT0, LON0, 10, sort="length_m", desc=False)
        self.assertLess(r["climbs"][0]["length_m"], r["climbs"][1]["length_m"])
        self.assertEqual(r["climbs"][0]["rank"], 1)             # rang = classement par difficulté
        full = st.get(r["climbs"][0]["id"])
        self.assertEqual(len(full["geometry"]), len(full["profile"]))
        # recalcul de la case : remplace, pas de doublons
        save_by_tile(st, climbs, stats)
        self.assertEqual(st.stats()["climbs"], 4)


    def test_search_groups_variants_by_summit(self):
        import os, tempfile
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

        def fn(x, y):  # Rue A (5 %) puis Rue B (8 %) en tournant de 90° ; Rue C (5 %) tout droit
            if abs(y) < 0.5 and x <= 400.5:
                return 100 + 0.05 * x
            if abs(x - 400) < 0.5 and y < 0:
                return 120 + 0.05 * y
            return 120 + 0.08 * max(0, y)
        a = way(line(0, 0, 400, 0), {"highway": "residential", "name": "Rue A"}, 1)
        b = way(line(400, 0, 400, 500), {"highway": "residential", "name": "Rue B"}, 2)
        c = way(line(400, -400, 400, 0), {"highway": "residential", "name": "Rue C"}, 3)
        b["nodes"][0] = c["nodes"][-1] = a["nodes"][-1]
        climbs, stats = scanner.scan_elements([a, b, c], FakeElevation(fn), (LAT0, LON0), 1e9)
        save_by_tile(st, climbs, stats)
        r = st.search(LAT0, LON0, 10)
        top = [g for g in r["climbs"] if g["variants"]]
        self.assertEqual(len(top), 1, [(g["streets"], len(g["variants"])) for g in r["climbs"]])
        g = top[0]
        self.assertEqual((g["streets"], g["variant"]), ("Rue C → Rue B", "principale"))
        self.assertIn("Rue A → Rue B", [v["streets"] for v in g["variants"]])
        self.assertEqual(r["total"], len(r["climbs"]))          # total = nombre de sommets
        # filtre qui exclut la principale : la famille reste, avec la variante en tête
        r = st.search(LAT0, LON0, 10, q="Rue A → Rue B")
        self.assertEqual([x["streets"] for x in r["climbs"]], ["Rue A → Rue B"])
        # le tri porte sur les lignes affichées (têtes de famille) : valeurs dans l'ordre
        for key in ("score", "length_m", "avg_grade"):
            for desc in (True, False):
                vals = [x[key] for x in st.search(LAT0, LON0, 10, sort=key, desc=desc)["climbs"]]
                self.assertEqual(vals, sorted(vals, reverse=desc), (key, desc))
        full = st.get(g["id"])
        self.assertGreaterEqual(len(full["family_members"]), 2)
        self.assertIn("junctions", full)

    def test_sort_follows_displayed_rows(self):
        # Cas de la capture : une famille faible (43) dont une variante est très difficile (900) ne
        # doit pas passer devant une famille à 718 quand on trie par difficulté
        import os, tempfile
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

        def climb(cid, fam, variant, score, length):
            la, lo = ll(length / 10, 0)
            return {"id": cid, "start": [la, lo], "end": [la + 0.001, lo], "name": cid, "highway": "residential",
                    "surface": "confirmé", "length_m": length, "gain_m": 10, "ascent_m": 10, "avg_grade": 5,
                    "max_grade_100": 6, "max_grade_300": 6, "score": score, "alt_start": 0, "alt_end": 10,
                    "geometry": [[la, lo], [la + 0.001, lo]], "profile": [[0, 0], [length, 10]],
                    "family": fam, "variant": variant, "streets": cid, "junctions": 0}
        save_by_tile(st, [climb("A", "A", "principale", 718, 4000),
                          climb("B", "B", "principale", 43, 339), climb("B2", "B", "longue", 900, 2000),
                          climb("C", "C", "principale", 565, 2800)])
        r = st.search(LAT0, LON0, 10, sort="score", desc=True)
        self.assertEqual([(x["id"], x["rank"]) for x in r["climbs"]], [("A", 1), ("C", 2), ("B", 3)])
        self.assertEqual([v["id"] for v in r["climbs"][2]["variants"]], ["B2"])
        r = st.search(LAT0, LON0, 10, sort="length_m", desc=True)
        self.assertEqual([x["id"] for x in r["climbs"]], ["A", "C", "B"])
        # la variante qui correspond seule à un filtre reste trouvée (en tête de sa famille)
        r = st.search(LAT0, LON0, 10, len_min=1500, len_max=2500)
        self.assertEqual([x["id"] for x in r["climbs"]], ["B2"])

    def test_search_in_box_of_visible_map(self):
        # Plus de cercle : le tableau liste les côtes de la zone affichée (même zone que la carte)
        import os, tempfile
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        els = [way(line(0, 0, 3000, 0), {"highway": "residential", "name": "Rue A"}, 1),
               way(line(0, 2000, 3000, 2000), {"highway": "residential", "name": "Rue B"}, 2)]
        climbs, stats = scanner.scan_elements(els, FakeElevation(profile_east), (LAT0, LON0), 1e9)
        save_by_tile(st, climbs, stats)
        s, w = ll(-500, -500)
        n, e = ll(3500, 1000)       # ne contient que la rue A (y = 0)
        r = st.search(LAT0, LON0, None, box=(s, w, n, e))
        self.assertEqual({c["name"] for c in r["climbs"]}, {"Rue A"})
        n2, _ = ll(0, 2500)
        r = st.search(LAT0, LON0, None, box=[s, w, n2, e])   # liste (comme le serveur) acceptée
        self.assertEqual({c["name"] for c in r["climbs"]}, {"Rue A", "Rue B"})

    def test_pagination_and_cache(self):
        import os, tempfile
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

        def climb(k):
            la, lo = ll(10 * k, 0)
            return {"id": f"c{k}", "start": [la, lo], "end": [la + 0.001, lo], "name": f"c{k}", "highway": "residential",
                    "surface": "confirmé", "length_m": 100 + k, "gain_m": 10, "ascent_m": 10, "avg_grade": 5,
                    "max_grade_100": 6, "max_grade_300": 6, "score": float(k), "alt_start": 0, "alt_end": 10,
                    "geometry": [[la, lo], [la + 0.001, lo]], "profile": [[0, 0], [100, 10]]}
        save_by_tile(st, [climb(k) for k in range(25)])
        pages = [st.search(LAT0, LON0, 10, limit=10, offset=o) for o in (0, 10, 20)]
        ids = [c["id"] for p in pages for c in p["climbs"]]
        self.assertEqual(ids, [f"c{k}" for k in range(24, -1, -1)])          # dans l'ordre, sans trou
        self.assertEqual([p["total"] for p in pages], [25, 25, 25])
        self.assertEqual([c["rank"] for c in pages[1]["climbs"]], list(range(11, 21)))
        # une case recalculée invalide le cache
        far = ll(0, 6000)   # 6 km au nord : une autre case
        save_by_tile(st, [dict(climb(99), id="new", score=1000.0, start=list(far))])
        self.assertEqual(st.search(LAT0, LON0, 10, limit=1)["climbs"][0]["id"], "new")

    def test_map_shows_all_climbs_of_view_not_only_top(self):
        # La carte ne doit pas se limiter à la page du tableau (les 300 plus difficiles) : toutes les
        # côtes principales de la zone visible, filtres compris ; au-delà du plafond, les plus difficiles
        import os, tempfile
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

        def climb(k, variant="principale", surface="confirmé"):
            la, lo = ll(10 * k, 0)
            return {"id": f"c{k}", "start": [la, lo], "end": [la + 0.001, lo], "name": f"c{k}", "highway": "residential",
                    "surface": surface, "length_m": 100 + k, "gain_m": 10, "ascent_m": 10, "avg_grade": 5,
                    "max_grade_100": 6, "max_grade_300": 6, "score": float(k), "alt_start": 0, "alt_end": 10,
                    "geometry": [[la + i * 1e-4, lo] for i in range(30)], "profile": [[0, 0], [100, 10]],
                    "family": f"c{k}", "variant": variant}
        save_by_tile(st, [climb(k) for k in range(20)] + [climb(20, "longue"), climb(21, surface="pavés")])
        s, w = ll(-1000, -1000)
        n, e = ll(1000, 1000)
        r = st.map_climbs(s, w, n, e)
        self.assertEqual(sorted(c["id"] for c in r["climbs"]), sorted(f"c{k}" for k in range(20)))
        self.assertFalse(r["truncated"])
        self.assertEqual(len(r["climbs"][0]["geometry"]), 12)                 # tracé simplifié
        self.assertIn("c21", [c["id"] for c in st.map_climbs(s, w, n, e, cobbles=True)["climbs"]])
        r = st.map_climbs(s, w, n, e, limit=5)
        self.assertEqual([c["id"] for c in r["climbs"]], ["c19", "c18", "c17", "c16", "c15"])
        self.assertTrue(r["truncated"])

    def test_dem_bilinear_and_decode(self):
        dem = scanner.DemService.__new__(scanner.DemService)
        dem.tiles = {}
        n = scanner.DEM_PX
        # carte synthétique : altitude = 100 + 2 * colonne + 1 * ligne, en big-endian
        vals = [100 + 2 * (k % n) + (k // n) for k in range(n * n)]
        import struct
        arr = scanner.DemService._decode(struct.pack(f">{n * n}f", *vals))
        t = (2400, 66)  # lat 48.00-48.02, lon 1.98-2.01
        dem.tiles[t] = arr
        lat_n = (t[0] + 1) * scanner.DEM_TILE_DLAT
        lon_w = t[1] * scanner.DEM_TILE_DLON
        # centre du pixel (ligne 10, colonne 20)
        la = lat_n - 10.5 / n * scanner.DEM_TILE_DLAT
        lo = lon_w + 20.5 / n * scanner.DEM_TILE_DLON
        self.assertAlmostEqual(dem.z(la, lo), 100 + 40 + 10, places=3)
        # à mi-chemin entre deux pixels
        lo2 = lon_w + 21.0 / n * scanner.DEM_TILE_DLON
        self.assertAlmostEqual(dem.z(la, lo2), 151, places=3)
        self.assertIsNone(dem.z(10.0, 10.0))

    def test_dem_without_valid_altitude_is_not_an_error(self):
        # carte en pleine mer / hors de France : que des valeurs « pas de donnée » (ou des artefacts
        # très négatifs en bord de mer) -> points sans altitude, et non une erreur qui ferait
        # retenter la case indéfiniment
        import struct
        n = scanner.DEM_PX
        arr = scanner.DemService._decode(struct.pack(f"<{n * n}f", *([-99999.0] * (n * n - 2) + [-969.0, 12.5])))
        self.assertEqual(len(arr), n * n)
        self.assertTrue(all(v != v for v in arr[:-1]))
        self.assertEqual(arr[-1], 12.5)


class DemPrefetchTests(unittest.TestCase):
    def test_dem_maps_for_tile_cover_tile_and_margin(self):
        tile = (1220, 36)
        maps = set(scanner.dem_maps_for_tile(tile))
        S, W, N, E = scanner.tile_bounds_with_margin(tile)
        for k in range(21):
            for q in range(21):
                la, lo = S + (N - S) * k / 20, W + (E - W) * q / 20
                self.assertIn(scanner.DemService.tile_of(la, lo), maps)

    def test_run_tiles_prefetches_dem_maps(self):
        import os, tempfile, threading, time
        import precompute
        from store import Store
        tile = (1000, 50)
        wanted = set(scanner.dem_maps_for_tile(tile))
        got, lock = set(), threading.Lock()

        class Dem:
            def download(self, m):
                with lock:
                    got.add(m)

            def lookup(self, points, progress=None):
                t0 = time.time()   # le calcul attend que le préchargement soit passé
                while time.time() - t0 < 5:
                    with lock:
                        if wanted <= got:
                            break
                    time.sleep(0.01)
                return [100.0 for _ in points]

        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        done, failed = precompute.run_tiles([tile], st, dem=Dem(), fetch=lambda t: [])
        self.assertEqual((done, failed), (1, []))
        self.assertTrue(wanted <= got, wanted - got)


class ShareTests(unittest.TestCase):
    def test_share_meta_for_link_preview(self):
        import server
        c = {"name": "Rue Lepic", "variant": "principale", "length_m": 720.0, "avg_grade": 6.4, "gain_m": 46.2,
             "max_grade_100": 9.85, "score": 33.4}
        m = server.share_meta(c)
        self.assertIn("<title>Rue Lepic — 720 m à 6,4 %</title>", m)
        self.assertIn('content="D+ 46 m, jusqu&#x27;à 9,8 % sur 100 m, difficulté 33. Côte à vélo', m)
        v = dict(c, name="Av. Foch", variant="longue", streets="Av. Foch → Rue <b>", length_m=1234)
        m = server.share_meta(v)
        self.assertIn("<title>Av. Foch → Rue &lt;b&gt; — 1,23 km à 6,4 %</title>", m)   # échappé, points intacts


class CompactStorageTests(unittest.TestCase):
    def test_polyline_roundtrip_and_legacy_json(self):
        import store
        geom = [[48.88468, 2.3336], [48.88501, 2.33317], [-0.00001, -179.99999], [43.0, 6.5]]
        prof = [[0, 530.9], [62, 533.6], [132, -2.5], [8000, 4807.0]]
        g = store.encode_pairs(geom, store.GEOM_SCALES)
        p = store.encode_pairs(prof, store.PROFILE_SCALES)
        self.assertTrue(g.startswith("!") and len(g) < len(str(geom)) / 2)
        self.assertEqual(store.decode_pairs(g, store.GEOM_SCALES), geom)
        self.assertEqual(store.decode_pairs(p, store.PROFILE_SCALES), prof)
        self.assertEqual(store.decode_pairs("[[1.5,2]]", store.GEOM_SCALES), [[1.5, 2]])   # ancien format

    def test_compact_converts_old_rows(self):
        import os, tempfile, json, sqlite3
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        els = [way(line(0, 0, 3000, 0), {"highway": "residential", "name": "Rue A"}, 1)]
        climbs, stats = scanner.scan_elements(els, FakeElevation(profile_east), (LAT0, LON0), 1e9)
        save_by_tile(st, climbs, stats)
        before = {c["id"]: st.get(c["id"]) for c in climbs}
        con = sqlite3.connect(st.path)   # on remet ces lignes à l'ancien format JSON
        for cid, c in before.items():
            con.execute("UPDATE climbs SET geometry = ?, profile = ? WHERE id = ?",
                        (json.dumps(c["geometry"]), json.dumps(c["profile"]), cid))
        con.commit(); con.close()
        self.assertEqual(st.compact(progress=lambda m: None), len(climbs))
        for cid, c in before.items():
            after = st.get(cid)
            self.assertEqual((after["geometry"], after["profile"]), (c["geometry"], c["profile"]))
        r = st.search(LAT0, LON0, 10)
        self.assertTrue(all(len(x["geometry"]) >= 2 for x in r["climbs"]))


class FranceTests(unittest.TestCase):
    """Indexation de toute la France (france.py) : lecture des extraits OSM, cases, blocs."""

    def test_parse_opl_way_and_node(self):
        import france
        kind, w = france.parse_opl_line(
            "w7982833 Thighway=residential,name=Boulevard%20%Masseria,note=a%2c%b%3d%c,oneway=yes "
            "Nn275387026x8.7370589y41.9260778,n59747742x8.7369543y41.9255896\n")
        self.assertEqual(kind, "way")
        self.assertEqual(w["id"], 7982833)
        self.assertEqual(w["nodes"], [275387026, 59747742])
        self.assertEqual(w["geometry"][0], {"lat": 41.9260778, "lon": 8.7370589})
        self.assertEqual(w["tags"], {"highway": "residential", "name": "Boulevard Masseria", "note": "a,b=c", "oneway": "yes"})
        kind, n = france.parse_opl_line("n255850525 Tcrossing=traffic_signals,highway=traffic_signals x8.7524411 y41.9339735")
        self.assertEqual((kind, n["id"], n["lat"], n["tags"]["highway"]), ("node", 255850525, 41.9339735, "traffic_signals"))
        # nœud tagué mais ni feu ni stop (arrêt de bus) : ignoré
        self.assertIsNone(france.parse_opl_line("n1 Tbus=yes,public_transport=stop_position x8.7 y41.9"))

    def test_same_selection_as_overpass(self):
        import france
        self.assertTrue(france.keep_way({"highway": "residential"}))
        self.assertTrue(france.keep_way({"highway": "service"}))
        self.assertFalse(france.keep_way({"highway": "pedestrian", "area": "yes"}))
        self.assertFalse(france.keep_way({"highway": "residential", "area": "yes"}))
        self.assertFalse(france.keep_way({"highway": "footway"}))
        self.assertFalse(france.keep_way({"highway": "track"}))
        self.assertTrue(france.keep_way({"highway": "track", "surface": "asphalt"}))
        self.assertTrue(france.keep_way({"highway": "track", "tracktype": "grade1"}))

    def test_split_into_tiles_and_read_back(self):
        import france, json
        t0 = scanner.osm_tile_of(41.9255, 8.7369)
        lines = [
            "w1 Thighway=residential,name=A Nn1x8.7369y41.9255,n2x8.7369y41.9256",       # dans t0
            "w2 Thighway=footway Nn3x8.7369y41.9255,n4x8.7369y41.9256",                   # pas une route
            "w3 Thighway=residential Nn5x8.7369y41.9255,n6x%.4fy41.9255" % (8.7369 + 0.06),  # t0 et voisine
            "n9 Thighway=stop x8.7369 y41.9255",
        ]
        tiles = {t0, (t0[0], t0[1] + 1), (t0[0] + 5, t0[1])}
        out = france.split_into_tiles(lines, tiles)
        ids = lambda t: sorted(json.loads(s).get("id") for s in out[t])
        self.assertEqual(ids(t0), [1, 3, 9])
        self.assertEqual(ids((t0[0], t0[1] + 1)), [3])
        self.assertFalse(france.has_way(out[(t0[0] + 5, t0[1])]))
        # le format écrit est celui du cache Overpass : process_tile le lit tel quel, feux/stops compris
        els = json.loads("[" + ",".join(out[t0] + [france.META]) + "]")
        self.assertEqual(els[-1]["type"], "meta")
        self.assertEqual(els[0]["geometry"][0], {"lat": 41.9255, "lon": 8.7369})

    def test_blocks_cover_france_without_overlap(self):
        import france
        blocks = france.all_blocks()
        seen = set()
        for b in blocks:
            tiles = france.block_tiles(b)
            self.assertEqual(len(tiles), france.BLOCK ** 2)
            self.assertFalse(seen & tiles)
            seen |= tiles
        for la, lo in ((48.8566, 2.3522), (43.6589, 6.9237), (41.9192, 8.7386), (51.0, 2.3), (42.5, -1.7)):
            self.assertIn(scanner.osm_tile_of(la, lo), seen)
        order = france.ordered_blocks()
        self.assertEqual(len(order), len(blocks))
        s, w, n, e = france.block_bbox(order[0])
        self.assertTrue(s <= 48.8566 < n and w <= 2.3522 < e)          # Paris d'abord

    def test_dem_rotation_keeps_maps_of_pending_blocks(self):
        import france
        b = france.all_blocks()[40]
        right = (b[0], b[1] + 1)
        s, w, n, e = france.block_bbox(b)
        inside = scanner.DemService.tile_of((s + n) / 2, (w + e) / 2)
        border = scanner.DemService.tile_of((s + n) / 2, e - 0.001)    # au bord, côté bloc de droite
        self.assertFalse(france.still_needed(inside, [right]))          # plus personne n'en a besoin
        self.assertTrue(france.still_needed(border, [right]))           # le bloc voisin en aura besoin
        self.assertTrue(france.still_needed(inside, [b]))


class FranceComputeTests(unittest.TestCase):
    def test_stalled_block_is_abandoned_not_hung(self):
        # Le 13/11, le calcul du bloc de Lyon s'est figé (pool de processus bloqué, rien dans le
        # journal) : si plus aucune case ne se termine, le bloc doit être abandonné (cases retentées
        # plus tard) au lieu d'attendre indéfiniment.
        import os, tempfile, threading, time
        from concurrent.futures import ThreadPoolExecutor
        import france
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        release = threading.Event()
        stuck = (1000, 50)

        def fake_work(tile):
            if tile == stuck:
                release.wait(10)                      # ne se termine jamais pendant le test
            return tile, [], {"road_km": 0}, 0.0
        old = france._work
        france._work = fake_work
        try:
            run = france.Run("x.osm.pbf", st)
            run.make_pool = lambda: ThreadPoolExecutor(2)
            t0 = time.time()
            failed = run.compute("b", [(1000, 51), stuck, (1000, 52)], 0, 1, stall_timeout=0.5)
            self.assertLess(time.time() - t0, 5)
            self.assertEqual(failed, 1)
            self.assertEqual(st.done_tiles([(1000, 51), (1000, 52), stuck]), {(1000, 51), (1000, 52)})
        finally:
            release.set()
            france._work = old


class VariantTests(unittest.TestCase):
    def test_variants_with_junction_counts(self):
        # Mur en L : Rue A monte vers l'est jusqu'au carrefour, puis Rue B monte vers le nord jusqu'au
        # sommet. Rue C arrive aussi au carrefour par le sud en montant : deux façons d'arriver au
        # sommet par Rue B. Une rue latérale (Rue D) croise Rue B : un carrefour traversé.
        def fn(x, y):
            if x <= 400.5 and abs(y) < 0.5:
                return 100 + 0.05 * x                       # Rue A : 400 m à 5 %
            if abs(x - 400) < 0.5 and y < 0:
                return 120 + 0.05 * y                       # Rue C : de (400, -400) à 4 %... -> 100 à 120 m
            return 120 + 0.08 * max(0, y)                   # Rue B : 500 m à 8 %
        a = way(line(0, 0, 400, 0), {"highway": "residential", "name": "Rue A"}, 1)
        b = way(line(400, 0, 400, 500), {"highway": "residential", "name": "Rue B"}, 2)
        c = way(line(400, -400, 400, 0), {"highway": "residential", "name": "Rue C"}, 3)
        b["nodes"][0] = a["nodes"][-1]
        c["nodes"][-1] = a["nodes"][-1]
        d = way([(300, 250), (400, 250), (500, 250)], {"highway": "residential", "name": "Rue D"}, 4)
        d["nodes"][1] = b["nodes"][10]                      # (400, 250) : nœud de Rue B
        res = run([a, b, c, d], fn)
        top = [cl for cl in res["climbs"] if cl["end"][0] > ll(0, 450)[0]]
        fams = {cl["family"] for cl in top}
        self.assertEqual(len(fams), 1, [(cl["streets"], cl["family"]) for cl in top])
        by = {cl["streets"]: cl for cl in top}
        # Rue A -> Rue B et Rue C -> Rue B passent tout droit ou tournent de 90° : les deux trouvées
        self.assertIn("Rue A → Rue B", by, list(by))
        self.assertIn("Rue C → Rue B", by, list(by))
        self.assertEqual(by["Rue C → Rue B"]["variant"], "principale")  # route droite = classique
        self.assertEqual(len({cl["id"] for cl in top}), len(top))
        for cl in top:
            self.assertEqual(cl["family"], by["Rue C → Rue B"]["id"])
        # carrefours traversés : Rue A/C/B au point bas (pour la variante qui vient de A ou C) + Rue D
        self.assertEqual(by["Rue A → Rue B"]["junctions"], 2)
        self.assertEqual(by["Rue A → Rue B"]["main_junctions"], 0)

    def test_no_hairpin_turn_at_junction(self):
        # Rue A monte vers le carrefour en arrivant presque parallèle à Rue B (virage de ~170°) :
        # pas d'enchaînement Rue A -> Rue B, mais chacune reste une côte
        def fn(x, y):
            return 120 - 0.05 * math.hypot(x, y) if x > 0.5 else 120 + 0.08 * max(0.0, y)
        b = way(line(0, 0, 0, 500), {"highway": "residential", "name": "Rue B"}, 1)
        a = way(line(60, 400, 0, 0), {"highway": "residential", "name": "Rue A"}, 2)
        a["nodes"][-1] = b["nodes"][0]
        streets = {c["streets"] for c in run([a, b], fn)["climbs"]}
        self.assertIn("Rue B", streets)
        self.assertIn("Rue A", streets)
        self.assertNotIn("Rue A → Rue B", streets)

    def test_at_most_three_street_changes(self):
        # Escalier de 5 rues (virages à 90°), 6 % partout : au plus 3 changements de rue
        names = ["S1", "S2", "S3", "S4", "S5"]
        pts = [(0, 0), (150, 0), (150, 150), (300, 150), (300, 300), (600, 300)]
        els = []
        for k, nm in enumerate(names):
            els.append(way(line(*pts[k], *pts[k + 1]), {"highway": "residential", "name": nm}, k + 1))
            if k:
                els[k]["nodes"][0] = els[k - 1]["nodes"][-1]
        streets = [c["streets"] for c in run(els, lambda x, y: 100 + 0.06 * (x + y))["climbs"]]
        self.assertIn("S5", streets)
        self.assertIn("S2 → S3 → S4 → S5", streets)
        self.assertFalse([st for st in streets if st.startswith("S1")], streets)

    def test_reference_rue_de_la_garenne(self):
        # Cas réel (EVOLUTIONS.md), à partir du cache : Rue de la Garenne à Meudon/Sèvres
        import json, os
        tile = (1220, 36)
        paths = [os.path.join(scanner.CACHE_DIR, "osm", f"{i}_{j}.json") for i, j in scanner.neighbor_tiles(tile)]
        if not all(os.path.exists(p) for p in paths) or not os.path.isdir(os.path.join(scanner.CACHE_DIR, "dem")):
            self.skipTest("cache OSM/MNT absent")
        foot = (48.82025, 2.206304)
        els, seen = [], set()
        for p in paths:
            with open(p) as f:
                data = json.load(f)
            for el in data:
                g = el.get("geometry") or []
                if el.get("type") in ("node", "meta") or el["id"] in seen:
                    continue
                if any(scanner.haversine(foot[0], foot[1], q["lat"], q["lon"]) < 1500 for q in g if q):
                    seen.add(el["id"])
                    els.append(el)
        climbs, _ = scanner.scan_elements(els, scanner.DemService(), foot, 1300, include_cobbles=True)
        fam = [c for c in climbs if c["family"] == "4ef0c94ea2"]
        by = {c["streets"]: c for c in fam}
        self.assertEqual(by["Rue de la Garenne"]["variant"], "principale", list(by))
        self.assertAlmostEqual(by["Rue de la Garenne"]["length_m"], 680, delta=30)
        self.assertIn("Rue du Parc Cheviron → Rue Victor Pauchet → Rue de la Garenne", by, list(by))
        self.assertIn("Rue des Bois → Rue de la Garenne", by, list(by))
        self.assertEqual(len({c["id"] for c in fam}), len(fam))
        self.assertEqual(len(by), len(fam))              # pas deux fois le même enchaînement
        self.assertLessEqual(len(fam), 1 + scanner.MAX_VARIANTS)
        for c in fam:
            self.assertLessEqual(c["streets"].count("→"), scanner.MAX_STREET_CHANGES)


class PrecomputeTests(unittest.TestCase):
    def test_stale_prefetch_failure_is_retried(self):
        """Overpass en panne pendant le préchargement, revenu au moment de calculer la case :
        la case doit être calculée (nouvel essai) et non abandonnée sur l'ancien échec."""
        import os, tempfile, threading
        import precompute
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        tile = scanner.osm_tile_of(LAT0 + 0.01, LON0 + 0.01)
        tries, lock = {}, threading.Lock()

        def fetch(t):
            with lock:
                tries[t] = tries.get(t, 0) + 1
                first = tries[t] == 1
            if first:
                raise RuntimeError("Overpass indisponible : HTTP Error 504")
            return []

        done, failed = precompute.run_tiles([tile], st, dem=FakeElevation(lambda x, y: 100), fetch=fetch)
        self.assertEqual((done, failed), (1, []))
        self.assertIn(tile, st.done_tiles([tile]))

    def test_failed_tile_does_not_block_next_tile_behind_prefetch_queue(self):
        """Une case A échoue ; la case B voisine partage une voisine X dont le préchargement avait
        échoué. B ne doit pas attendre un nouveau préchargement de X placé en fin de file
        (derrière des téléchargements lents), mais retenter X tout de suite."""
        import os, tempfile, threading, time
        import precompute
        from store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        A, B, C = (100, 100), (100, 101), (200, 200)   # C loin : ses téléchargements sont lents
        X, Y = (99, 100), (100, 99)                    # X voisine de A et B ; Y voisine de A seulement
        slow = {C} | set(scanner.neighbor_tiles(C))
        release, tries, lock = threading.Event(), {}, threading.Lock()

        def fetch(t):
            with lock:
                tries[t] = tries.get(t, 0) + 1
                n = tries[t]
            if t in slow:
                release.wait(10)
                return []
            if t == Y or (t == X and n == 1):
                raise RuntimeError("Overpass indisponible : HTTP Error 504")
            return []

        out = {}
        th = threading.Thread(target=lambda: out.update(r=precompute.run_tiles(
            [A, B, C], st, dem=FakeElevation(lambda x, y: 100), fetch=fetch)))
        th.start()
        t0 = time.time()
        while B not in st.done_tiles([B]) and time.time() - t0 < 5:
            time.sleep(0.05)
        b_done = B in st.done_tiles([B])
        release.set()
        th.join(15)
        self.assertTrue(b_done, "la case B attend un préchargement placé en fin de file")
        self.assertEqual(out["r"], (2, [A]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

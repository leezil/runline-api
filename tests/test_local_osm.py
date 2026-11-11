import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from routing import InputError, graph_from_osm
import local_osm


class LocalRoadsTests(unittest.TestCase):
    def test_spatial_filter_shared_ids_and_blocked_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"roads.sqlite3"
            db = sqlite3.connect(p)
            db.executescript("create table meta(k,v);insert into meta values('timestamp','test');"
                             "create table ways(id integer primary key,tags,geometry);"
                             "create virtual table bounds using rtree(id,minx,maxx,miny,maxy);")
            ways=[(10,[[1,127,37,0],[2,127.001,37,0]]),
                  (11,[[2,127.001,37,0],[3,127.002,37,1]]),
                  (12,[[4,129,35,0],[5,129.001,35,0]])]
            for wid, pts in ways:
                db.execute("insert into ways values(?,?,?)",(wid,'{"highway":"footway"}',json.dumps(pts)))
                db.execute("insert into bounds values(?,?,?,?,?)",(wid,pts[0][1],pts[1][1],pts[0][2],pts[1][2]))
            db.commit()
            db.close()
            with patch.object(local_osm,"DATABASE",p), patch("urllib.request.urlopen",side_effect=AssertionError("unexpected network")):
                raw,_=local_osm.read_roads([127,37],800)
                self.assertEqual([e["id"] for e in raw["elements"] if e["type"]=="way"],[10,11])
                g=graph_from_osm(raw,[127,37],800)
                self.assertIn("2",g.nodes)
                self.assertNotIn("3",g.nodes)
                self.assertEqual(raw["_drawrun_fetch"]["osm_provider"],"local_korea")

    def test_missing_file_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local_osm,"DATABASE",Path(tmp)/"absent"):
            with self.assertRaises(InputError):
                local_osm.read_roads([127,37],800)

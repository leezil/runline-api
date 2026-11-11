"""Read a local Geofabrik South Korea extract. Runtime: Python standard library."""
from contextlib import closing
from pathlib import Path
import json
import math
import sqlite3

from routing import InputError

ROOT = Path(__file__).resolve().parent
STORE = ROOT / ".cache" / "korea"
DATABASE = STORE / "roads.sqlite3"


def read_roads(center, radius, provider="local"):
    # main and coffee remain valid legacy client values; neither contacts a network.
    if not DATABASE.is_file():
        raise InputError("대한민국 로컬 도로 인덱스가 없습니다. local_osm.py로 먼저 생성해주세요.")
    x, y = center
    dy = math.degrees(radius / 6371008.8)
    dx = dy / math.cos(math.radians(y))
    nodes, ways = {}, []
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
        stamp = db.execute("select v from meta where k='timestamp'").fetchone()[0]
        for wid, tags, geometry in db.execute(
            "select w.id,w.tags,w.geometry from bounds b join ways w on w.id=b.id "
            "where b.minx<=? and b.maxx>=? and b.miny<=? and b.maxy>=?",
            (x+dx, x-dx, y+dy, y-dy)):
            points = json.loads(geometry)
            for nid, lon, lat, blocked in points:
                if nid not in nodes:
                    n = {"type":"node","id":nid,"lon":lon,"lat":lat}
                    if blocked:
                        n["tags"] = {"foot":"no"}
                    nodes[nid] = n
            ways.append({"type":"way","id":wid,"tags":json.loads(tags),
                         "nodes":[a[0] for a in points]})
            if len(nodes) + len(ways) > 180000:
                raise InputError("도로 데이터가 너무 많습니다. radius_m을 줄여주세요.")
    info = {"osm_provider":"local_korea","osm_timestamp":stamp,
            "attribution":"© OpenStreetMap contributors (ODbL), extract: Geofabrik"}
    return {"elements":[*nodes.values(), *ways], "osm3s":{"timestamp_osm_base":stamp},
            "_drawrun_fetch":info}, False


def build():
    import os
    import tempfile
    import osmium
    from routing import walkable, FOOT_DENY, FOOT_ALLOW

    pbf = STORE / "south-korea.osm.pbf"
    with osmium.io.Reader(str(pbf)) as reader:
        stamp = reader.header().get("osmosis_replication_timestamp") or "unknown"
    blocked = set()
    for n in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("foot","access")):
        foot, access = n.tags.get("foot"), n.tags.get("access")
        if foot in FOOT_DENY or (access in {"private","no"} and foot not in FOOT_ALLOW):
            blocked.add(n.id)
    tmp = tempfile.NamedTemporaryFile(dir=STORE, prefix="roads-", suffix=".building", delete=False)
    tmp.close()
    count = 0
    try:
        with closing(sqlite3.connect(tmp.name)) as db:
            db.executescript("pragma synchronous=off; pragma journal_mode=off;"
                "create table ways(id integer primary key,tags text,geometry text);"
                "create virtual table bounds using rtree(id,minx,maxx,miny,maxy);"
                "create table meta(k text primary key,v text);")
            proc = osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)).with_filter(osmium.filter.KeyFilter("highway"))
            for way in proc:
                tags = dict(way.tags)
                if not walkable(tags):
                    continue
                pts = [(n.ref,n.lon,n.lat,int(n.ref in blocked)) for n in way.nodes]
                if len(pts)<2:
                    continue
                xs,ys=zip(*[(n[1],n[2]) for n in pts])
                db.execute("insert into ways values(?,?,?)",(way.id,json.dumps(tags,ensure_ascii=False,separators=(",",":")),json.dumps(pts,separators=(",",":"))))
                db.execute("insert into bounds values(?,?,?,?,?)",(way.id,min(xs),max(xs),min(ys),max(ys)))
                count+=1
                if count%100000==0:
                    print("indexed ways:",count,flush=True)
            if not count:
                raise ValueError("empty road index")
            db.execute("insert into meta values('timestamp',?)",(stamp,))
            db.commit()
        os.replace(tmp.name,DATABASE)
        print("Local Korea ready:",stamp,count,DATABASE,flush=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


if __name__=="__main__":
    build()

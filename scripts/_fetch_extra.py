# -*- coding: utf-8 -*-
"""补抓「徒步参考点」：水源 / 营地 / 补给 / 山屋（OSM node 类）。

为什么要有这个脚本
------------------
`fetch_osm.py` 的主 QUERY 只抓路网、水系、地名、山峰、垭口、冰川 —— **不含**
`natural=spring`（泉水）、`man_made=water_tap`（取水点）、`amenity=drinking_water`、
`tourism=camp_site`（营地）、`amenity=shelter|ranger_station|fuel`、
`shop=*`（小卖部）。这些恰恰是徒步者最想知道的「哪里有水、哪里能补给」。

为什么不把它们塞进主 QUERY
-------------------------
主 QUERY 是 way 为主的大查询，公共实例（overpass-api.de 等）对它本来就容易
504 / 返回半截数据；再加一批 node 只会让它更慢更不稳。而这几类**全是 node**，
单独查一次几十秒就能回来。→ 走独立脚本 + 合并进 `out/osm.json`。

用法
----
    python _fetch_extra.py                 # 用 route_def 的窗口
    python _fetch_extra.py --bbox 101.5 29.3 102.2 30.1
    python _fetch_extra.py --no-merge      # 只落 out/osm_extra.json

合并规则：按 (type, id) 去重；新点的 tags 会被打上 `_kind`：
    spring / water → 水源（蓝点）   camp → 营地（绿三角）   supply → 补给（方块）
`draw_osm.py` 认这个 `_kind` 标签并画对应符号与注记；没有 `_kind` 的老缓存
完全不受影响（这是加这个脚本而不改主 QUERY 的关键）。
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guide_common as GC                                            # noqa: E402

OUT = HERE / "out"
UA = "hiking-route-guide/1.0 (offline guide map; contact: local user)"

# ⚠ overpass.osm.ch 只有欧洲数据，查亚洲会返回空 —— 不放进首选（fetch_osm 同样坑过）
MIRRORS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

EXTRA_QUERY = """
[out:json][timeout:180];
(
  node["natural"="spring"]({bbox});
  node["amenity"="drinking_water"]({bbox});
  node["man_made"="water_tap"]({bbox});
  node["tourism"~"^(camp_site|alpine_hut|wilderness_hut)$"]({bbox});
  node["amenity"~"^(shelter|ranger_station|fuel|cafe|restaurant|marketplace)$"]({bbox});
  node["shop"]({bbox});
  node["place"~"^(farm|isolated_dwelling|farmstead|allotments)$"]({bbox});
  way["place"~"^(farm|isolated_dwelling|farmstead)$"]({bbox});
);
out;
"""


def _kind_of(t):
    if t.get("natural") == "spring":
        return "spring"
    if t.get("man_made") == "water_tap" or t.get("amenity") == "drinking_water":
        return "water"
    if t.get("tourism") in ("camp_site", "alpine_hut", "wilderness_hut"):
        return "camp"
    if t.get("place"):
        return "place"
    return "supply"


def fetch(bbox):
    q = EXTRA_QUERY.format(bbox=bbox)
    last = None
    for host in MIRRORS:
        for rnd in range(2):
            try:
                t = time.time()
                req = Request(host, data=urlencode({"data": q}).encode(),
                              headers={"User-Agent": UA})
                with urlopen(req, timeout=240) as r:
                    d = json.loads(r.read().decode("utf-8"))
                els = d.get("elements", [])
                print(f"  {host.split('/')[2]} 返回 {len(els)} 个点（{time.time()-t:.0f}s）")
                return els
            except Exception as e:                                    # noqa: BLE001
                last = e
                print(f"  ✗ {host.split('/')[2]} 第 {rnd+1} 轮：{e}")
                time.sleep(3)
    raise SystemExit(f"全部镜像失败：{last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("LON0", "LAT0", "LON1", "LAT1"),
                    help="显式窗口（默认取 route_def 的）")
    ap.add_argument("--no-merge", action="store_true", help="只落 out/osm_extra.json，不合并")
    args = ap.parse_args()

    if args.bbox:
        lon0, lat0, lon1, lat1 = args.bbox
    else:
        lon0, lat0, lon1, lat1, _ = GC.resolve_bbox()
    bbox = f"{lat0},{lon0},{lat1},{lon1}"
    print(f"窗口 {bbox}")

    els = fetch(bbox)
    keep = []
    for e in els:
        t = dict(e.get("tags") or {})
        nm = t.get("name") or t.get("name:zh") or t.get("name:en") or ""
        kind = _kind_of(t)
        t["_kind"] = kind
        e["tags"] = t
        # 无名的泉水/取水点也要留（画符号不写字）；无名营地同理
        if nm or kind in ("spring", "water", "camp"):
            keep.append(e)

    OUT.mkdir(exist_ok=True)
    (OUT / "osm_extra.json").write_text(
        json.dumps({"elements": keep}, ensure_ascii=False), encoding="utf-8")
    print(f"  保留 {len(keep)} 个 → out/osm_extra.json")
    if args.no_merge:
        return

    main_f = OUT / "osm.json"
    if not main_f.exists():
        print("  ⚠ 没有 out/osm.json，先跑 fetch_osm.py")
        return
    base = json.loads(main_f.read_text(encoding="utf-8"))
    cur = base["elements"] if isinstance(base, dict) else base
    seen = {(x.get("type"), x.get("id")) for x in cur}
    add = 0
    for e in keep:
        k = (e.get("type"), e.get("id"))
        if k in seen:
            continue
        seen.add(k)
        cur.append(e)
        add += 1
    main_f.write_text(json.dumps({"elements": cur}, ensure_ascii=False), encoding="utf-8")
    print(f"  合并 {add} 个新点 → out/osm.json 共 {len(cur)} 个要素")


if __name__ == "__main__":
    main()

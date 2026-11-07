# -*- coding: utf-8 -*-
"""从官方 PBF 大切片裁出「窗口级小 PBF」，之后 pbf_extract 秒级复用。

为什么需要
----------
`pbf_extract()` 顺序扫全片，实测约 2 s/MB：广东 185 MB 抽一个 0.2° 窗口
要 369 s，日本 kansai 377 MB 约 12 min，日本全国片 2750 MB 约 1.5 h。
大片只需扫一次，把窗口结果写成标准 PBF（几 MB），之后每次抽取都是秒级。
（技能里已有的 `cluster_extract()` 是同省多线路共用一次扫描的 JSON 方案；
 本工具产出的是标准 PBF，可被 pbf_extract 直接当切片消费，也能跨工具复用。）

产物约定（落盘即生效，零配置）
------------------------------
    <cache>/<name>-latest.osm.pbf   裁出的窗口片
    <cache>/<name>-latest.json      含 `_bbox` 的来源/范围元数据
`fetch_osm._local_slices()` 看到同名 json 就自动把它并入候选、
自动视为 LOCAL_ONLY（不联网下载）、自动按精确切片优先排序 —— 不用改注册表。

用法（默认**不联网、不自动下载**，源片需先放好）
------------------------------------------------
    python _pbf_clip.py --source kansai \
        --bbox 135.475,33.478,136.012,34.403 --name kumano
    python _pbf_clip.py --source D:/osm/kansai-latest.osm.pbf \
        --bbox 135.475,33.478,136.012,34.403 --name kumano --pad 0.03 --force
    python _pbf_clip.py --source kansai --download --bbox ... --name kumano

⚠ 写入逻辑沿用已验证的尼泊尔合成片做法：node id 必须是**紧凑正整数**，
  否则 sparse_file_array 位置缓存索引不到，osmium.area 拼不出环，
  湖/冰川面会静默消失。way / relation 保留原始 OSM id 便于与 osm.json 去重；
  relation 里只有几何、没有 way 对象的 member 会补建一个 10^12 段的 way。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import osmium

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fetch_osm as F                                             # noqa: E402


def _write_pbf(els, out):
    """把 pbf_extract 的 Overpass-schema 要素写成标准 PBF。返回 (计数, 实际范围)。"""
    pool = {}                  # (lon7, lat7) -> 紧凑 node id
    nodes = []                 # (id, lon, lat, tags)
    xs, ys = [], []

    def cid(lon, lat):
        k = (round(lon, 7), round(lat, 7))
        i = pool.get(k)
        if i is None:
            # ⚠ 必须紧凑正整数：负 id 索引不到、上亿原始 id 撑爆稀疏索引，
            #   两者都会让 AreaManager 拿不到 way 节点坐标 → 面状要素全丢。
            i = len(nodes) + 1
            pool[k] = i
            nodes.append((i, lon, lat, {}))
        xs.append(lon)
        ys.append(lat)
        return i

    # 具名 node 也走同一个池，name / ele 等标签要保住
    for e in els:
        if e["type"] == "node" and "lat" in e and "lon" in e:
            i = cid(e["lon"], e["lat"])
            nid, lon, lat, old = nodes[i - 1]
            nodes[i - 1] = (nid, lon, lat, e.get("tags") or old)

    ways, seen = [], set()
    for e in els:
        if e["type"] != "way":
            continue
        refs = [cid(p["lon"], p["lat"]) for p in e.get("geometry") or []]
        if len(refs) < 2:
            continue
        ways.append((e["id"], refs, e.get("tags") or {}))
        seen.add(e["id"])

    rels = []
    # ⚠ 补建的 member way **不能用负 id**：osmium 的 AreaManager 按
    #   positive_id()（= abs(id)）追踪 way，-1,-2,-3… 取绝对值后变成递减，
    #   二次抽取会报 "Way IDs out of order: -135"。用远大于真实 OSM id
    #   的正数段，既不与原始 way 撞号，也保持严格升序。
    synth = 10 ** 12
    for e in els:
        if e["type"] != "relation":
            continue
        mem = []
        for m in e.get("members", []):
            if m.get("type") != "way":
                continue
            g = m.get("geometry") or []
            if len(g) < 2:
                continue
            ref = m.get("ref")
            if ref is None:
                # pbf_extract 的 area 派生 relation 只给整环几何、不给 way ref
                synth += 1
                ref = synth
            if ref not in seen:
                ways.append((ref, [cid(p["lon"], p["lat"]) for p in g], {}))
                seen.add(ref)
            mem.append(("way", ref, m.get("role") or ""))
        if mem:
            rels.append((e["id"], mem, e.get("tags") or {}))

    if not nodes:
        raise RuntimeError("没有可写的 node，拒绝产出空片")

    # ⚠ libosmium 在 Windows 打不开含中文的输出路径；缓存目录本身是纯 ASCII。
    # ⚠ 临时文件必须以 .osm.pbf 结尾，否则 "Could not detect file format"。
    tmp = out.parent / ("clip-%d.osm.pbf" % int(time.time()))
    w = osmium.SimpleWriter(str(tmp))
    for i, lon, lat, tags in sorted(nodes, key=lambda x: x[0]):
        w.add_node(osmium.osm.mutable.Node(id=i, location=(lon, lat), tags=tags))
    for i, refs, tags in sorted(ways, key=lambda x: x[0]):
        w.add_way(osmium.osm.mutable.Way(id=i, nodes=refs, tags=tags))
    for i, mem, tags in sorted(rels, key=lambda x: x[0]):
        w.add_relation(osmium.osm.mutable.Relation(id=i, members=mem, tags=tags))
    w.close()
    tmp.replace(out)

    actual = [min(xs), min(ys), max(xs), max(ys)] if xs else None
    return ({"nodes": len(nodes), "ways": len(ways), "relations": len(rels)}, actual)


def main():
    ap = argparse.ArgumentParser(description="把官方 PBF 大切片裁成窗口级小 PBF")
    ap.add_argument("--source", required=True, help="切片 slug 或本地 .osm.pbf 路径")
    ap.add_argument("--bbox", required=True, help="窗口 lon0,lat0,lon1,lat1")
    ap.add_argument("--name", required=True, help="产物 slug（<name>-latest.osm.pbf）")
    ap.add_argument("--pad", type=float, default=0.02,
                    help="窗口外扩度数，默认 0.02（约 2 km），避免边缘要素被切掉")
    ap.add_argument("--download", action="store_true",
                    help="源片不存在时显式下载（默认不联网）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的产物")
    args = ap.parse_args()

    try:
        parts = [float(v) for v in args.bbox.split(",")]
        if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
            raise ValueError
    except ValueError:
        ap.error("--bbox 必须是 lon0,lat0,lon1,lat1 四个数")
    bbox = tuple(parts)
    bb = [bbox[0] - args.pad, bbox[1] - args.pad,
          bbox[2] + args.pad, bbox[3] + args.pad]

    cache = F.PBF_CACHE_DIR
    src_arg = Path(args.source)
    if src_arg.suffix in (".pbf", ".osm"):
        src = src_arg
        src_label = src.name
    else:
        slug = args.source
        src = cache / (slug + "-latest.osm.pbf")
        src_label = src.name
        if not src.exists() and args.download:
            print(f"显式下载 {F._pbf_url(slug)} ...")
            got = F._pbf_download(slug)
            if got:
                src = got
    if not src.exists():
        raise SystemExit(
            f"源片不存在：{src}\n"
            f"  先把切片放到缓存目录，或加 --download 显式下载（默认不联网）。\n"
            f"  已登记区域见 fetch_osm.PBF_REGION_BBOX，自定义区域见 regions.json。")

    out = cache / (args.name + "-latest.osm.pbf")
    meta = cache / (args.name + "-latest.json")
    if out.exists() and not args.force:
        raise SystemExit(f"产物已存在：{out}（要覆盖请加 --force）")
    cache.mkdir(parents=True, exist_ok=True)

    print(f"源片 {src_label}（{src.stat().st_size / 1048576:.1f} MB）")
    print(f"抽取窗口 {bb[0]:.4f},{bb[1]:.4f} → {bb[2]:.4f},{bb[3]:.4f}"
          f"（外扩 {args.pad}°）")
    t0 = time.time()
    els = F.pbf_extract(src, bb)
    els = [e for e in els
           if (e.get("tags") or {}).get("boundary") != "administrative"]
    if len(els) < 40:
        raise SystemExit(f"只抽出 {len(els)} 个要素（<40），拒绝产出空片；"
                         f"检查 --bbox / 源片是否覆盖该区域")
    print(f"  抽出 {len(els)} 个要素，耗时 {time.time() - t0:.0f}s")

    counts, actual = _write_pbf(els, out)
    print(f"  ✓ {out.name} {out.stat().st_size / 1048576:.2f} MB  {counts}")

    meta.write_text(json.dumps({
        "_synthetic": True,
        "_note": "窗口级裁剪切片（由官方 PBF 大切片裁出，不是全国片）",
        "_bbox": [round(v, 7) for v in bb],
        "_bbox_actual": [round(v, 7) for v in actual] if actual else None,
        "_source": src_label,
        "_source_bbox": list(bbox),
        "_counts": {"elements": len(els), **counts},
        "_built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ {meta.name}（_bbox 会被 fetch_osm 自动认到）")

    t1 = time.time()
    back = F.pbf_extract(out, bbox, verbose=False)
    print(f"  二次抽取 {len(back)} 个要素 / {time.time() - t1:.1f}s"
          f"（源片抽取 {len(els)} 个）")
    if not back:
        raise SystemExit("裁片二次抽取为 0，写出可能失败；请检查后重跑 --force")
    print(f"完成。此后该窗口走 {args.name} 小片，无需再扫 "
          f"{src.stat().st_size / 1048576:.0f} MB 的源片。")


if __name__ == "__main__":
    main()

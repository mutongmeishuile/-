# -*- coding: utf-8 -*-
"""按「簇」批量抽 OSM 矢量：一次扫片，服务多条线路窗口。

为什么要它（2026-11-02 实测）：单次 PBF 抽取 5–6 分钟里，绝大部分是"把整省读一遍"的
**固定开销**，与窗口面积无关（三个完全不同窗口都是 302–357 s，48 MB 的西藏切片只要 175 s）。
同省 34 条线路各抽一次 = 2.8 h 反复读同一份四川切片。
改成：把相邻的窗口合成一个"簇"→ 扫一次 → 每条线只做**亚秒级裁剪**。

用法（在工作区根目录跑，或 --root 指定）：
    python scripts/cluster_osm.py --dry              # 只打印规划，不抽不写
    python scripts/cluster_osm.py                    # 执行（会下载缺失的省级切片）
    python scripts/cluster_osm.py --only 梅里 --force-cluster
    python scripts/cluster_osm.py --gap-km 25 --suffix _cl   # 试跑：写到 osm_cl.json 不覆盖

判据与代价：
  · 同簇 = 相邻（扩展 gap-km 后仍相交）且候选省集合相同；
  · 簇窗口越大，扫得越慢（∝ 该 bbox 内的数据量），但**只付一次**；
  · 跨省簇会为每个省各扫一次（每片各一份簇缓存），合并时按 (type,id) 去重留顶点多的。
"""
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_osm as F                                              # noqa: E402


def _expand(b, deg):
    return (b[0] - deg, b[1] - deg, b[2] + deg, b[3] + deg)


def _hit(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _union(cl):
    b = cl["_u"]
    for it in cl["items"]:
        c = it["bbox"]
        b = (min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3]))
    return b


def discover(root, only=None):
    out = []
    for p in sorted(root.glob("*/scripts/out/base_meta.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                          # noqa
            continue
        bb = m.get("bbox")
        if not bb or len(bb) != 4:
            continue
        proj = p.parents[2]
        if only and only not in proj.name:
            continue
        out.append({"proj": proj, "bbox": tuple(float(x) for x in bb),
                    "provs": F._candidate_provinces(tuple(float(x) for x in bb))})
    return out


def _diag_km(b):
    """簇窗口的对角线长度（km）—— 用来挡住"全省连成一片"的巨型簇。"""
    import math
    midlat = (b[1] + b[3]) / 2
    dx = (b[2] - b[0]) * 111.32 * math.cos(math.radians(midlat))
    dy = (b[3] - b[1]) * 110.57
    return math.hypot(dx, dy)


def _overlap_frac(bb, prov):
    """窗口与某省粗框的重叠面积占比。"""
    x0, y0, x1, y1 = F.PROVINCE_BBOX[prov]
    area = max(1e-12, (bb[2] - bb[0]) * (bb[3] - bb[1]))
    ow = max(0.0, min(bb[2], x1) - max(bb[0], x0))
    oh = max(0.0, min(bb[3], y1) - max(bb[1], y0))
    return ow * oh / area


def plan_by_province(items):
    """按"主省"分组：一组 = 主省相同的所有线路。

    为什么这比"按邻接"更省：扫描耗时几乎只取决于**整省要读多少数据**，与 bbox 无关
    （实测三个完全不同窗口 302/302/357 s，48 MB 的西藏切片 175 s）。所以把川西 34 条
    合成一个覆盖大半个川西的 bbox，扫描时间几乎不变，扫描次数从 34 降到 1。
    代价只是簇缓存的 JSON 更大（几万要素、几十 MB），可接受。
    """
    by = {}
    for it in items:
        if not it["provs"]:
            print(f"   跳过 {it['proj'].name}：境外/未覆盖省份")
            continue
        main = max(it["provs"], key=lambda p: _overlap_frac(it["bbox"], p))
        by.setdefault(main, []).append(it)
    clusters = []
    for main, its in sorted(by.items()):
        b = its[0]["bbox"]
        for it in its[1:]:
            c = it["bbox"]
            b = (min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3]))
        provs = sorted({p for it in its for p in it["provs"]})
        clusters.append({"provs": provs, "items": its, "_u": b, "main": main})
    return clusters


def plan(items, gap_deg, max_diag_km=900.0, max_items=40):
    """把相邻窗口合成簇。

    合并判据（2026-11-02 修正）：**候选省集合有交集 + 扩展后窗口相交**。
    早先要求"省份集合完全相同"，会漏掉 {sichuan,yunnan} 与 {sichuan} 这种相邻组合 ——
    而它们的成本是 |A∪B|=2 次扫描 vs 分开 3 次，合起来本身就赚。
    上限：簇窗口对角线 ≤ max_diag_km。**给得很大（900 km）是有依据的**：
      扫描耗时几乎只取决于"整省要读多少数据"，与 bbox 大小无关（实测三个完全不同窗口
      都是 302–357 s）—— 所以把同省的线合并成一个大簇几乎不额外花钱，扫描次数却从
      N 次降到 1 次。真正的开销在"每扫一次就要把整省过一遍"。
    """
    clusters = []
    for it in sorted(items, key=lambda x: (x["bbox"][0], x["bbox"][1])):
        if not it["provs"]:
            print(f"   跳过 {it['proj'].name}：境外/未覆盖省份")
            continue
        best, best_diag = None, None
        for cl in clusters:
            if len(cl["items"]) >= max_items:
                continue
            if not (set(cl["provs"]) & set(it["provs"])):
                continue
            if not _hit(_expand(cl["_u"], gap_deg), it["bbox"]):
                continue
            b = cl["_u"]
            u = (min(b[0], it["bbox"][0]), min(b[1], it["bbox"][1]),
                 max(b[2], it["bbox"][2]), max(b[3], it["bbox"][3]))
            d = _diag_km(u)
            if d > max_diag_km:
                continue
            if best is None or d < best_diag:
                best, best_diag = cl, d
        if best is None:
            clusters.append({"provs": list(it["provs"]), "items": [it], "_u": it["bbox"]})
        else:
            best["items"].append(it)
            best["provs"] = sorted(set(best["provs"]) | set(it["provs"]))
            best["_u"] = _union(best)
    return clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="工作区根目录（默认当前目录）")
    ap.add_argument("--only", default=None, help="只处理名字含该子串的项目")
    ap.add_argument("--province", default=None,
                    help="只处理候选省包含该省的项目（如 xinjiang）—— 单簇验证用")
    ap.add_argument("--gap-km", type=float, default=24.0,
                    help="簇合并的间隙（公里）：两个窗口扩这么远还能相交 → 同簇（默认 20）")
    ap.add_argument("--suffix", default="", help="输出文件名后缀（试跑用 _cl，不覆盖 osm.json）")
    ap.add_argument("--force-cluster", action="store_true", help="忽略已有簇缓存重扫")
    ap.add_argument("--group", choices=("province", "adjacency"), default="province",
                    help="分组方式：province=按主省分组（默认，最省）；"
                         "adjacency=只合并相邻窗口（簇更小、单片扫得更快）")
    ap.add_argument("--dry", action="store_true", help="只打印规划")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    items = discover(root, a.only)
    if a.province:
        items = [it for it in items if a.province in it["provs"]]
        print(f"（--province {a.province}：筛出 {len(items)} 条）")
    if not items:
        raise SystemExit(f"{root} 下没有找到任何 */scripts/out/base_meta.json")
    gap_deg = a.gap_km / 111.0
    clusters = plan_by_province(items) if a.group == "province" else plan(items, gap_deg)

    n_scan = sum(len({p for it in cl["items"] for p in it["provs"]}) for cl in clusters)
    print(f"\n{len(items)} 条线路 → {len(clusters)} 个簇；需要扫 {n_scan} 次切片"
          f"（散着做要扫 {sum(len(it['provs']) for it in items)} 次）")
    for i, cl in enumerate(clusters, 1):
        u = cl["_u"]
        names = "、".join(it["proj"].name.replace("攻略", "") for it in cl["items"])
        print(f"  簇{i}: {len(cl['items'])} 条 · 省 {cl['provs']} · "
              f"{u[0]:.2f},{u[1]:.2f}→{u[2]:.2f},{u[3]:.2f}（{len(names)} 个字）")
        print(f"        {names}")
    if a.dry:
        return

    t_all = time.time()
    for i, cl in enumerate(clusters, 1):
        u = cl["_u"]
        provs = sorted({p for it in cl["items"] for p in it["provs"]})
        print(f"\n=== 簇{i}/{len(clusters)}：{len(cl['items'])} 条 · 扫 {provs}")
        cache = {}
        for prov in provs:
            t0 = time.time()
            dst = F.cluster_extract(prov, u, force=a.force_cluster)
            if dst:
                cache[prov] = json.loads(dst.read_text(encoding="utf-8"))
                print(f"   {prov}: {len(cache[prov]['elements'])} 个要素 "
                      f"（{time.time() - t0:.0f}s）")
        if not cache:
            print("   ✗ 这个簇一片都没扫成，跳过")
            continue
        for it in cl["items"]:
            els_map = {}
            for prov in it["provs"]:
                d = cache.get(prov)
                if not d:
                    continue
                for e in F.clip_els(d["elements"], it["bbox"]):
                    k = (e["type"], e["id"])
                    old = els_map.get(k)
                    if old is None or F._pt_count(e) > F._pt_count(old):
                        els_map[k] = e
            els = list(els_map.values())
            old_n = 0
            tgt = it["proj"] / "scripts" / "out" / f"osm{a.suffix}.json"
            if tgt.exists():
                try:
                    old_n = len(json.loads(tgt.read_text(encoding="utf-8")).get("elements") or [])
                except Exception:                                  # noqa
                    old_n = 0
            out = {"elements": els, "_source": "pbf", "_province": "+".join(it["provs"]),
                   "_pbf": "cluster", "_bbox": list(it["bbox"]),
                   "_cluster": "+".join(sorted(cache)),
                   "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            tgt.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            print(f"   {it['proj'].name}: {old_n} → {len(els)} 个要素"
                  f"  ({tgt.stat().st_size // 1024} KB)")

    print(f"\n总耗时 {time.time() - t_all:.0f}s")
    if not a.suffix:
        print("⚠ 已覆盖各项目的 out/osm.json → 必须重跑 make_terrain.py 与三件套")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""地图窗口自检 —— 「名峰是不是被窗口切在图外」。

用法:
    python scripts/check_window.py             # 人读
    python scripts/check_window.py --json      # 机器可读
    python scripts/check_window.py --km 5      # 列出窗口外 5 km 内的名峰（默认 5）

为什么需要这个:
    窗口由 `GC.resolve_bbox()` 从**轨迹包围盒 + 留白**自动推（默认 pad 0.35）。
    轨迹只走东坡/西坡一侧时，线路名字里那座主峰可能在窗口外几百米 —— 底图、等高线、
    峰点数据全都正常（`osm.json` 里也确实有这个峰），只是**画不出来**。
    2026-10-25 珠峰东坡就是这样：窗口 lon0=86.9319，珠峰在 86.9252，**差 663 m 在图外**；
    洛子峰只进来 5 px（贴左边缘），章子峰差 1.4 km 也在外面 —— 而图里最高的名峰是马卡鲁。

判据只有一句:
    窗口外 `--km` 范围内存在名峰，且高程**高于**窗口内最高名峰 → 报红（exit 1）。
    （"图里最高的山不是它" —— 用户一眼就看出来不对。）

修法（报红时）:
    `route_def.CFG["bbox"]` 显式给窗口（死坐标，轨迹一改要重给），或抬 `CFG["bbox_pad"]`。
    改完 bbox 必须 `fetch_osm.py --force`（旧缓存不覆盖会沿用旧窗口的要素）+ 重跑 make_terrain。
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQ = 111320.0


def _ele(tags):
    raw = (tags.get("ele") or "").strip().replace(",", "")
    # ele 偶尔写成 "8848.86 m" / "8848 m"
    for tok in raw.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def _nm(tags):
    return (tags.get("name") or tags.get("name:zh") or tags.get("name:en") or "").strip()


def load(meta_p, osm_p):
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    lon0, lat0, lon1, lat1 = meta["bbox"]
    osm = json.loads(osm_p.read_text(encoding="utf-8"))
    peaks = []
    for e in osm.get("elements", []):
        t = e.get("tags", {}) or {}
        if t.get("natural") != "peak":
            continue
        nm, ele = _nm(t), _ele(t)
        if not nm or ele is None:
            continue
        peaks.append({"name": nm, "ele": ele, "lon": float(e["lon"]), "lat": float(e["lat"])})
    return meta, (lon0, lat0, lon1, lat1), peaks


def margins(lon, lat, bb):
    """四个方向的余量（米，正=在内）。顺序 西/南/东/北。"""
    lon0, lat0, lon1, lat1 = bb
    mx = M_PER_DEG_LON_EQ * math.cos(math.radians(lat))
    return ((lon - lon0) * mx, (lat - lat0) * M_PER_DEG_LAT,
            (lon1 - lon) * mx, (lat1 - lat) * M_PER_DEG_LAT)


def out_dist(ms):
    """到窗口边界的距离（米）：完全在内返回 0。"""
    return max(0.0, -min(ms))


def main():
    as_json = "--json" in sys.argv
    km = 5.0
    if "--km" in sys.argv:
        try:
            km = float(sys.argv[sys.argv.index("--km") + 1])
        except (IndexError, ValueError):
            pass
    meta_p = HERE / "out" / "base_meta.json"
    osm_p = HERE / "out" / "osm.json"
    for p in (meta_p, osm_p):
        if not p.exists():
            print(f"✗ 缺 {p.name} —— 先跑 make_terrain.py / fetch_osm.py")
            return 2

    meta, bb, peaks = load(meta_p, osm_p)
    rows = []
    for p in peaks:
        ms = margins(p["lon"], p["lat"], bb)
        rows.append(dict(p, margins=[round(v) for v in ms], inside=min(ms) > 0,
                         dist_out=round(out_dist(ms))))
    ins = [r for r in rows if r["inside"]]
    outs = sorted([r for r in rows if not r["inside"]], key=lambda r: -r["ele"])
    hi_in = max((r["ele"] for r in ins), default=None)
    near = [r for r in outs if r["dist_out"] <= km * 1000]
    alarm = [r for r in near if hi_in is not None and r["ele"] > hi_in]

    if as_json:
        print(json.dumps({"bbox": list(bb), "named_peaks": len(rows),
                          "inside": len(ins), "outside": len(outs),
                          "highest_inside": hi_in, "outside_near": near,
                          "alarm": [r["name"] for r in alarm]},
                         ensure_ascii=False, indent=2))
        return 1 if alarm else 0

    lon0, lat0, lon1, lat1 = bb
    print(f"窗口 {lon0:.4f},{lat0:.4f} – {lon1:.4f},{lat1:.4f}   "
          f"({(lon1-lon0)*M_PER_DEG_LON_EQ*math.cos(math.radians((lat0+lat1)/2))/1000:.1f}"
          f" × {(lat1-lat0)*M_PER_DEG_LAT/1000:.1f} km)")
    print(f"具名峰 {len(rows)} 个：窗口内 {len(ins)} / 窗口外 {len(outs)}；"
          f"窗口内最高 {hi_in:.0f} m" if hi_in else "具名峰 0 个")
    print("\n窗口外名峰（按高程，取前 8）：")
    for r in outs[:8]:
        flag = "⚠" if r["ele"] > (hi_in or 0) else " "
        print(f"  {flag} {r['name'][:22]:24s} {r['ele']:6.0f} m  出界 {r['dist_out']:6.0f} m"
              f"   余量 西{r['margins'][0]:6d} 南{r['margins'][1]:6d}"
              f" 东{r['margins'][2]:6d} 北{r['margins'][3]:6d}")

    if alarm:
        print(f"\n✗ {len(alarm)} 个名峰在窗口外 {km:g} km 内、却高于窗口内最高峰"
              f"（{hi_in:.0f} m）：")
        for r in alarm:
            print(f"    {r['name']} {r['ele']:.0f} m，出界 {r['dist_out']/1000:.2f} km")
        print("  → 用户会直接说「这里怎么没有 XX」。给 route_def.CFG['bbox'] 显式窗口，"
              "或抬 CFG['bbox_pad']；\n"
              "    改完 **必须** fetch_osm.py --force 再重跑 make_terrain（窗口变了旧要素缓存不算数）。")
        return 1
    if near:
        print(f"\n· 窗口外 {km:g} km 内有 {len(near)} 个名峰，但都低于图内最高峰，属正常留白。")
    print("\n✓ 通过：窗口外的名峰都不比图里的山高。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

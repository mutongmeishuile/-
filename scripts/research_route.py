# -*- coding: utf-8 -*-
"""研究提纲生成器：把本地已有数据变成「可联网核实」的清单。

为什么需要这一步（自主生成的关键缺口）：
    new_route.py 只负责把机械数字（里程/爬升/海拔/分段/POI 坐标）填进骨架，
    地名核实、许可、水源、营地、装备、文案这些**必须联网或凭经验**的内容
    仍写成 `★ TODO`。过去这一步靠人补。本脚本把"该去查什么"先列清楚，
    模型（或人）照着清单用 WebSearch / 实地资料回填 route_def 即可，
    从而做到"只依赖技能 + 一份轨迹"就能自主产出攻略。

输入（都在 scripts/out/）：
    track_real.json  轨迹简化点（含 km / ele / lat / lon）
    kml_pois.json    用户在轨迹 app 里标的途经点（营地/水源/木屋…）
    osm.json         Overpass 抓的具名地物（湖/河/村/峰…）
输出：
    控制台打印一份研究提纲；同时写 out/research_brief.md（可直接当回填清单）。

用法：
    python research_route.py                 # 自动读 out/
    python research_route.py --region 乌孙古道   # 给清单里的检索词加地域前缀
    python research_route.py --max-offset 8    # 只列距线路 N km 内的 OSM 地物
"""
import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def load_track():
    p = OUT / "track_real.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    pts = d.get("pts") or []
    return {
        "pts": [(pt["lat"], pt["lon"], pt.get("km", 0.0), pt.get("ele"))
                for pt in pts],
        "total_km": d.get("total_km"),
        "asc": d.get("asc"), "desc": d.get("desc"),
        "ele_min": d.get("ele_min"), "ele_max": d.get("ele_max"),
    }


def nearest(pt_lat, pt_lon, track):
    """返回 (沿程 km, 横向偏移 km, 海拔)。"""
    best = (1e9, 0.0, None)
    for lat, lon, km, ele in track["pts"]:
        d = haversine(pt_lat, pt_lon, lat, lon)
        if d < best[0]:
            best = (d, km, ele)
    return best[1], best[0], best[2]


def load_kml_pois():
    p = OUT / "kml_pois.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def load_osm():
    p = OUT / "osm.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for e in d.get("elements", []):
        t = e.get("tags", {})
        name = t.get("name")
        if not name:
            continue
        # 代表点：node 用自身；way 用几何质心（河流/湖泊用质心足够近）
        if e.get("type") == "node":
            lat, lon = e.get("lat"), e.get("lon")
        else:
            geom = e.get("geometry") or []
            if not geom:
                continue
            lat = sum(g["lat"] for g in geom) / len(geom)
            lon = sum(g["lon"] for g in geom) / len(geom)
        if lat is None or lon is None:
            continue
        kind = next((t[k] for k in ("place", "natural", "waterway", "highway",
                                    "tourism", "amenity", "landuse") if k in t), "地物")
        out.append({"name": name, "kind": kind, "lat": lat, "lon": lon})
    return out


def load_route_ctx():
    """尽量从 route_def 取标题与每日分界，丰富提纲（取不到就跳过）。"""
    try:
        sys.path.insert(0, str(HERE))
        import route_def as RD  # noqa
        cfg = getattr(RD, "CFG", {})
        title = cfg.get("title", "")
        days = cfg.get("days", 0)
        # 每日分界 km（SCHEDULE 里第 0 项通常是 D1 起点；用 day_cards 数量推断）
        return {"title": title, "days": days}
    except Exception:  # noqa
        return {"title": "", "days": 0}


def fmt_coord(lat, lon):
    return f"{lat:.4f},{lon:.4f}"


def build_brief(region, max_offset):
    track = load_track()
    pois = load_kml_pois()
    osm = load_osm()
    ctx = load_route_ctx()
    days_raw = ctx.get("days")
    n_days = days_raw if isinstance(days_raw, int) else (len(days_raw) if days_raw else 0)
    water_names, shelter_names = [], []

    L = []
    w = L.append
    w(f"# 路线研究提纲{' · ' + region if region and region != '本线' else ''}\n")
    w("> 由 `research_route.py` 自动生成。把下面清单逐条联网核实后，回填进 "
      "`route_def.py` 里的 `★ TODO` 占位，再跑 `make_all.py`。\n")

    # 0. 概览
    w("## 0. 路线概览")
    if track:
        w(f"- 总里程 **{track['total_km']:.1f} km** ｜ 累计爬升 "
          f"+{track['asc']} / 下降 −{track['desc']} m ｜ 海拔 "
          f"{track['ele_min']}–{track['ele_max']} m")
    else:
        w("- ⚠ 未找到 out/track_real.json，先跑 `prep_track.py`。")
    if ctx["title"]:
        w(f"- 标题（route_def）：{ctx['title']}")
    if n_days:
        w(f"- 建议分 {n_days} 天（按日均~20km 自动估算，**请联网核实该线路真实常见天数**）")
    w("")

    # 1. 用户标注点（按里程）
    w("## 1. 用户在轨迹里标的途经点（= 营地/水源/庇护所候选，按里程排序）")
    w("> 这些是你自己标的，攻略的「水源情况 / 住宿营地 / 体力提示」要逐一对应上。\n")
    if track and pois:
        rows = []
        for x in pois:
            if x.get("lat") is None or x.get("lon") is None:
                continue
            km, off, ele = nearest(x["lat"], x["lon"], track)
            rows.append((km, x.get("name", "?"), ele, off,
                         fmt_coord(x["lat"], x["lon"])))
        rows.sort()
        w("| 里程 km | 名称 | 海拔 m | 距线路 km | 坐标 |")
        w("|---|---|---|---|---|")
        for km, name, ele, off, c in rows:
            w(f"| {km:.1f} | {name} | {ele if ele is not None else '—'} "
              f"| {off:.2f} | {c} |")
        # 挑出像营地/水源的提示
        water = [r for r in rows if any(k in r[1] for k in ("溪", "河", "湖", "水"))]
        shelter = [r for r in rows if any(k in r[1] for k in ("屋", "包", "营", "棚", "家", "店", "寺"))]
        water_names[:] = [r[1] for r in water]
        shelter_names[:] = [r[1] for r in shelter]
        w("")
        w(f"- 疑似水源点（{len(water)}）：" + "、".join(water_names[:20]) or "（无）")
        w(f"- 疑似庇护/补给（{len(shelter)}）：" + "、".join(shelter_names[:20]) or "（无）")
    else:
        w("- ⚠ 未找到 out/kml_pois.json。")
    w("")

    # 2. OSM 具名地物
    w("## 2. OSM 官方具名地物（核对标准地名）")
    w(f"> 仅列距线路 ≤ {max_offset} km 的；其余见 out/osm.json。\n")
    if track and osm:
        rows = []
        for o in osm:
            km, off, _ = nearest(o["lat"], o["lon"], track)
            if off <= max_offset:
                rows.append((km, o["name"], o["kind"], off,
                             fmt_coord(o["lat"], o["lon"])))
        rows.sort()
        w("| 里程 km | 名称 | 类型 | 距线路 km | 坐标 |")
        w("|---|---|---|---|---|")
        for km, name, kind, off, c in rows:
            w(f"| {km:.1f} | {name} | {kind} | {off:.2f} | {c} |")
        w("")
        w("- 重点核对：" + "、".join(r[1] for r in rows[:15]) or "（无）")
    else:
        w("- ⚠ 未找到 out/osm.json（fetch_osm 失败或被跳过）。")
    w("")

    # 3. 联网核实清单
    w("## 3. 联网核实清单（逐条填进 route_def 的 ★ TODO）")
    w("用 WebSearch / 户外社区（8264、两步路、莉景/六只脚、小红书/抖音攻略、"
      "官方保护区公告）逐条核实：\n")
    R = region if region and region != "本线" else "<线路名>"
    w(f"- [ ] **保护区 / 许可 / 门票**：搜「{R} 保护区 规定 / 进山许可 / 门票价格」")
    w(f"- [ ] **是否野线 / 景区大门 / 明火露营限制**：搜「{R} 是否野线 / 景区 / 露营规定」")
    if osm:
        rivers = [r[1] for r in (rows if track and osm else []) if "river" in r[2] or "water" in r[2]]
        if rivers:
            w(f"- [ ] **过河 / 涉水风险**：搜「{'、'.join(rivers[:3])} 过河 时机 / 水位 / 危险」")
    if track and pois:
        focus = "、".join(shelter_names[:6]) or "上面庇护/补给点"
        w(f"- [ ] **营地与水源**：围绕第 1 节的标注点，搜「<标注名> 营地 / 水源 / 海拔」"
          f"（重点：{focus}）")
    w(f"- [ ] **天气与装备**：搜「{R} 徒步 最佳季节 / 天气风险 / 必带装备」")
    w(f"- [ ] **信号与下撤**：搜「{R} 徒步 手机信号 / 下撤路线 / 救援」")
    w(f"- [ ] **天数核实**：骨架已按 {n_days or 'N'} 天切分（按日均~20km 估算）。"
      f"联网确认该线路**常见天数**——乌孙古道常见 6 天（6–8 天区间），与骨架不符就先 "
      f"`python new_route.py --days M` 重生成骨架，再回填本清单，避免把百公里长线切成 2 天。")
    w(f"- [ ] **各日卡片**：把第 1 节标注点按里程分配到 D1..D{n_days or 'n'}，"
      f"逐日写「水源情况 / 住宿营地 / 体力提示」")
    w(f"- [ ] **安全提示脚注**：综合上面的风险，写一句最要紧的提醒")
    w("")

    text = "\n".join(L)
    out_md = OUT / "research_brief.md"
    out_md.write_text(text, encoding="utf-8")
    return text, out_md


def main():
    ap = argparse.ArgumentParser(description="生成路线研究提纲（自主生成的关键一步）")
    ap.add_argument("--region", default="本线", help="地域前缀，用于检索词（默认 本线）")
    ap.add_argument("--max-offset", type=float, default=6.0,
                    help="只列距线路该 km 数内的 OSM 地物（默认 6）")
    a = ap.parse_args()
    text, out_md = build_brief(a.region, a.max_offset)
    print(text)
    print(f"\n—— 提纲已写入 {out_md.name} ——")


if __name__ == "__main__":
    main()

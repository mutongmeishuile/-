# -*- coding: utf-8 -*-
"""导出可导航 GPX：全量轨迹 <trkpt> + 具名航点 <wpt>。

产物：`<ROOT>/<file_stem>.gpx`，可直接导入手表 / 两步路 / Garmin。

三个必须记住的点（都踩过）：
  1. **航点不要直接搬 KML 注记** —— 里面混着「3550」「回望某某垭口」这类随手标注，
     导进手表就是噪声。用**已核验过的规范 POI 列表**（route_def.POIS）。
     POIS 允许**元组或字典**两种写法（`(name, lon, lat, ele, kind, …)` 或同名键的 dict），
     由 `guide_common.poi()` 统一归一化 —— 别再自己下标取值。
  2. **时间戳要写 UTC**（`YYYY-MM-DDTHH:MM:SSZ`）。轨迹里存的是北京时间（UTC+8），
     取 `HH:MM:SS` 字符串直接反推会让手表整体偏 8 小时。
  3. **分成几段就把段界点归到前一天**，别把界点写两遍，否则 trkpt 总数会多出"段数−1"个。
     导出后用 xml.etree 回读断言 trkpt 数 == 原始点数。

轨迹点取自 out/track_full.json（**原始全量点**，不是简化后的 —— 导航要的是精度）。
"""
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import guide_common as GC
import route_def as RD

CFG = getattr(RD, "CFG", {})
STEM = CFG.get("file_stem", "线路")
OUT_GPX = GC.ROOT / f"{STEM}.gpx"
CN = timezone(timedelta(hours=8))


def _find(name):
    for p in (GC.OUT / name, GC.HERE / name, GC.HERE / "kml" / name):
        if p.exists():
            return p
    raise FileNotFoundError(f"找不到 {name} —— 先跑 parse_track_kml.py")


def _utc(ts):
    """把 track_full.json 里的 '# HH:MM:SS' 转成 UTC ISO8601。

    parse_track_kml.py 已把 KML 的 UTC 时间 +8h 存成北京时间，
    这里再 -8h 还原成 UTC 写进 GPX（GPX 规范要求 UTC）。
    """
    if not ts:
        return None
    s = ts.split(" ")[-1].strip()
    try:
        t = datetime.strptime(s, "%H:%M:%S")
    except ValueError:
        return None
    return (datetime(2000, 1, 1, t.hour, t.minute, t.second, tzinfo=CN)
            .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def main():
    if not getattr(RD, "TRACK_READY", True):
        raise SystemExit("轨迹数据未就绪：先跑 python prep_track.py <你的.kml>")
    data = json.loads(_find("track_full.json").read_text(encoding="utf-8"))
    pts = data["pts"]
    cum = data.get("cum_m") or [0.0]

    # 分段：按 route_def 的分日界里程切
    bounds = list(CFG.get("day_bounds", [])) or []
    if not bounds and hasattr(RD, "SPLIT1_KM"):
        bounds = [RD.SPLIT1_KM]
    idx = sorted({next((i for i, k in enumerate(cum) if k / 1000.0 >= b), len(pts) - 1)
                  for b in bounds} | {0, len(pts)})

    gpx = ET.Element("gpx", {
        "version": "1.1", "creator": "hiking-route-guide-poster",
        "xmlns": "http://www.topografix.com/GPX/1/1",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:schemaLocation": "http://www.topografix.com/GPX/1/1 "
                              "http://www.topografix.com/GPX/1/1/gpx.xsd"})
    md = ET.SubElement(gpx, "metadata")
    ET.SubElement(md, "name").text = CFG.get("title", STEM)
    ET.SubElement(md, "desc").text = CFG.get("meta", "")

    trk = ET.SubElement(gpx, "trk")
    ET.SubElement(trk, "name").text = CFG.get("title", STEM)
    n_pt = 0
    for a, b in zip(idx[:-1], idx[1:]):
        if b <= a:
            continue
        seg = ET.SubElement(trk, "trkseg")
        for p in pts[a:b]:
            lon, lat, ele = p[0], p[1], p[2]
            tp = ET.SubElement(seg, "trkpt", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"})
            if ele is not None:
                ET.SubElement(tp, "ele").text = f"{ele:.1f}"
            t = _utc(p[3] if len(p) > 3 else None)
            if t:
                ET.SubElement(tp, "time").text = t
            n_pt += 1

    for d in GC.pois_of(RD):
        # POIS 允许元组或字典两种写法（GC.poi 已归一化）；ele 缺省时不写 <ele>，
        # 别硬塞一个 0 —— 手表会把它当成"海拔 0 m 的航点"。
        w = ET.SubElement(gpx, "wpt",
                          {"lat": f"{float(d['lat']):.6f}", "lon": f"{float(d['lon']):.6f}"})
        if d["ele"] is not None:
            ET.SubElement(w, "ele").text = f"{float(d['ele']):.1f}"
        ET.SubElement(w, "name").text = d["name"]
        ET.SubElement(w, "desc").text = str(d["kind"])
        ET.SubElement(w, "sym").text = ("Flag, Blue" if d["kind"] in ("start", "end")
                                        else "Pin")

    ET.indent(gpx, space=" ")
    OUT_GPX.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                       + ET.tostring(gpx, encoding="unicode"), encoding="utf-8")

    # ---- 回读自检（300 KB 文本里肉眼数不出 trkpt）----
    root = ET.parse(OUT_GPX).getroot()
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    got_pt = len(root.findall(".//g:trkpt", ns))
    got_wpt = len(root.findall(".//g:wpt", ns))
    got_seg = len(root.findall(".//g:trkseg", ns))
    print(f"{got_pt} trkpt / {got_wpt} wpt / {got_seg} trkseg")
    assert got_pt == n_pt == len(pts), f"trkpt 数 {got_pt} != 原始点数 {len(pts)}"
    assert got_wpt == len(getattr(RD, "POIS", [])), "wpt 数与 POIS 不符"
    print(f"-> {OUT_GPX.name}  {OUT_GPX.stat().st_size/1024:.0f} KB  自检通过 ✓")


if __name__ == "__main__":
    main()

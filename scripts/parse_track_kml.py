# -*- coding: utf-8 -*-
"""解析两步路（2bulu）导出的 KML，抽出真实轨迹与标注点，供后续绘图使用。

输入：两步路 App「导出轨迹 → KML」得到的 *.kml（Document id = TbuluKmlVersion2）
输出（写到 KML 同目录）：
    track_full.json —— {"pts": [[lon,lat,ele,"hh:mm:ss"], ...], "cum_m": [...]}
    kml_pois.json   —— [{"name","lon","lat","ele","desc"}, ...]  具名标注点

用法：
    python parse_track_kml.py "D:/路径/九华山南北穿越.kml"        # 解析并落盘
    python parse_track_kml.py "D:/路径/xx.kml" --srtm             # 无高程时用 srtm 补（需联网）

要点（踩过的坑）：
  * 两步路把轨迹存成 <gx:Track>，坐标在 <gx:coord>（顺序是 lon lat ele，空格分隔），
    时间戳在并列的 <when>（UTC，形如 2026-05-31T08:36:01Z），必须按出现顺序一一配对。
  * 部分轨迹没有 ele（coord 只有两个数），此时高程留空，交给 --srtm 或后续用 11 点滑动平均兜底。
  * 标注点在 <Placemark> 里，name 是地名，description 可能含 <br> 等 HTML，需去标签。
  * 解析用 ElementTree + 命名空间通配（'*'），不要硬编 gx 前缀（不同导出器前缀会变）。
"""
import sys, re, json, argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def find_all(elem, local):
    """按 local name 取所有后代（忽略命名空间前缀）。"""
    return elem.findall(f".//{{*}}{local}")


def parse_kml(path: Path):
    tree = ET.parse(path)
    root = tree.getroot()

    # ---- 1. 轨迹：收集所有 gx:coord 与 gx:when，按文档顺序配对 ----
    coords, whens = [], []
    for trk in root.iter():
        if trk.tag.endswith("}Track") or trk.tag == "Track":
            for c in trk:
                if c.tag.endswith("}coord") or c.tag == "coord":
                    coords.append(c.text.strip())
                elif c.tag.endswith("}when") or c.tag == "when":
                    whens.append(c.text.strip())

    pts, cum = [], [0.0]
    R = 6371008.8
    import math
    for i, c in enumerate(coords):
        parts = c.split()
        lon, lat = float(parts[0]), float(parts[1])
        ele = float(parts[2]) if len(parts) > 2 else None
        # UTC -> 北京时间（UTC+8），只取 hh:mm:ss
        t = ""
        if i < len(whens):
            try:
                dt = datetime.strptime(whens[i], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                t = dt.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
            except ValueError:
                t = whens[i]
        pts.append([lon, lat, ele, t])
        if i:
            a, b = pts[i - 1], pts[i]
            lo1, la1 = math.radians(a[0]), math.radians(a[1])
            lo2, la2 = math.radians(b[0]), math.radians(b[1])
            h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
            cum.append(cum[-1] + 2 * R * math.asin(math.sqrt(h)))

    # ---- 2. 标注点：Placemark 里有坐标且不是轨迹本身 ----
    pois = []
    for pm in find_all(root, "Placemark"):
        name_el = pm.find("{*}name")
        coord_el = pm.find(".//{*}coordinates")
        if name_el is None or coord_el is None:
            continue
        raw = (coord_el.text or "").strip()
        if not raw or "\n" in raw:      # 轨迹 Placemark 的 coordinates 是一大串多行数据，跳过
            continue
        first = raw.split()[0].split(",")
        if len(first) < 2:
            continue
        desc_el = pm.find("{*}description")
        pois.append({
            "name": strip_tags(name_el.text),
            "lon": float(first[0]),
            "lat": float(first[1]),
            "ele": float(first[2]) if len(first) > 2 and first[2] else None,
            "desc": strip_tags(desc_el.text) if desc_el is not None else "",
        })

    return pts, cum, pois


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kml", help="两步路导出的 .kml 路径")
    ap.add_argument("--out", default=None, help="输出目录（默认 KML 同目录）")
    args = ap.parse_args()

    src = Path(args.kml)
    out = Path(args.out) if args.out else src.parent
    out.mkdir(parents=True, exist_ok=True)

    pts, cum, pois = parse_kml(src)
    if not pts:
        sys.exit("未在 KML 中找到 <gx:Track> 轨迹点，请确认是两步路导出的轨迹文件。")

    (out / "track_full.json").write_text(
        json.dumps({"pts": pts, "cum_m": [round(v, 2) for v in cum]}, ensure_ascii=False),
        encoding="utf-8")
    (out / "kml_pois.json").write_text(json.dumps(pois, ensure_ascii=False, indent=1), encoding="utf-8")

    ele = [p[2] for p in pts if p[2] is not None]
    print(f"轨迹点 {len(pts)}  里程 {cum[-1]/1000:.2f} km  时长 {pts[0][3]}–{pts[-1][3]}")
    if ele:
        print(f"原始高程 {min(ele):.0f}–{max(ele):.0f} m")
    print(f"标注点 {len(pois)}")
    print(f"→ {out/'track_full.json'}  /  {out/'kml_pois.json'}")
    print("下一步：把 track_full.json 放到 prep_kml_track.py 同目录后运行它，得到 track_real.json / profile_real.json")


if __name__ == "__main__":
    main()

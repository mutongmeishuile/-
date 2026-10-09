# -*- coding: utf-8 -*-
"""解析真实轨迹文件（两步路 KML / 标准 GPX），抽出轨迹点与具名标注，供后续绘图使用。

**支持多种输入，按内容自动识别，不用换脚本**：
  ① 两步路 KML「<gx:Track> + <gx:coord>」（带 <when> 时间戳）—— 主用
  ② 标准 GPX（手表 / 其它 App 导出：`<trkpt lat lon><ele><time>` + `<wpt>`）
  ③ 分段式 KML（多个 <Placemark>/<LineString>）—— **不在这里处理**：
     它没有时间戳、段间还可能有几百米断点，走 `parse_kml_ls.py`

输出（一律写 scripts/out/，与下游约定一致）：
    track_full.json —— {"pts": [[lon,lat,ele,"hh:mm:ss"], ...], "cum_m": [...]}
    kml_pois.json   —— [{"name","lon","lat","ele","desc"}, ...]  具名标注点

用法：
    python parse_track_kml.py "D:/路径/线路.kml"        # 两步路 KML
    python parse_track_kml.py "D:/路径/线路.gpx"        # 手表导出的 GPX，同一条命令
    python parse_track_kml.py "D:/路径/线路.kml" --out ./out

要点（踩过的坑）：
  * 两步路把轨迹存成 <gx:Track>，坐标在 <gx:coord>（顺序是 lon lat ele，空格分隔），
    时间戳在并列的 <when>（UTC，形如 2026-05-31T08:36:01Z），必须按出现顺序一一配对。
  * GPX 用**属性**存经纬度（`<trkpt lat=".." lon="..">`），<ele>/<time> 是子元素。
    两种格式的时间都统一转成**北京时间 hh:mm:ss**，输出 schema 完全一致 ——
    所以下游 prep_kml_track / make_gpx / map_svg 一行都不用改。
  * 部分轨迹没有 ele（coord 只有两个数 / GPX 无 <ele>），此时高程留空，
    由后续 11 点滑动平均兜底。
  * 标注点在 KML 的 <Placemark> / GPX 的 <wpt> 里；description 可能含 <br> 等 HTML，需去标签。
  * 解析用 ElementTree + 命名空间通配（'*'），不要硬编 gx 前缀（不同导出器前缀会变）。
"""
import sys, re, json, math, argparse
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


def _lname(e):
    """元素的 local name（去掉 `{ns}` 前缀）。"""
    return e.tag.split("}")[-1]


def _text_of(e, local):
    c = e.find(f"{{*}}{local}")
    return c.text.strip() if c is not None and c.text else ""


def _to_bj_hm(s):
    """ISO8601 时间（GPX <time> / KML <when>）→ 北京时间 hh:mm:ss；解析不了就原样返回。"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")


def _cum_of(pts):
    """原始点串的累计 haversine 里程（米）。"""
    R = 6371008.8
    cum = [0.0]
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        lo1, la1 = math.radians(a[0]), math.radians(a[1])
        lo2, la2 = math.radians(b[0]), math.radians(b[1])
        h = (math.sin((la2 - la1) / 2) ** 2
             + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
        cum.append(cum[-1] + 2 * R * math.asin(math.sqrt(h)))
    return cum


def parse_gpx(root):
    """标准 GPX → 与 KML 分支**同一 schema**（pts = [lon, lat, ele, hh:mm:ss]）。

    轨迹取 <trkpt>（多个 <trkseg> 一律串成一条，段界如实保留）；没有则退回 <rtept>。
    具名点取 <wpt>。GPX 的经纬度在**属性**里，不是文本节点。
    """
    pts = []
    for want in ("trkpt", "rtept"):        # 优先轨迹点；纯路线文件才用 rtept
        for e in root.iter():
            if _lname(e) != want:
                continue
            try:
                lon, lat = float(e.get("lon")), float(e.get("lat"))
            except (TypeError, ValueError):
                continue
            ele = _text_of(e, "ele")
            pts.append([lon, lat, float(ele) if ele else None, _to_bj_hm(_text_of(e, "time"))])
        if pts:
            break

    pois = []
    for e in root.iter():
        if _lname(e) != "wpt":
            continue
        try:
            lon, lat = float(e.get("lon")), float(e.get("lat"))
        except (TypeError, ValueError):
            continue
        ele = _text_of(e, "ele")
        pois.append({
            "name": _text_of(e, "name") or _text_of(e, "desc") or "航点",
            "lon": lon, "lat": lat,
            "ele": float(ele) if ele else None,
            "desc": _text_of(e, "desc"),
        })
    return pts, _cum_of(pts), pois


def parse_kml(path: Path):
    """解析 KML 或 GPX（按根元素自动识别，不靠扩展名）。返回 (pts, cum, pois, 格式名)。"""
    root = ET.parse(path).getroot()
    if _lname(root).lower() == "gpx":
        return (*parse_gpx(root), "GPX")

    # ---- 1. 轨迹：收集所有 gx:coord 与 gx:when，按文档顺序配对 ----
    coords, whens = [], []
    for trk in root.iter():
        if _lname(trk) != "Track":
            continue
        for c in trk:
            if _lname(c) == "coord":
                coords.append(c.text.strip())
            elif _lname(c) == "when":
                whens.append(c.text.strip())

    pts = []
    for i, c in enumerate(coords):
        parts = c.split()
        lon, lat = float(parts[0]), float(parts[1])
        ele = float(parts[2]) if len(parts) > 2 else None
        pts.append([lon, lat, ele, _to_bj_hm(whens[i]) if i < len(whens) else ""])

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

    return pts, _cum_of(pts), pois, "两步路 KML"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kml", help="两步路导出的 .kml，或标准 .gpx")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 scripts/out/，与后续步骤的约定一致）")
    args = ap.parse_args()

    src = Path(args.kml)
    out = Path(args.out) if args.out else (Path(__file__).resolve().parent / "out")
    out.mkdir(parents=True, exist_ok=True)

    pts, cum, pois, kind = parse_kml(src)
    if not pts:
        sys.exit("没解析到任何轨迹点：\n"
                 "  · KML → 确认是两步路 <gx:Track> 导出（分段式 <LineString> 请改用 parse_kml_ls.py）\n"
                 "  · GPX → 确认含 <trkpt> 或 <rtept>")

    (out / "track_full.json").write_text(
        json.dumps({"pts": pts, "cum_m": [round(v, 2) for v in cum]}, ensure_ascii=False),
        encoding="utf-8")
    (out / "kml_pois.json").write_text(json.dumps(pois, ensure_ascii=False, indent=1), encoding="utf-8")

    ele = [p[2] for p in pts if p[2] is not None]
    print(f"识别格式 {kind}")
    print(f"轨迹点 {len(pts)}  里程 {cum[-1]/1000:.2f} km  时长 {pts[0][3]}–{pts[-1][3]}")
    if ele:
        print(f"原始高程 {min(ele):.0f}–{max(ele):.0f} m")
    print(f"标注点 {len(pois)}")
    print(f"→ {out/'track_full.json'}  /  {out/'kml_pois.json'}")
    print("下一步：python prep_kml_track.py  （它直接从 out/ 读，不必手工搬文件）")


if __name__ == "__main__":
    main()

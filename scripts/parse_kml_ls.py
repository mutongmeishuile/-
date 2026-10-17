# -*- coding: utf-8 -*-
"""解析两步路导出的 **LineString 分段式** KML（TbuluKmlVersion2 旧版 / 分段导出）。

⚠ 先判断你的 KML 属于哪种格式再选脚本（选错会得到 0 点，很容易误判成"文件坏了"）：
    grep -c "gx:coord"     线路.kml     # >0  → 用 scripts/parse_track_kml.py
    grep -c "<LineString>" 线路.kml     # >0  → 用本脚本
   <gx:Track> 式带 <when> 时间戳；本式**无时间戳**，逐日用时只能按公开攻略估算。

与 parse_track_kml.py 的区别：
  * 轨迹存放在多个 <Placemark>/<LineString>/<coordinates>（不是 <gx:Track>）；
  * 无时间戳（无 <when>），时间字段留空；
  * 注记点在 <Folder id="TbuluHisPointFolder"> 下的 <Placemark>/<Point>。

⚠ 段与段之间可能有**几百米的无记录断点**（实测南天山北线 483 m）：默认按 gap 阈值识别，
   **只记不补点**，如实保留；下游 GPX 会表现为多个 <trkseg>。

⚠⚠ **平行备选段会把总里程撑假**（2026-10-17 北天山中线实测）：两步路的 KML 里除了主线，
   还常带作者手绘的**平行备选段**（绕法 / 切法），它们的首尾都落在主线上、长度也接近。
   默认"全部合并"会把它们串成一条：段间出现几万米的假断点，总里程虚增一倍
   （143.6 km → 311.56 km），剖面图跟着出现两道空白悬崖。
   → 本脚本检测到这种情况会打印提示，并用 `--keep 0` / `--longest` 只保留要的那几段。

用法：
    python parse_kml_ls.py "D:/路径/线路.kml"          # 输出到 scripts/out/（与 parse_track_kml.py 一致）
    python parse_kml_ls.py "D:/路径/线路.kml" --out ./kml
    python parse_kml_ls.py "D:/路径/线路.kml" --gap 300
    python parse_kml_ls.py "D:/路径/线路.kml" --longest      # 只保留最长的一段（主线）
    python parse_kml_ls.py "D:/路径/线路.kml" --keep 0,2     # 只保留第 0、2 段（0 起算，按文件顺序）

输出（--out 目录下）：
  track_full.json —— {"pts": [[lon,lat,ele,""], ...], "cum_m": [...], "seg_id": [...]}
  kml_pois.json   —— [{"name","lon","lat","ele","desc","km"}, ...]

下一步：把 track_full.json 放到 prep_kml_track.py 同目录再运行 → track_real.json + profile_real.json
（注意 prep_kml_track.py 的 DP 简化里纬度是硬编码的，换纬度带要改 kx 的 cos(纬度)）。
"""
import re, json, math, argparse
from pathlib import Path
from xml.etree import ElementTree as ET

R = 6371008.8


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", " ", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def hav(a, b):
    lo1, la1, lo2, la2 = math.radians(a[0]), math.radians(a[1]), math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def parse_coords(txt):
    out = []
    for tok in (txt or "").split():
        a = tok.split(",")
        if len(a) >= 2:
            try:
                ele = float(a[2]) if len(a) > 2 and a[2] not in ("", "0") else None
                out.append((float(a[0]), float(a[1]), ele))
            except ValueError:
                pass
    return out


def _seg_len(pts):
    return sum(hav((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1]))
               for i in range(len(pts) - 1))


def _cum(pts):
    """逐点累计里程（米）。"""
    out = [0.0]
    for i in range(len(pts) - 1):
        out.append(out[-1] + hav((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1])))
    return out


def _nearest_idx(other, lon, lat):
    return min(range(len(other)),
               key=lambda i: hav((lon, lat), (other[i][0], other[i][1])))


def _diagnose_segs(segs):
    """打印每段的长度/范围，并指出哪些是「平行备选段」（首尾都落在另一段上）。

    判定：段 s 的首尾点都在另一段 o 的 400 m 邻域内，且 s 的长度与
    **o 上这两个投影点之间的沿线距离**接近（±35%）—— 说明 s 是 o 那一段的另一种走法。
    ⚠ 不能拿 s 的长度和 o 的**总长**比：备选段通常只有 1–2 km，而主线 140 km，
      比值恒 ≈1，任何阈值都拦不住（2026-10-17 第一版就踩了这个坑，没报出来）。
    """
    lens = [_seg_len(s) / 1000 for s in segs]
    for i, (s, L) in enumerate(zip(segs, lens)):
        lons = [p[0] for p in s]; lats = [p[1] for p in s]
        print("  段 %d：%6d 点  %7.2f km  lon %.4f–%.4f  lat %.4f–%.4f"
              % (i, len(s), L, min(lons), max(lons), min(lats), max(lats)))
    if len(segs) < 2:
        return
    alt = []
    for i, s in enumerate(segs):
        for j, o in enumerate(segs):
            if i == j or len(o) < 2:
                continue
            d0 = hav((s[0][0], s[0][1]), (o[0][0], o[0][1]))
            d1 = hav((s[-1][0], s[-1][1]), (o[0][0], o[0][1]))
            for p in o:
                d0 = min(d0, hav((s[0][0], s[0][1]), (p[0], p[1])))
                d1 = min(d1, hav((s[-1][0], s[-1][1]), (p[0], p[1])))
            if d0 > 400.0 or d1 > 400.0:
                continue
            co = _cum(o)
            i0, i1 = _nearest_idx(o, s[0][0], s[0][1]), _nearest_idx(o, s[-1][0], s[-1][1])
            along = abs(co[i1] - co[i0])
            if along < 50:                      # 太短，构不成"另一种走法"
                continue
            if abs(_seg_len(s) - along) / along < 0.35:
                alt.append((i, j, along / 1000))
                break
    if alt:
        print("  ⚠ 检测到平行备选段（首尾都落在另一段上、长度接近，是同一段的另一种走法）：")
        for i, j, along in alt:
            print("     段 %d 平行于段 %d 的第 %.2f km 区间 —— 全部合并会把里程撑假"
                  "（段间出现几万米假断点，剖面图出现空白悬崖）" % (i, j, along))
        drop = [a[0] for a in alt]
        print("     → 只保留主线：--longest；或按需挑段：--keep %s"
              % ",".join(str(k) for k in range(len(segs)) if k not in drop))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kml")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gap", type=float, default=500.0,
                    help="段间距离超过该值(m)时按断点处理，不在两段之间补点（默认 500）")
    ap.add_argument("--keep", default=None,
                    help="只保留这些段（0 起算、按文件顺序、逗号分隔），如 --keep 0 或 --keep 0,2")
    ap.add_argument("--longest", action="store_true",
                    help="只保留最长的一段（通常是主线，用于剔除平行备选段）")
    args = ap.parse_args()

    src = Path(args.kml)
    # 默认与 parse_track_kml.py 一致落 scripts/out/，下游 prep_kml_track._find 才能直接找到
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "out"
    out.mkdir(parents=True, exist_ok=True)

    root = ET.parse(src).getroot()
    ns = "{*}"

    # ---- 1. 轨迹段：Document 直属的 Placemark/LineString ----
    segs = []
    for pm in root.findall(f".//{ns}Placemark"):
        ls = pm.find(f"{ns}LineString")
        if ls is None:
            continue
        for c in ls.findall(f"{ns}coordinates"):
            pts = parse_coords(c.text)
            if len(pts) >= 2:
                segs.append(pts)

    if not segs:
        raise SystemExit("未找到任何 LineString 轨迹段")

    print(f"轨迹段数 {len(segs)}  原始点数 {sum(len(s) for s in segs)}")
    _diagnose_segs(segs)

    # ---- 段选择：默认全并；--longest / --keep 可只留主线，避免平行备选段撑假里程 ----
    if args.longest:
        keep = [max(range(len(segs)), key=lambda i: _seg_len(segs[i]))]
        print(f"  --longest → 只保留段 {keep[0]}")
    elif args.keep:
        try:
            keep = sorted({int(x) for x in str(args.keep).replace("，", ",").split(",") if x != ""})
        except ValueError:
            raise SystemExit(f"--keep 解析失败：{args.keep!r}（应形如 0 或 0,2）")
        bad = [k for k in keep if k < 0 or k >= len(segs)]
        if bad:
            raise SystemExit(f"--keep 段号越界：{bad}（共 {len(segs)} 段，合法 0–{len(segs)-1}）")
        print(f"  --keep → 只保留段 {keep}")
    else:
        keep = list(range(len(segs)))
    segs = [segs[i] for i in keep]

    # ---- 2. 合并成一条，记录段号；大断点只记不补点 ----
    pts, seg_id, cum_seg = [], [], []
    cum = [0.0]
    breaks = []
    for si, sg in enumerate(segs):
        for k, p in enumerate(sg):
            if pts:
                d = hav((pts[-1][0], pts[-1][1]), (p[0], p[1]))
                if k == 0 and d > args.gap:
                    breaks.append((si, d, cum[-1]))
                    cum_seg.append(cum[-1])
                cum.append(cum[-1] + d)
            pts.append([p[0], p[1], p[2], ""])
            seg_id.append(si)
            if k == 0 and si > 0:
                cum_seg.append(cum[-1])

    # 修正 cum 长度与 pts 对齐（首个点 cum=0）
    cum = cum[: len(pts)]
    while len(cum) < len(pts):
        cum.append(cum[-1])

    print("合并后点数 %d  里程 %.2f km" % (len(pts), cum[-1] / 1000))
    for si, d, at in breaks:
        print("  ⚠ 段 %d 起点断点 %.0f m（累计 %.2f km 处）—— 未补点，如实保留" % (si, d, at / 1000))

    eles = [p[2] for p in pts if p[2]]
    print("高程 %d~%d m  有效点 %d/%d" % (min(eles), max(eles), len(eles), len(pts)))

    # ---- 3. 注记点 ----
    pois = []
    for pm in root.findall(f".//{ns}Placemark"):
        pt = pm.find(f"{ns}Point")
        if pt is None:
            continue
        c = pt.find(f"{ns}coordinates")
        if c is None:
            continue
        raw = (c.text or "").strip()
        first = raw.split()[0].split(",") if raw.split() else []
        if len(first) < 2:
            continue
        nm = pm.find(f"{ns}name")
        de = pm.find(f"{ns}description")
        pois.append({
            "name": strip_tags(nm.text if nm is not None else ""),
            "lon": float(first[0]), "lat": float(first[1]),
            "ele": float(first[2]) if len(first) > 2 and first[2] else None,
            "desc": strip_tags(de.text if de is not None else "")[:160],
            "km": None,
        })
    # 注记点挂累计里程：找最近轨迹点
    for po in pois:
        best, bk = 1e18, 0
        for i, p in enumerate(pts):
            d = (p[0] - po["lon"]) ** 2 + (p[1] - po["lat"]) ** 2
            if d < best:
                best, bk = d, i
        po["km"] = round(cum[bk] / 1000, 3)

    named = [p for p in pois if p["name"] and p["name"] not in ("起点", "终点")]

    (out / "track_full.json").write_text(json.dumps(
        {"pts": pts, "cum_m": [round(v, 2) for v in cum], "seg_id": seg_id},
        ensure_ascii=False), encoding="utf-8")
    (out / "kml_pois.json").write_text(json.dumps(pois, ensure_ascii=False, indent=1), encoding="utf-8")

    print("注记点 %d（其中具名 %d）" % (len(pois), len(named)))
    print("→ %s" % (out / "track_full.json"))


if __name__ == "__main__":
    main()

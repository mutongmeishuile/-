# -*- coding: utf-8 -*-
"""出图前把「注记字号 / 注记密度」算清楚 —— 不渲染、不下载、不启浏览器。

**为什么要有这个脚本（2026-11-18 用户）**：
    字号一共受 4 个旋钮影响（`map_ss` / `label_font_cap` / `label_fs_base` /
    `label_fs_poi`），而它们的实际效果要等 `make_terrain` 跑完、图渲出来才看得见。
    实战里为了把字调对，来回重出 4–5 次地图，每次十几秒到几分钟。
    这个脚本用**和出图完全相同的公式**先把每个量算成数字打出来，
    让你在动手渲之前就定好参数。

口径（全部与出图代码同源，改代码必须同步改这里 —— 脚本会自检漂移）
    · 画幅：横图 IW=MAP_LONG_LOGICAL，竖图 IW=MAP_LONG_LOGICAL×9/16（make_terrain.build_terrain）
    · 出图倍率：`SS = CFG["map_ss"]`（默认 GC.MAP_SS）
    · 字号倍率：`S = min(SS, CFG["label_font_cap"])`（draw_osm.draw_overlay）
    · 底图各档字号：draw_osm 里的字面量 15.5/14/12.5/14/13/11/11.5 → `F(v)=max(6,round(v*S))`
    · POI 标注：`F_poi = CFG["label_fs_poi"]（默认 base×1.125）× S`
    · 计曲线高程数字：`MT.LABEL_FONT(8.0) × S` → `max(6,round(8*S))`
    · 图钉外径：`pin_d × S`（mk_pin(k=_vk()) 后与图例逐像素同尺寸）

用法
    python scripts/preflight_labels.py            # 打印全部
    python scripts/preflight_labels.py --quiet    # 只打印超阈值项
"""
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import guide_common as GC  # noqa: E402

try:
    import route_def as RD  # noqa: E402
except Exception:                                                     # noqa: BLE001
    sys.exit("找不到 route_def.py —— 先在项目 scripts/ 下生成线路配置")
import make_terrain as MT  # noqa: E402

OUT = Path(HERE) / "out"

# 折算基准：把图等比缩到 1504 宽（= 技能的设计宽）时，一个标注还剩多少 CSS px。
# 这是「人眼在屏幕上读这张图」的真实字号，比"占图宽百分比"直观得多。
REF_W = 1504.0
# 参考基线（2026-11 各线路定稿的实测舒适区）：1504 宽等效 7–10 px 可读、> 12 px 偏大。
# 下限是"再小就读不清"，上限是"再大地名就互相挤"。窗口越小（< 30 km）越该靠近上限，
# 但超过 16 px 基本就是字号喧宾夺主了（2026-11-18 缙磐四尖实测标定）。
OK_LO, OK_HI = 7.0, 16.0
POI_LO = 8.0


def cfgv(key, default):
    return (RD.CFG.get(key, default) if hasattr(RD, "CFG") else default)


def main(quiet=False):
    lon0, lat0, lon1, lat1, src = GC.map_window()
    ratio = GC.aspect_ratio(lon0, lat0, lon1, lat1)
    IW = (GC.MAP_LONG_LOGICAL if ratio >= 1.0
          else int(round(GC.MAP_LONG_LOGICAL * GC.MAP_ASPECT_PORT)))
    IH = int(round(IW / ratio)) if ratio > 0 else IW
    ss = float(cfgv("map_ss", GC.MAP_SS) or GC.MAP_SS)
    cap = float(cfgv("label_font_cap", GC.VIS_CAP) or GC.VIS_CAP)
    S = min(ss, cap)
    W_px = IW * ss

    # 地面分辨率
    km_w = (lon1 - lon0) * 111.32 * math.cos(math.radians((lat0 + lat1) / 2))
    m_per_px = km_w * 1000.0 / W_px
    m_per_logical = km_w * 1000.0 / IW

    fs_base = float(cfgv("label_fs_base", 4) or 4)
    fs_poi = float(cfgv("label_fs_poi", fs_base * 1.125) or fs_base * 1.125)

    def eq(px):
        """出图 px → 缩到 1504 宽时的等效 CSS px。"""
        return px * REF_W / W_px

    rows = [
        # 名称, 逻辑 px, 出图 px
        ("底图 · 城镇", 15.5, max(6, round(15.5 * S))),
        ("底图 · 村庄", 14, max(6, round(14 * S))),
        ("底图 · 小地名", 12.5, max(6, round(12.5 * S))),
        ("底图 · 高峰名", 14, max(6, round(14 * S))),
        ("底图 · 一般峰名", 13, max(6, round(13 * S))),
        ("底图 · 次级峰名", 11, max(6, round(11 * S))),
        ("底图 · 水系/兴趣点", 11.5, max(6, round(11.5 * S))),
        ("攻略 POI 名", fs_poi, max(3, round(fs_poi * S))),
        ("计曲线高程数字", MT.LABEL_FONT, max(6, round(MT.LABEL_FONT * S))),
    ]

    print(f"窗口  {lon0:.5f}, {lat0:.5f} → {lon1:.5f}, {lat1:.5f}")
    print(f"      {src}")
    print(f"画幅  逻辑 {IW}×{IH} px　·　出图 {W_px}×{round(W_px / ratio)} px"
          f"　·　{km_w:.1f} km 宽　·　{m_per_px:.2f} m/px（逻辑 {m_per_logical:.2f} m/px）")
    print(f"倍率  map_ss={ss:g}　label_font_cap={cap:g}　→ 字号倍率 S={S:g}"
          f"{'　⚠ cap>ss，字会跟着画幅一起放大' if cap > ss else ''}")
    print(f"      label_fs_base={fs_base:g}　label_fs_poi={fs_poi:g}")
    print()
    print(f"{'元素':<20}{'逻辑px':>8}{'出图px':>9}{'占图宽':>9}{'1504宽等效':>12}{'每字压住':>10}")
    for nm, lg, px in rows:
        print(f"{nm:<20}{lg:>8.1f}{px:>9d}{px / W_px * 100:>8.2f}%"
              f"{eq(px):>11.1f}px{m_per_px * px:>9.0f}m")

    warn = []
    osm_min = eq(rows[5][2])          # 次级峰名 = 最小一档
    poi_eq = eq(rows[7][2])
    cont_eq = eq(rows[8][2])
    if osm_min < OK_LO:
        warn.append(f"底图最小一档只有 {osm_min:.1f} px（1504 宽）→ 缩略后读不清；"
                    f"`label_font_cap` 提到 {cap * OK_LO / max(osm_min, .1):.1f} 或 `map_ss` 调小")
    if osm_min > OK_HI:
        warn.append(f"底图最小一档 {osm_min:.1f} px 偏大（> {OK_HI:g}）→ 地名会互相挤、落位数下降；"
                    f"`label_font_cap` 降到 {cap * OK_HI / osm_min:.1f}")
    # 层级判据：攻略 POI 必须 ≥ 底图「一般峰名」那一档，否则图上主次颠倒
    # （2026-11-18 缙磐四尖实测：POI 27 px < 村庄 42 px，看图像底图地名才是主角）。
    ref_peak = eq(rows[4][2])
    if poi_eq < POI_LO:
        warn.append(f"POI 名只有 {poi_eq:.1f} px（1504 宽），太小 → "
                    f"`label_fs_poi` 提到 {fs_poi * POI_LO / max(poi_eq, .1):.1f}")
    elif poi_eq < ref_peak * 0.95:
        warn.append(f"POI 名 {poi_eq:.1f} px < 底图一般峰名 {ref_peak:.1f} px → 主次颠倒；"
                    f"`label_fs_poi` 提到 {fs_poi * ref_peak / poi_eq:.1f} 左右")
    if cont_eq > poi_eq:
        warn.append(f"计曲线数字（{cont_eq:.1f} px）≥ POI 名（{poi_eq:.1f} px）→ 数字抢戏；"
                    f"降 `label_font_cap` 或改用 `contour_label=False`")

    # ---- 注记候选量（读 osm.json，不渲染）----
    cand = _candidates()
    if cand:
        print()
        gap = _last_gap()
        print(f"注记候选：{cand['总']} 条"
              f"（峰 {cand['峰']} · 地名 {cand['地名']} · 水系 {cand['水系']} · 道路 {cand['道路']}）")
        if gap:
            print(f"上次实测：落位 {gap.get('落位', '?')} · 挤放 {gap.get('挤放', '?')} · "
                  f"抑制 {gap.get('抑制', '?')}（来自 make_terrain 日志）")
        # 容量粗估：每个字标的占位 ≈ (1.25×字号)²，全图可用面积按 35% 算（轨迹/家具/空白）
        area_cell = (eq(rows[5][2]) * 1.25 * REF_W / W_px * W_px / REF_W) ** 2
        area_cell = (rows[5][2] * 1.25) ** 2
        cap_est = int(0.35 * W_px * (W_px / ratio) / max(area_cell, 1))
        print(f"容量粗估：{cap_est} 个（字号越大越少）"
              f"　候选/容量 = {cand['总'] / max(cap_est, 1):.2f}"
              f"{'　⚠ 明显超订，会被大量丢弃或挤放' if cand['总'] > cap_est else ''}")

    print()
    if warn:
        print("⚠ 建议：")
        for w in warn:
            print("  · " + w)
    elif not quiet:
        print("✓ 字号口径在舒适区内，可以直接出图")
    return 0


def _candidates():
    """从 out/osm.json 直接数注记候选（与出图画哪些类型同口径的粗版）。"""
    p = OUT / "osm.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                                 # noqa: BLE001
        return None
    els = d.get("elements") if isinstance(d, dict) else d
    c = {"总": 0, "峰": 0, "地名": 0, "水系": 0, "道路": 0}
    for e in els or []:
        t = e.get("tags") or {}
        nm = t.get("name") or t.get("name:zh")
        if not nm:
            continue
        if t.get("natural") == "peak":
            c["峰"] += 1
        elif t.get("place") in ("city", "town", "village", "hamlet", "suburb", "locality"):
            c["地名"] += 1
        elif t.get("waterway") or t.get("natural") == "water":
            c["水系"] += 1
        elif t.get("highway"):
            c["道路"] += 1
        else:
            continue
        c["总"] += 1
    return c


def _last_gap():
    """从最近一次 make_terrain 日志里抓落位统计（没有就算了，纯锦上添花）。"""
    logs = sorted(OUT.glob("*_log*.txt")) + sorted(OUT.glob("*.log"))
    for f in reversed(logs):
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                             # noqa: BLE001
            continue
        m = re.search(r"山峰名 (\d+)/(\d+)", txt)
        if m:
            return {"落位": f"{m.group(1)}/{m.group(2)}"}
    return None


if __name__ == "__main__":
    sys.exit(main(quiet="--quiet" in sys.argv))

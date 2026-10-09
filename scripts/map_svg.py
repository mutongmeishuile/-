# -*- coding: utf-8 -*-
"""全线地图 SVG（通用版）。

输入：out/base_meta.json（投影基准）+ out/base_map.jpg（自渲染底图）+ route_def.py
输出：`build_svg(...)` 返回一段可直接内嵌的 <svg> 字符串。

本轮相对旧版的三处改动（都是"踩过才知道"的）：
  ① **三件套（图例 / 指北针 / 比例尺）缩小 + 动态落点**：
     统一交给 guide_common.Placer，硬约束「不压轨迹、互不重叠」，软避让标注与 POI。
     旧版把指北针写死在右上角、图例只试 8 个候选位且只按"轨迹点数"打分 —— 线一密就压线。
  ② **标注避让三级降级**（严格无冲突 → 重叠面积最小 → 钳位），
     旧的"找不到就钳位"兜底会把标签甩到占用表外、压住别家文字。
  ③ 家具先摆、标注后摆：保证标注一定绕开家具（反过来无法保证）。
"""
import base64
import io
import math

import guide_common as GC
import route_def as RD

if not getattr(RD, "TRACK_READY", True):
    raise SystemExit("轨迹数据未就绪：先跑 python prep_track.py <你的.kml>")

meta = GC.load_meta()
IW, IH = meta["size"]
PROJ = GC.projector_of(meta)
M_PER_PX = GC.m_per_px(meta)

INK = "#1F2430"
# ---- 家具缩放系数（要求「缩小」）----
FURN_LG = 0.78        # 图例：566×218 → 442×170（面积 ≈ 0.61）
FURN_CP = 0.72        # 指北针：半径 42 → 30
FURN_SB = 0.80        # 比例尺

EMBED_MAX = 1600      # 网页内嵌底图上限；高清地图用 embed_max=0
PS_BASE = 0.85        # 点位符号／标注基准缩放
MFS_M = 1.9           # 手机版地图统一放大系数（图被缩到 ~0.25 倍，必须放大）
MFS_S = 2.8           # 手机版里程点/线宽的额外放大

_CFG = getattr(RD, "CFG", {})
D1_C = _CFG.get("d1_color", "#E4572E")
D2_C = _CFG.get("d2_color", "#1E9E76")
D1_T = _CFG.get("d1_text", "#B23A1C")
D2_T = _CFG.get("d2_text", "#0F6E5C")

PS = PS_BASE
_PAD = 22
_PIN_R = 24
_OCC, _LABELS, LABEL_OF = [], [], {}
_lg_rect = _sb_rect = _cp_rect = None


def P(lon, lat):
    x, y = PROJ(lon, lat)
    return round(x, 1), round(y, 1)


def d_of(pts):
    return "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)


# ---------------------------------------------------------------- 标注避让（三级降级）
def _layout(x, y, dx, dy, anc, w, fs):
    h = fs * 1.6
    cand = [o * h for o in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6)]
    if anc == "middle":
        hplans = [(0, "middle"), (w / 2 + 34, "start"), (-w / 2 - 34, "end")]
    elif dx >= 0:
        hplans = [(dx, "start"), (-abs(dx) - 6, "end")]
    else:
        hplans = [(dx, "end"), (abs(dx) + 6, "start")]

    def boxof(hdx, hanc, ody):
        tx, ty = x + hdx, y + dy + ody
        x0, x1 = ((tx, tx + w) if hanc == "start" else
                  (tx - w, tx) if hanc == "end" else (tx - w / 2, tx + w / 2))
        return tx, ty, (x0, ty - h / 2, x1, ty + h / 2)

    def dist(box):
        return max(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))

    free, loose = None, None
    for hdx, hanc in hplans:
        for ody in cand:
            tx, ty, box = boxof(hdx, hanc, ody)
            x0, y0, x1, y1 = box
            if x0 < _PAD or x1 > IW - _PAD or y0 < _PAD or y1 > IH - _PAD:
                continue
            area = sum(GC.rect_overlap_area(box, o) for o in _OCC)
            key = (round(dist(box)), round(abs(ody)))
            if area == 0:
                if free is None or key < free[0]:
                    free = (key, tx, ty, hanc, box)
            elif loose is None or (area, *key) < loose[0]:
                loose = ((area, *key), tx, ty, hanc, box)
    best = free or loose
    if best is not None:
        _, tx, ty, hanc, box = best
        _OCC.append(box)
        _LABELS.append(box)
        return round(tx, 1), round(ty, 1), hanc, box
    tx = min(max(x + dx, _PAD + w if dx >= 0 else _PAD), IW - _PAD)
    ty = min(max(y + dy, _PAD), IH - _PAD)
    box = (tx - w, ty - h / 2, tx + w, ty + h / 2)
    _LABELS.append(box)
    return round(tx, 1), round(ty, 1), anc, box


# ---------------------------------------------------------------- 点位符号
def mk_pin(x, y, kind):
    if kind == "start":
        r = 19 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#D93B2B" stroke="#fff" '
                f'stroke-width="{5*PS:.1f}"/>'
                f'<text x="{x}" y="{y+8*PS:.1f}" text-anchor="middle" font-size="{21*PS:.1f}" '
                f'font-weight="800" fill="#fff">起</text>')
    if kind == "end":
        r = 19 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#0F6E5C" stroke="#fff" '
                f'stroke-width="{5*PS:.1f}"/>'
                f'<text x="{x}" y="{y+8*PS:.1f}" text-anchor="middle" font-size="{19*PS:.1f}" '
                f'font-weight="800" fill="#fff">终</text>')
    if kind in ("peak", "peak_hi"):
        hi = kind == "peak_hi"
        col = D1_T if hi else "#4A5261"
        r = (19 if hi else 15) * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r+5*PS:.1f}" fill="#fff" opacity="0.9"/>'
                f'<path d="M {x-r:.1f},{y+r*0.62:.1f} L {x},{y-r*0.78:.1f} '
                f'L {x+r:.1f},{y+r*0.62:.1f} Z" fill="{col}"/>')
    if kind == "temple":
        return (f'<circle cx="{x}" cy="{y}" r="{19*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{14.6*PS:.1f}" fill="#5A4A8A"/>'
                f'<path d="M {x-9.5*PS:.1f},{y-1.2*PS:.1f} L {x},{y-10.4*PS:.1f} '
                f'L {x+9.5*PS:.1f},{y-1.2*PS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-6.2*PS:.1f}" y="{y+1.2*PS:.1f}" width="{12.4*PS:.1f}" '
                f'height="{7.6*PS:.1f}" fill="#fff"/>')
    if kind == "camp":
        return (f'<circle cx="{x}" cy="{y}" r="{20*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{15.5*PS:.1f}" fill="{D1_C}"/>'
                f'<path d="M {x-10*PS:.1f},{y+8*PS:.1f} L {x},{y-9*PS:.1f} '
                f'L {x+10*PS:.1f},{y+8*PS:.1f} Z" fill="#fff"/>')
    if kind == "water":
        return (f'<circle cx="{x}" cy="{y}" r="{19*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{14.6*PS:.1f}" fill="#0F8A9B"/>'
                f'<path d="M {x},{y-9*PS:.1f} C {x+9*PS:.1f},{y+1*PS:.1f} '
                f'{x+7*PS:.1f},{y+9*PS:.1f} {x},{y+9*PS:.1f} C {x-7*PS:.1f},{y+9*PS:.1f} '
                f'{x-9*PS:.1f},{y+1*PS:.1f} {x},{y-9*PS:.1f} Z" fill="#fff"/>')
    if kind == "warn":
        return (f'<circle cx="{x}" cy="{y}" r="{19*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{14.6*PS:.1f}" fill="#D0871B"/>'
                f'<text x="{x}" y="{y+7.4*PS:.1f}" text-anchor="middle" '
                f'font-size="{20*PS:.1f}" font-weight="900" fill="#fff">!</text>')
    return ""


# ---------------------------------------------------------------- 三件套
def _legend_rows():
    rows = []
    for d in _CFG.get("days", []):
        rows.append((d.get("color", D1_C), d.get("tag", "D"), d.get("legend", ""),
                     d.get("dist_label", ""), 6.5, ""))
    if _CFG.get("extra_legend_desc"):
        rows.append((_CFG.get("extra_legend_color", "#8C8A86"),
                     _CFG.get("extra_legend_tag", "步道"),
                     _CFG["extra_legend_desc"], "", 2.1, ' stroke-dasharray="8 5"'))
    return rows


def _legend_size():
    rows = _legend_rows()
    return (round(566 * FURN_LG), round((96 + 52 * len(rows)) * FURN_LG))


def build_map_overlay(mobile=False):
    """返回地图 SVG 的内容（不含 <svg> 外壳与底图）。"""
    global PS, _PAD, _PIN_R
    global _lg_rect, _sb_rect, _cp_rect
    M = MFS_M if mobile else 1.0
    PS = PS_BASE * M
    _PIN_R = 24 * M
    _PAD = 22
    _OCC.clear()
    _LABELS.clear()
    LABEL_OF.clear()
    # ⚠ 三件套矩形必须在每轮开头清零：手机版**不摆图例**，若不清零，
    #   它读到的是上一轮（桌面版）留下的 _lg_rect —— 自检会凭空报一条「图例 × 比例尺」重叠，
    #   而图上根本没有图例。这类"幽灵家具"只看图是发现不了的。
    _lg_rect = _sb_rect = _cp_rect = None

    r = RD.build_route()
    XY = [P(*p) for p in r["pts"]]
    KM = r["km"]
    SPLIT_I = r["split1"]
    TOTAL_KM = r["total_km"]
    POIS = GC.pois_of(RD)        # 元组 / 字典都收，统一成 dict（guide_common.poi）

    o = []
    ROUTE_W, HALO_W = 5.4 * M, 8.6 * M
    o.append(f'<path d="{d_of(XY)}" fill="none" stroke="#FFFFFF" stroke-width="{HALO_W}" '
             f'stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/>')
    for seg, col in ((XY[:SPLIT_I + 1], D1_C), (XY[SPLIT_I:], D2_C)):
        if len(seg) < 2:
            continue
        o.append(f'<path d="{d_of(seg)}" fill="none" stroke="{col}" stroke-width="{ROUTE_W}" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')

    # 每 5 km 里程点
    for target in range(5, int(TOTAL_KM) + 1, 5):
        i = min(range(len(KM)), key=lambda j: abs(KM[j] - target))
        x, y = XY[i]
        rr = 7.4 * (MFS_S if mobile else 1.0)
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr:.1f}" fill="#FFFFFF" '
                 f'stroke="#3A4250" stroke-width="{2*M:.1f}"/>'
                 f'<text x="{x:.1f}" y="{y+4.2*M:.1f}" text-anchor="middle" '
                 f'font-size="{10*(MFS_S if mobile else 1.0):.0f}" font-weight="800" '
                 f'fill="#3A4250">{target}</text>')

    # 分日界短线
    sx, sy = XY[SPLIT_I]
    o.append(f'<line x1="{sx-56*M:.0f}" y1="{sy:.1f}" x2="{sx+56*M:.0f}" y2="{sy:.1f}" '
             f'stroke="#FFFFFF" stroke-width="{7*M:.1f}" stroke-linecap="round"/>'
             f'<line x1="{sx-56*M:.0f}" y1="{sy:.1f}" x2="{sx+56*M:.0f}" y2="{sy:.1f}" '
             f'stroke="#6C7480" stroke-width="{2.4*M:.1f}" '
             f'stroke-dasharray="{11*M:.0f} {8*M:.0f}" stroke-linecap="round"/>')

    pin_xy = [P(d["lon"], d["lat"]) for d in POIS]

    # ---- 家具先摆（硬约束：不压轨迹、互不重叠）；标注随后绕开它们 ----
    placer = GC.Placer(IW, IH, track=XY, pad=_PAD, track_half_w=(ROUTE_W + HALO_W) / 2 + 2)
    placer.pins = pin_xy
    for px, py in pin_xy:
        _OCC.append((px - _PIN_R, py - _PIN_R, px + _PIN_R, py + _PIN_R))

    lg_rows = _legend_rows()
    LG_W, LG_H = _legend_size()
    lg_pad = round(14 * M)
    sb_bar = 2000 / M_PER_PX
    SB_W, SB_H = round((sb_bar + 46 * M) * FURN_SB), round(66 * M * FURN_SB)
    CP_R = round(42 * M * FURN_CP)          # 42 → 30
    CP_W = CP_H = CP_R * 2 + 16 * M

    placed = {}
    if not mobile:                              # 手机版地图过小，图例改为页内文字图例
        got = placer.place(LG_W + 2 * lg_pad, LG_H + 2 * lg_pad,
                           prefer=("tl", "tr", "br", "bl", "ml", "mr", "tc", "bc"))
        if got:
            # place() 返回 (x, y, rect)；家具占位登记的是 rect（含内衬那一圈）
            _lg_rect = (got[0] + lg_pad, got[1] + lg_pad, got[0] + lg_pad + LG_W,
                        got[1] + lg_pad + LG_H)
            _OCC.append(got[2])
            placed["lg"] = got
    got = placer.place(SB_W, SB_H, prefer=("bl", "br", "tl", "tr", "bc", "tc", "ml", "mr"))
    if got:
        _sb_rect = got[2]
        placed["sb"] = got
    got = placer.place(CP_W, CP_H, prefer=("tr", "tl", "br", "bl", "tc", "bc", "mr", "ml"))
    if got:
        _cp_rect = got[2]
        placed["cp"] = got

    for d in POIS:
        o.append(mk_pin(*P(d["lon"], d["lat"]), d["kind"]))

    # ---- 标注：越挤越先摆 ----
    def _crowd(i):
        px, py = pin_xy[i]
        return min(((px - pin_xy[j][0]) ** 2 + (py - pin_xy[j][1]) ** 2) ** 0.5
                   for j in range(len(pin_xy)) if j != i)

    for i in sorted(range(len(POIS)), key=_crowd):
        d = POIS[i]
        name, ele, k = d["name"], d["ele"], d["kind"]
        dx, dy, anc = d["dx"], d["dy"], d["anchor"]
        x, y = pin_xy[i]
        fs = (28 if k in ("start", "end") else 26) * PS
        col = D1_T if k == "peak_hi" else (D2_T if k in ("start", "end") else INK)
        if mobile:
            nm = getattr(RD, "SHORT", {}).get(name, name)
            txt, t = nm, nm
        else:
            txt = f"{name} {RD.fmt_ele(ele)} m"
            t = (f'{name}<tspan font-size="{fs*0.78:.1f}" font-weight="700" fill="#5F6875"> '
                 f'{RD.fmt_ele(ele)} m</tspan>')
        w = GC.text_w(txt, fs) + 34 * M
        tx, ty, ta, box = _layout(x, y, dx * M, dy * M, anc, w, fs)
        LABEL_OF[name] = box
        gap = max(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))
        if gap > 34 * M:
            px = min(max(x, box[0]), box[2])
            py = min(max(y, box[1]), box[3])
            o.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="#FFFFFF" stroke-width="{5*M:.1f}" stroke-linecap="round"/>'
                     f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="{col}" stroke-width="{1.6*M:.1f}" stroke-linecap="round" '
                     f'opacity="0.75"/>')
        o.append(f'<text x="{tx}" y="{ty+9*PS:.1f}" text-anchor="{ta}" font-size="{fs:.1f}" '
                 f'font-weight="800" fill="{col}" stroke="#FFFFFF" stroke-width="{7*PS:.1f}" '
                 f'paint-order="stroke" stroke-linejoin="round">{t}</text>')

    # ---- 落位绘制 ----
    if "lg" in placed:
        lx, ly = _lg_rect[0], _lg_rect[1]
        S = FURN_LG
        g = [f'<g class="il"><rect x="{lx}" y="{ly}" width="{LG_W}" height="{LG_H}" rx="{round(14*S)}" '
             f'fill="#FFFFFF" opacity="0.93" stroke="#D9D2C6" stroke-width="1.4"/>',
             f'<text x="{lx+18*S:.0f}" y="{ly+30*S:.0f}" font-size="{round(21*S)}" '
             f'font-weight="800" fill="{INK}">{_CFG.get("legend_title", "分日路段")}</text>']
        for i, (col, tag, desc, km, w, dash) in enumerate(lg_rows):
            ry = ly + 60 * S + i * 44 * S
            g.append(f'<line x1="{lx+18*S:.0f}" y1="{ry-7*S:.0f}" x2="{lx+78*S:.0f}" y2="{ry-7*S:.0f}" '
                     f'stroke="{col}" stroke-width="{w:.1f}" stroke-linecap="round"{dash}/>')
            g.append(f'<text x="{lx+90*S:.0f}" y="{ry+11*S:.0f}" font-size="{round(19*S)}" '
                     f'font-weight="800" fill="{col}">{tag}</text>'
                     f'<text x="{lx+152*S:.0f}" y="{ry+11*S:.0f}" font-size="{round(16.5*S)}" '
                     f'font-weight="600" fill="#4A5261">{desc}</text>')
            if km:
                g.append(f'<text x="{lx+LG_W-18*S:.0f}" y="{ry+11*S:.0f}" text-anchor="end" '
                         f'font-size="{round(16.5*S)}" font-weight="800" fill="#3A4250">{km}</text>')
        g.append(f'<text x="{lx+18*S:.0f}" y="{ly+LG_H-14*S:.0f}" font-size="{round(15*S)}" '
                 f'font-weight="600" fill="#7A8290">{_CFG.get("legend_foot", "全线为实测轨迹 · 非导航用图")}</text>')
        g.append("</g>")
        o.append("".join(g))

    if "cp" in placed:
        _, _, cr = placed["cp"]
        gx, gy = (cr[0] + cr[2]) / 2, (cr[1] + cr[3]) / 2
        R = CP_R
        o.append(f'<g><circle cx="{gx:.0f}" cy="{gy:.0f}" r="{R+M:.0f}" fill="#FFFFFF" opacity="0.92"/>'
                 f'<path d="M {gx:.0f},{gy-R*0.71:.0f} L {gx+R*0.26:.0f},{gy+R*0.29:.0f} '
                 f'L {gx:.0f},{gy+R*0.12:.0f} L {gx-R*0.26:.0f},{gy+R*0.29:.0f} Z" fill="#3A4250"/>'
                 f'<text x="{gx:.0f}" y="{gy+R*0.9:.0f}" text-anchor="middle" '
                 f'font-size="{round(14*M):d}" font-weight="800" fill="#3A4250">N</text></g>')

    if "sb" in placed:
        sx0, sy0, srect = placed["sb"]
        bw, bh = srect[2] - srect[0], srect[3] - srect[1]
        bx, by = sx0 + 16 * M, sy0 + bh - 16 * M
        o.append(f'<g><rect x="{sx0}" y="{sy0}" width="{bw:.0f}" height="{bh:.0f}" '
                 f'rx="{round(9*M)}" fill="#FFFFFF" opacity="0.9"/>'
                 f'<rect x="{bx:.0f}" y="{by-20*M:.0f}" width="{sb_bar/2:.1f}" '
                 f'height="{7*M:.0f}" fill="#3A4250"/>'
                 f'<rect x="{bx+sb_bar/2:.1f}" y="{by-20*M:.0f}" width="{sb_bar/2:.1f}" '
                 f'height="{7*M:.0f}" fill="#FFFFFF" stroke="#3A4250" stroke-width="{1.3*M:.1f}"/>'
                 f'<text x="{bx:.0f}" y="{by-27*M:.0f}" font-size="{round(15*M)}" '
                 f'font-weight="700" fill="#3A4250">0</text>'
                 f'<text x="{bx+sb_bar/2:.1f}" y="{by-27*M:.0f}" text-anchor="middle" '
                 f'font-size="{round(15*M)}" fill="#3A4250">1</text>'
                 f'<text x="{bx+sb_bar:.1f}" y="{by-27*M:.0f}" text-anchor="end" '
                 f'font-size="{round(15*M)}" font-weight="700" fill="#3A4250">2 km</text></g>')
    return "".join(o), placed


# ---------------------------------------------------------------- 自检
def self_check(mobile=False, placed=None):
    bad = [b for b in _LABELS
           if b[0] < _PAD - 2 or b[2] > IW - _PAD + 2 or b[1] < _PAD - 2 or b[3] > IH - _PAD + 2]
    n = len(_LABELS)
    ov, ovnames = 0, []
    names = list(LABEL_OF.keys())
    for i in range(n):
        for j in range(i + 1, n):
            if GC.rect_overlap_area(_LABELS[i], _LABELS[j]) > 0:
                ov += 1
                ovnames.append(f"{names[i]} × {names[j]}")

    r = RD.build_route()
    XY = [P(*p) for p in r["pts"]]
    furn = {"图例": _lg_rect, "指北针": _cp_rect, "比例尺": _sb_rect}
    furn = {k: v for k, v in furn.items() if v}
    hit_track = {k: sum(1 for x, y in XY if v[0] <= x <= v[2] and v[1] <= y <= v[3])
                 for k, v in furn.items()}
    furn_ov = []
    ks = list(furn)
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            if GC.rect_overlap_area(furn[ks[i]], furn[ks[j]]) > 0:
                furn_ov.append(f"{ks[i]} × {ks[j]}")
    lab_ov_furn = sum(1 for b in _LABELS if any(GC.rect_overlap_area(b, v) > 0
                                                for v in furn.values()))

    tag = "（手机版地图）" if mobile else "（地图）"
    print(f"   自检{tag} · 标注 {n} 条 | 越界 {len(bad)} | 两两重叠 {ov} | "
          f"压轨迹点 {sum(hit_track.values())} {hit_track or ''}")
    print(f"      家具 {list(furn) or '（无）'} | 家具互相重叠 {len(furn_ov) or 0} | "
          f"标注压家具 {lab_ov_furn}")
    if ovnames:
        print("      重叠:", "; ".join(ovnames))
    if furn_ov:
        print("      家具重叠:", "; ".join(furn_ov))

    # ---- 失败码分两档（这是"别让自检变成狼来了"的关键）----
    # 硬指标：会**误导读图**的错误，任何尺寸下都必须为 0 ——
    #   · 标注越界（读者看到的是一条被切掉一半的地名）
    #   · 家具压轨迹（把轨迹盖住 = 把路线画错）
    #   · 家具互相重叠（图例和比例尺糊在一起）
    hard = len(bad) + sum(hit_track.values()) + len(furn_ov)
    # 软指标：**拥挤**，不是"错"。手机版地图是缩略图，标注字号被缩到 ~0.25 倍，
    #   十来个标注出现 1–3 处轻微互压是版面密度的自然结果，不影响读图；
    #   而桌面版（也是出长图那一版）必须为 0。
    soft = ov + lab_ov_furn
    if mobile and soft:
        print(f"      ⚠ 手机版缩略图有 {soft} 处标注重叠 —— 只告警，不计入失败"
              f"（桌面版为 0 即可；实在介意就减少 route_def.POIS）")
    return hard + (0 if mobile else soft)


# ---------------------------------------------------------------- 外壳
def _embed_jpeg(max_w=EMBED_MAX):
    from PIL import Image
    im = Image.open(GC.OUT / "base_map.jpg").convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=86, optimize=True, progressive=True, subsampling=0)
    return base64.b64encode(b.getvalue()).decode()


def build_svg(mobile=False, embed_image=True, embed_max=EMBED_MAX):
    """返回 (svg 字符串, 自检信息)。embed_max=0 表示不降采样（高清地图用）。"""
    overlay, placed = build_map_overlay(mobile)
    img = ""
    if embed_image:
        img = (f'<image x="0" y="0" width="{IW}" height="{IH}" '
               f'href="data:image/jpeg;base64,{_embed_jpeg(embed_max)}"/>')
    cls = "mapsvg map-p" if mobile else "mapsvg map-d"
    svg = (f'<svg class="{cls}" viewBox="0 0 {IW} {IH}" xmlns="http://www.w3.org/2000/svg" '
           f'role="img" aria-label="{_CFG.get("aria_map", "全线地图")}">'
           f'{img}{overlay}</svg>')
    return svg, placed

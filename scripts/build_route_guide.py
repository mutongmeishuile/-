# -*- coding: utf-8 -*-
"""生成「党岭拉东线（精简版）」重装穿越路线攻略 HTML（自包含，含全线地图 + 海拔剖面 + 逐日攻略）"""
import math, random
from pathlib import Path

OUT_DIR = Path(r"C:/Users/S6576/WorkBuddy/2026-09-29-09-25-03/党岭拉东线攻略")
OUT_DIR.mkdir(parents=True, exist_ok=True)

D1_C, D2_C, D3_C, D4_C = "#E4572E", "#1E9E76", "#6C4FD8", "#2D7FF9"

# ---------------------------------------------------------------- 工具
def polyline(pts, color, w=5.0, opacity=1.0):
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<path d="{d}" fill="none" stroke="#FFFFFF" stroke-width="{w+4.5:.1f}" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>'
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w:.1f}" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="{opacity}"/>')

def ring(cx, cy, rx, ry, seed, rot=0.0, wob=0.11, n=54):
    rnd = random.Random(seed)
    ph = [rnd.uniform(0, 6.283) for _ in range(3)]
    amp = [rnd.uniform(0.5, 1.0), rnd.uniform(0.2, 0.5), rnd.uniform(0.1, 0.3)]
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + wob * (amp[0] * math.sin(3 * a + ph[0]) + amp[1] * math.sin(5 * a + ph[1]) + amp[2] * math.sin(2 * a + ph[2]))
        x, y = rx * r * math.cos(a), ry * r * math.sin(a)
        xr = x * math.cos(rot) - y * math.sin(rot)
        yr = x * math.sin(rot) + y * math.cos(rot)
        pts.append((cx + xr, cy + yr))
    return "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts) + " Z"

def smooth(pts, tension=0.5):
    """把折线转成平滑的三次贝塞尔路径（Catmull-Rom -> Bezier）"""
    if len(pts) < 2:
        return ""
    d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else pts[i + 1]
        c1 = (p1[0] + (p2[0] - p0[0]) * tension / 3, p1[1] + (p2[1] - p0[1]) * tension / 3)
        c2 = (p2[0] - (p3[0] - p1[0]) * tension / 3, p2[1] - (p3[1] - p1[1]) * tension / 3)
        d += f" C {c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d

# ---------------------------------------------------------------- 地图数据
W, H = 1000, 552

S_PT   = (818, 508)          # 起点 东谷乡 2614m
C1_PT  = (753, 346)          # C1 牧屋 4162m
P_DONGXI = (677, 340)        # 东/西海子 垭口
P_HEI    = (632, 311)        # 黑海子 垭口
C2_PT  = (536, 176)          # C2 五号调水库 3525m
P_CANGYI = (455, 178)        # 藏益措
P_XIAGU  = (392, 175)        # 下古洛沟达纳错
P_TAO    = (356, 146)        # 桃海子垭口 4829m（P6 全程最高点）
C3_PT  = (330, 96)           # C3 斯达纳错 4465m
E_PT   = (168, 390)          # 终点 沙冲乡 3323m

D1_ROUTE = [S_PT, (813, 487), (819, 467), (808, 447), (800, 429), (803, 411),
            (792, 393), (784, 377), (790, 363), (779, 353), (766, 348), C1_PT]
D2_ROUTE = [C1_PT, (731, 342), (712, 349), (693, 345), P_DONGXI, (663, 332), (650, 322),
            (640, 314), P_HEI, (620, 296), (610, 278), (600, 258), (590, 236),
            (580, 214), (568, 196), (552, 184), C2_PT]
D3_ROUTE = [C2_PT, (505, 190), (478, 187), P_CANGYI, (430, 176), (410, 178), P_XIAGU,
            (376, 166), (366, 157), P_TAO, (344, 132), (334, 114), C3_PT]
D4_ROUTE = [C3_PT, (305, 104), (287, 120), (270, 142), (252, 168), (234, 196), (216, 224),
            (201, 254), (189, 285), (179, 318), (172, 352), E_PT]

# 等高线山体（示意地形）：沿 NE-SW 走向的山脊链，等高线互相交叠形成山脊—河谷格局
ROT = -0.55
CHAINS = [
    ((70, 60), (300, 300), 5, 76), ((350, 40), (540, 215), 4, 70),
    ((600, 50), (790, 250), 4, 72), ((840, 40), (980, 200), 4, 66),
    ((60, 250), (250, 470), 4, 74), ((300, 250), (470, 440), 4, 68),
    ((540, 300), (700, 500), 4, 70), ((760, 300), (930, 480), 4, 72),
    ((120, 430), (330, 560), 4, 70), ((390, 470), (600, 570), 4, 68),
]
_rnd = random.Random(3721)
PEAKS = []
for (x1, y1), (x2, y2), n, r in CHAINS:
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L          # 法向
    for i in range(n):
        t = (i + 0.5) / n
        j = _rnd.uniform(-22, 22)
        PEAKS.append((x1 + dx * t + nx * j, y1 + dy * t + ny * j,
                      r * _rnd.uniform(0.78, 1.18)))

def build_contours():
    out = []
    for idx, (cx, cy, rx) in enumerate(PEAKS):
        ry = rx * 0.5
        for i in range(9, -1, -1):
            f = 1 - i * 0.092
            if f <= 0.06:
                continue
            heavy = (i % 3 == 0)
            col = "#D6CBB4" if heavy else "#E4DBC8"
            sw = 0.8 if heavy else 0.55
            fill = 'fill="#F2ECDD" opacity="0.8"' if i == 0 else 'fill="none"'
            out.append(f'<path d="{ring(cx, cy, rx*f, ry*f, idx*29+i, ROT, wob=0.13, n=56)}" {fill} stroke="{col}" stroke-width="{sw}"/>')
    return "\n    ".join(out)

RIVERS = [
    [(250, 552), (300, 505), (360, 458), (430, 422), (500, 398), (570, 382), (650, 375), (760, 378)],
    [(905, 150), (886, 245), (866, 335), (850, 435), (842, 552)],
    [(700, 552), (672, 486), (640, 434), (612, 400), (585, 385)],
    [(30, 442), (88, 416), (148, 399), (205, 393), (262, 401), (325, 417), (392, 432), (445, 440)],
]

LAKES = [
    (548, 188, 17, 8, -0.25, "五号调水库"),
    (622, 302, 11, 7, -0.4, "黑海子"),
    (668, 333, 9, 6, -0.3, "东西海子"),
    (687, 344, 7, 5, -0.3, ""),
    (462, 172, 12, 6, -0.2, "藏益措"),
    (400, 168, 14, 7, -0.25, "下古洛沟达纳错"),
    (321, 99, 17, 9, -0.2, "斯达纳错"),
    (363, 141, 8, 5, -0.3, "桃海子"),
]

def build_lakes():
    out = []
    for cx, cy, rx, ry, rot, _ in LAKES:
        out.append(f'<path d="{ring(cx, cy, rx, ry, int(cx*7+cy), rot, wob=0.16, n=40)}" '
                   f'fill="#A9CFE8" stroke="#7FB4D4" stroke-width="1"/>')
    return "\n    ".join(out)

def build_rivers():
    out = []
    for r in RIVERS:
        out.append(f'<path d="{smooth(r)}" fill="none" stroke="#B7D2E4" stroke-width="2.6" stroke-linecap="round"/>')
        out.append(f'<path d="{smooth(r)}" fill="none" stroke="#FFFFFF" stroke-width="0.9" opacity="0.55"/>')
    return "\n    ".join(out)

def mk_marker(x, y, fill, label=None, r=11, tcol="#FFFFFF", fs=10.5, sw=2.6):
    s = f'<circle cx="{x}" cy="{y}" r="{r+2.4}" fill="#FFFFFF" opacity="0.95"/>'
    s += f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="#FFFFFF" stroke-width="{sw}"/>'
    if label:
        s += (f'<text x="{x}" y="{y+3.7}" text-anchor="middle" font-size="{fs}" font-weight="700" '
              f'fill="{tcol}">{label}</text>')
    return s

def mk_pass(x, y, label=None, ly=-18, anchor="middle"):
    s = (f'<circle cx="{x}" cy="{y}" r="9.5" fill="#FFFFFF" opacity="0.95"/>'
         f'<circle cx="{x}" cy="{y}" r="7.6" fill="#3A4250"/>'
         f'<path d="M {x-4.4},{y+3.2} L {x-1.1},{y-2.6} L {x+0.4},{y+0.2} L {x+2.0},{y-3.4} L {x+4.6},{y+3.2} Z" '
         f'fill="#FFFFFF"/>')
    if label:
        ly_ = y + ly
        s += (f'<text x="{x}" y="{ly_}" text-anchor="{anchor}" font-size="12.5" font-weight="700" fill="#2A3140" '
              f'stroke="#FFFFFF" stroke-width="3.4" paint-order="stroke" stroke-linejoin="round">{label}</text>')
    return s

def mk_lbl(x, y, txt, anchor="start", fs=12.5, fill="#2A3140", weight=700, stroke=3.4):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{fs}" font-weight="{weight}" fill="{fill}" '
            f'stroke="#FFFFFF" stroke-width="{stroke}" paint-order="stroke" stroke-linejoin="round">{txt}</text>')

MAP_SVG = f'''<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="党岭拉东线全线地图">
  <defs>
    <linearGradient id="terr" x1="0" y1="0" x2="0.6" y2="1">
      <stop offset="0%" stop-color="#F8F4ED"/><stop offset="100%" stop-color="#F1EADF"/>
    </linearGradient>
    <clipPath id="mapclip"><rect x="0" y="0" width="{W}" height="{H}" rx="14"/></clipPath>
  </defs>
  <g clip-path="url(#mapclip)">
    <rect x="0" y="0" width="{W}" height="{H}" fill="url(#terr)"/>
    <g opacity="0.95">
    {build_contours()}
    </g>
    {build_rivers()}
    {build_lakes()}

    <!-- 路线 -->
    {polyline(D1_ROUTE, D1_C)}
    {polyline(D2_ROUTE, D2_C)}
    {polyline(D3_ROUTE, D3_C)}
    {polyline(D4_ROUTE, D4_C)}

    <!-- 垭口 -->
    {mk_pass(*P_DONGXI, ly=-16)}
    {mk_pass(*P_HEI, ly=-16)}
    {mk_pass(*P_TAO, ly=-14)}

    <!-- 营地 / 起点终点 -->
    {mk_marker(*C1_PT, D1_C, "C1")}
    {mk_marker(*C2_PT, D2_C, "C2")}
    {mk_marker(*C3_PT, D3_C, "C3")}
    {mk_marker(*S_PT, "#D93B2B", "S", r=11)}
    {mk_marker(*E_PT, "#2D7FF9", "E", r=11)}

    <!-- 地名标注 -->
    {mk_lbl(806, 533, "起点 | 东谷乡 2614m", "end", 13)}
    {mk_lbl(768, 341, "C1 牧屋 4162m", "start")}
    {mk_lbl(645, 291, "黑海子", "end", 12.5)}
    {mk_lbl(692, 331, "东/西海子", "start", 12.5)}
    {mk_lbl(552, 172, "C2 五号调水库 3525m", "start")}
    {mk_lbl(462, 156, "藏益措 4100", "middle", 12.5)}
    {mk_lbl(400, 198, "下古洛沟达纳错", "middle", 12.5)}
    {mk_lbl(338, 130, "桃海子垭口", "end", 12.5, "#B23A1C")}
    {mk_lbl(338, 146, "4829m 全程最高", "end", 12.5, "#B23A1C")}
    {mk_lbl(330, 68, "C3 斯达纳错 4465m", "middle")}
    {mk_lbl(152, 415, "终点 | 沙冲乡 3323m", "end", 13)}

    <!-- 指北针 -->
    <g transform="translate(940,52)">
      <circle r="24" fill="#FFFFFF" opacity="0.9"/>
      <path d="M 0,-17 L 6.5,7 L 0,3 L -6.5,7 Z" fill="#3A4250"/>
      <text y="22" text-anchor="middle" font-size="11" font-weight="700" fill="#3A4250">N</text>
    </g>

    <!-- 比例尺 -->
    <g transform="translate(40,508)">
      <rect x="-10" y="-24" width="196" height="44" rx="8" fill="#FFFFFF" opacity="0.88"/>
      <text x="0" y="-6" font-size="12" font-weight="700" fill="#3A4250">5 km</text>
      <line x1="0" y1="6" x2="170" y2="6" stroke="#3A4250" stroke-width="2.4"/>
      <line x1="0" y1="0" x2="0" y2="12" stroke="#3A4250" stroke-width="2.4"/>
      <line x1="170" y1="0" x2="170" y2="12" stroke="#3A4250" stroke-width="2.4"/>
    </g>
  </g>
</svg>'''

# ---------------------------------------------------------------- 剖面数据
PROF = [
    (0.0, 2614), (1.0, 2700), (2.0, 2860), (3.0, 3060), (4.0, 3250), (5.0, 3440),
    (6.0, 3620), (7.0, 3800), (8.0, 3980), (9.0, 4150), (9.6, 4270), (10.2, 4376),
    (10.6, 4300), (11.1, 4162),
    (12.0, 4260), (13.0, 4330), (14.0, 4400), (14.8, 4450), (15.6, 4330), (16.6, 4230),
    (17.6, 4280), (18.4, 4400), (19.0, 4532), (19.8, 4400), (20.6, 4250), (21.4, 4100),
    (22.2, 3960), (23.0, 3820), (23.8, 3700), (24.6, 3600), (25.4, 3525),
    (26.2, 3620), (27.0, 3750), (27.8, 3900), (28.6, 4050), (29.4, 4180), (30.2, 4300),
    (31.0, 4560), (31.5, 4760), (32.2, 4700), (33.0, 4560), (33.8, 4600), (34.6, 4700),
    (35.4, 4790), (36.0, 4829), (36.4, 4700), (36.8, 4465),
    (37.2, 4300), (37.8, 4150), (38.2, 4250), (38.6, 4400), (39.0, 4560), (39.4, 4652),
    (40.0, 4600), (40.8, 4500), (41.6, 4400), (42.4, 4280), (43.2, 4150), (44.0, 4020),
    (45.0, 3870), (46.0, 3720), (47.0, 3610), (48.0, 3510), (49.0, 3450), (50.0, 3400),
    (51.0, 3360), (52.0, 3323),
]
DAYS = [(0.0, 11.1, D1_C, "D1"), (11.1, 25.4, D2_C, "D2"),
        (25.4, 36.8, D3_C, "D3"), (36.8, 52.0, D4_C, "D4")]
PEAKS_LBL = [(10.2, 4376, "P1"), (14.8, 4450, "P3"), (19.0, 4532, "P4"),
             (31.5, 4760, "P5"), (36.0, 4829, "P6"), (39.4, 4652, "P7")]
BOT_LBL = [(0.0, 2614, "东谷乡 2614", "start"), (11.1, 4162, "C1 4162", "middle"),
           (25.4, 3525, "C2 3525", "middle"), (36.8, 4465, "C3 4465", "middle"),
           (52.0, 3323, "终点 3323", "end")]

PW, PH = 1000, 336
PL, PR, PT, PB = 62, 976, 46, 292
KX = (PR - PL) / 52.0
KY = (PB - PT) / 2500.0
def X(km): return PL + km * KX
def Y(m):  return PB - (m - 2500) * KY

def build_profile():
    o = []
    # 网格
    for m in range(2500, 5001, 500):
        o.append(f'<line x1="{PL}" y1="{Y(m):.1f}" x2="{PR}" y2="{Y(m):.1f}" stroke="#E9E3D9" stroke-width="1"/>')
        o.append(f'<text x="{PL-10}" y="{Y(m)+4:.1f}" text-anchor="end" font-size="11.5" fill="#8C93A0">{m}</text>')
    for km in range(0, 53, 5):
        o.append(f'<line x1="{X(km):.1f}" y1="{PT-8}" x2="{X(km):.1f}" y2="{PB}" stroke="#EDE7DD" stroke-width="1"/>')
        o.append(f'<text x="{X(km):.1f}" y="{PB+18}" text-anchor="middle" font-size="11.5" fill="#8C93A0">{km}</text>')
    o.append(f'<line x1="{PL}" y1="{PB}" x2="{PR}" y2="{PB}" stroke="#CFC6B8" stroke-width="1.2"/>')
    o.append(f'<text x="{(PL+PR)/2:.0f}" y="{PH-6}" text-anchor="middle" font-size="12" fill="#6C7480">累计里程 (km)</text>')
    o.append(f'<text x="18" y="{(PT+PB)/2:.0f}" font-size="12" fill="#6C7480" transform="rotate(-90 18 {(PT+PB)/2:.0f})" text-anchor="middle">海拔 (m)</text>')

    # 分日填充 + 描边
    for k, (a, b, col, name) in enumerate(DAYS):
        seg = [p for p in PROF if a <= p[0] <= b]
        if seg[0][0] > a: seg = [(a, seg[0][1])] + seg
        if seg[-1][0] < b: seg = seg + [(b, seg[-1][1])]
        d = "M " + " L ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in seg)
        fill = d + f" L {X(seg[-1][0]):.1f},{PB} L {X(seg[0][0]):.1f},{PB} Z"
        o.append(f'<path d="{fill}" fill="{col}" opacity="0.15"/>')
        o.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>')
        xm = (X(a) + X(b)) / 2
        o.append(f'<text x="{xm:.1f}" y="{PT-14}" text-anchor="middle" font-size="14" font-weight="800" fill="{col}">{name}</text>')

    # 分日竖线
    for km in (11.1, 25.4, 36.8):
        o.append(f'<line x1="{X(km):.1f}" y1="{PT-4}" x2="{X(km):.1f}" y2="{PB}" stroke="#B9B2A6" stroke-width="1.1" stroke-dasharray="4 4"/>')

    # 垭口点
    for km, m, tag in PEAKS_LBL:
        hi = (tag == "P6")
        o.append(f'<circle cx="{X(km):.1f}" cy="{Y(m):.1f}" r="{5.0 if hi else 3.6}" fill="{"#B23A1C" if hi else "#3A4250"}" stroke="#FFFFFF" stroke-width="1.6"/>')
        o.append(f'<text x="{X(km):.1f}" y="{Y(m)-10:.1f}" text-anchor="middle" font-size="{13 if hi else 12}" font-weight="800" fill="{"#B23A1C" if hi else "#3A4250"}">{tag}</text>')
    o.append(mk_lbl(X(36.0) + 14, Y(4829) + 5, "桃海子垭口 4829m（全程最高）", "start", 12.5, "#B23A1C"))

    # 关键点
    for km, m, txt, anc in BOT_LBL:
        x, y = X(km), Y(m)
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="#FFFFFF" stroke="#3A4250" stroke-width="2"/>')
        if anc == "start":
            ty, tx, a2 = y + 22, x + 9, "start"
        elif anc == "end":
            ty, tx, a2 = y + 22, x - 6, "end"
        else:
            ty, tx, a2 = y + 22, x, "middle"
        o.append(f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{a2}" font-size="12" font-weight="700" fill="#3A4250" '
                 f'stroke="#FFFFFF" stroke-width="3.2" paint-order="stroke" stroke-linejoin="round">{txt}</text>')
    return "\n    ".join(o)

PROFILE_SVG = f'''<svg viewBox="0 0 {PW} {PH}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="全程海拔剖面">
  <rect x="0" y="0" width="{PW}" height="{PH}" fill="#FFFFFF" rx="14"/>
  <g>
    {build_profile()}
  </g>
</svg>'''

# ---------------------------------------------------------------- 逐日攻略
DAY_CARDS = [
    (1, "D1", "东谷乡 → 牧屋4162", "11.1 km", "+1770 / -286 m", "9–11 h", "最高 4376 m",
     "KML 无明确稳定水点，起点附近补足", "C1 牧屋附近，选择平地扎营", ""),
    (2, "D2", "牧屋4162 → 五号调水库3525", "14.3 km", "+881 / -1516 m", "8–10 h", "最高 4532 m",
     "海子、牧场、桥与水库较多，取水须净化", "五号营地、黑海子或黄金牧场", ""),
    (3, "D3", "五号调水库3525 → 斯达纳错4465", "11.4 km", "+1578 / -632 m", "10–12 h", "最高 4829 m",
     "前 3.2 km 河流；随后约 6 km 无明确水点", "藏益措 4100 m 或 牧屋 4247 m", "本日全天最重：翻桃海子垭口（4829 m）"),
    (4, "D4", "斯达纳错4465 → 沙冲乡3323", "15.2 km", "+237 / -1385 m", "6–8 h", "最高 4652 m",
     "斯达纳错、小海子及下古洛沟附近可取水", "终点沙冲乡，正常计划当天出山", "全程以下降为主，注意碎石坡与膝部保护"),
]

def day_card(i, tag, route, dist, gain, dur, top, water, camp, note):
    col = [D1_C, D2_C, D3_C, D4_C][i - 1]
    note_html = f'<div class="dnote">{note}</div>' if note else ""
    return f'''<article class="day" style="--c:{col}">
      <div class="day-head"><span class="dtag">{tag}</span><span class="droute">{route}</span></div>
      <div class="drow">
        <span class="dnum">{dist}</span><span class="dsep">|</span>
        <span class="dnum">{gain}</span><span class="dsep">|</span>
        <span class="dnum">{dur}</span><span class="dsep dsep2">|</span>
        <span class="dtop">{top}</span>
      </div>
      <div class="dline"><span class="dkey">水源</span><span class="dval">{water}</span></div>
      <div class="dline"><span class="dkey">营地</span><span class="dval">{camp}</span></div>
      {note_html}
    </article>'''

# ---------------------------------------------------------------- HTML
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#EFEBE4;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif;color:#1F2430;-webkit-font-smoothing:antialiased}
.page{width:1120px;margin:0 auto;padding:26px 26px 34px;background:#FBF8F4}
.hd{display:flex;align-items:center;gap:12px;padding:2px 2px 10px}
.hd .badge{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:700;color:#0F6E5C;background:#E4F2EE;border:1px solid #CBE6DF;border-radius:999px;padding:5px 12px}
.hd .badge i{width:8px;height:8px;background:#0F6E5C;transform:rotate(45deg);display:inline-block;border-radius:1.5px}
.hd .meta{margin-left:auto;font-size:12.5px;color:#68717E;letter-spacing:.4px}
h1{margin:6px 2px 4px;font-size:40px;line-height:1.15;letter-spacing:-.5px}
h1 .lite{font-size:20px;color:#5A6270;font-weight:600;letter-spacing:0}
.sub{margin:0 2px 18px;font-size:15.5px;font-weight:600;color:#5A6270}
.sub em{font-style:normal;color:#0F6E5C}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.stat{background:#fff;border:1px solid #EAE3D9;border-radius:14px;padding:16px 18px 14px;box-shadow:0 1px 2px rgba(31,36,48,.03)}
.stat b{display:block;font-size:26px;letter-spacing:-.4px;line-height:1.2}
.stat span{display:block;margin-top:5px;font-size:12.5px;color:#68717E;font-weight:600}
.panel{background:#fff;border:1px solid #EAE3D9;border-radius:16px;padding:16px 18px 18px;margin-bottom:20px;box-shadow:0 1px 2px rgba(31,36,48,.03)}
.ph{display:flex;align-items:baseline;gap:10px;margin:0 2px 12px}
.ph h2{margin:0;font-size:19px;letter-spacing:.2px}
.ph .tagn{font-size:12.5px;color:#68717E;font-weight:600;letter-spacing:.5px}
.ph .right{margin-left:auto;font-size:13px;color:#B23A1C;font-weight:700}
.ph .right.n{color:#68717E;font-weight:600}
svg{display:block;width:100%;height:auto}
.legend{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px;padding-top:14px;border-top:1px dashed #E7E0D6}
.lg{display:flex;align-items:center;gap:9px;font-size:13px;color:#3A4250}
.lg i{width:22px;height:5px;border-radius:3px;flex:0 0 auto}
.lg b{font-weight:800}
.lg span{color:#68717E;font-size:12px;margin-left:2px}
.days{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}
.day{background:#fff;border:1px solid #EAE3D9;border-radius:14px;padding:14px 16px 15px;border-top:4px solid var(--c);box-shadow:0 1px 2px rgba(31,36,48,.03)}
.day-head{display:flex;align-items:baseline;gap:9px;margin-bottom:9px}
.dtag{font-size:20px;font-weight:900;color:var(--c);letter-spacing:-.3px}
.droute{font-size:15.5px;font-weight:700}
.drow{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:13.5px;margin-bottom:10px}
.dnum{font-weight:800;color:#2A3140}
.dsep{color:#C9BFAE}
.dtop{color:#B23A1C;font-weight:800}
.dline{display:flex;gap:9px;font-size:13px;line-height:1.6;margin-bottom:3px}
.dkey{flex:0 0 34px;font-weight:800;color:#0F6E5C}
.dval{color:#4A5261}
.dnote{margin-top:8px;font-size:12.5px;color:#7A6357;background:#FBF3EC;border:1px solid #F1E2D5;border-radius:8px;padding:6px 9px}
.notes{background:#fff;border:1px solid #EAE3D9;border-radius:16px;padding:16px 18px 18px;margin-bottom:20px}
.ngrid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.nbox h3{margin:0 0 8px;font-size:14.5px;color:#0F6E5C;letter-spacing:.2px}
.nbox ul{margin:0;padding-left:0;list-style:none}
.nbox li{font-size:12.8px;line-height:1.72;color:#4A5261;padding-left:14px;position:relative}
.nbox li:before{content:"";position:absolute;left:2px;top:9px;width:5px;height:5px;border-radius:50%;background:#C9D8D3}
.plan{display:flex;align-items:center;gap:18px;background:linear-gradient(90deg,#F4F7F5,#FAF7F3);border:1px solid #E4EBE7;border-radius:14px;padding:14px 18px;margin-bottom:18px}
.plan .pl{font-size:13px;color:#0F6E5C;font-weight:800;letter-spacing:.5px;white-space:nowrap}
.plan .steps{display:flex;align-items:center;gap:12px;font-size:14px;font-weight:700;flex-wrap:wrap}
.plan .steps i{font-style:normal;color:#AFBAB5}
.plan .tagline{margin-left:auto;font-size:13px;color:#5A6270;font-weight:600}
.foot{font-size:12px;color:#68717E;line-height:1.7;padding:0 4px}
.foot b{color:#4A5261}
/* 「数字＋单位」「区间」不可断行（否则窄版会把 18.5–20 km 拆成「18.5–」换行「20 km」）。
   生成时用 nb() 包一层；只认"有空格"的写法，别把 559m 改成 559 m。 */
.nb{white-space:nowrap}
/* 只在窄版生效的那一份文案/换行，桌面默认隐藏 */
.stat span.monly,.ms,.brm{display:none}
"""

# ---------------------------------------------------------------- 手机版（响应式）
# 长图要在手机上按满宽看，版心设计宽度就必须接近手机宽度 ——
# 2344 px 宽的图在 390 px 屏上缩放比只有 0.166，13 px 正文落到屏幕上是 2 px。
# 这段单栏 CSS 同时服务两件事：① HTML 在手机上直接可读；
# ② 用同一个 HTML 截一张窄版长图（**裁切按像素量版心边界，别按窗宽算** ——
#    无头 Chrome 有最小窗宽 500，按窗宽算会把右侧整条切掉、每行末尾缺字）。
#
# 窄版不只是"把桌面折成一列"，还要单独处理三类排版事故（详见
# references/legend-and-color.md §8.3.1）：卡片内长标签折出孤字 → 换短文案；
# 行末悬空的分隔符 | → 隐藏那一个并主动换行；句中 18.5– / 20 km 被拆 → .nb 包住。
MOBILE_CSS = """
@media (max-width:900px){
  body{background:#FBF8F4}
  .page{width:100%;max-width:430px;padding:16px 15px 24px}
  .hd{flex-wrap:wrap;gap:8px;padding-bottom:8px}
  .hd .meta{margin-left:0;width:100%;font-size:12.5px;line-height:1.5}
  h1{font-size:30px;line-height:1.25}
  h1 .lite{font-size:16px}
  .sub{font-size:14.5px;line-height:1.7;margin-bottom:16px}
  .stats{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
  .stat{padding:12px 14px 11px}
  .stat b{font-size:19px}          /* 19 px 是「+3081 / −3371 m」在 2 列网格里不折行的上限 */
  .stat span{font-size:12.2px}
  /* 卡片内宽 ≈167 px ≈ 13 个汉字，长标签必然折行并留孤字 → 用短文案，
     四张卡才都是单行、等高（注意要带 .stat 前缀才压得住 .stat span{display:block}） */
  .stat span.donly{display:none}
  .stat span.monly{display:block}
  /* 副标题折成整句，不再出现行末孤零零的「|」 */
  .ds{display:none}
  .ms{display:inline}
  .brm{display:inline}
  .panel{border-radius:14px;padding:14px 15px 16px;margin-bottom:16px}
  .ph{flex-wrap:wrap;gap:6px 10px}
  .ph h2{font-size:18px}
  .ph .right{margin-left:0;width:100%;font-size:12.5px}
  .days{grid-template-columns:1fr;gap:12px}
  .dtag{font-size:21px}
  .droute{font-size:15.5px}
  .drow{font-size:14.6px}
  /* 末尾那个分隔符会悬在上一行行尾 → 隐藏它，并把要换行的项主动推到下一行 */
  .drow>.dsep2{display:none}
  .drow>.dtop{flex:0 0 100%}
  .dline{font-size:14.2px;line-height:1.68}
  .dnote{font-size:13.6px;padding:8px 11px}
  .ngrid{grid-template-columns:1fr;gap:15px}
  .nbox h3{font-size:15.5px}
  .nbox li{font-size:14.2px;line-height:1.8}
  .legend{grid-template-columns:1fr 1fr;gap:9px}
  .lg{font-size:14px}
  .lg span{font-size:13.2px}
  .plan{flex-direction:column;align-items:flex-start;gap:9px}
  .plan .tagline{margin-left:0;text-align:left;font-size:13.6px}
  .foot{font-size:12.8px;line-height:1.8}
}
"""

HTML = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>党岭拉东线（精简版）· 重装穿越路线攻略</title>
<style>{CSS}{MOBILE_CSS}</style>
</head>
<body>
<div class="page">
  <div class="hd">
    <span class="badge"><i></i>川西 · 丹巴 &nbsp;|&nbsp; 重装穿越路线图</span>
    <span class="meta">WGS84 · 轨迹口径</span>
  </div>
  <h1>党岭拉东线 <span class="lite">（精简版）</span></h1>
  <p class="sub">4 天 3 晚 &nbsp;|&nbsp; <em>东谷乡进 · 沙冲乡出</em> &nbsp;|&nbsp; 51.95 km 全程无补给重装穿越</p>

  <div class="stats">
    <div class="stat"><b>51.95 km</b><span>总里程</span></div>
    <div class="stat"><b>+4467 / -3823 m</b><span>累计爬升 / 下降</span></div>
    <div class="stat"><b>7 个</b><span>命名垭口</span></div>
    <div class="stat"><b>4829 m</b><span>最高点 · 桃海子垭口</span></div>
  </div>

  <section class="panel">
    <div class="ph"><h2>全线地图</h2><span class="tagn">路线走向示意 · 非导航用图</span>
      <span class="right n">北为上 · 比例尺 5 km</span></div>
    {MAP_SVG}
    <div class="legend">
      <div class="lg"><i style="background:{D1_C}"></i><b>D1</b> 东谷乡 → 牧屋4162<span>11.1 km</span></div>
      <div class="lg"><i style="background:{D2_C}"></i><b>D2</b> 牧屋4162 → 五号调水库<span>14.3 km</span></div>
      <div class="lg"><i style="background:{D3_C}"></i><b>D3</b> 五号调水库 → 斯达纳错<span>11.4 km</span></div>
      <div class="lg"><i style="background:{D4_C}"></i><b>D4</b> 斯达纳错 → 沙冲乡<span>15.2 km</span></div>
    </div>
  </section>

  <section class="panel">
    <div class="ph"><h2>全程海拔剖面</h2>
      <span class="right">最高点：P6 桃海子垭口 4829 m</span></div>
    {PROFILE_SVG}
    <div class="foot" style="margin-top:10px;padding:0 2px">P1–P7 为沿途命名垭口（共 7 个，原图未单独标注 P2）；海拔为轨迹统计值，营地海拔为实走参考值。</div>
  </section>

  <section class="ph" style="margin:0 2px 10px"><h2>逐日攻略</h2><span class="tagn">里程 / 爬升 / 用时 / 水源 / 营地</span></section>
  <div class="days">
    {''.join(day_card(*c) for c in DAY_CARDS)}
  </div>

  <section class="plan">
    <span class="pl">出行安排</span>
    <span class="steps">10/2 避峰出行 <i>›</i> 10/3 进山 <i>›</i> 10/6 晚返回成都</span>
    <span class="tagline">4 天 3 晚 · 全程无补给、无信号、无外部协助 · 需重装经验，不适合新手</span>
  </section>

  <section class="notes">
    <div class="ph" style="margin-bottom:12px"><h2>关键提示</h2><span class="tagn">水源 · 路况 · 风险 · 装备</span></div>
    <div class="ngrid">
      <div class="nbox">
        <h3>水源与补水</h3>
        <ul>
          <li><b>D1 全程无稳定水点</b>：出发时按 2–3 L 备足，起点附近补满</li>
          <li><b>D3 中段约 6 km 无水</b>：过河后必须背足当日及次日用水</li>
          <li>海子、溪流、牧场水源一律净化/煮沸后饮用</li>
          <li>营地全部在 3500–4500 m，夜间结冰，睡前给水壶保温</li>
        </ul>
      </div>
      <div class="nbox">
        <h3>路况与强度</h3>
        <ul>
          <li>累计爬升 4467 m，主要集中在 <b>D1（+1770）与 D3（+1578）</b></li>
          <li><b>D3 全天最重</b>：11.4 km 走 10–12 h，需翻 4829 m 桃海子垭口</li>
          <li>石海、碎石坡、陡坡为主，下降日（D2/D4）注意膝部与崴脚</li>
          <li>D4 以下降收尾，正常计划当天出山，不宜拖延</li>
        </ul>
      </div>
      <div class="nbox">
        <h3>风险与应急</h3>
        <ul>
          <li>高海拔 + 连续重装：务必提前 1–2 天适应，备葡萄糖、氧气瓶</li>
          <li>山区联通/电信基本无信号，建议携带卫星通信或北斗短报文</li>
          <li>垭口处天气突变快，午后不强行翻越，设关门时间</li>
          <li>10 月垭口可能积雪结冰，备冰爪/雪套；涉水点防滑</li>
          <li>保险、现金、应急路线与后方联络人提前落实</li>
        </ul>
      </div>
    </div>
  </section>

  <div class="foot">
    <b>数据说明：</b>本攻略依据组织者提供的路线海报与轨迹文件（WGS84）整理；地图为走向示意图，仅用于了解路线结构，不可作为导航使用。
    里程、爬升下降与海拔均为轨迹统计值，逐日"最高点"为该日轨迹最高海拔，实际以现场轨迹、天气与队伍决策为准。
  </div>
</div>
</body>
</html>'''

(OUT_DIR / "党岭拉东线-攻略.html").write_text(HTML, encoding="utf-8")
print("written:", OUT_DIR / "党岭拉东线-攻略.html")

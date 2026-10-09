# -*- coding: utf-8 -*-
"""【兜底模板 · 不在 make_all 链里】手绘方案：**完全拿不到经纬度轨迹**时才用。

正常情况请走真实轨迹流水线（`prep_track.py` → `new_route.py` → `make_all.py`），
那份是真实经纬度投影、可直接导航；本文件产出的图是"走向示意"，**不可导航**。

用法：**复制本文件**改成自己的脚本，然后按下面的分区替换。本文件自带的
「示例线路」是一组明显的小样例（2 天、12 km），点开就能看出结构；
它存在的意义是让骨架**能跑起来**，不是给你一套真实数据 ——
旧版这里塞的是党岭拉东线整套数据，抄过去的人要删掉几十行才知道自己要填什么。

要替换的只有 ②③④ 三个分区，① ⑤ 不用动：

    ① 画布与配色        W/H、分日色，一般不动
    ② 线路几何          ★ 关键点坐标 + 每日折线（按"实走方向在图上怎么走"手摆）
    ③ 地形示意          ★ 山脊链（等高线质感）、水系、海子
    ④ 逐日与文案        ★ 数据条 / 图例 / 剖面标注 / 逐日卡 / 提示 / 落款，全在这里
    ⑤ 渲染引擎          出图逻辑，改版面时才动

```bash
python scripts/build_route_guide.py     # → scripts/out_html/<STEM>-攻略.html
```
只依赖标准库；输出单文件 HTML，字体走系统字体栈，不引用任何 CDN，离线可渲染。
"""
import math
import random
from pathlib import Path

# ★ 输出目录：默认脚本同级 out_html/ —— 绝不写死成某台机器的绝对路径
OUT_DIR = Path(__file__).resolve().parent / "out_html"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STEM = "示例线路"

# ============================================================ ① 画布与配色
W, H = 1000, 552
C1, C2, C3, C4 = "#E4572E", "#1E9E76", "#6C4FD8", "#2D7FF9"      # 分日色
COLORS = [C1, C2, C3, C4]
OK = "#0F6E5C"          # 强调绿（小标题、关键词）
HI = "#B23A1C"          # 强调红（最高点、风险）
INK = "#2A3140"         # 正文黑
GREY = "#68717E"        # 次级文字

# ============================================================ ② 线路几何（★）
# 坐标是**画布坐标**（1000×552），不是经纬度；按"实走方向在图上怎么走"手摆。
# 技巧：把每日折线多插几个中间点并让它左右摆动，线才像是沿着山势走的，
# 直挺挺两点连线一眼就假。
S_PT = (820, 508)        # ★ 起点
C1_PT = (690, 330)       # ★ 第 1 晚营地 / 分日点
P_MAIN = (470, 210)      # ★ 主垭口
E_PT = (180, 388)        # ★ 终点

D1_ROUTE = [S_PT, (812, 486), (818, 462), (806, 440), (798, 418), (804, 396),
            (790, 372), (778, 352), (786, 340), (760, 336), C1_PT]
D2_ROUTE = [C1_PT, (664, 322), (640, 318), (612, 306), (586, 292), (556, 276),
            (528, 258), (500, 238), (478, 220), P_MAIN, (438, 214), (404, 226),
            (368, 246), (334, 268), (300, 292), (266, 318), (232, 348), E_PT]
ROUTES = [D1_ROUTE, D2_ROUTE]
DAY_BOUNDS = [C1_PT]     # 分日界（画剖面竖虚线用；多日就往里加）

# ============================================================ ③ 地形示意（★）
# 等高线质感来自「山脊链」：每条链上撒一串同心椭圆，交叠起来就是山脊—河谷格局。
# 它**不表示真实地形**，只是让底图别是一块白板。旋转角 ROT 决定山脊走向。
ROT = -0.5
CHAINS = [
    ((70, 40), (320, 300), 5, 74), ((360, 30), (560, 210), 4, 68),
    ((600, 40), (800, 240), 4, 70), ((840, 30), (980, 190), 4, 64),
    ((60, 250), (270, 470), 4, 72), ((310, 250), (490, 440), 4, 66),
    ((540, 300), (710, 500), 4, 68), ((760, 300), (930, 480), 4, 70),
    ((120, 430), (340, 560), 4, 68), ((400, 470), (610, 570), 4, 66),
]
RIVERS = [
    [(250, 552), (300, 505), (360, 458), (430, 422), (500, 398), (570, 382),
     (650, 375), (760, 378)],
    [(905, 150), (886, 245), (866, 335), (850, 435), (842, 552)],
    [(30, 442), (88, 416), (148, 399), (205, 393), (262, 401), (325, 417)],
]
# (cx, cy, rx, ry, 旋转, 名字) —— 名字只用于你自己对照，图上不画
LAKES = [
    (462, 176, 14, 7, -0.22, "示例海子"),
    (596, 300, 10, 6, -0.35, "营地水塘"),
]

# ============================================================ ④ 逐日与文案（★）
PROF = [                 # 剖面折线：(累计 km, 海拔 m)。★ 峰值点必须与逐日卡对得上
    (0.0, 2600), (1.5, 2900), (3.0, 3300), (4.5, 3700), (6.0, 4100), (6.8, 4300),
    (7.5, 4180), (9.0, 3900), (10.5, 3500), (12.0, 3100),
]
PEAKS_LBL = [(6.8, 4300, "P1")]          # 剖面高点：(km, 海拔, 标签)
BOT_LBL = [(0.0, 2600, "起点 2600", "start"),
           (7.5, 4180, "营地 4180", "middle"),
           (12.0, 3100, "终点 3100", "end")]

DATA = {
    "page_title": f"{STEM} · 徒步攻略",
    "title": STEM,
    "title_lite": "（2 天 · 手绘示意）",
    "badge": "★ 省市 | 线路方向",
    "meta": "WGS84 · 手绘示意图",
    "sub": "2 天 1 晚 &nbsp;|&nbsp; <em>起点进 · 终点出</em> &nbsp;|&nbsp; 12 km 示意线",
    # 数据条四格：(数值, 桌面副标题, 窄版副标题)。
    # ⚠ 窄版副标题别把数值抄一遍 —— 否则手机上会出现「12 km / 12 km」
    "stats": [
        ("12.0 km", "总里程（示意）", "总里程 · 示意"),
        ("+1700 / −1200 m", "累计爬升 / 下降", "累计爬升 / 下降"),
        ("4300 m", "最高点 · P1 垭口", "最高点 · P1 垭口"),
        ("2 天 1 晚", "建议行程", "建议行程"),
    ],
    # 图例：(颜色, 日标, 路段说明, 距离标签)
    "legend": [
        (C1, "D1", "起点 → 营地", "6.0 km"),
        (C2, "D2", "营地 → 终点", "6.0 km"),
    ],
    "legend_foot": "路线走向示意 · 非导航用图",
    "map_right": "北为上 · 比例尺 5 km",
    "prof_right": "最高点：P1 垭口 4300 m",
    "prof_note": "P1 为示意垭口；海拔为示意值。手绘方案的数值请以现场轨迹为准。",
    # 逐日卡：(序号, 日标, 路线串, 里程, 升降, 用时, 最高点, 水源, 营地, 提示)
    "cards": [
        (1, "D1", "起点 → P1 垭口 → 营地", "6.0 km", "+1700 / −300 m", "6–8 h",
         "最高 4300 m", "★ 水源情况", "★ 营地位置", "★ 当日强度提示"),
        (2, "D2", "营地 → 河谷 → 终点", "6.0 km", "+0 / −900 m", "4–5 h",
         "最高 4180 m", "★ 水源情况", "★ 是否当日出山", "全程下降为主，注意膝部"),
    ],
    "plan": ("出行安排", "D1 进山 <i>›</i> D2 出山",
             "★ 季节 / 补给 / 强度一句话说清"),
    "notes": [
        ("水源与补给", ["★ 哪段无水、要背多少", "★ 取水是否需净化"]),
        ("路况与强度", ["★ 累计爬升集中在哪天", "★ 最难的一段在哪、为什么不建议硬闯"]),
        ("风险与应急", ["★ 天气突变 / 失温 / 迷路的具体应对",
                        "★ 信号与下撤预案；本图为示意，不可导航"]),
    ],
    "foot": ("<b>数据说明：</b>本图由手绘方案生成，**走向示意，不可作为导航使用**。"
             "★ 数值来源与口径在此写清。"),
}

# ============================================================ ⑤ 渲染引擎
def polyline(pts, color, w=5.0, opacity=1.0):
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return (f'<path d="{d}" fill="none" stroke="#FFFFFF" stroke-width="{w + 4.5:.1f}" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>'
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w:.1f}" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="{opacity}"/>')


def ring(cx, cy, rx, ry, seed, rot=0.0, wob=0.11, n=54):
    """抖动椭圆：给等高线一点"手绘的呼吸感"，正椭圆看起来像塑料。"""
    rnd = random.Random(seed)
    ph = [rnd.uniform(0, 6.283) for _ in range(3)]
    amp = [rnd.uniform(0.5, 1.0), rnd.uniform(0.2, 0.5), rnd.uniform(0.1, 0.3)]
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + wob * (amp[0] * math.sin(3 * a + ph[0])
                       + amp[1] * math.sin(5 * a + ph[1])
                       + amp[2] * math.sin(2 * a + ph[2]))
        x, y = rx * r * math.cos(a), ry * r * math.sin(a)
        pts.append((cx + x * math.cos(rot) - y * math.sin(rot),
                    cy + x * math.sin(rot) + y * math.cos(rot)))
    return "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts) + " Z"


def smooth(pts, tension=0.5):
    """折线 → 平滑三次贝塞尔（Catmull-Rom 转 Bezier），画河流用。"""
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


_rnd = random.Random(3721)
PEAKS = []
for (x1, y1), (x2, y2), n, r in CHAINS:
    dx, dy = x2 - x1, y2 - y1
    ln = math.hypot(dx, dy) or 1
    nx, ny = -dy / ln, dx / ln          # 法向：让同一条链上的椭圆错开一点
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
            heavy = (i % 3 == 0)         # 每 3 条一根计曲线，比全同粗细更像地形图
            col = "#D6CBB4" if heavy else "#E4DBC8"
            fill = 'fill="#F2ECDD" opacity="0.8"' if i == 0 else 'fill="none"'
            out.append(f'<path d="{ring(cx, cy, rx * f, ry * f, idx * 29 + i, ROT, wob=0.13, n=56)}" '
                       f'{fill} stroke="{col}" stroke-width="{0.8 if heavy else 0.55}"/>')
    return "\n    ".join(out)


def build_lakes():
    return "\n    ".join(
        f'<path d="{ring(cx, cy, rx, ry, int(cx * 7 + cy), rot, wob=0.16, n=40)}" '
        f'fill="#A9CFE8" stroke="#7FB4D4" stroke-width="1"/>'
        for cx, cy, rx, ry, rot, _ in LAKES)


def build_rivers():
    out = []
    for r in RIVERS:
        out.append(f'<path d="{smooth(r)}" fill="none" stroke="#B7D2E4" '
                   f'stroke-width="2.6" stroke-linecap="round"/>')
        out.append(f'<path d="{smooth(r)}" fill="none" stroke="#FFFFFF" '
                   f'stroke-width="0.9" opacity="0.55"/>')
    return "\n    ".join(out)


def mk_marker(x, y, fill, label=None, r=11, tcol="#FFFFFF", fs=10.5, sw=2.6):
    s = f'<circle cx="{x}" cy="{y}" r="{r + 2.4}" fill="#FFFFFF" opacity="0.95"/>'
    s += f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="#FFFFFF" stroke-width="{sw}"/>'
    if label:
        s += (f'<text x="{x}" y="{y + 3.7}" text-anchor="middle" font-size="{fs}" '
              f'font-weight="700" fill="{tcol}">{label}</text>')
    return s


def mk_pass(x, y, label=None, ly=-18, anchor="middle"):
    """垭口符号。label=None 时不写字 —— 别让每个符号都挂个名字，图会糊。"""
    s = (f'<circle cx="{x}" cy="{y}" r="9.5" fill="#FFFFFF" opacity="0.95"/>'
         f'<circle cx="{x}" cy="{y}" r="7.6" fill="#3A4250"/>'
         f'<path d="M {x - 4.4},{y + 3.2} L {x - 1.1},{y - 2.6} L {x + 0.4},{y + 0.2} '
         f'L {x + 2.0},{y - 3.4} L {x + 4.6},{y + 3.2} Z" fill="#FFFFFF"/>')
    if label:
        s += (f'<text x="{x}" y="{y + ly}" text-anchor="{anchor}" font-size="12.5" '
              f'font-weight="700" fill="{INK}" stroke="#FFFFFF" stroke-width="3.4" '
              f'paint-order="stroke" stroke-linejoin="round">{label}</text>')
    return s


def mk_lbl(x, y, txt, anchor="start", fs=12.5, fill=None, weight=700, stroke=3.4):
    """带白描边的文字：叠在等高线上也读得清（比给文字加底色干净）。"""
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{fs}" '
            f'font-weight="{weight}" fill="{fill or INK}" stroke="#FFFFFF" '
            f'stroke-width="{stroke}" paint-order="stroke" stroke-linejoin="round">{txt}</text>')


def build_map():
    d1, d2 = D1_ROUTE, D2_ROUTE
    return f'''<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{DATA["title"]}全线地图">
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
    {polyline(d1, C1)}
    {polyline(d2, C2)}
    {mk_pass(*P_MAIN, "P1 垭口", ly=-16)}
    {mk_marker(*C1_PT, C1, "C1")}
    {mk_marker(*S_PT, "#D93B2B", "S", r=11)}
    {mk_marker(*E_PT, "#2D7FF9", "E", r=11)}
    {mk_lbl(S_PT[0] - 12, S_PT[1] + 22, "起点", "end", 13)}
    {mk_lbl(C1_PT[0] + 14, C1_PT[1] + 4, "C1 营地 4180m", "start")}
    {mk_lbl(P_MAIN[0], P_MAIN[1] - 30, "P1 垭口 4300m 全程最高", "middle", 12.5, HI)}
    {mk_lbl(E_PT[0] - 12, E_PT[1] + 22, "终点", "start", 13)}
    <!-- 指北针：缩到 r=20 别抢戏，放在角上不压路线 -->
    <g transform="translate(944,50)">
      <circle r="20" fill="#FFFFFF" opacity="0.88"/>
      <path d="M 0,-14 L 5.5,6 L 0,2.5 L -5.5,6 Z" fill="#3A4250"/>
      <text y="19" text-anchor="middle" font-size="10" font-weight="700" fill="#3A4250">N</text>
    </g>
    <!-- 比例尺：长度只是示意，手绘方案没有真实比例 -->
    <g transform="translate(40,506)">
      <rect x="-10" y="-22" width="186" height="40" rx="8" fill="#FFFFFF" opacity="0.86"/>
      <text x="0" y="-5" font-size="11.5" font-weight="700" fill="#3A4250">5 km（示意）</text>
      <line x1="0" y1="6" x2="160" y2="6" stroke="#3A4250" stroke-width="2.2"/>
      <line x1="0" y1="0" x2="0" y2="12" stroke="#3A4250" stroke-width="2.2"/>
      <line x1="160" y1="0" x2="160" y2="12" stroke="#3A4250" stroke-width="2.2"/>
    </g>
  </g>
</svg>'''


# ---------------------------------------------------------------- 剖面
PW, PH = 1000, 336
PL, PR, PT, PB = 62, 976, 46, 292
_TOTAL_KM = PROF[-1][0]
_ELE_LO = int(min(m for _, m in PROF) // 500 * 500)
_ELE_HI = int(-(-max(m for _, m in PROF) // 500 * 500))
KX = (PR - PL) / _TOTAL_KM
KY = (PB - PT) / max(_ELE_HI - _ELE_LO, 1)


def X(km):
    return PL + km * KX


def Y(m):
    return PB - (m - _ELE_LO) * KY


def build_profile():
    o = []
    for m in range(_ELE_LO, _ELE_HI + 1, 500):
        o.append(f'<line x1="{PL}" y1="{Y(m):.1f}" x2="{PR}" y2="{Y(m):.1f}" '
                 f'stroke="#E9E3D9" stroke-width="1"/>')
        o.append(f'<text x="{PL - 10}" y="{Y(m) + 4:.1f}" text-anchor="end" '
                 f'font-size="11.5" fill="#8C93A0">{m}</text>')
    for km in range(0, int(_TOTAL_KM) + 1, 5):
        o.append(f'<line x1="{X(km):.1f}" y1="{PT - 8}" x2="{X(km):.1f}" y2="{PB}" '
                 f'stroke="#EDE7DD" stroke-width="1"/>')
        o.append(f'<text x="{X(km):.1f}" y="{PB + 18}" text-anchor="middle" '
                 f'font-size="11.5" fill="#8C93A0">{km}</text>')
    o.append(f'<line x1="{PL}" y1="{PB}" x2="{PR}" y2="{PB}" stroke="#CFC6B8" stroke-width="1.2"/>')
    o.append(f'<text x="{(PL + PR) / 2:.0f}" y="{PH - 6}" text-anchor="middle" '
             f'font-size="12" fill="#6C7480">累计里程 (km)</text>')
    o.append(f'<text x="18" y="{(PT + PB) / 2:.0f}" font-size="12" fill="#6C7480" '
             f'transform="rotate(-90 18 {(PT + PB) / 2:.0f})" text-anchor="middle">海拔 (m)</text>')

    # 分日填色 + 描边；边界取 PROF 首尾 + 分日界的里程（去重后再切）
    mids = sorted({p[0] for p in PROF if any(abs(p[0] - b) < 1e-9 for b in _bound_km())})
    edges = [0.0] + [m for m in mids if 0.0 < m < _TOTAL_KM] + [_TOTAL_KM]
    for k, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        col = COLORS[k % len(COLORS)]
        seg = [p for p in PROF if a <= p[0] <= b]
        if len(seg) < 2:
            continue
        if seg[0][0] > a:
            seg = [(a, seg[0][1])] + seg
        if seg[-1][0] < b:
            seg = seg + [(b, seg[-1][1])]
        d = "M " + " L ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in seg)
        o.append(f'<path d="{d} L {X(seg[-1][0]):.1f},{PB} L {X(seg[0][0]):.1f},{PB} Z" '
                 f'fill="{col}" opacity="0.15"/>')
        o.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.6" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
        o.append(f'<text x="{(X(a) + X(b)) / 2:.1f}" y="{PT - 14}" text-anchor="middle" '
                 f'font-size="14" font-weight="800" fill="{col}">D{k + 1}</text>')
    for km in _bound_km():
        o.append(f'<line x1="{X(km):.1f}" y1="{PT - 4}" x2="{X(km):.1f}" y2="{PB}" '
                 f'stroke="#B9B2A6" stroke-width="1.1" stroke-dasharray="4 4"/>')
    _hi_m = max((x[1] for x in PEAKS_LBL), default=None)
    for km, m, tag in PEAKS_LBL:
        hi = (m == _hi_m)
        col = HI if hi else "#3A4250"
        o.append(f'<circle cx="{X(km):.1f}" cy="{Y(m):.1f}" r="{5.0 if hi else 3.6}" '
                 f'fill="{col}" stroke="#FFFFFF" stroke-width="1.6"/>')
        o.append(f'<text x="{X(km):.1f}" y="{Y(m) - 10:.1f}" text-anchor="middle" '
                 f'font-size="{13 if hi else 12}" font-weight="800" fill="{col}">{tag}</text>')
    for km, m, txt, anc in BOT_LBL:
        x, y = X(km), Y(m)
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="#FFFFFF" '
                 f'stroke="#3A4250" stroke-width="2"/>')
        a2 = "start" if anc == "start" else ("end" if anc == "end" else "middle")
        tx = x + 9 if anc == "start" else (x - 6 if anc == "end" else x)
        o.append(f'<text x="{tx:.1f}" y="{y + 22:.1f}" text-anchor="{a2}" font-size="12" '
                 f'font-weight="700" fill="#3A4250" stroke="#FFFFFF" stroke-width="3.2" '
                 f'paint-order="stroke" stroke-linejoin="round">{txt}</text>')
    return "\n    ".join(o)


def _bound_km():
    """分日界的里程值：把 DAY_BOUNDS 里的画布点换算成它在 PROF 上的里程。
    （手绘方案没有真实里程，取折线上离该点最近的一点的 km。）"""
    out = []
    for bx, by in DAY_BOUNDS:
        best = min(PROF, key=lambda p: (X(p[0]) - bx) ** 2 + (Y(p[1]) - by) ** 2)
        out.append(best[0])
    return out


def build_profile_svg():
    return (f'<svg viewBox="0 0 {PW} {PH}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="全程海拔剖面">'
            f'<rect x="0" y="0" width="{PW}" height="{PH}" fill="#FFFFFF" rx="14"/>'
            f'<g>{build_profile()}</g></svg>')


# ---------------------------------------------------------------- 逐日卡
def day_card(i, tag, route, dist, gain, dur, top, water, camp, note):
    col = COLORS[(i - 1) % len(COLORS)]
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
/* 「数字＋单位」「区间」不可断行，否则窄版会把 18.5–20 km 拆成「18.5–」换行「20 km」 */
.nb{white-space:nowrap}
/* 只在窄版生效的那一份文案，桌面默认隐藏 */
.stat span.monly,.ms,.brm{display:none}
"""

# 手机（窄版）：HTML 本身就是响应式的，手机上直接看页面即可，
# **不再另出一版手机长图**。这段 CSS 同时服务两件事：
#   ① 手机上页面直接可读；② 窄版排版的三类事故（孤字 / 悬空分隔符 / 数字被拆行）
# 无头 Chrome 有最小窗宽 500：若要截窄图，**必须按像素量版心边界裁，别按窗宽反推**。
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
  .stat b{font-size:19px}
  .stat span{font-size:12.2px}
  .stat span.donly{display:none}
  .stat span.monly{display:block}
  .panel{border-radius:14px;padding:14px 15px 16px;margin-bottom:16px}
  .ph{flex-wrap:wrap;gap:6px 10px}
  .ph h2{font-size:18px}
  .ph .right{margin-left:0;width:100%;font-size:12.5px}
  .days{grid-template-columns:1fr;gap:12px}
  .dtag{font-size:21px}
  .droute{font-size:15.5px}
  .drow{font-size:14.6px}
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

D = DATA


def _stats_html():
    # 桌面副标题 / 窄版副标题各写各的：窄版若把数值抄一遍，手机上会出现「12.0 km / 12.0 km」
    return "".join(f'<div class="stat"><b>{a}</b><span class="donly">{b}</span>'
                   f'<span class="monly">{c}</span></div>' for a, b, c in D["stats"])


def _legend_html():
    return "".join(f'<div class="lg"><i style="background:{c}"></i><b>{tag}</b> {desc}'
                   f'<span>{dist}</span></div>' for c, tag, desc, dist in D["legend"])


def _notes_html():
    return "".join(f'<div class="nbox"><h3>{t}</h3><ul>'
                   + "".join(f"<li>{i}</li>" for i in items) + "</ul></div>"
                   for t, items in D["notes"])


HTML = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{D["page_title"]}</title>
<style>{CSS}{MOBILE_CSS}</style>
</head>
<body>
<div class="page">
  <div class="hd">
    <span class="badge"><i></i>{D["badge"]}</span>
    <span class="meta">{D["meta"]}</span>
  </div>
  <h1>{D["title"]} <span class="lite">{D["title_lite"]}</span></h1>
  <p class="sub">{D["sub"]}</p>

  <div class="stats">{_stats_html()}</div>

  <section class="panel">
    <div class="ph"><h2>全线地图</h2><span class="tagn">路线走向示意 · 非导航用图</span>
      <span class="right n">{D["map_right"]}</span></div>
    {build_map()}
    <div class="legend">{_legend_html()}</div>
  </section>

  <section class="panel">
    <div class="ph"><h2>全程海拔剖面</h2>
      <span class="right">{D["prof_right"]}</span></div>
    {build_profile_svg()}
    <div class="foot" style="margin-top:10px;padding:0 2px">{D["prof_note"]}</div>
  </section>

  <section class="ph" style="margin:0 2px 10px"><h2>逐日攻略</h2>
    <span class="tagn">里程 / 爬升 / 用时 / 水源 / 营地</span></section>
  <div class="days">
    {''.join(day_card(*c) for c in D["cards"])}
  </div>

  <section class="plan">
    <span class="pl">{D["plan"][0]}</span>
    <span class="steps">{D["plan"][1]}</span>
    <span class="tagline">{D["plan"][2]}</span>
  </section>

  <section class="notes">
    <div class="ph" style="margin-bottom:12px"><h2>关键提示</h2>
      <span class="tagn">水源 · 路况 · 风险 · 装备</span></div>
    <div class="ngrid">{_notes_html()}</div>
  </section>

  <div class="foot">{D["foot"]}</div>
</div>
</body>
</html>'''


if __name__ == "__main__":
    p = OUT_DIR / f"{STEM}-攻略.html"
    p.write_text(HTML, encoding="utf-8")
    print(f"written: {p}  {p.stat().st_size / 1024:.0f} KB")
    print(f"画布 {W}x{H} · 剖面 {PW}x{PH} · 分日 {len(D['cards'])} 天")
    n_todo = HTML.count("★")
    print(f"★ 待填 {n_todo} 处 —— 交付前清零（页面上会直接显示 ★ 开头的占位文字）")

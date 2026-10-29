# -*- coding: utf-8 -*-
"""把 OSM 矢量要素（fetch_osm.py 抓的 osm.json）叠到已渲染的地形底图上。

设计取向：**制图学优先，不是"把数据画上去"**。
  * 面要素（林地/草地/水体）用半透明填充，让下面的晕渲地形透出来；
  * 线要素按等级分宽度与配色，道路带描边（casing）——这是纸质地形图的画法；
  * 步道用**深紫点线 + 纸色描边**，色相刻意避开棕（等高线）/ 橙（公路）/ 蓝（水系），
    与"攻略主线路"（粗实线）在视觉上永远不打架；
  * 注记按等级排序占位，冲突则跳过——宁可少标，不可糊成一片。

投影约定（与 make_terrain.py / map_svg.py 一致，必须严格一致否则整体错位）：
    输出像素 = ( lonlat_to_px(lon,lat,z) - [tx0,ty0]*256 - box[0:2] ) * scale

用法：
    python draw_osm.py                       # 就地叠加 out/base_map.jpg
    python draw_osm.py in.jpg out.jpg        # 指定输入输出
"""
import json, math, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import guide_common as GC

TILE = 256
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

# 字体清单收敛到 guide_common.FONT_CANDIDATES（跨平台共用 + 按 (size,bold) 缓存）。
# 原清单前四项是 msyh / msyhbd / simhei / PingFang —— 与收敛后的前四项**逐项相同**，
# 所以这里没有字重回归，只是顺带多了 Linux 的 Noto/wqy 兜底。
FONT_CANDIDATES = GC.FONT_CANDIDATES

# ---- 配色（浅色地形底图上取可读性优先）----
# 林地/草地面：**只描边、不填充**（2026-10-20 用户反馈"斑块"）。
# 半透明填充 + 羽化会把每块 OSM 面变成一坨淡色雾斑 —— 面本身碎、边界又不规则，
# 整片山体像被抹了抹布。林地范围只是背景信息，画一圈极淡的绿虚线足够，
# 地形与晕渲完整保留。想恢复旧观感：CFG["landuse_fill"]=True。
C_WOOD      = (198, 220, 190, 95)
C_WOOD_ED   = (150, 172, 136, 190)
C_MEADOW_ED = (176, 190, 150, 150)
C_MEADOW    = (228, 240, 210, 70)
C_WATER     = (166, 205, 233, 225)
C_WATER_ED  = (116, 166, 205, 255)
C_RIVER     = (108, 162, 208, 255)
C_STREAM    = (138, 182, 216, 255)
# 时令河（intermittent=yes）：冰川融水与季节性冲沟，只在暖季有水。
# ⚠ 不能和常年河画成一样的实线 —— 徒步攻略里"线=水源"是默认读法，
#   把夏季才有的水画成常流水，等于给出错误的水源信息（喜马拉雅北坡旱季断流很常见）。
# 希夏邦马窗口实测：333 条 stream 里 79 条 intermittent=yes（24%）。
C_STREAM_SEASON = (152, 192, 222, 200)

# 国界（2026-10-25 用户：珠峰东坡「缺少国界」）。
# 取**深赭红**而不是地图册上常见的紫红：本图步道层已定案紫罗兰、冰川注记是深冰蓝，
# 再来一层紫会和步道糊成一片（用户此前就嫌"步道太浓"）。
# 赭红在这张暖调地形底图上对比度足够，又不与等高线的褐色数字混淆（那是不透明小字）。
C_BORDER      = (150, 64, 56, 255)
C_BORDER_HALO = (252, 249, 243, 225)   # 纸色描边：压到雪线白/深色林地上都能看清

# ⚠ 冰川层开关（2026-10-17 希夏邦马：默认 **关闭**）。
# 要不要这一层，判据是「DEM 雪线白 + 等高线是否已经把冰雪范围说清楚了」：
#   * 开：冰川面能把冰舌范围一眼圈出来（登山线有用）；
#     代价是填充会**压掉冰面下的晕渲**——冰瀑、裂隙场是行军信息，被压平就成了白板。
#   * 关（当前）：冰雪范围交给「雪线以上的积雪白 + 等高线」，冰面起伏完整保留。
# 判据落地成一句可测的话（2026-10-17 贡嘎）：**数一数有多少冰川的末端伸到雪线以下**。
#   希夏邦马：冰川基本都在雪线以上 → 与雪线白重复 → 关。
#   贡嘎：93 块冰川里 **71 块（76%）末端伸到雪线以下**（季风海洋型冰川，冰舌大幅下探，
#         海螺沟冰川末端低到 2980 m）→ 雪线白根本盖不住它们 → **开**。
# 所以这个开关**按线路配**：route_def.CFG["glacier_layer"] = True（见 _cfg_flag）。
# ⚠ 开了要同步：图例加一行冰川、图注写明"浅蓝面为冰川（OSM）"。
DRAW_GLACIER = False
# 底图注记是否走矢量层（2026-10-20，贡嘎大环）：
#   True  = 只记录位置/字号，字由 map_svg 画成 <text> —— 轨迹线不会再压住地名、缩放不糊、
#           全图文字的避让在一个坐标系里算（详见 references/pitfalls-map-svg.md §19）。
#   False = 回到"把字烤进 jpg"的旧行为（CFG["labels_vector"]=False 可临时切回）。
LABELS_VECTOR = True
# 林地/草地是否保留半透明填充（False = 只描边，默认）。
LANDUSE_FILL = False
# 无名峰是否只标高程（2026-10-20 北天山：OSM 峰点 333 个，带名字的 0 个）。
# 只标「▲ 4016」这样的读数，不编造名字 —— 一片没有地名的山至少还有高程可读。
PEAK_UNNAMED_ELE = True

# 注记之间的留白（逻辑像素 × S）。2026-10-17 希夏邦马：字号收 0.85 倍后仍有挤压，
# 把留白从 2.0 提到 **2.8** —— 在"避免重叠"这件事上，留白比缩字号更有效：
# 字号缩 15% 只让单个注记面积降约 28%（仍可能互相贴边），
# 而留白直接决定两个注记**能不能挨着放** —— 它才是冲突判据里的硬门槛。
LBL_PAD = 2.8

# 峰名三级的**相对**阈值 = 本窗口峰点高程的分位数。
# ⚠ 绝不能写死绝对米数：7000/6500 是喜马拉雅的值，搬到武功山（1918 m）、亚丁（6032 m）
#   会让**所有**峰落进最低档 → 主峰跟无名矮峰一个字号、还不保证占位，
#   等于把 §12 ② 修好的「主峰没名字」倒挂在低海拔线路上重演一遍。
#   相对阈值随窗口自适应：希夏邦马实测 p88≈7010 / p62≈6518，与原绝对阈值近乎等价。
PEAK_TIER_PCT = (88, 62)
PEAK_TIER_MIN_N = 8     # 有效高程样本少于此值就不分级（分位数不稳），一律中档保证峰名出得来

# 冰川（natural=glacier + relation）：高海拔线路的核心图层，本区实测占图幅 15.2%。
# 配色取「白 + 淡蓝阴影」（见 references/china-terrain.md §7），但**关键是边界**：
# 雪线以上的地形底图本身就是积雪白，冰川若也画近白边界，两者就糊成一片、
# 完全看不出冰舌范围（旧实现 C_GLACIER_ED=(238,247,253) 就是这个问题）。
# → 填充走"近白淡蓝"（比雪原略冷一档），边界必须是**可读的冰蓝**。
C_GLACIER    = (224, 237, 249, 150)
C_GLACIER_ED = (118, 168, 210, 235)
C_GLACIER_LB = (24, 92, 146, 255)      # 冰川注记：深冰蓝 + 白描边

ROAD_STYLE = {
    #            (芯色,                    线宽, 描边色)
    # 公路也压一档饱和度：原 primary 的橙芯 + 橙棕描边和攻略 D1（#E4572E）同色相，
    # 党岭村一带公路与 D1 起点相距不到 1 km，会互相"冒充"。公路只做背景，一律走暖沙色。
    "motorway":      ((236, 196, 158), 5.0, (196, 166, 140)),
    "trunk":         ((238, 202, 164), 4.6, (198, 168, 142)),
    "primary":       ((240, 208, 172), 4.2, (200, 170, 144)),
    "secondary":     ((245, 218, 186), 3.6, (204, 178, 154)),
    "tertiary":      ((250, 232, 202), 3.0, (196, 180, 158)),
    "unclassified":  ((255, 253, 248), 2.6, (168, 162, 152)),
    "residential":   ((255, 253, 248), 2.6, (168, 162, 152)),
    "living_street": ((255, 253, 248), 2.4, (168, 162, 152)),
    "service":       ((255, 254, 251), 2.0, (182, 176, 166)),
    "track":         ((248, 240, 224), 1.9, (176, 160, 132)),
}
TRAIL_STYLE = {
    # 步道配色（2026-10-20 统一定案，与贡嘎大环一致）：**深紫细虚线**。
    # 走过的弯路（别再试）：浅棕→与地形同调消失；深棕→与计曲线同色调；
    #   中灰 140→暖棕地形上对比只有 1.25:1，缩到 60% 就是噪点；
    #   深石墨→太重、压过主线；淡紫 184→雪白底上消失；中紫 158→与主线紫撞色。
    # 结论：与主线同色相**不是**问题，靠明度 + 线宽分层即可（主线粗/亮/白晕，步道细/深/断续）。
    "path":      ((86, 58, 138), 1.6, 7, 4),
    "footway":   ((86, 58, 138), 1.5, 6, 4),
    "steps":     ((86, 58, 138), 1.8, 3.5, 2.5),
    "bridleway": ((96, 68, 142), 1.5, 7, 5),
}
# 步道描边：**取消**。原本纸色 (252,250,245) 是用来把深紫步道从地形里"抬"起来的，
# 但描边比地形亮得多 → 步道网成片铺开时整片只剩白色"鬼影线"，
# 灰芯的虚线反而看不见（实测：白色描边成了图上最抢眼的东西）。
# 换成中性灰芯之后，描边已经没有存在意义，直接不画。
# 暖白细描边：只比芯宽 0.9 px，把深紫从晕渲阴影里「切」出来（纯白全粗会成鬼影线）
C_TRAIL_CASING = (252, 249, 243)
C_TRAIL_CASING_EXTRA = 0.9
NO_CROWD = False

# ⚠ 2026-10-18 多洛米蒂 AV1：窗口内 OSM 具名要素上千（峰 494 / 地名 605 / 垭口 157），
#   Labeller 的「实在放不下也要放」会把整张图铺满。
#   → `route_def.CFG["label_no_crowd"] = True` 之后**只放找得到完全空位的注记**，
#     放不下的直接丢弃 —— 宁可少标，也不许互相压。
#     配合 prune_osm 按「离轨迹多远 + 图上间距」先裁一轮，效果最好。


def _font(size, bold=False):
    """取中文字体（清单与缓存都在 guide_common，跨平台共用）。

    ⚠ `bold` 这个形参**收敛前就是摆设** —— 旧实现的循环根本不看它，所有字号
      都走同一份清单。收敛后**继续沿用**这一行为：若真按 bold 去选粗体清单，
      OSM 全部底图标注会一次性从常规变粗体，属于视觉变更，要单独定，
      不能夹在"消除重复代码"里悄悄改掉。
    """
    return GC.label_font(size)


# ------------------------------------------------------------------ 投影
def make_projector(z, tx0, ty0, box, scale):
    """返回 lonlat → 输出像素 的函数。

    实现已委托 `GC.make_projector`（全技能唯一权威实现，见 guide_common.mercator_px
    的说明）；保留同名函数是为了不重写调用点。
    """
    return GC.make_projector(z, tx0, ty0, box, scale, TILE)


# ------------------------------------------------------------------ 线型
def _dashed(d, pts, fill, width, dash=6.0, gap=5.0, casing=None, casing_w=None):
    """PIL 没有虚线，自己按弧长切。

    casing 不为空时，先用它在下层画一条同节拍、更粗的虚线 —— 这就是"描边"。
    关键在于**两遍的 dash/gap 完全一致**，缺口才会对齐，看上去是一条有轮廓的虚线，
    而不是两条错开的虚线。
    """
    if casing:
        _dash_pass(d, pts, casing, casing_w or width + 2, dash, gap)
    _dash_pass(d, pts, fill, width, dash, gap)


def _dash_pass(d, pts, fill, width, dash, gap):
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg < 0.5:
            continue
        t = 0.0
        while t < seg:
            a, b = t / seg, min(t + dash, seg) / seg
            d.line([x0 + (x1 - x0) * a, y0 + (y1 - y0) * a,
                    x0 + (x1 - x0) * b, y0 + (y1 - y0) * b], fill=fill, width=width)
            t += dash + gap


def _pts_of(e, proj, size):
    """way 的几何 → 输出像素点列；整条在画布外则丢弃。"""
    g = e.get("geometry") or []
    pts = [proj(p["lon"], p["lat"]) for p in g if p.get("lon") is not None]
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if max(xs) < 0 or min(xs) > size[0] or max(ys) < 0 or min(ys) > size[1]:
        return None
    return [(round(x, 1), round(y, 1)) for x, y in pts]


def _chain_open(segs, tol=1.2):
    """把首尾相接的开放折线贪心串成尽量长的**开放**链（不要求闭合）。

    与 `_stitch` 的区别：那个只保留能闭合成环的（面要素用），
    而国界在窗口里必然是一段**开口**的线 —— 用它会把整条边界丢光。
    """
    segs = [list(s) for s in segs if len(s) >= 2]
    used = [False] * len(segs)
    chains = []

    def same(a, b):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    for i, s in enumerate(segs):
        if used[i]:
            continue
        used[i] = True
        chain = list(s)
        progress = True
        while progress:
            progress = False
            for j, t in enumerate(segs):
                if used[j]:
                    continue
                if same(chain[-1], t[0]):
                    chain.extend(t[1:])
                elif same(chain[-1], t[-1]):
                    chain.extend(reversed(t[:-1]))
                elif same(chain[0], t[-1]):
                    chain[:0] = t[:-1]
                elif same(chain[0], t[0]):
                    chain[:0] = list(reversed(t[1:]))
                else:
                    continue
                used[j] = True
                progress = True
                break
        chains.append(chain)
    return chains


def _line_parts(e, proj, size, seen=None):
    """线状要素 → 若干像素点列。

    way：顶层 geometry 一条。
    relation（国界常组织成 relation）：各 member way 的 geometry 先转像素，
    再**串成尽量长的开放链** —— 逐段画会让虚线节拍在每段重头开始，
    边界线变成"一截截红点"；也避免短于 dash 的 member 全退化成点。
    ⚠ 这里**不做画布裁剪**（只丢整段都在图外的），裁断会让串链接不上，
    与 `_poly_rings` 对 relation 的处理同理。

    `seen`：几何指纹集合，用于**跨要素去重**。国界在 OSM 里是「一个国家一个
    relation」，中尼边界会同时属于 relation 184633(नेपाल) 和 270056(中国) ——
    同一段画两遍，虚线相位不同，看上去从"点划线"变成"实线"。
    两个 relation 的 member 在 `out geom` 里**没有 id**（实测为 None），
    只能按端点 + 点数做指纹。
    """
    parts = []
    if e.get("type") == "relation":
        for m in e.get("members") or []:
            g = m.get("geometry") or []
            pts = [proj(p["lon"], p["lat"]) for p in g if p.get("lon") is not None]
            if len(pts) < 2:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if max(xs) < 0 or min(xs) > size[0] or max(ys) < 0 or min(ys) > size[1]:
                continue
            parts.append([(round(x, 1), round(y, 1)) for x, y in pts])
        chains = [c for c in _chain_open(parts) if len(c) >= 2]
    else:
        p = _pts_of(e, proj, size)
        chains = [p] if p else []
    if seen is None:
        return chains
    out = []
    for c in chains:
        a, b = c[0], c[-1]
        # 端点排序后再做指纹 → 同一段无论正向反向都算重复
        fp = (len(c),) + tuple(sorted([(round(a[0]), round(a[1])),
                                       (round(b[0]), round(b[1]))]))
        if fp in seen:
            continue
        seen.add(fp)
        out.append(c)
    return out


def _ring_area(ring):
    """鞋带公式：环的有向面积（用来在 relation 的多个 member 里挑主环）。"""
    s = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


# ------------------------------------------------------------------ 标注锚点精度
# 用户 2026-10-24："OSM 地名标注点要绝对精确"。
# 实测（百错千寻，8 条具名水系）：**顶点算术质心**离河道中位 320 m、最远 5955 m ——
# 弯曲/分叉的河，质心落在河道外的空地上，图上"桑曲"三个字指着几公里外的地方。
# 面状要素（湖、冰川）同理：环质心对凹形湖可能落在湖外。
# → 线状要素取「离质心最近的线上的点」，面状要素取「多边形内部的点」。

def _nearest_on_polyline(cx, cy, pts):
    """离 (cx,cy) 最近的折线上的点（像素坐标）。线状要素的标注锚点必须落在要素本身。"""
    best, bd = None, None
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / L))
        px, py = ax + t * dx, ay + t * dy
        dd = (px - cx) ** 2 + (py - cy) ** 2
        if bd is None or dd < bd:
            bd, best = dd, (px, py)
    return best


def _point_in_ring(px, py, ring):
    """射线法判点是否在环内。"""
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > py) != (y1 > py):
            xin = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
            if px < xin:
                inside = not inside
    return inside


def _anchor_in_ring(cx, cy, ring):
    """面状要素的标注锚点：保证落在多边形**内部**（质心在面内就用质心，否则取离质心最近的边并向内收）。"""
    if _point_in_ring(cx, cy, ring):
        return (cx, cy)
    if not ring:
        return (cx, cy)
    bx, by = min(ring, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
    # 向质心方向收一点，避免锚点正好压在边界线上（抗锯齿后会看起来在面外）
    mx, my = (bx + cx) * 0.5, (by + cy) * 0.5
    return (mx, my) if _point_in_ring(mx, my, ring) else (bx, by)


_STITCH_STATS = {"ok": 0, "dropped": 0, "dropped_pts": 0}


def _stitch(segs, tol=1.2):
    """把首尾相接的开放折线**缝合成闭合环**（OSM multipolygon 的标准做法）。

    为什么必须缝（2026-10-18 熊野古道实测，用户指着湖问"渲染的对吗"）：
    OSM 把一个大湖/水库的外环按「岛 / 桥 / 行政界」切成若干**开放** way，
    relation 的 members 里每段都没首尾闭合。旧实现「每个 member 各自 polygon() 填充」，
    而 ImageDraw.polygon 遇到开放折线会**自动把最后一个点连回第一个点** ——
    于是每段都凭空拉出一条弦，弦与真实岸线之间被填成大块假水面。
    实测：本窗口 14 个水体 relation 里 12 个有开放成员（共 59 段），
    風屋貯水池 2 段（1340 + 1102 点）、合川貯水池 13 段全部开放 ——
    图上就是「带尖刺的浅蓝珊瑚块」。

    做法：贪心串接 —— 每段的首/尾与当前链的尾/首在 tol 内吻合就接上（必要时反向），
    直到绕回起点闭合成环。**无法闭合的残链直接丢弃**，绝不再隐式闭合。
    """
    segs = [list(s) for s in segs if len(s) >= 2]
    used = [False] * len(segs)
    rings = []

    def same(a, b):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    for i, s in enumerate(segs):
        if used[i]:
            continue
        used[i] = True
        chain = list(s)
        progress = True
        while progress and not (len(chain) >= 4 and same(chain[0], chain[-1])):
            progress = False
            for j, t in enumerate(segs):
                if used[j]:
                    continue
                if same(chain[-1], t[0]):
                    chain.extend(t[1:])
                elif same(chain[-1], t[-1]):
                    chain.extend(reversed(t[:-1]))
                elif same(chain[0], t[-1]):
                    chain[:0] = t[:-1]
                elif same(chain[0], t[0]):
                    chain[:0] = list(reversed(t[1:]))
                else:
                    continue
                used[j] = True
                progress = True
                break
        if len(chain) >= 4 and same(chain[0], chain[-1]):
            rings.append(chain)
            _STITCH_STATS["ok"] += 1
        elif len(chain) >= 12:
            # 残链：说明 member 缺失（超出下载窗口）或 OSM 数据本身断口。
            # 宁可少画一块，也绝不用隐式闭合去糊 —— 那正是假水面的来源。
            _STITCH_STATS["dropped"] += 1
            _STITCH_STATS["dropped_pts"] += len(chain)
    return rings


def _poly_rings(e, proj, size):
    """要素 → (外环列表, 内环列表)，环都已闭合、已裁到画布范围。

    way：geometry 直接当外环。
    relation：outer / inner 成员各自**缝合**成闭合环（见 _stitch 的踩坑说明）。
    注意 relation 不对单个 member 做画布裁剪 —— 半张湖被裁掉会让缝合断链，
    反而漏画整片水面；改为对缝合后的整环做一次 bbox 判断即可。
    """
    outers, inners = [], []
    if e["type"] == "way":
        pts = _pts_of(e, proj, size)
        if pts and len(pts) > 2:
            outers.append(pts)
        return outers, inners
    if e["type"] != "relation":
        return outers, inners

    so, si = [], []
    for m in e.get("members", []):
        if m.get("type") != "way":
            continue
        role = m.get("role") or "outer"
        if role not in ("outer", "inner", ""):
            continue
        g = m.get("geometry") or []
        pts = [proj(p["lon"], p["lat"]) for p in g if p.get("lon") is not None]
        if len(pts) < 2:
            continue
        pts = [(round(x, 1), round(y, 1)) for x, y in pts]
        (si if role == "inner" else so).append(pts)

    for src, dst in ((so, outers), (si, inners)):
        for ring in _stitch(src):
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            if max(xs) < 0 or min(xs) > size[0] or max(ys) < 0 or min(ys) > size[1]:
                continue
            dst.append(ring)
    return outers, inners


def _rings_of(e, proj, size):
    """只要外环的便捷入口（注记定位等场合用）。"""
    return _poly_rings(e, proj, size)[0]


# ------------------------------------------------------------------ 注记
def _pct(vals, q):
    """线性插值分位数（纯 Python —— 别给 draw_osm 引入 numpy，它只依赖 PIL）。"""
    vs = sorted(vals)
    if not vs:
        return None
    if len(vs) == 1:
        return vs[0]
    pos = (len(vs) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def _cfg_flag(key, default=False):
    """读 route_def.CFG 里的布尔开关（取不到就给默认）。

    实现已收敛到 `guide_common.cfg_flag`（全技能唯一入口，见那里的说明）；
    这里保留一层薄封装，是为了不重写已有的 8 处调用点。

    存在的意义：**按线路切换图层**不该靠改脚本常量 —— 那等于把线路差异写死进技能。
    """
    return GC.cfg_flag(key, default)


class Labeller:
    """按优先级占位放注记，重叠就跳过。cell 是占位网格边长（随超采样倍率放大）。"""

    def __init__(self, size, cell=14):
        self.size = size
        self.grid = set()
        self.cell = cell
        self.n_overlap = 0     # 「实在放不下也要放」的强制落位数（= 会与已有注记挤在一起的个数）
        self.boxes = []          # 已落位注记的矩形（栅格 px），导出给 map_svg 避让
        self.draw_pixels = True  # False = 只记录不画（矢量注记模式）
        self.records = []        # 矢量注记：文本 / 左上角 / 基线 / 字号 / 颜色

    def _mark(self, bb):
        x0, y0, x1, y1 = bb
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                self.grid.add((cx, cy))

    def taken(self, bb):
        x0, y0, x1, y1 = bb
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                if (cx, cy) in self.grid:
                    return True
        return False

    def _hits(self, box):
        """这个矩形压到了几个已占用的网格单元。"""
        x0, y0, x1, y1 = box
        n = 0
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                if (cx, cy) in self.grid:
                    n += 1
        return n

    def place(self, d, text, xy, font, fill, stroke=(255, 255, 255),
              anchor_center=True, force=False, pad=2, stroke_w=1.8, fallback=True,
              kind="", anchor_right=False, gap=5.0, right_only=False):
        """放注记。

        三种锚定（2026-10-24 用户：标注名称默认在标注点的正右侧，依然考虑动态避让）：
          · anchor_right=True  → 默认紧贴锚点**右侧、垂直居中**（地图注记通用惯例：
            峰名/湖名/地名/道路名统一走此默认位）。
          · anchor_center=True  → 文本正中以锚点为中心（保留兼容，旧水系/道路行为）。
          · 二者皆否           → 文本左上角落在锚点（旧峰名"下方偏右"行为）。

        动态避让：理想位被占就向外做**紧凑**环形偏移（上限 ~12 逻辑 px），
        优先保持右侧；旧实现最大偏移 28 px，会出现标签漂到几十像素外（"明显偏移"）。
        记录时把**要素真实锚点** ax/ay 一起写进 records，供 map_svg 当矢量基准
        （不再把文字框位置当成锚点，避免引出位置偏掉）。
        """
        W, H = self.size
        bb = d.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        x0c, y0c = xy
        S = self.cell / 14.0                      # 反推超采样倍率（cell = 14*S）
        g = gap * S
        if anchor_right:
            ox0, oy0 = x0c + g, y0c - th / 2.0    # 右侧、垂直居中
        elif anchor_center:
            ox0, oy0 = x0c - tw / 2.0, y0c - th / 2.0
        else:
            ox0, oy0 = x0c, y0c
        # 候选偏移：以默认位为基准向外做紧凑环形避让，右侧优先。
        U = S
        cand = [(0, 0)]
        if right_only:
            # 只在右侧方向延长搜索：正右 / 右上 / 右下，偏移步进 2/4/6/9/12 逻辑 px。
            # 2026-10-29 用户定：不换左侧；6 px 不够错开密集峰群，上限放宽到 12 px。
            for r in (2, 4, 6, 9, 12):
                # (r,-2r) 是更陡的右上：上下只差 55 px 的两座峰（提诺山/梅尔西峰）
                # 靠 12 px 的抬高错不开，需要拉到约一个字高。
                cand += [(r, 0), (r, -r), (r, r), (r, -2 * r)]
        else:
            for r in (5, 9, 12):
                cand += [(r, 0), (r, -r), (r, r),       # 右、右上、右下（保持右侧优先）
                         (0, -r), (0, r),               # 正上、正下
                         (-r, -r), (-r, r), (-r, 0)]    # 左上、左下、左侧（最后才用）
        best = None
        for odx, ody in cand:
            dx, dy = odx * U, ody * U
            x, y = ox0 + dx, oy0 + dy
            box = (x - pad, y - pad, x + tw + pad, y + th + pad)
            if box[0] < 0 or box[2] > W or box[1] < 0 or box[3] > H:
                continue
            hits = self._hits(box)
            if hits == 0:                                  # 完美位置
                self._emit(d, text, x, y, font, fill, stroke, stroke_w,
                           ax=x0c, ay=y0c)
                self._mark(box)
                self.boxes.append(box)
                return True
            if best is None or hits < best[0]:             # 记下最不挤的那个
                best = (hits, x, y, box)
        if not force and not fallback:
            return False
        if best is None:
            return False
        self.n_overlap += 1            # 命中"两害相权"分支：记录一次挤放，供自检核对
        _, x, y, box = best
        self._emit(d, text, x, y, font, fill, stroke, stroke_w, ax=x0c, ay=y0c)
        self._mark(box)
        self.boxes.append(box)
        return True

    def _emit(self, d, text, x, y, font, fill, stroke, stroke_w, ax=None, ay=None):
        """画，或只记录（矢量模式）。位图用左上角，SVG 用基线 —— 两个都存。

        ax/ay：要素**真实锚点**（栅格 px），与落位后的文字框无关 —— 专门留给
        map_svg 当矢量标注的基准。旧实现把"文字框左上角"当成锚点传给 map_svg，
        导致峰名/湖名的引线起点、避让基准都偏在文字位置，看着像"标注点偏移"
        （2026-10-24 用户：部分标注地点存在明显偏移）。
        """
        if self.draw_pixels:
            d.text((x, y), text, font=font, fill=fill,
                   stroke_width=stroke_w, stroke_fill=stroke)
        try:
            asc = font.getmetrics()[0]
        except Exception:                                          # noqa
            asc = int(getattr(font, "size", 12) * 0.8)
        self.records.append({"t": text, "x": round(float(x), 1), "y": round(float(y), 1),
                             "base": round(float(y + asc), 1),
                             "s": int(getattr(font, "size", 12)),
                             "f": list(fill), "st": list(stroke),
                             "sw": round(float(stroke_w), 1),
                             "ax": round(float(ax if ax is not None else x), 1),
                             "ay": round(float(ay if ay is not None else y), 1)})


# ------------------------------------------------------------------ 主流程
def _clean_name(s):
    """OSM 名里常混藏文/天城文等 PIL 字体画不出的字符（会渲染成豆腐块□□□）。
    只保留 CJK / 拉丁 / 数字 / 常见标点；整名都在可渲染集之外的直接弃用。
    必须在收集阶段就清洗，skip_names 匹配与出图才会用同一套名字。"""
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        if (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF) \
           or ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch.isdigit() \
           or ch in " ·-—–()（）.°'’&,+/~":
            out.append(ch)
    return "".join(out).strip(" ·-")


# 取名的键顺序：先 `name`（OSM 的"通用名"，本地语），再各变体的中文标签。
_NAME_KEYS = ("name", "name:zh", "name:zh-Hans", "name:zh-CN")


def _pick_name(t):
    """从 tags 里取一个**能画**的名字；取不到返回 ""。

    为什么要多键
    ------------
    OSM 里存在一类要素**只有 `name:en`（甚至连 `name` 都没有）**，典型是峰：
    博格达实测 91 个峰点里有 3 个是 `Nangfeng Peak` / `Xifeng Peak` / `Hongfeng Peak`
    （4683 / 4613 / 4218 m）—— 中文名被录入英文栏，`name` 是空的。旧实现只看
    `t.get("name")`，于是这三个**有名字的山头在图上是无名峰**，还会把"本区只有 7 个
    有名字的峰"这种统计口径带偏。

    不自动音译
    ----------
    `Nanfeng → 南峰` 看着显然，但同一套规则放到藏语/蒙古语地名上就会造出错名。
    所以只认两条确定的路：
      ① `CFG["name_map"] = {"Nangfeng Peak": "南峰", ...}` —— 线路配置里**逐条显式**给；
      ② `CFG["label_foreign_names"] = True` —— 允许直接显示原文（默认关，中文图上不协调）。
    """
    for k in _NAME_KEYS:
        v = (t.get(k) or "").strip()
        if v:
            return v
    en = (t.get("name:en") or "").strip()
    if not en:
        return ""
    mp = GC.cfg("name_map", None) or {}
    for k, v in mp.items():                       # 大小写与首尾空格都不计较
        if en.strip().lower() == str(k).strip().lower():
            return str(v)
    if GC.cfg_flag("label_foreign_names", False):
        return en
    return ""


def draw_overlay(pil, meta, osm_path=None, skip_names=None,
                 px_scale=1.0, proj_scale=None, seed_cells=None, verbose=True,
                 peak_snap=None):
    """把 OSM 要素叠到 pil 上。

    skip_names: 需要抑制的注记名（一般是攻略自己的 POI 名）。
      重名时两边海拔常不同（OSM 十王峰 1344.4 vs 实测轨迹 1345.5），
      同图出现两个数会让人误判——**以实测轨迹为准，抑制 OSM 那条**。
    px_scale:  栅格超采样倍率。底图按 LOGICAL_W×SS 出图时传 SS，
      线宽/字号/图标/占位网格全部同比放大，缩回逻辑尺寸后即得抗锯齿的锐利边缘。
    proj_scale: 投影用的「栅格像素 / 瓦片像素」比例；默认取 meta["scale"]（非超采样场景）。
    seed_cells: 已被占用的注记网格（cell = 14×px_scale），一般传等高线高程标注的占位，
      让地名/峰名主动避开等高线数字，避免两种注记叠在同一个点上。
    peak_snap: `{"lon,lat": [lon, lat]}`，把漂在山腰的 OSM 峰点换到 DEM 山顶。
      由 `make_terrain.snap_peaks_to_dem()` 算好传进来（本模块没有 DEM）；
      key 用**原始坐标**（7 位小数），所以不依赖要素顺序。空表 = 不校正。
    """
    osm_path = Path(osm_path) if osm_path else (OUT / "osm.json")
    # 冰川层按线路开关：默认关，route_def.CFG["glacier_layer"]=True 才画（判据见常量注释）
    draw_glacier = _cfg_flag("glacier_layer", DRAW_GLACIER)
    if not osm_path.exists():
        print(f"   跳过 OSM 叠加（未找到 {osm_path.name}，先跑 fetch_osm.py）")
        return pil
    skip_names = set(skip_names or ())
    S = float(px_scale)
    global NO_CROWD
    NO_CROWD = _cfg_flag("label_no_crowd", False)

    def W(v):
        """线宽 → 栅格整数像素。

        ⚠ 2026-10-21 用户：水系/公路线太粗。总览图 ss=4（10000 px）时，
        5.0 的公路芯线乘 4 就是 20 px —— 比轨迹还宽。大栅格统一乘 0.55，
        小栅格（ss=2）保持不变。
        """
        k = 0.55 if S >= 3 else 1.0
        return max(1, int(round(v * S * k)))

    def F(v):
        """字号 → 栅格像素。"""
        return max(6, int(round(v * S)))

    data = json.loads(osm_path.read_text(encoding="utf-8"))
    els = data["elements"]
    proj = make_projector(meta["z"], meta["tx0"], meta["ty0"], meta["box"],
                          proj_scale or meta["scale"])
    size = pil.size
    ov = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov, "RGBA")

    def bucket(e):
        t = e.get("tags", {})
        # way / relation 共用同一套标签判定；node 不参与面/线要素（node 的峰/地名/水体
        # 在下方注记段单独处理）。relation 常为 multipolygon（大湖、冰川、林地），
        # 绝不能整类跳过 —— 否则 relation 水体/冰川会在 bucket 阶段被直接丢弃，
        # 地图把湖画成平地（2026-10-17 年宝玉则仙女湖踩坑）。
        if e["type"] == "node":
            return None
        # 国界：admin_level=2 才算国界；3/4/6 是省/地/县界，只做 2（见 fetch_osm 的说明）。
        # 必须排在 highway/waterway 之前 —— 界桩附近的道路 way 偶尔也带 boundary 标签。
        if t.get("boundary") == "administrative" and str(t.get("admin_level")) == "2":
            return ("border", None)
        if "highway" in t:
            h = t["highway"]
            if h in ROAD_STYLE:
                return ("road", h)
            if h in TRAIL_STYLE:
                return ("trail", h)
            return None
        if "waterway" in t:
            return ("water", t["waterway"])
        if t.get("natural") == "water" or t.get("landuse") == "reservoir":
            return ("waterbody", None)
        if t.get("natural") == "glacier" and draw_glacier:
            return ("glacier", None)
        if t.get("natural") == "wood" or t.get("landuse") == "forest":
            return ("wood", None)
        if t.get("landuse") == "meadow":
            return ("meadow", None)
        return None

    # 面 → 线 → 注记（保证层次）。冰川画在林地/草地之上、水体之下——
    # 冰川湖（终碛阻塞湖）坐在冰川面上，必须能透出来。
    order = {"wood": 0, "meadow": 1, "glacier": 2, "waterbody": 3, "water": 4,
             "trail": 5, "road": 6, "border": 7}
    drawable = []
    _border_seen = set()    # 国界几何指纹（见 _line_parts 的 seen 说明）
    _STITCH_STATS.update(ok=0, dropped=0, dropped_pts=0)
    for e in els:
        b = bucket(e)
        if not b:
            continue
        if b[0] == "glacier":
            for ring in _rings_of(e, proj, size):
                drawable.append((order["glacier"], "glacier", None, ring, e, ()))
            continue
        # 面状要素（林地/草地/水体）统一走 _poly_rings —— way 直接成环，
        # relation（multipolygon，如 OSM 大湖）把 outer 成员**缝合**成闭合环。
        # 否则 relation 水体没有顶层 geometry，_pts_of 返回 None 会被整片漏掉
        # （2026-10-17 年宝玉则仙女湖踩坑）；而 member 未闭合时逐段填充会拉出
        # 横贯湖面的假弦（2026-10-18 熊野古道 風屋/合川貯水池 尖刺块）。
        if b[0] in ("wood", "meadow", "waterbody"):
            outs, ins = _poly_rings(e, proj, size)
            for ring in outs:
                drawable.append((order[b[0]], b[0], b[1], ring, e,
                                 tuple(ins) if b[0] == "waterbody" else ()))
            continue
        if b[0] == "border":
            # `_border_seen` 跨要素共享：中尼边界同时属于中国和尼泊尔两个 relation
            for part in _line_parts(e, proj, size, _border_seen):
                drawable.append((order["border"], "border", None, part, e, ()))
            continue
        pts = _pts_of(e, proj, size)
        if pts:
            drawable.append((order[b[0]], b[0], b[1], pts, e, ()))
    drawable.sort(key=lambda x: x[0])

    # 水体带岛屿/内湖时要挖洞：PIL 的 polygon 不支持洞，用一张可复用的 L 蒙版，
    # 每次只清/画当前要素的 bbox（整幅 3008×6244 的 L 图每次新建太慢）。
    _hole_mask = [None]

    def _fill_poly(ring, holes, color):
        if not holes:
            d.polygon(ring, fill=color)
            return
        if _hole_mask[0] is None:
            _hole_mask[0] = Image.new("L", size, 0)
        mk = _hole_mask[0]
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        for h in holes:
            xs.extend(p[0] for p in h)
            ys.extend(p[1] for p in h)
        bb = (max(0, int(min(xs)) - 1), max(0, int(min(ys)) - 1),
              min(size[0], int(max(xs)) + 2), min(size[1], int(max(ys)) + 2))
        if bb[2] <= bb[0] or bb[3] <= bb[1]:
            return
        md = ImageDraw.Draw(mk)
        md.rectangle(bb, fill=0)
        md.polygon(ring, fill=255)
        for h in holes:
            md.polygon(h, fill=0)
        ov.paste(Image.new("RGBA", (bb[2] - bb[0], bb[3] - bb[1]), color),
                 bb[:2], mk.crop(bb))

    land_fill = _cfg_flag("landuse_fill", LANDUSE_FILL)
    n_poly = n_line = n_glacier = 0
    n_border = 0            # 国界：已绘折线段数（用于日志核对「到底画上了没有」）
    road_refs = []          # (pts, ref, highway_class) 道路编号标注候选
    for _, kind, sub, pts, e, holes in drawable:
        if kind in ("wood", "meadow"):
            if len(pts) > 2:
                if land_fill:
                    d.polygon(pts, fill=C_WOOD if kind == "wood" else C_MEADOW)
                else:
                    _dashed(d, pts + [pts[0]], C_WOOD_ED if kind == "wood" else C_MEADOW_ED,
                            W(1.0), 5.0 * S, 5.0 * S)
                n_poly += 1
        elif kind == "waterbody":
            if len(pts) > 2:
                _fill_poly(pts, holes, C_WATER)
                d.line(pts + [pts[0]], fill=C_WATER_ED, width=W(1), joint="curve")
                for h in holes:
                    d.line(h + [h[0]], fill=C_WATER_ED, width=W(1), joint="curve")
                n_poly += 1
        elif kind == "glacier":
            # 冰面近白淡蓝填充 + 冰蓝边界；OSM 冰川面普遍偏旧（数字化早于近年退缩），
            # 边界只作参考，实际冰舌以现场为准。
            d.polygon(pts, fill=C_GLACIER)
            d.line(pts + [pts[0]], fill=C_GLACIER_ED, width=W(1.4), joint="curve")
            n_glacier += 1
        elif kind == "water":
            big = sub in ("river", "canal")
            if e.get("tags", {}).get("intermittent") == "yes":
                # 时令河：虚线、细一档。见 C_STREAM_SEASON 的说明。
                _dashed(d, pts, C_STREAM_SEASON, W(1), 7 * S, 6 * S)
            else:
                d.line(pts, fill=C_RIVER if big else C_STREAM,
                       width=W(2) if big else W(1), joint="curve")
            n_line += 1
        elif kind == "trail":
            core, w, dash, gap = TRAIL_STYLE[sub]
            # 不加纸色描边（见 C_TRAIL_CASING 的说明）
            _dashed(d, pts, core, W(w), dash * S, gap * S)
            n_line += 1
        elif kind == "border":
            # 国界：纸色描边 + 赭红虚线。节拍特意比步道疏（步道 5/4）——
            # 国界是"读一眼就够"的背景信息，不该比徒步轨迹更抢眼。
            # 2026-10-25 实测第一版 W(1.6)/11·6 在 4512 px 图上只有 3 px 宽，
            # 压在雪线白上几乎看不出是"界线"；加到 2.0 并把节拍压到 9/5.5。
            _dashed(d, pts, C_BORDER, W(2.0), 9.0 * S, 5.5 * S,
                    casing=C_BORDER_HALO, casing_w=W(2.0) + W(1.4))
            n_border += len(pts)
        elif kind == "road":
            core, w, ed = ROAD_STYLE[sub]
            d.line(pts, fill=ed, width=W(w) + W(2), joint="curve")   # 描边
            d.line(pts, fill=core, width=W(w), joint="curve")
            n_line += 1
            # 收集道路注记，稍后在地名注记之后尝试标注（优先级低于地名）。
            # 2026-10-24 博罗科努补：**无 ref 但有 name 的道路也要标**。
            # 徒步线上 track / tertiary 常常只有名字（「乌图精牧道」39 km、「则库北路」），
            # 旧实现只收 ref，这类沟谷名一条都上不了图 —— 而它们恰恰是
            # 「我现在在哪条沟里」的唯一线索（本窗口 27 个具名要素里，山名 0、垭口名 0）。
            _t = e.get("tags", {})
            ref = _t.get("ref")
            if ref and sub in ("motorway", "trunk", "primary", "secondary", "tertiary"):
                road_refs.append((pts, ref, sub, False))
            elif sub in ("motorway", "trunk", "primary", "secondary", "tertiary", "track"):
                _nm = _clean_name(_t.get("name") or "")
                if _nm:
                    road_refs.append((pts, _nm, sub, True))

    # ---------------- 注记 ----------------
    lab = Labeller(size, cell=14 * S)
    lab.draw_pixels = not _cfg_flag("labels_vector", LABELS_VECTOR)
    if seed_cells:
        lab.grid |= set(seed_cells)

    def _dedupe_by_name(cands, sep, name_idx, sort_idx=None, x_idx=0, y_idx=1):
        """按**名字**去重：同名注记至少相距 sep（栅格 px）才允许再放一个。

        为什么必须做（2026-10-18 熊野古道实测）：OSM 把同一条河拆成上百个 way，
        每个 way 都带同一个 name —— 本窗口 1279 条具名水系要素只有 **329 个不同的名字**，
        熊野川一条河占了 130 个 way。Labeller 只避让"空间重叠"，
        同名标签沿河一字排开互不重叠 → 水系名落了 **1222 条**，
        熊野川在图中被写了 20+ 遍（用户反馈"水系渲染有问题"）。
        道路编号同病：国道 168 号被拆成几十段，ref 沿路重复。
        地名也是：大黒谷在 2 km 内出现 4 次。

        做法：按 sort_idx 指定的几何尺寸降序（大河/大湖/长路先占位），
        同名已落位的标签距新候选小于 sep 就跳过。sep 取图幅尺度的比例，
        长河因此仍能拿到 2–3 个标签（符合地形图惯例），短溪只留 1 个。
        """
        def _key(c):
            return c[sort_idx] if sort_idx is not None else 0
        placed, out = {}, []
        for c in sorted(cands, key=_key, reverse=True):
            nm = c[name_idx]
            if not nm:
                continue
            cx, cy = c[x_idx], c[y_idx]
            seen = placed.setdefault(nm, [])
            if any(math.hypot(cx - px, cy - py) < sep for px, py in seen):
                continue
            seen.append((cx, cy))
            out.append(c)
        return out

    # ⚠ 字号的上下界是两头夹出来的，别只顾一头：
    #   · 下界来自 2026-10-09 亚丁线：11.5 px 的峰名缩进长图（~0.4×）后完全看不见
    #     → 曾整体放大约 1.35 倍、颜色加深，做到与等高线高程标注同一量级；
    #   · 上界来自 2026-10-17 希夏邦马：本窗口 191 个峰点 + 30 个水系名 + 地名同屏，
    #     18 px 的地名与 16.5 px 的峰名在 1504 px 逻辑画幅上互相挤压，
    #     用户直接反馈"字体缩小、避免重叠" → 再收约 **0.85 倍**。
    #   结论：**13–15.5 px（逻辑）** 是这一族底图的可用区间，两者都别越界。
    f_town = _font(F(15.5), True)
    f_vil = _font(F(14), True)
    f_ham = _font(F(12.5))
    f_peak_hi = _font(F(14), True)       # 顶级：本窗口峰点的 p88 以上
    f_peak = _font(F(13), True)          # 中档：p62 – p88
    f_peak_lo = _font(F(11), True)       # p62 以下 / 高程缺失：背景峰，只填空位
    f_poi = _font(F(11.5))
    f_water = _font(F(11.5), True)

    # 可选：所有 OSM 地名统一成同一字号（默认关闭，保持上面的高程/类别分级）。
    # 区域总览图这类「地名密、缩略图看得多」的画幅，分级会让同屏字号 11–15.5 显得杂乱，
    # 配 CFG["osm_label_size"]=13 即可全部拉平。
    _uni_sz = GC.cfg("osm_label_size", None)
    if _uni_sz:
        _funi = _font(F(float(_uni_sz)), True)
        f_town = f_vil = f_ham = f_peak_hi = f_peak = f_peak_lo = f_poi = f_water = _funi

    peaks, places, pois, waters, saddles = [], [], [], [], []
    extras = []          # 水源 / 营地 / 补给（scripts/_fetch_extra.py 补抓，tags 带 _kind）
    for e in els:
        if e["type"] != "node":
            continue
        t = e.get("tags", {})
        if "lon" not in e:
            continue
        x, y = proj(e["lon"], e["lat"])
        m = 40 * S
        if not (-m < x < size[0] + m and -m < y < size[1] + m):
            continue
        if t.get("natural") == "peak":
            # 峰点校正：OSM 的人工点常漂在山腰，符号要画在 DEM 山顶上（见 peak_snap 说明）
            _sn = (peak_snap or {}).get(f"{e['lon']:.7f},{e['lat']:.7f}")
            if _sn:
                x, y = proj(_sn[0], _sn[1])
            peaks.append((x, y, _pick_name(t), t.get("ele", "")))
        elif t.get("natural") == "saddle":
            # 哑口 / 垭口（2026-10-17 念青东补）。OSM 用 natural=saddle，
            # 也常带 mountain_pass=yes。高原线路的垭口是**关键地理标识**，
            # 而 peak 在非登山区（念青东/横断山腹地）常常一个都没有 ——
            # saddle 是那一带唯一稳的山口标注来源。
            # ⚠ 与攻略自己的垭口 POI 同名时由 skip_names 去重，不会画两遍。
            saddles.append((x, y, _pick_name(t), t.get("ele", "")))
        elif "place" in t:
            # place=locality（小地名）也走这支：prio 里没有 locality 会排到最后，
            # 用 f_ham 画，无需单独分支。
            places.append((t["place"], x, y, _pick_name(t)))
        elif t.get("natural") == "water":
            waters.append((x, y, _pick_name(t), 0.0))
        elif t.get("tourism") in ("attraction", "viewpoint", "alpine_hut"):
            pois.append((x, y, _pick_name(t), t["tourism"]))
        elif t.get("amenity") == "place_of_worship":
            pois.append((x, y, _pick_name(t), "temple"))
        elif t.get("_kind") in ("spring", "water", "camp", "supply"):
            # 徒步参考点（2026-10-29 贡嘎总览图）：水源 / 营地 / 补给。
            # 数据由 scripts/_fetch_extra.py 单独抓并打 _kind 标记 ——
            # 主 QUERY 不含这些标签，所以**没有 _kind 的老缓存完全不受影响**。
            extras.append((x, y, _pick_name(t), t["_kind"],
                           t.get("ele") or t.get("capacity") or ""))

    # 湖泊（面）与河流（线）的名字：锚点取要素顶点/最大环的重心，再交给 Labeller 动态避让
    for e in els:
        t = e.get("tags", {})
        nm = _pick_name(t)
        if not nm:
            continue
        is_lake = t.get("natural") == "water" or t.get("landuse") == "reservoir"
        is_river = bool(t.get("waterway"))
        if not (is_lake or is_river):
            continue
        # 河流（线）用 _pts_of；湖泊/水库（面，可能是 way 或 relation）用 _rings_of
        # 取面积最大的环当锚点 —— 否则 relation 大湖（无顶层 geometry）取不到名字。
        if is_river:
            pts = _pts_of(e, proj, size)
        else:
            rings = _rings_of(e, proj, size)
            pts = max(rings, key=lambda r: abs(_ring_area(r))) if rings else None
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        # ⚠ 锚点必须落在要素**本身**上（2026-10-24 用户"标注点要绝对精确"）：
        #   线状（河）取离质心最近的线上点；面状（湖/水库）取多边形内部的点。
        if is_river:
            _a = _nearest_on_polyline(cx, cy, pts)
        else:
            _a = _anchor_in_ring(cx, cy, pts)
        if _a is None:
            continue
        cx, cy = _a
        if not (-40 * S < cx < size[0] + 40 * S and -40 * S < cy < size[1] + 40 * S):
            continue
        # size = 该段几何的尺寸（河长 / 湖面积），供下面按名去重时排序：
        # 最长的那段先占位，避免"名字落在一条 3 个点的支流碎段上"。
        if is_river:
            sz = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                     for i in range(len(pts) - 1))
        else:
            sz = abs(_ring_area(pts))
        waters.append((cx, cy, nm, sz))

    # 冰川名：锚点取最大环的重心（relation 的多个 member 里挑面积最大的那个）
    glaciers = []
    for e in els:
        t = e.get("tags", {})
        if t.get("natural") != "glacier":
            continue
        nm = _clean_name(_pick_name(t))
        if not nm:
            continue
        rings = _rings_of(e, proj, size)
        if not rings:
            continue
        ring = max(rings, key=lambda r: abs(_ring_area(r)))
        _gcx = sum(p[0] for p in ring) / len(ring)
        _gcy = sum(p[1] for p in ring) / len(ring)
        _a = _anchor_in_ring(_gcx, _gcy, ring)          # 锚点必须落在冰川面内
        glaciers.append((_a[0], _a[1], nm, abs(_ring_area(ring))))

    # 山峰：▲ + 名 + 高程
    #
    # ⚠ 分级 + 排序都是必须的，2026-10-16 希夏邦马实测：
    #   ① 本窗口 OSM 有 191 个峰点，旧实现一律 fallback=True「实在放不下也要放」
    #      → 画出 102 个峰名，山体区被注记糊死、等高线数字全被压住。
    #      制图学上峰名本来就按高程分级：8000 米级是地标，6000 米以下只是背景。
    #   ② 旧实现按 OSM 返回顺序落位，一座 4300 m 的矮峰会先占掉 7000 m 主峰
    #      需要的空位，出现「主峰没名字、无名矮峰有名字」的倒挂 —— 必须按 ele 降序。
    #   ③ 阈值必须是**本窗口峰点高程的分位数**，不能是绝对米数（见 PEAK_TIER_PCT 注释）。
    def _ele_of(s):
        """OSM 的 ele 常带单位或空格（'7292 m'），解析失败返回 None。"""
        try:
            return float(str(s).replace("m", "").replace("米", "").strip())
        except (TypeError, ValueError):
            return None

    n_peak = n_skip = 0
    # 点名强制出名字的峰（CFG["peak_label_force"] = ["大雪山"]）：
    # 低分位峰名默认"放不下就丢"，但个别峰（如主线视野里的 6000 米级）名字丢了
    # 比压住两三个等高线数字更糟 —— 允许它挤放，靠白描边保证可读。
    try:
        _force_names = {str(x).strip() for x in (GC.cfg("peak_label_force", None) or [])}
    except Exception:                                              # noqa
        _force_names = set()
    peaks = [(x, y, _clean_name(n), e) for x, y, n, e in peaks]
    saddles = [(x, y, _clean_name(n), e) for x, y, n, e in saddles]
    places = [(k, x, y, _clean_name(n)) for k, x, y, n in places]
    waters = [(x, y, _clean_name(n)) for x, y, n, _sz in waters]
    pois = [(x, y, _clean_name(n), k) for x, y, n, k in pois]

    # ---- 按名去重（必须在候选表收集完之后、Labeller 落位之前）----
    # OSM 把同一条河/同一条路/同一个村拆成几十上百个要素，每个都带同一个 name ——
    # 不去重就会沿河沿路把名字写满（见 _dedupe_by_name 的文档串）。
    # 水系名：sep 放到 1/4 图宽 —— 熊野川纵贯全图也只会留 3–4 个名
    waters = _dedupe_by_name(waters, max(600.0, size[0] * 0.25) * S, 2)
    # 道路编号：同一条路拆几十段，ref 只在该路另一端才重复出现
    road_refs = _dedupe_by_name(
        [(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts),
          str(txt).split(";")[0].strip(), pts, isn)
         for pts, txt, sub, isn in road_refs],
        max(500.0, size[0] * 0.22) * S, 2)
    # 地名：村落常被录成多个相邻 node，sep 收窄到 1/8 图宽（同村只出一个名）
    places = _dedupe_by_name(places, max(260.0, size[0] * 0.12) * S, 3, x_idx=1, y_idx=2)

    peaks.sort(key=lambda p: _ele_of(p[3]) or -1.0, reverse=True)
    # 2026-10-29 新增（默认关，只给总览这类"要求峰名尽量全出"的画幅用）：
    #   peak_label_all   → 所有具名峰都允许挤放（不再"低分位放不下就丢"）
    #   peak_label_tight → 峰名左缘紧贴三角右缘 0.5 逻辑 px（默认 gap 5.0 太远）
    _peak_all = GC.cfg_flag("peak_label_all", False)
    _peak_tight = GC.cfg_flag("peak_label_tight", False)
    _peak_right = GC.cfg_flag("peak_label_right_only", False)
    # 分位数按本窗口**所有有高程的峰点**算（含无名字的）——要反映"这片地形上多高算突出"，
    # 而不是 OSM 的命名稀疏度（高峰也常有没录名字的）。
    _ev = [v for v in (_ele_of(p[3]) for p in peaks) if v is not None]
    if len(_ev) >= PEAK_TIER_MIN_N:
        q_hi, q_mid = _pct(_ev, PEAK_TIER_PCT[0]), _pct(_ev, PEAK_TIER_PCT[1])
    else:
        q_hi = q_mid = None       # 样本太少：不做相对分级，一律中档
    n_named = sum(1 for _p in peaks if _p[2])
    n_named_done = 0
    for x, y, name, ele in peaks:
        if not name:
            ev0 = _ele_of(ele)
            if PEAK_UNNAMED_ELE and ev0:
                k = 0.72
                d.polygon([(x, y - 6 * S * k), (x - 5.4 * S * k, y + 3.6 * S * k),
                           (x + 5.4 * S * k, y + 3.6 * S * k)],
                          fill=(150, 140, 124), outline=(255, 255, 255))
                if lab.place(d, f"{ev0:.0f}", (x, y), f_peak_lo, (120, 106, 88),
                             anchor_right=True, stroke_w=1.8 * S, pad=LBL_PAD * S,
                             fallback=False, kind="peak"):
                    n_peak += 1
            continue
        if name in skip_names:
            n_skip += 1
            continue
        ev = _ele_of(ele)
        # 指定峰强制进顶级（2026-10-24 四姑娘大环线）：分级是**按本窗口高程分位**算的，
        # 一山之主（四姑娘山的大/二/三姑娘峰 5025–5355）在 5500–6250 一大片峰的窗口里
        # 只能落到 p62 以下 → 最低档、not forced → 拥挤时直接丢掉，名字整片消失。
        # CFG["peak_top_names"] 里的名字跳过分级，按顶级字号/颜色/三角出图（与其它具名峰同款）。
        _top_names = set(GC.cfg("peak_top_names", ()) or ())
        if name in _top_names:
            f_peak_use, forced, tri_k, col, tcol = f_peak_hi, True, 1.0, (84, 60, 40), (48, 32, 20)
        elif ev is None:
            f_peak_use, forced, tri_k, col, tcol = f_peak_lo, False, 0.82, (124, 102, 82), (92, 74, 56)
        elif q_hi is None:
            # 样本不足 PEAK_TIER_MIN_N：无法可靠分级，统一中档保证有名字的峰都出得来
            f_peak_use, forced, tri_k, col, tcol = f_peak, True, 0.92, (84, 60, 40), (58, 40, 26)
        elif ev >= q_hi:
            f_peak_use, forced, tri_k, col, tcol = f_peak_hi, True, 1.0, (84, 60, 40), (48, 32, 20)
        elif ev >= q_mid:
            f_peak_use, forced, tri_k, col, tcol = f_peak, True, 0.92, (84, 60, 40), (58, 40, 26)
        else:
            f_peak_use, forced, tri_k, col, tcol = f_peak_lo, False, 0.82, (124, 102, 82), (92, 74, 56)
        if name in _force_names:
            forced = True            # 点名峰：允许挤放（fallback 生效）
        if _peak_all:
            forced = True            # 全量模式：具名峰一律允许挤放，尽量都出
        tri = [(x, y - 6 * S * tri_k), (x - 5.4 * S * tri_k, y + 3.6 * S * tri_k),
               (x + 5.4 * S * tri_k, y + 3.6 * S * tri_k)]
        d.polygon(tri, fill=col, outline=(255, 255, 255))
        txt = f"{name} {ele}m" if ele else name
        if lab.place(d, txt, (x, y), f_peak_use, tcol, anchor_right=True,
                     stroke_w=2.1 * S, pad=LBL_PAD * S, fallback=forced and not NO_CROWD,
                     gap=(5.4 * tri_k + 0.5) if _peak_tight else 5.0,
                     right_only=_peak_right):
            n_peak += 1
            n_named_done += 1

    # 冰川名：菱形冰符号 + 深冰蓝粗体 + 白描边。冰川面上只标最大的几条
    # （本区 382 条冰川里只有 18 条有名字），注记里不带高程 —— 冰川看的是范围与末端。
    n_glacier_lb = 0
    if draw_glacier:
        glaciers.sort(key=lambda g: g[3], reverse=True)
        for x, y, name, _a in glaciers:
            if name in skip_names:
                continue
            r = 4.6 * S
            d.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)],
                      fill=(233, 244, 252), outline=C_GLACIER_LB)
            if lab.place(d, name, (x, y + 7 * S), f_water, C_GLACIER_LB, anchor_center=False,
                         stroke_w=2.1 * S, pad=LBL_PAD * S):
                n_glacier_lb += 1

    # 哑口 / 垭口：倒三角 ▽ + 名 + ele，用 warn 橙（与峰名的 ▲ 明确区分）。
    # 画在峰名之后、地名之前 —— 垭口是"线路必经点"，重要性高于一般地名。
    # 字号用 f_water（11.5px 加粗）而不是 peaks 循环里的 f_peak_use：
    # 后者是循环内变量，且念青东这类 peak=1 的窗口压根不进分级逻辑。
    n_saddle = 0
    for x, y, name, ele in saddles:
        if not name or name in skip_names:
            continue
        r = 4.2 * S
        d.polygon([(x - r, y - r * 0.9), (x + r, y - r * 0.9), (x, y + r * 1.1)],
                  fill=(255, 244, 232), outline=(176, 96, 32))
        txt = f"{name} {ele}m" if ele else name
        if lab.place(d, txt, (x, y), f_water, (150, 78, 24), anchor_right=True,
                     stroke_w=2.1 * S, pad=LBL_PAD * S, fallback=not NO_CROWD):
            n_saddle += 1

    prio = {"city": 0, "town": 1, "suburb": 2, "village": 3, "neighbourhood": 4, "hamlet": 5}
    places.sort(key=lambda p: prio.get(p[0], 9))
    n_place = 0
    for kind, x, y, name in places:
        if not name or name in skip_names:
            continue
        f = f_town if kind in ("city", "town") else (f_vil if kind == "village" else f_ham)
        col = (40, 40, 40) if kind in ("city", "town") else (62, 62, 62)
        # 点位符号跟着字号收一档（2026-10-17）：字缩 0.85 后，大圆点配小字会"头重脚轻"
        r = (2.7 if kind in ("city", "town") else 2.2) * S
        d.ellipse([x - r, y - r, x + r, y + r], fill=(70, 70, 70), outline=(255, 255, 255))
        if lab.place(d, name, (x, y), f, col, anchor_right=True,
                     stroke_w=2 * S, pad=LBL_PAD * S, fallback=not NO_CROWD):
            n_place += 1

    n_poi = 0
    for x, y, name, kind in pois:
        if not name or name in skip_names:
            continue
        if kind == "temple":
            d.rectangle([x - 3 * S, y - 3 * S, x + 3 * S, y + 3 * S], fill=(178, 96, 72),
                        outline=(255, 255, 255))
        else:
            r = 2.4 * S
            d.ellipse([x - r, y - r, x + r, y + r], fill=(96, 132, 96),
                      outline=(255, 255, 255))
        # 兴趣点名（观景点/民宿/寺庙）优先级最低：**不放 fallback** ——
        # 它们的价值远低于"被压住"的代价，而峰名/湖名/地名宁可轻微重叠也要显示。
        if lab.place(d, name, (x, y), f_poi, (58, 66, 56), anchor_right=True,
                     stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
            n_poi += 1

    # ---- 山谷 / 山脊名：荒野区唯一可用的地形参照（2026-10-29 补） ----
    # 此前 draw_osm 只处理 node 的 peak/saddle/place，`natural=valley` 的 way
    # （贡嘎窗口 38 条、37 条有名字）一条都没画 —— 荒野段图上只剩山峰三角。
    # ⚠ 同一条山谷会被 OSM 拆成多个 way，各带同一个 name（巴王沟实测同屏两遍、
    #   大沟同屏两遍）→ 必须走 _dedupe_by_name，不然名字会沿山谷写满。
    geo = []
    for e in els:
        t = e.get("tags", {})
        if t.get("natural") not in ("valley", "ridge"):
            continue
        nmv = _pick_name(t)
        if not nmv or nmv in skip_names:
            continue
        # ⚠ 同一要素常同时有 natural=valley + waterway=stream（贡嘎的巴王沟/大沟都是），
        #   两个通道各画一遍 → 名字同屏重复。约定：**带 waterway 的归水系通道**（蓝色斜体，
        #   有自己的 _dedupe_by_name），这里只画纯 valley/ridge。
        if t.get("waterway"):
            continue
        if e["type"] == "node":
            if "lon" not in e:
                continue
            x, y = proj(e["lon"], e["lat"])
        else:
            pts = _pts_of(e, proj, size)
            if not pts or len(pts) < 2:
                continue
            x, y = pts[len(pts) // 2]
        if not (0 <= x <= size[0] and 0 <= y <= size[1]):
            continue
        geo.append((x, y, nmv, t.get("natural") == "valley"))
    # sep 取图宽 12%（与 places 同档）：山谷名之间的合法间距通常远大于这个
    geo = _dedupe_by_name(geo, max(300.0, size[0] * 0.12) * S, 2)
    n_geo = 0
    for x, y, nmv, _is_valley in geo:
        _col = (84, 104, 88) if _is_valley else (110, 98, 86)
        if lab.place(d, nmv, (x, y), f_poi, _col, anchor_right=True,
                     stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
            n_geo += 1

    # ---- 水源 / 营地 / 补给：徒步者最需要的一类点（2026-10-29 补） ----
    # 有名字的配字；无名的水源只画小蓝点（知道"这里有水"就够了，写字反而变噪声）。
    n_extra = {"spring": 0, "water": 0, "camp": 0, "supply": 0}
    # ⚠ 只对**具名**的去重：无名水源/营地名字为空，彼此全视为同名 → 会被去成 1 个
    #   （2026-10-29 实测「水源 10 → 0」）。无名的按坐标容忍共存（画符号不写字）。
    _extras_named = [e for e in extras if e[2]]
    _extras_anon = [e for e in extras if not e[2]]
    extras = _dedupe_by_name(_extras_named, max(300.0, size[0] * 0.12) * S, 2) + _extras_anon
    for x, y, name, kind, sub in extras:
        if kind in ("spring", "water"):
            r = 2.0 * S
            d.ellipse([x - r, y - r, x + r, y + r], fill=(30, 106, 168), outline=(255, 255, 255))
            if name and name not in skip_names:
                if lab.place(d, name, (x, y), f_poi, (26, 84, 138), anchor_right=True,
                             stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
                    n_extra[kind] += 1
            else:
                n_extra[kind] += 1
        elif kind == "camp":
            r = 3.4 * S
            d.polygon([(x, y - r), (x - r * 1.15, y + r * 0.8), (x + r * 1.15, y + r * 0.8)],
                      fill=(226, 238, 224), outline=(74, 118, 74))
            if name and name not in skip_names:
                if lab.place(d, name, (x, y), f_poi, (60, 92, 60), anchor_right=True,
                             stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
                    n_extra[kind] += 1
        else:                                   # supply：小卖部 / 补给 / 餐饮
            r = 2.6 * S
            d.rectangle([x - r, y - r, x + r, y + r], fill=(206, 168, 96), outline=(255, 255, 255))
            if name and name not in skip_names:
                if lab.place(d, name, (x, y), f_poi, (122, 92, 40), anchor_right=True,
                             stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
                    n_extra[kind] += 1

    # ---- 海子 / 水系名：蓝色斜体感（用深青蓝 + 白描边），动态避让已有注记 ----
    n_water = 0
    for x, y, name in waters:
        if not name or name in skip_names:
            continue
        if lab.place(d, name, (x, y), f_water, (36, 84, 128), anchor_right=True,
                     stroke_w=2.1 * S, pad=LBL_PAD * S, fallback=not NO_CROWD):
            n_water += 1

    # ---- 道路编号（ref）标注：省道/国道/县道等，沿道路中点水平放置 ----
    # road_refs 已被 _dedupe_by_name 去过重，这里是 (cx, cy, ref, pts)
    n_road_ref = 0
    if road_refs:
        f_road_ref = _font(F(10.5), True)
        f_road_name = _font(F(10.0), False)
        for _cx, _cy, ref_clean, pts, _is_name in road_refs:
            if len(pts) < 2:
                continue
            # 计算道路在图上的长度，太短不标
            L = 0.0
            segs = []
            for i in range(len(pts) - 1):
                seg = math.hypot(pts[i + 1][0] - pts[i][0],
                                 pts[i + 1][1] - pts[i][1])
                segs.append(seg)
                L += seg
            if L < 30 * S:                 # 图上不足约 30 px 不标
                continue
            # ref 已在去重阶段 split(";") 取过首编号，这里只做空值兜底
            if not ref_clean:
                continue
            # 颜色与暖沙色道路芯形成对比，深棕近黑；路名（无编号）退一档
            col = (104, 78, 58) if _is_name else (88, 60, 40)
            # 沿路多点尝试（2026-10-24）：牧道动辄 30–40 km、纵贯全图，
            # 只试"半程中点"太脆弱 —— 中点正好压在营地图钉群上就整条名字丢掉
            # （实测「乌图精牧道」39 km 一条都没上）。按 中 → 3/4 → 1/4 → 两端 依次外移，
            # 第一个放得下的位置胜出，读起来仍像"沿这条路标注"。
            _ok = False
            for _frac in (0.5, 0.34, 0.66, 0.2, 0.8, 0.1, 0.9):
                _target = L * _frac
                acc = 0.0
                mx, my = pts[0]
                for i, seg in enumerate(segs):
                    if acc + seg >= _target:
                        t = (_target - acc) / seg if seg else 0.0
                        x0, y0 = pts[i]
                        x1, y1 = pts[i + 1]
                        mx = x0 + (x1 - x0) * t
                        my = y0 + (y1 - y0) * t
                        break
                    acc += seg
            if lab.place(d, ref_clean, (mx, my),
                         f_road_name if _is_name else f_road_ref, col,
                         anchor_right=True, stroke_w=2.0 * S,
                         pad=LBL_PAD * S, fallback=not NO_CROWD):
                    _ok = True
                    break
            if _ok:
                n_road_ref += 1

    _tier = (f"[p{PEAK_TIER_PCT[0]}≥{q_hi:.0f}m/p{PEAK_TIER_PCT[1]}≥{q_mid:.0f}m]"
             if q_hi is not None else f"[样本<{PEAK_TIER_MIN_N}，未分级]")
    if verbose:
        extra = f" · 抑制重名 {n_skip}" if n_skip else ""
        gl = f" · 冰川面 {n_glacier} · 冰川名 {n_glacier_lb}/{len(glaciers)}" if draw_glacier \
            else " · 冰川层 关"
        # ⚠ 挤放数（n_overlap）是"有没有重叠"的**唯一可测指标**：
        #   落位率高不代表排得好（可能全是挤出来的），必须看这个数。
        #   2026-10-17 希夏邦马：字号 ×0.85 + 留白 2→2.8 后由 挤放 N 降到 M。
        bd = f" · 国界 {n_border} 段" if n_border else ""
        print(f"   OSM 叠加：面 {n_poly} · 线 {n_line}{gl}{bd} · "
              f"山峰名 {n_peak}/{len(peaks)}（具名 {n_named_done}/{n_named}）" + _tier + " · "
              f"哑口名 {n_saddle}/{len(saddles)} · "
              f"地名 {n_place}/{len(places)} · "
              f"水系名 {n_water}/{len(waters)} · "
              f"兴趣点 {n_poi}/{len(pois)} · "
              f"地形名 {n_geo} · 音源 {n_extra['spring'] + n_extra['water']} · "
              f"营地 {n_extra['camp']} · 补给 {n_extra['supply']} · "
              f"道路编号 {n_road_ref}/{len(road_refs)} · 挤放 {lab.n_overlap}{extra}")
        if _STITCH_STATS["dropped"]:
            print(f"   ⚠ 缝合残链 {_STITCH_STATS['dropped']} 条"
                  f"（{_STITCH_STATS['dropped_pts']} 点）已丢弃 —— "
                  f"relation 成员未闭合且接不上，检查下载窗口是否截断了湖/冰川")

    # 导出给 map_svg：占位框（避让）+ 矢量注记（画字）
    try:
        (OUT / "label_boxes.json").write_text(
            json.dumps({"ss": S,
                        "boxes": [[round(v, 1) for v in b] for b in lab.boxes],
                        "labels": lab.records}, ensure_ascii=False), encoding="utf-8")
    except Exception as _e:                                        # noqa
        print(f"   ⚠ 注记导出失败：{_e}")

    return Image.alpha_composite(pil.convert("RGBA"), ov).convert("RGB")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / "base_map.jpg"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    meta = json.loads((OUT / "base_meta.json").read_text(encoding="utf-8"))
    im = Image.open(src).convert("RGB")
    # 底图可能是超采样栅格（宽 = 逻辑宽 × ss）。这里按实际像素自动推断倍率，
    # 免得手工传错导致线宽/字号或投影整体错位。
    ss = im.width / meta["size"][0]
    print(f"输入 {im.size}  逻辑 {tuple(meta['size'])}  超采样 {ss:.2f}×  z{meta['z']}")
    try:
        import guide_common as GC
        import route_def as _RD
        skip = {d["name"] for d in GC.pois_of(_RD)}
    except Exception:                                              # noqa
        skip = set()
    im = draw_overlay(im, meta, skip_names=skip,
                      px_scale=ss, proj_scale=meta["scale"] * ss)
    im.save(dst, quality=88, optimize=True, progressive=True)
    print(f"输出 {dst}  {dst.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()

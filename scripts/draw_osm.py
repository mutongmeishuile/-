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

TILE = 256
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

# ---- 配色（浅色地形底图上取可读性优先）----
C_WOOD      = (198, 220, 190, 95)
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
    # 步道配色是这张图最容易翻车的地方，踩过两轮：
    #   ① 浅棕 → 直接"消失"在山体里；
    #   ② 深棕（86,58,44）→ 虽然压得住底色，却和**计曲线**（120,94,68）同色调，
    #      等高线一密，步道就"长"成了等高线，两者谁也认不出。用户原话："小路与整体配色接近"。
    # 结论：**必须换色相，不是换明度**。棕色已归等高线、橙色归公路、蓝色归水系。
    #   ③ 又一轮：改用的深紫撞上了攻略主线里的紫系（#6C4FD8）。
    #      OSM 步道网在线路侧翼成片铺开时，一眼看去分不清"哪条是我要走的"。
    #      攻略主线通常已占掉橙红 / 绿 / 紫三个色相，底图步道就不该再用有彩色 ——
    #      最终定案**中性灰**：无色相即不可能撞色，且灰在暖棕地形上是天然"背景层"色。
    "path":      ((140, 138, 134), 2.1, 8, 5),
    "footway":   ((140, 138, 134), 2.0, 7, 5),
    "steps":     ((140, 138, 134), 2.3, 4.5, 3),
    "bridleway": ((152, 148, 144), 1.9, 8, 6),
}
# 步道描边：**取消**。原本纸色 (252,250,245) 是用来把深紫步道从地形里"抬"起来的，
# 但描边比地形亮得多 → 步道网成片铺开时整片只剩白色"鬼影线"，
# 灰芯的虚线反而看不见（实测：白色描边成了图上最抢眼的东西）。
# 换成中性灰芯之后，描边已经没有存在意义，直接不画。
C_TRAIL_CASING = None


def _font(size, bold=False):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:                                          # noqa
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ 投影
def make_projector(z, tx0, ty0, box, scale):
    """返回 lonlat → 输出像素 的函数。"""
    n = TILE * 2 ** z
    ox, oy = tx0 * TILE + box[0], ty0 * TILE + box[1]

    def proj(lon, lat):
        x = (lon + 180.0) / 360.0 * n
        s = math.sin(math.radians(lat))
        y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
        return ((x - ox) * scale, (y - oy) * scale)

    return proj


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


def _ring_area(ring):
    """鞋带公式：环的有向面积（用来在 relation 的多个 member 里挑主环）。"""
    s = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def _rings_of(e, proj, size):
    """要素 → 多边形环列表（每个环是一串像素点）。

    way：geometry 直接成环。
    relation（OSM 的冰川常用 relation 打组，`out geom` 下 members 带 geometry）：
    逐个 outer member way 成环 —— 相邻 member 各自填充后视觉上仍是一个整体。
    """
    rings = []
    if e["type"] == "way":
        pts = _pts_of(e, proj, size)
        if pts and len(pts) > 2:
            rings.append(pts)
    elif e["type"] == "relation":
        for m in e.get("members", []):
            if m.get("type") != "way" or m.get("role") not in (None, "", "outer"):
                continue
            g = m.get("geometry") or []
            pts = [proj(p["lon"], p["lat"]) for p in g if p.get("lon") is not None]
            if len(pts) > 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if max(xs) < 0 or min(xs) > size[0] or max(ys) < 0 or min(ys) > size[1]:
                    continue
                rings.append([(round(x, 1), round(y, 1)) for x, y in pts])
    return rings


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
    """读 route_def.CFG 里的布尔开关（取不到 / 不是布尔就给默认）。

    存在的意义：**按线路切换图层**不该靠改脚本常量 —— 那等于把线路差异写死进技能。
    （同 _route_cfg，只是那个在 make_terrain 里，且偏数值。）
    """
    try:
        import route_def as _RD
        v = (getattr(_RD, "CFG", None) or {}).get(key)
    except Exception:                                              # noqa
        v = None
    return default if v is None else bool(v)


class Labeller:
    """按优先级占位放注记，重叠就跳过。cell 是占位网格边长（随超采样倍率放大）。"""

    def __init__(self, size, cell=14):
        self.size = size
        self.grid = set()
        self.cell = cell
        self.n_overlap = 0     # 「实在放不下也要放」的强制落位数（= 会与已有注记挤在一起的个数）

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
              anchor_center=True, force=False, pad=2, stroke_w=1.8, fallback=True):
        """放注记。优先完全不压已有注记；**实在放不下也要放**（fallback）。

        ⚠ 2026-10-09 亚丁线实测：等高线高程标注会先占用网格（104 个标注 ≈ 10% 单元，
        且**恰好密集在山脊/峰顶**），而峰名/湖名本来就在同一片区域 —— 旧实现
        "找不到空位就 return False"（静默丢弃），结果是 **15 个山峰名只画出来 2 个**，
        用户看到的就是"底图没有地名"。地名被压在等高线数字上顶多是拥挤，
        整条名字消失是信息缺失 —— 两害相权，宁可轻微重叠也要显示。
        """
        W, H = self.size
        bb = d.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        x0c, y0c = xy
        offsets = [(0, 0)]
        for r in (10, 18, 28):                 # 偏移量别太大：峰名离峰顶太远会误读
            offsets += [(0, -r), (0, r), (-r, 0), (r, 0),
                        (-r, -r), (r, -r), (-r, r), (r, r)]
        best = None
        for odx, ody in offsets:
            x, y = x0c + odx, y0c + ody
            if anchor_center:
                x -= tw / 2
            box = (x - pad, y - pad, x + tw + pad, y + th + pad)
            if box[0] < 0 or box[2] > W or box[1] < 0 or box[3] > H:
                continue
            hits = self._hits(box)
            if hits == 0:                                  # 完美位置
                d.text((x, y), text, font=font, fill=fill,
                       stroke_width=stroke_w, stroke_fill=stroke)
                self._mark(box)
                return True
            if best is None or hits < best[0]:             # 记下最不挤的那个
                best = (hits, x, y, box)
        if not force and not fallback:
            return False
        if best is None:
            return False
        self.n_overlap += 1            # 命中"两害相权"分支：记录一次挤放，供自检核对
        _, x, y, box = best
        d.text((x, y), text, font=font, fill=fill,
               stroke_width=stroke_w, stroke_fill=stroke)
        self._mark(box)
        return True


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


def draw_overlay(pil, meta, osm_path=None, skip_names=None,
                 px_scale=1.0, proj_scale=None, seed_cells=None, verbose=True):
    """把 OSM 要素叠到 pil 上。

    skip_names: 需要抑制的注记名（一般是攻略自己的 POI 名）。
      重名时两边海拔常不同（OSM 十王峰 1344.4 vs 实测轨迹 1345.5），
      同图出现两个数会让人误判——**以实测轨迹为准，抑制 OSM 那条**。
    px_scale:  栅格超采样倍率。底图按 LOGICAL_W×SS 出图时传 SS，
      线宽/字号/图标/占位网格全部同比放大，缩回逻辑尺寸后即得抗锯齿的锐利边缘。
    proj_scale: 投影用的「栅格像素 / 瓦片像素」比例；默认取 meta["scale"]（非超采样场景）。
    seed_cells: 已被占用的注记网格（cell = 14×px_scale），一般传等高线高程标注的占位，
      让地名/峰名主动避开等高线数字，避免两种注记叠在同一个点上。
    """
    osm_path = Path(osm_path) if osm_path else (OUT / "osm.json")
    # 冰川层按线路开关：默认关，route_def.CFG["glacier_layer"]=True 才画（判据见常量注释）
    draw_glacier = _cfg_flag("glacier_layer", DRAW_GLACIER)
    if not osm_path.exists():
        print(f"   跳过 OSM 叠加（未找到 {osm_path.name}，先跑 fetch_osm.py）")
        return pil
    skip_names = set(skip_names or ())
    S = float(px_scale)

    def W(v):
        """线宽 → 栅格整数像素。"""
        return max(1, int(round(v * S)))

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
             "trail": 5, "road": 6}
    drawable = []
    for e in els:
        b = bucket(e)
        if not b:
            continue
        if b[0] == "glacier":
            for ring in _rings_of(e, proj, size):
                drawable.append((order["glacier"], "glacier", None, ring, e))
            continue
        # 面状要素（林地/草地/水体）统一走 _rings_of —— way 直接成环，
        # relation（multipolygon，如 OSM 大湖）取 outer member 环。
        # 否则 relation 水体没有顶层 geometry，_pts_of 返回 None 会被整片漏掉
        # （2026-10-17 年宝玉则仙女湖踩坑）。
        if b[0] in ("wood", "meadow", "waterbody"):
            for ring in _rings_of(e, proj, size):
                drawable.append((order[b[0]], b[0], b[1], ring, e))
            continue
        pts = _pts_of(e, proj, size)
        if pts:
            drawable.append((order[b[0]], b[0], b[1], pts, e))
    drawable.sort(key=lambda x: x[0])

    n_poly = n_line = n_glacier = 0
    for _, kind, sub, pts, e in drawable:
        if kind == "wood":
            d.polygon(pts, fill=C_WOOD) if len(pts) > 2 else None
            n_poly += 1
        elif kind == "meadow":
            if len(pts) > 2:
                d.polygon(pts, fill=C_MEADOW)
                n_poly += 1
        elif kind == "waterbody":
            if len(pts) > 2:
                d.polygon(pts, fill=C_WATER)
                d.line(pts + [pts[0]], fill=C_WATER_ED, width=W(1))
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
        elif kind == "road":
            core, w, ed = ROAD_STYLE[sub]
            d.line(pts, fill=ed, width=W(w) + W(2), joint="curve")   # 描边
            d.line(pts, fill=core, width=W(w), joint="curve")
            n_line += 1

    # ---------------- 注记 ----------------
    lab = Labeller(size, cell=14 * S)
    if seed_cells:
        lab.grid |= set(seed_cells)
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

    peaks, places, pois, waters, saddles = [], [], [], [], []
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
            peaks.append((x, y, t.get("name", ""), t.get("ele", "")))
        elif t.get("natural") == "saddle":
            # 哑口 / 垭口（2026-10-17 念青东补）。OSM 用 natural=saddle，
            # 也常带 mountain_pass=yes。高原线路的垭口是**关键地理标识**，
            # 而 peak 在非登山区（念青东/横断山腹地）常常一个都没有 ——
            # saddle 是那一带唯一稳的山口标注来源。
            # ⚠ 与攻略自己的垭口 POI 同名时由 skip_names 去重，不会画两遍。
            saddles.append((x, y, t.get("name", ""), t.get("ele", "")))
        elif "place" in t:
            # place=locality（小地名）也走这支：prio 里没有 locality 会排到最后，
            # 用 f_ham 画，无需单独分支。
            places.append((t["place"], x, y, t.get("name", "")))
        elif t.get("natural") == "water":
            waters.append((x, y, t.get("name", "")))
        elif t.get("tourism") in ("attraction", "viewpoint", "alpine_hut"):
            pois.append((x, y, t.get("name", ""), t["tourism"]))
        elif t.get("amenity") == "place_of_worship":
            pois.append((x, y, t.get("name", ""), "temple"))

    # 湖泊（面）与河流（线）的名字：锚点取要素顶点/最大环的重心，再交给 Labeller 动态避让
    for e in els:
        t = e.get("tags", {})
        nm = t.get("name")
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
        if not (-40 * S < cx < size[0] + 40 * S and -40 * S < cy < size[1] + 40 * S):
            continue
        waters.append((cx, cy, nm))

    # 冰川名：锚点取最大环的重心（relation 的多个 member 里挑面积最大的那个）
    glaciers = []
    for e in els:
        t = e.get("tags", {})
        if t.get("natural") != "glacier":
            continue
        nm = _clean_name(t.get("name", ""))
        if not nm:
            continue
        rings = _rings_of(e, proj, size)
        if not rings:
            continue
        ring = max(rings, key=lambda r: abs(_ring_area(r)))
        glaciers.append((sum(p[0] for p in ring) / len(ring),
                         sum(p[1] for p in ring) / len(ring), nm,
                         abs(_ring_area(ring))))

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
    peaks = [(x, y, _clean_name(n), e) for x, y, n, e in peaks]
    saddles = [(x, y, _clean_name(n), e) for x, y, n, e in saddles]
    places = [(k, x, y, _clean_name(n)) for k, x, y, n in places]
    waters = [(x, y, _clean_name(n)) for x, y, n in waters]
    pois = [(x, y, _clean_name(n), k) for x, y, n, k in pois]
    peaks.sort(key=lambda p: _ele_of(p[3]) or -1.0, reverse=True)
    # 分位数按本窗口**所有有高程的峰点**算（含无名字的）——要反映"这片地形上多高算突出"，
    # 而不是 OSM 的命名稀疏度（高峰也常有没录名字的）。
    _ev = [v for v in (_ele_of(p[3]) for p in peaks) if v is not None]
    if len(_ev) >= PEAK_TIER_MIN_N:
        q_hi, q_mid = _pct(_ev, PEAK_TIER_PCT[0]), _pct(_ev, PEAK_TIER_PCT[1])
    else:
        q_hi = q_mid = None       # 样本太少：不做相对分级，一律中档
    for x, y, name, ele in peaks:
        if not name or name in skip_names:
            n_skip += 1 if name else 0
            continue
        ev = _ele_of(ele)
        if ev is None:
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
        tri = [(x, y - 6 * S * tri_k), (x - 5.4 * S * tri_k, y + 3.6 * S * tri_k),
               (x + 5.4 * S * tri_k, y + 3.6 * S * tri_k)]
        d.polygon(tri, fill=col, outline=(255, 255, 255))
        txt = f"{name} {ele}m" if ele else name
        if lab.place(d, txt, (x, y + 7 * S), f_peak_use, tcol, anchor_center=False,
                     stroke_w=2.1 * S, pad=LBL_PAD * S, fallback=forced):
            n_peak += 1

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
        if lab.place(d, txt, (x, y + 7 * S), f_water, (150, 78, 24), anchor_center=False,
                     stroke_w=2.1 * S, pad=LBL_PAD * S, fallback=True):
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
        if lab.place(d, name, (x, y + 4 * S), f, col, anchor_center=False,
                     stroke_w=2 * S, pad=LBL_PAD * S):
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
        if lab.place(d, name, (x, y + 4 * S), f_poi, (58, 66, 56), anchor_center=False,
                     stroke_w=2 * S, pad=LBL_PAD * S, fallback=False):
            n_poi += 1

    # ---- 海子 / 水系名：蓝色斜体感（用深青蓝 + 白描边），动态避让已有注记 ----
    n_water = 0
    for x, y, name in waters:
        if not name or name in skip_names:
            continue
        if lab.place(d, name, (x, y), f_water, (36, 84, 128), anchor_center=True,
                     stroke_w=2.1 * S, pad=LBL_PAD * S):
            n_water += 1

    _tier = (f"[p{PEAK_TIER_PCT[0]}≥{q_hi:.0f}m/p{PEAK_TIER_PCT[1]}≥{q_mid:.0f}m]"
             if q_hi is not None else f"[样本<{PEAK_TIER_MIN_N}，未分级]")
    if verbose:
        extra = f" · 抑制重名 {n_skip}" if n_skip else ""
        gl = f" · 冰川面 {n_glacier} · 冰川名 {n_glacier_lb}/{len(glaciers)}" if draw_glacier \
            else " · 冰川层 关"
        # ⚠ 挤放数（n_overlap）是"有没有重叠"的**唯一可测指标**：
        #   落位率高不代表排得好（可能全是挤出来的），必须看这个数。
        #   2026-10-17 希夏邦马：字号 ×0.85 + 留白 2→2.8 后由 挤放 N 降到 M。
        print(f"   OSM 叠加：面 {n_poly} · 线 {n_line}{gl} · "
              f"山峰名 {n_peak}/{len(peaks)}" + _tier + " · "
              f"哑口名 {n_saddle}/{len(saddles)} · "
              f"地名 {n_place}/{len(places)} · "
              f"水系名 {n_water}/{len(waters)} · "
              f"兴趣点 {n_poi}/{len(pois)} · 挤放 {lab.n_overlap}{extra}")

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

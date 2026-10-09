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


# ------------------------------------------------------------------ 注记
class Labeller:
    """按优先级占位放注记，重叠就跳过。cell 是占位网格边长（随超采样倍率放大）。"""

    def __init__(self, size, cell=14):
        self.size = size
        self.grid = set()
        self.cell = cell

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
              anchor_center=True, force=False, pad=2, stroke_w=2, fallback=True):
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
        _, x, y, box = best
        d.text((x, y), text, font=font, fill=fill,
               stroke_width=stroke_w, stroke_fill=stroke)
        self._mark(box)
        return True


# ------------------------------------------------------------------ 主流程
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
        if e["type"] != "way":
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
        if t.get("natural") == "wood" or t.get("landuse") == "forest":
            return ("wood", None)
        if t.get("landuse") == "meadow":
            return ("meadow", None)
        return None

    # 面 → 线 → 注记（保证层次）
    order = {"wood": 0, "meadow": 1, "waterbody": 2, "water": 3,
             "trail": 4, "road": 5}
    drawable = []
    for e in els:
        b = bucket(e)
        if not b:
            continue
        pts = _pts_of(e, proj, size)
        if pts:
            drawable.append((order[b[0]], b[0], b[1], pts, e))
    drawable.sort(key=lambda x: x[0])

    n_poly = n_line = 0
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
        elif kind == "water":
            big = sub in ("river", "canal")
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
    # ⚠ 字号要"按最坏情况"定：底图会同时被 ①长图/网页按 ~0.4 倍缩小嵌入、②高清图 1:1 看。
    #   2026-10-09 亚丁线实测反馈：11.5 px 的峰名缩进长图后完全看不见
    #   → 整体放大约 1.35 倍、颜色加深，做到与等高线高程标注同一量级，缩图后仍可读。
    f_town = _font(F(18), True)
    f_vil = _font(F(16.5), True)
    f_ham = _font(F(14.5))
    f_peak = _font(F(15), True)
    f_poi = _font(F(13))
    f_water = _font(F(13.5), True)

    peaks, places, pois, waters = [], [], [], []
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
        elif "place" in t:
            places.append((t["place"], x, y, t.get("name", "")))
        elif t.get("natural") == "water":
            waters.append((x, y, t.get("name", "")))
        elif t.get("tourism") in ("attraction", "viewpoint", "alpine_hut"):
            pois.append((x, y, t.get("name", ""), t["tourism"]))
        elif t.get("amenity") == "place_of_worship":
            pois.append((x, y, t.get("name", ""), "temple"))

    # 湖泊（面）与河流（线）的名字：锚点取要素顶点的重心，再交给 Labeller 动态避让
    for e in els:
        t = e.get("tags", {})
        nm = t.get("name")
        if e["type"] != "way" or not nm:
            continue
        is_lake = t.get("natural") == "water" or t.get("landuse") == "reservoir"
        is_river = bool(t.get("waterway"))
        if not (is_lake or is_river):
            continue
        pts = _pts_of(e, proj, size)
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        if not (-40 * S < cx < size[0] + 40 * S and -40 * S < cy < size[1] + 40 * S):
            continue
        waters.append((cx, cy, nm))

    # 山峰：▲ + 名 + 高程
    n_peak = n_skip = 0
    for x, y, name, ele in peaks:
        if not name or name in skip_names:
            n_skip += 1 if name else 0
            continue
        tri = [(x, y - 6 * S), (x - 5.4 * S, y + 3.6 * S), (x + 5.4 * S, y + 3.6 * S)]
        d.polygon(tri, fill=(84, 60, 40), outline=(255, 255, 255))
        txt = f"{name} {ele}m" if ele else name
        if lab.place(d, txt, (x, y + 7 * S), f_peak, (58, 40, 26), anchor_center=False,
                     stroke_w=2.8 * S, pad=2 * S):
            n_peak += 1

    prio = {"city": 0, "town": 1, "suburb": 2, "village": 3, "neighbourhood": 4, "hamlet": 5}
    places.sort(key=lambda p: prio.get(p[0], 9))
    n_place = 0
    for kind, x, y, name in places:
        if not name or name in skip_names:
            continue
        f = f_town if kind in ("city", "town") else (f_vil if kind == "village" else f_ham)
        col = (40, 40, 40) if kind in ("city", "town") else (62, 62, 62)
        r = (3 if kind in ("city", "town") else 2.4) * S
        d.ellipse([x - r, y - r, x + r, y + r], fill=(70, 70, 70), outline=(255, 255, 255))
        if lab.place(d, name, (x, y + 4 * S), f, col, anchor_center=False,
                     stroke_w=2 * S, pad=2 * S):
            n_place += 1

    n_poi = 0
    for x, y, name, kind in pois:
        if not name or name in skip_names:
            continue
        if kind == "temple":
            d.rectangle([x - 3 * S, y - 3 * S, x + 3 * S, y + 3 * S], fill=(178, 96, 72),
                        outline=(255, 255, 255))
        else:
            r = 2.6 * S
            d.ellipse([x - r, y - r, x + r, y + r], fill=(96, 132, 96),
                      outline=(255, 255, 255))
        # 兴趣点名（观景点/民宿/寺庙）优先级最低：**不放 fallback** ——
        # 它们的价值远低于"被压住"的代价，而峰名/湖名/地名宁可轻微重叠也要显示。
        if lab.place(d, name, (x, y + 4 * S), f_poi, (58, 66, 56), anchor_center=False,
                     stroke_w=2 * S, pad=2 * S, fallback=False):
            n_poi += 1

    # ---- 海子 / 水系名：蓝色斜体感（用深青蓝 + 白描边），动态避让已有注记 ----
    n_water = 0
    for x, y, name in waters:
        if not name or name in skip_names:
            continue
        if lab.place(d, name, (x, y), f_water, (36, 84, 128), anchor_center=True,
                     stroke_w=2.8 * S, pad=2 * S):
            n_water += 1

    if verbose:
        extra = f" · 抑制重名 {n_skip}" if n_skip else ""
        print(f"   OSM 叠加：面 {n_poly} · 线 {n_line} · "
              f"山峰名 {n_peak}/{len(peaks)} · 地名 {n_place}/{len(places)} · "
              f"水系名 {n_water}/{len(waters)} · "
              f"兴趣点 {n_poi}/{len(pois)}{extra}")

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

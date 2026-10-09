# -*- coding: utf-8 -*-
"""攻略流水线共享工具。

集中放"所有脚本必须达成一致"的东西，避免各脚本各写一套：

  ① 路径约定：HERE = scripts/，OUT = scripts/out/，ROOT = 项目根（交付物落这里）
  ② 无头浏览器探测：Chrome → Edge → PATH，不写死某一个
  ③ Web Mercator 投影：**必须与 make_terrain.py 的出图投影完全一致**
     （底图栅格 / 地图 SVG / 高清地图三处投影不一致 → 轨迹整体错位）
  ④ 中英混排文本宽度估算（标注避让与家具落点都要用）
  ⑤ 地图「三件套」（图例 / 指北针 / 比例尺）的动态落点求解器 `Placer`
  ⑥ **地图窗口（bbox）自动推导 + 覆盖校验** —— 窗口写死是"画错山"的根源，
     详见 `resolve_bbox()` / `assert_bbox_covers()` 的注释。

Placer 的设计目标（对应技能要求「缩小 + 动态放在合适位置，不遮盖轨迹」）：
  · 尺寸由调用方给定，调用方按 FURN_* 系数缩小；
  · **硬约束**：不与已放置的家具重叠、不与轨迹相交、不越界 —— 轨迹是硬约束，
    因为家具压住轨迹是"读图错误"，不是"不好看"；
  · 软约束：避开标注文本框（重罚）、避开 POI 符号（重罚）、避开轨迹密集区（轻罚）；
  · 偏好序：图例偏好四角、指北针偏好右上、比例尺偏好下缘。
"""
import json
import math
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ROOT = HERE.parent                      # 交付物（HTML / 长图 / 高清图 / GPX）落在这里
OUT.mkdir(exist_ok=True)

BROWSER_CANDS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_browser():
    """返回可用的无头浏览器路径（Chrome 优先，Edge 兜底，最后查 PATH）。"""
    for p in BROWSER_CANDS:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    raise FileNotFoundError(
        "找不到无头浏览器（Chrome / Edge）。装一个，或把路径加进 guide_common.BROWSER_CANDS。")


# ------------------------------------------------------------------ 底图元数据 / 投影
def load_meta():
    """读 make_terrain.py 落盘的 out/base_meta.json。

    缺文件时**干净退出**（打印一句"下一步该跑什么"，而不是甩一段 Traceback）——
    自助流程里"先跑谁"的提示比异常栈有用得多。
    """
    f = OUT / "base_meta.json"
    if not f.exists():
        raise SystemExit(
            f"缺 {f}\n"
            f"  → 还没出自渲染底图。按顺序跑：\n"
            f"      python scripts/prep_track.py      # 先有轨迹（窗口由它推）\n"
            f"      python scripts/fetch_osm.py --soft\n"
            f"      python scripts/make_terrain.py    # 出 base_map.jpg + base_meta.json\n"
            f"    或者直接：python scripts/make_all.py")
    return json.loads(f.read_text(encoding="utf-8"))


def projector_of(meta):
    """底图像素 ← 经纬度。**必须与 make_terrain.py 的投影逐字一致。**"""
    ox = meta["tx0"] * 256 + meta["box"][0]
    oy = meta["ty0"] * 256 + meta["box"][1]
    s, z = meta["scale"], meta["z"]

    def proj(lon, lat):
        n = 256 * 2 ** z
        x = (lon + 180.0) / 360.0 * n
        sn = math.sin(math.radians(lat))
        y = (0.5 - math.log((1 + sn) / (1 - sn)) / (4 * math.pi)) * n
        return (x - ox) * s, (y - oy) * s
    return proj


def m_per_px(meta):
    """1 个底图**栅格**像素对应多少米（比例尺用）。"""
    b = meta["bbox"]
    return (156543.03392 * math.cos(math.radians((b[1] + b[3]) / 2))
            / (2 ** meta["z"]) / meta["scale"])


# ------------------------------------------------------------------ 地图窗口（bbox）
# 为什么单独抽出来：窗口是**底图、OSM 矢量、地图 SVG、高清地图**四处共用的地基，
# 一旦某处写死坐标，底图与轨迹就会整体错位 —— 更糟的是**不报错**：
# terrarium 会老老实实把千里之外的另一座山渲出来，页面看起来"有山有水"，
# 只是那不是你要走的山。教训：曾把南京的窗口留给峨眉山用，整张图全错。
def track_pts():
    """读 out/track_real.json 的简化点（画图用那一份）。"""
    for cand in (OUT / "track_real.json", HERE / "track_real.json",
                 HERE / "kml" / "track_real.json"):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))["pts"]
            except Exception:                                      # noqa
                return []
    return []


def track_bbox(extra=()):
    """轨迹（+ 可选 POI）的经纬度包围盒。extra 项形如 (name, lon, lat) 或 (lon, lat)。"""
    lons, lats = [], []
    for p in track_pts():
        lons.append(p["lon"])
        lats.append(p["lat"])
    for e in extra:
        lons.append(e[-2])
        lats.append(e[-1])
    if not lons:
        return None
    return min(lons), min(lats), max(lons), max(lats)


def resolve_bbox(pad_frac=0.08, min_pad=0.004):
    """地图窗口 = 轨迹包围盒 + 留白。返回 `(lon0, lat0, lon1, lat1, 来源说明)`。

    优先级：`route_def.CFG['bbox']`（显式覆盖，给"轨迹只走了一小段、但想多看周围
    地形"的场合用）> 轨迹自动推。**默认走自动推，别写死坐标。**

    ⚠ 窗口**必须**以真实轨迹为前提。曾经踩过：轨迹还没生成时，`route_def.POIS` 的
      `_p()` 会退化成占位坐标 `(0.0, 0.0, 0.0)`，于是"轨迹+POI"的包围盒变成
      0°N 0°E 附近的一个小方块 —— **几内亚湾**。底图会老老实实把赤道大西洋渲出来，
      而且全程不报错。所以这里先卡住：没有真实轨迹就干净退出，让用户先去跑 prep。
    """
    if not track_pts():
        raise SystemExit(
            "地图窗口推不出来：out/track_real.json 还没生成（或没有轨迹点）。\n"
            "  → 地图窗口**必须**由真实轨迹决定。先跑：\n"
            "      python scripts/prep_track.py <你的.kml>\n"
            "    （万一确实想要一个与轨迹无关的窗口，就在 route_def.CFG['bbox'] 里显式给。）")
    extra = []
    try:
        sys.path.insert(0, str(HERE))
        import route_def as RD
        extra = [(p[1], p[2]) for p in getattr(RD, "POIS", [])
                 if len(p) >= 3 and (p[1] or p[2])]     # 丢掉 (0,0) 占位点
        fixed = getattr(RD, "CFG", {}).get("bbox")
        if fixed:
            return (*[float(v) for v in fixed], "route_def.CFG['bbox']（显式覆盖）")
    except SystemExit:
        raise
    except Exception:                                              # noqa
        pass
    bb = track_bbox(extra)
    if bb is None:
        raise SystemExit("地图窗口推不出来：轨迹与 POI 都没有可用坐标。")
    x0, y0, x1, y1 = bb
    px = max((x1 - x0) * pad_frac, min_pad)
    py = max((y1 - y0) * pad_frac, min_pad)
    return (round(x0 - px, 6), round(y0 - py, 6), round(x1 + px, 6), round(y1 + py, 6),
            f"轨迹包围盒 + {pad_frac:.0%} 留白（自动）")


def assert_bbox_covers(bbox, where=""):
    """自检：轨迹必须落在窗口内 —— 这是"画错山"唯一能自动拦下来的防线。"""
    bb = track_bbox()
    if bb is None:
        return
    x0, y0, x1, y1 = bbox
    ox0, oy0, ox1, oy1 = bb
    if ox0 < x0 - 1e-9 or oy0 < y0 - 1e-9 or ox1 > x1 + 1e-9 or oy1 > y1 + 1e-9:
        raise SystemExit(
            f"地图窗口没盖住轨迹（{where or '未知位置'}）！\n"
            f"  窗口 = {[round(v, 5) for v in bbox]}\n"
            f"  轨迹 = {[round(v, 5) for v in bb]}\n"
            f"  → 多半是 route_def.CFG['bbox'] 写错了；删掉它即可改用自动推导。")
    # 顺手报一下"窗口比轨迹大多少"，大到离谱说明坐标串错了半度
    dx = (x1 - x0) / max(ox1 - ox0, 1e-9)
    dy = (y1 - y0) / max(oy1 - oy0, 1e-9)
    if dx > 12 or dy > 12:
        print(f"   ⚠ 窗口是轨迹包围盒的 {dx:.1f}×{dy:.1f} 倍，先确认坐标没写错。")


# ------------------------------------------------------------------ 文本宽度
def text_w(txt, fs):
    """中英混排宽度估算：CJK 按 1 em，其余按 0.62 em。"""
    return sum(fs if ord(ch) > 0x2E80 else fs * 0.62 for ch in txt)


# ------------------------------------------------------------------ 几何
def rect_overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _seg_rect(p, q, r):
    """线段 pq 与矩形 r=(x0,y0,x1,y1) 是否相交（Liang–Barsky 裁剪）。"""
    dx, dy = q[0] - p[0], q[1] - p[1]
    u1, u2 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - r[0]), (dx, r[2] - p[0]),
                   (-dy, p[1] - r[1]), (dy, r[3] - p[1])):
        if pk == 0:
            if qk < 0:
                return False
        else:
            t = qk / pk
            if pk < 0:
                if t > u2:
                    return False
                u1 = max(u1, t)
            else:
                if t < u1:
                    return False
                u2 = min(u2, t)
    return True


class Placer:
    """地图三件套的动态落点求解器（详见模块 docstring）。"""

    ANCHORS = ("tl", "tr", "bl", "br", "tc", "bc", "ml", "mr")
    CELL = 24                      # 轨迹占用栅格的格边长（逻辑 px）

    def __init__(self, IW, IH, track=(), pad=22, track_half_w=5.0):
        self.IW, self.IH, self.pad = IW, IH, pad
        self.taken = []            # 已放置的家具矩形
        self.labels = []           # 标注文本框（软避让）
        self.pins = []             # POI 符号圆心（软避让）
        self.track = list(track)
        self._grid = None
        self._grid_of(track_half_w)

    # ---- 轨迹占用栅格：把轨迹（含线宽膨胀）栅格化，供 O(1) 相交预筛 ----
    def _grid_of(self, half_w):
        c = self.CELL
        gw, gh = int(self.IW // c) + 2, int(self.IH // c) + 2
        g = set()
        grow = int(half_w // c) + 1          # 膨胀格数 = 线宽 + 1 格余量
        for (x0, y0), (x1, y1) in zip(self.track, self.track[1:]):
            n = max(2, int(math.hypot(x1 - x0, y1 - y0) / (c * 0.6)) + 1)
            for k in range(n + 1):
                t = k / n
                cx, cy = int((x0 + (x1 - x0) * t) // c), int((y0 + (y1 - y0) * t) // c)
                for ddx in range(-grow, grow + 1):
                    for ddy in range(-grow, grow + 1):
                        g.add((cx + ddx, cy + ddy))
        self._grid = g

    def hits_track(self, rect):
        c = self.CELL
        x0, y0 = int(rect[0] // c) - 1, int(rect[1] // c) - 1
        x1, y1 = int(rect[2] // c) + 1, int(rect[3] // c) + 1
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                if (cx, cy) in self._grid:
                    return True
        return False

    def _anchor_xy(self, name, w, h, ins):
        p, IW, IH = self.pad + 6 + ins, self.IW, self.IH
        return {
            "tl": (p, p), "tr": (IW - p - w, p),
            "bl": (p, IH - p - h), "br": (IW - p - w, IH - p - h),
            "tc": ((IW - w) / 2, p), "bc": ((IW - w) / 2, IH - p - h),
            "ml": (p, (IH - h) / 2), "mr": (IW - p - w, (IH - h) / 2),
        }[name]

    def place(self, w, h, prefer=("tl", "tr", "bl", "br", "tc", "bc", "ml", "mr"),
              allow_track=False, weight_pin=400.0, weight_label=1000.0):
        """给尺寸 w×h 的家具找落点。返回 (x, y, rect)。

        **永远不返回 None**：硬约束全不满足时退到"加权最优"（宁可轻轻擦一下轨迹，
        也不能让整块图例凭空消失 —— 缺图例是读图事故，擦轨迹是美观问题）。
        """
        best = soft = None
        for ai, name in enumerate(prefer):
            for ins in (0, 14, 28):
                x0, y0 = self._anchor_xy(name, w, h, ins)
                if x0 < 2 or y0 < 2 or x0 + w > self.IW - 2 or y0 + h > self.IH - 2:
                    continue
                rect = (x0, y0, x0 + w, y0 + h)
                ov_f = sum(rect_overlap_area(rect, t) for t in self.taken)
                hit_t = self.hits_track(rect)
                cost = (weight_label * sum(rect_overlap_area(rect, b) for b in self.labels)
                        + weight_pin * sum(1 for px, py in self.pins
                                           if x0 - 26 <= px <= x0 + w + 26
                                           and y0 - 26 <= py <= y0 + h + 26)
                        + 3.0 * sum(1 for px, py in self.track
                                    if x0 <= px <= x0 + w and y0 <= py <= y0 + h)
                        + 6.0 * ai + 0.05 * ins)
                # 硬约束：家具互不重叠（1e6）、不压轨迹（1e4）
                if ov_f <= 0 and (allow_track or not hit_t):
                    if best is None or cost < best[0]:
                        best = (cost, x0, y0, rect)
                penal = cost + (1e6 if ov_f > 0 else 0.0) + (1e4 if hit_t else 0.0)
                if soft is None or penal < soft[0]:
                    soft = (penal, x0, y0, rect)
        pick = best or soft
        if pick is None:
            return None
        self.taken.append(pick[3])
        return pick[1], pick[2], pick[3]

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
  ⑦ 跨项目共享的 DEM 瓦片缓存目录 `dem_cache_dir()`（瓦片按 z/x/y 命名，天然全局唯一）
  ⑧ POI 归一化 `poi()` / `pois_of()`：元组与字典两种写法都收

Placer 的设计目标（对应技能要求「缩小 + 动态放在合适位置，不遮盖轨迹」）：
  · 尺寸由调用方给定，调用方按 FURN_* 系数缩小；
  · **硬约束**：不与已放置的家具重叠、不与轨迹相交、不越界 —— 轨迹是硬约束，
    因为家具压住轨迹是"读图错误"，不是"不好看"；
  · 软约束：避开标注文本框（重罚）、避开 POI 符号（重罚）、避开轨迹密集区（轻罚）；
  · 偏好序：图例偏好四角、指北针偏好右上、比例尺偏好下缘。
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ROOT = HERE.parent                      # 交付物（HTML / 长图 / 高清图 / GPX）落在这里
OUT.mkdir(exist_ok=True)


# ------------------------------------------------------------------ 全局缓存
def dem_cache_dir():
    """DEM 瓦片缓存目录 —— **用户级、跨项目共享**。

    瓦片文件名 `terr_{z}_{x}_{y}.png` 本身就全局唯一（z/x/y 是地球上的唯一格子），
    所以缓存**没有任何理由**放在项目里。旧版放在 `scripts/demcache/`，
    后果是：同一片山区做第二条线路时，一块都复用不上，又得重下几万块瓦片
    （实测冷缓存 100–200 s，热缓存 6 s —— 差别全在这里）。

    可用环境变量 `HIKING_DEM_CACHE` 覆盖（缓存盘不在 C 盘时用）。
    """
    env = os.environ.get("HIKING_DEM_CACHE")
    base = Path(env) if env else (Path.home() / ".workbuddy" / "cache" / "hiking-dem")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:                     # 家目录不可写时退回项目内，别让流水线断
        base = HERE / "demcache"
        base.mkdir(exist_ok=True)
    return base

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


# ------------------------------------------------------------------ 中文字体
# 2026-10-22：这份清单原先在 draw_osm.py 与 make_terrain.py **各抄一份**
# （make_terrain 那份还把 msyh.ttc 连写了两遍）。收敛到这里共用一份。
#
# ⚠ 首位是 msyh.ttc **常规**字重，不是粗体。用户 2026-10-21 反馈「等高线字太粗太大、
#   全局字体要精细」；提交 9a03d70 的说明写的是「首选 msyh.ttc 常规」，
#   但那次 diff 只往列表**第二位**插了一行，首位 msyhbd.ttc（粗体）没动 ——
#   装了微软雅黑的机器上第一位永远命中，等于这次改动从没生效过。这次才真正兑现。
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",            # 微软雅黑 Regular（首选）
    "C:/Windows/Fonts/msyhbd.ttc",          # 微软雅黑 Bold
    "C:/Windows/Fonts/simhei.ttf",          # 黑体：老 Windows 兜底（只有粗体）
    "/System/Library/Fonts/PingFang.ttc",   # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
FONT_CANDIDATES_BOLD = [                    # 粗体优先：标题带 / 数据块 / 峰名
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
] + FONT_CANDIDATES

_font_cache = {}


def label_font(size, bold=False, candidates=None):
    """取一个能画汉字的 PIL 字体；全平台都找不到就退 PIL 默认（会缺字，但不崩）。

    按 (size, bold, 清单名) 缓存 —— 大栅格上这函数会被调上万次，
    每次都 `ImageFont.truetype` 一遍（磁盘 IO + 解析字体表）是纯浪费。

    `candidates` 缺省用 `FONT_CANDIDATES`（常规优先）；`bold=True` 用粗体清单。
    """
    from PIL import ImageFont
    key = (int(size), bool(bold), id(candidates) if candidates is not None else 0)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit
    if candidates is None:
        candidates = FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES
    got = None
    for p in candidates:
        try:
            got = ImageFont.truetype(p, key[0])
            break
        except Exception:                                          # noqa
            continue
    if got is None:
        try:
            got = ImageFont.load_default(size=key[0])
        except TypeError:                   # 老 Pillow 的 load_default 不收 size
            got = ImageFont.load_default()
    _font_cache[key] = got
    return got


# ------------------------------------------------------------------ 线路配置
def cfg(key, default=None):
    """读当前线路的 `route_def.CFG` —— **全技能唯一入口**。

    2026-10-22：原先有 4 套实现并存，行为还不完全一样：

      · `draw_osm._cfg_flag` / `make_terrain._route_cfg` / `prep_track._cfg_get`
        —— 都 import 一次 route_def 再 .get()，运行时现取；
      · `map_svg._CFG = getattr(RD, "CFG", {})` —— **import 期快照**，
        import 那一刻就把配置固化了。

    真正麻烦的不是重复，是**两套语义**：绝大多数脚本"每次调用现取"，
    唯独 map_svg 是"取一次存着"。凡是需要"改完 CFG 不重启进程就生效"的场景
    （同一个进程里连跑几步、或脚本内动态改配置后重算），map_svg 会静默拿旧值 ——
    症状是"改了配置没反应"，而且只在特定执行顺序下才出现。
    现在统一：一律运行时现取，读不到就给 default。

    `sys.path` 那一行不是多余的：`route_def.py` 在 scripts/ 下，而脚本经常被
    从**项目根**调用（`python scripts/xxx.py` 时 sys.path[0] 就是 scripts/，
    但别的方式未必）。原 `prep_track._cfg_get` 就是靠这一手才能在任何 cwd 下
    读到配置 —— 收敛时把这个能力一起带过来，别丢。
    """
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import route_def as RD
        v = (getattr(RD, "CFG", None) or {}).get(key)
    except Exception:                                              # noqa
        return default
    return default if v is None else v


def cfg_flag(key, default=False):
    """布尔开关版：取不到（None）回退 default，取到了就 `bool()` 之。

    沿用 `draw_osm._cfg_flag` 的**旧语义**（写 `1` / `"true"` 这类非严格布尔值
    的地方行为不变）—— 这次收敛的目的是消除重复实现，不是顺手改判定规则；
    判定规则要改应当单独提，否则"改了代码、行为也跟着悄悄变了"。
    """
    v = cfg(key, None)
    return default if v is None else bool(v)


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


def mercator_px(lon, lat, z, tile=256):
    """经纬度 → 全球 Web Mercator 像素。**全技能唯一权威实现。**

    2026-10-22：这个公式原先有 **6 处独立抄写**（make_terrain 里 3 处：
    `_window_px` 内联 + `lonlat_to_px` + 若干局部 proj；draw_osm、tiles、
    以及本文件各 1 处），一致性只靠一句注释「必须与 make_terrain.py 逐字一致」
    来维持 —— 也就是**靠人记着**。

    为什么值得动：SKILL.md 自己把「投影错位」列为最惨翻车（轨迹整体偏移、
    底图与矢量层对不上），而这类错误**不报错、只画错**，跑到最后才看得见。
    现在收敛到这一处，并由 `selftest.py` 断言所有入口输出逐位相等 ——
    以后谁再抄一份，自检会立刻抓出来。
    """
    n = tile * (2.0 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * n
    return x, y


def mercator_ll(x, y, z, tile=256):
    """全球 Web Mercator 像素 → 经纬度（`mercator_px` 的严格逆运算）。"""
    n = tile * (2.0 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def make_projector(z, tx0, ty0, box, scale, tile=256):
    """经纬度 → 输出图**局部**像素（权威实现）。

    ox/oy 是输出图左上角在全球像素坐标系里的位置（瓦片原点 + 盒内偏移）；
    减掉它、再乘 scale，得到图内坐标。底图栅格 / OSM 叠加 / 地图 SVG 三处
    **必须**用同一个这个函数 —— 各写一份就是静默错位的源头。
    """
    ox, oy = tx0 * tile + box[0], ty0 * tile + box[1]

    def proj(lon, lat):
        x, y = mercator_px(lon, lat, z, tile)
        return ((x - ox) * scale, (y - oy) * scale)

    return proj


def projector_of(meta):
    """底图像素 ← 经纬度（从 `base_meta.json` 读参数）。

    实现已委托 `make_projector`，不再自己抄一遍公式 —— 见 `mercator_px` 的说明。
    """
    return make_projector(meta["z"], meta["tx0"], meta["ty0"],
                          meta["box"], meta["scale"])


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


BBOX_PAD_DEFAULT = 0.35      # 四周留白（占轨迹跨度）—— 0.35 → 窗口 = 跨度 ×1.70
def resolve_bbox(pad_frac=None, min_pad=0.004):
    """地图窗口 = 轨迹包围盒 + 留白。返回 `(lon0, lat0, lon1, lat1, 来源说明)`。

    优先级：`route_def.CFG['bbox']`（显式覆盖，给"轨迹只走了一小段、但想多看周围
    地形"的场合用）> 轨迹自动推（留白取 `CFG['bbox_pad']`，默认 8%）。
    **默认走自动推，别写死坐标。**

    `CFG["bbox_pad"]`：用户说「地图面积再大一点 / 多留点周边地形」时要的旋钮。
    写 0.20 就是四周各外扩轨迹跨度的 20%（窗口面积约 1.4²，不是 20%）。
    **默认 0.35**（2026-10-24 由 0.08 上调，用户要求"后续路线统一放宽"）：
    窗口 = 轨迹跨度 ×1.70，四周多出整整一个山头的余量 —— 8% 时轨迹几乎贴着边框，
    放大看细节时周边什么都没有。单条线路仍可用 CFG["bbox_pad"] 盖掉。
    比手填 `CFG["bbox"]` 安全得多 —— 后者是死坐标，轨迹一改就漏山。

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
    pad = BBOX_PAD_DEFAULT if pad_frac is None else float(pad_frac)
    try:
        sys.path.insert(0, str(HERE))
        import route_def as RD
        # 丢掉 (0,0) 占位点：轨迹未就绪时 _p() 会退化成占位坐标，混进包围盒会毁掉窗口
        extra = [(d["lon"], d["lat"]) for d in pois_of(RD) if (d["lon"] or d["lat"])]
        cfgd = getattr(RD, "CFG", {}) or {}
        fixed = cfgd.get("bbox")
        if fixed:
            return (*[float(v) for v in fixed], "route_def.CFG['bbox']（显式覆盖）")
        if pad_frac is None and cfgd.get("bbox_pad") is not None:
            pad = float(cfgd["bbox_pad"])          # 用户要"地图大一点"时的手工留白
    except SystemExit:
        raise
    except Exception:                                              # noqa
        pass
    bb = track_bbox(extra)
    if bb is None:
        raise SystemExit("地图窗口推不出来：轨迹与 POI 都没有可用坐标。")
    x0, y0, x1, y1 = bb
    px = max((x1 - x0) * pad, min_pad)
    py = max((y1 - y0) * pad, min_pad)
    return (round(x0 - px, 6), round(y0 - py, 6), round(x1 + px, 6), round(y1 + py, 6),
            f"轨迹包围盒 + {pad:.0%} 留白（自动）")


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


# ------------------------------------------------------------------ POI 归一化
# route_def.POIS 有两种合理写法，都要能收 —— 只认一种的话，手写配置的人一踩就是
# `KeyError: 0` 或 `TypeError`，而且报错点在下游几百行外，很难回想到是 schema 问题。
#   元组（模板默认，紧凑）：(name, lon, lat, ele, kind, dx, dy, anchor)
#   字典（手写直观，字段自解释）：
#       {"name":"金顶","lon":103.336,"lat":29.519,"ele":3079,"kind":"peak_hi",
#        "dx":-28,"dy":-6,"anchor":"end"}
_POI_KEYS = ("name", "lon", "lat", "ele", "kind", "dx", "dy", "anchor")


def poi(p):
    """把一条 POI 归一化成 dict，兼容元组与字典。**返回的 dict 八个键一定都在**，
    缺的给安全默认值 —— 下游可以放心 `d["lon"]`，不必层层判断。"""
    if isinstance(p, dict):
        d = dict(p)
    else:
        d = {k: p[i] for i, k in enumerate(_POI_KEYS) if i < len(p)}
    d.setdefault("name", "?")
    d.setdefault("lon", None)
    d.setdefault("lat", None)
    d.setdefault("ele", None)
    d.setdefault("dx", 0)
    d.setdefault("dy", 0)
    d.setdefault("anchor", "start")
    d.setdefault("kind", "poi")
    if d.get("kind") is None:
        d["kind"] = "poi"
    return d


def pois_of(mod=None):
    """取 route_def.POIS 并全部归一化。mod 省略时自动 import route_def。"""
    if mod is None:
        sys.path.insert(0, str(HERE))
        import route_def as mod                                    # noqa: F811
    return [poi(p) for p in (getattr(mod, "POIS", []) or [])]


# ------------------------------------------------------------------ 文本宽度
# 这些标点在 CJK 字体里是**全宽**（1 em），但码位在 0x2E80 以下，按 0.62 em 算会偏窄。
# 2026-10-23 博格达实测：图例 "D2 冰湖—老虎嘴—转山达坂13" —— 两个破折号少算约 8.6 px，
# 右对齐的里程数字直接把描述尾巴顶上了。只补这几个常用的，不动拉丁字母的 0.62。
_FULLWIDTH_PUNCT = "—–‒·…‰℃℉‘’“”«»"


def text_w(txt, fs):
    """中英混排宽度估算：CJK 与全宽标点按 1 em，其余按 0.62 em。"""
    return sum(fs if (ord(ch) > 0x2E80 or ch in _FULLWIDTH_PUNCT) else fs * 0.62
               for ch in txt)


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


# ------------------------------------------------------------------ 无头浏览器截图
# 本机实测（2026-10-17）：Edge 升到新版后 `--headless=new --screenshot` **静默失效** ——
# 进程 rc=0 退出，但**一个字节都不产出**（`--dump-dom` 同样 0 字节）。而旧版 `--headless`
# 稳定出图。另外若用默认 profile，新进程会**移交给已在运行的 Edge 实例**并立刻返回，
# 同样不产出截图。因此统一走：
#   · 每个截图任务用**独立临时 user-data-dir**，避免移交给常驻实例；
#   · 截图落到文件 → 轮询等文件写出并尺寸稳定（兼容异步写图）；
#   · 模式按 `--headless=new` → `--headless` **依次降级**，哪个先出图就用哪个（跨机器/跨版本稳）。
_BROWSER_MODES = ("--headless=new", "--headless")


def _browser_profiles():
    """给每个截图任务分配独立临时 profile 目录（避免移交给常驻 Edge 实例）。"""
    import tempfile, shutil
    d = Path(tempfile.gettempdir()) / f"wbshot_{os.getpid()}_{int(time.time()*1000)%100000}"
    shutil.rmtree(d, ignore_errors=True)
    return d


def browser_shot(html_path, out_path, win_w, win_h, scale=1, timeout=900):
    """用无头浏览器把 HTML 截成 PNG（落 out_path），返回 Image 对象。

    html_path 必须是绝对路径（file:// 也行）；out_path 用绝对路径，否则某些环境下
    截图落盘位置会跑偏。结束后轮询文件直到尺寸稳定，规避"主进程已返回、子进程还没写完"。
    模式按 `--headless=new` → `--headless` 降级，规避新版 Edge 静默不出图。
    """
    BROWSER = find_browser()
    out_path = Path(out_path)
    prof = _browser_profiles()
    last_err = None
    for mode in _BROWSER_MODES:
        out_path.unlink(missing_ok=True)
        args = [BROWSER, mode, "--disable-gpu", "--hide-scrollbars",
                "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                f"--user-data-dir={prof}",
                f"--force-device-scale-factor={scale}",
                f"--window-size={win_w},{win_h}",
                f"--screenshot={out_path}", Path(html_path).as_uri()]
        try:
            subprocess.run(args, check=True, capture_output=True, timeout=timeout)
        except Exception as e:                 # noqa: BLE001
            last_err = e
            continue
        # 主进程提前返回、子进程异步写盘（本机实测：文件在完成后一次性出现，无渐进写入）。
        prev, stable = -1, 0
        for _ in range(600):                   # 最多等 90s
            if out_path.exists():
                sz = out_path.stat().st_size
                if sz == prev and sz > 0:
                    stable += 1
                    if stable >= 2:       # 2 次足够：本机实测文件是"完成后一次性出现"，
                                          # 旧值 4 次 = 每次截图白等 0.6 s，一轮要截 4 张
                        break
                else:
                    stable = 0
                prev = sz
            time.sleep(0.15)
        if out_path.exists() and out_path.stat().st_size > 0:
            return Image.open(out_path).convert("RGB")
    raise RuntimeError(f"浏览器截图未生成（试过 {_BROWSER_MODES}；最后一次异常：{last_err}）")


def _loopback_ok(port):
    """自检：本机是否允许 127.0.0.1 回环连接。本环境沙箱禁用监听套接字，会返回 False。"""
    import socket
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(1.0)
        cl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cl.settimeout(1.0)
        cl.connect(("127.0.0.1", port))
        conn, _ = srv.accept()
        cl.close()
        conn.close()
        srv.close()
        return True
    except Exception:
        return False


def content_box(im, tol=16, min_frac=0.02):
    """返回内容（非背景）包围盒 (x0, y0, x1, y1)。

    背景色取四角主色（内容一般不贴到四个角）。用"整行/整列有多少非背景像素"判定，
    比逐像素找边界更抗抗锯齿与零散文字。返回 None 表示量不到（页面背景与内容同色）。
    """
    a = np.asarray(im).astype(np.int16)
    h, w = a.shape[:2]
    corners = np.concatenate([a[0, :max(1, w // 20)],
                              a[h - 1, :max(1, w // 20)],
                              a[:max(1, h // 20), 0],
                              a[:max(1, h // 20), w - 1]])
    bg = np.median(corners, axis=0)
    diff = np.abs(a - bg).sum(axis=2)
    mask = diff > tol * 3
    rows = np.where(mask.sum(axis=1) > min_frac * w)[0]
    cols = np.where(mask.sum(axis=0) > min_frac * h)[0]
    if not rows.size or not cols.size:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


# ------------------------------------------------------------------ 浏览器内执行 JS 并取回结果
# 本机 Edge 的 --dump-dom 不输出，无法用"取回 data-* 属性"的老办法。改用：
#   注入的 JS 把结果 JSON 通过 new Image().src 信标发回本地 HTTP 服务（无需 websocket / CDP）。
#   js 末尾约定：把结果放进全局 `OUT`，本函数自动补一段信标发出。
def browser_eval(html_text, js, port=9333, win_w=1200, win_h=2200, timeout=60):
    """在无头浏览器里跑 js（结果须赋给全局 OUT），通过信标取回，返回解析后的 dict。

    注意：本机沙箱禁用回环监听（127.0.0.1 连不通），信标走不通；且本机 Edge 的
    --dump-dom 不输出。所以若回环不可用，直接返回 None（调用方应优雅跳过该项检查）。
    """
    import http.server
    import socket
    import threading
    import urllib.parse

    if not _loopback_ok(port):
        return None

    BROWSER = find_browser()
    box = {"data": None, "raw": None}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/d":
                d = urllib.parse.parse_qs(u.query).get("d", [""])[0]
                try:
                    box["data"] = json.loads(urllib.parse.unquote(d))
                except Exception:
                    box["raw"] = d
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), _H)
    thr = threading.Thread(target=srv.handle_request, daemon=True)
    thr.start()

    tmp = OUT / f"_audit_{port}.html"
    beacon = (f"\ntry{{new Image().src='http://127.0.0.1:{port}/d?d='+"
              f"encodeURIComponent(JSON.stringify(OUT));}}catch(e){{}}")
    src = html_text
    if "</body>" in src:
        src = src.replace("</body>", f"<script>{js}{beacon}</script></body>", 1)
    else:
        src = src + f"<script>{js}{beacon}</script>"
    tmp.write_text(src, encoding="utf-8")
    prof = _browser_profiles()
    proc = None
    try:
        # 不用 --dump-dom / --screenshot：让浏览器驻留，等信标请求真正到达本地服务后再结束。
        # ⚠ 新版 Edge 的 `--headless=new` 可能静默不出图/不执行，故按模式降级重试。
        for mode in _BROWSER_MODES:
            proc = subprocess.Popen([BROWSER, mode, "--disable-gpu", "--hide-scrollbars",
                                    "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                                    f"--user-data-dir={prof}",
                                    f"--force-device-scale-factor=1",
                                    f"--window-size={win_w},{win_h}", tmp.as_uri()],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            thr.join(timeout=timeout)          # 等本地服务收到信标
            if proc.poll() is None:
                proc.terminate()              # 浏览器驻留 → 主动结束
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
            if box["data"] is not None or box["raw"] is not None:
                break
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        tmp.unlink(missing_ok=True)
    return box["data"] if box["data"] is not None else box["raw"]

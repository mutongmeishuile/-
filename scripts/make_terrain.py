# -*- coding: utf-8 -*-
"""本地渲染地形底图 —— 开放 DEM（AWS Terrain Tiles / SRTM）→ 晕渲 + 等高线。

不依赖任何瓦片服务，可无限次重出，无速率限制、无版权问题。

数据源（均为 AWS Open Data，免钥匙免注册）：
  * Terrarium 编码瓦片（推荐，Web Mercator 网格与底图完全一致）
      https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
      高程解码： elev = R*256 + G + B/256 - 32768     （单位：米）
  * SRTM 1″ HGT 分块（原始 30 m 栅格，非 Web Mercator，需自己重投影）
      https://s3.amazonaws.com/elevation-tiles-prod/skadi/N30/N30E117.hgt.gz
  * Copernicus GLO-30（最新、精度最好，GeoTIFF，单文件 ~48 MB/1°×1°）
      https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N30_00_E117_00_DEM/...tif

渲染管线：
  下载 DEM 瓦片 → 拼接 → 裁剪到 bbox → (a) Horn 晕渲  (b) marching-squares 等高线
  → 分层设色(hypsometric tint) × 晕渲 → 叠等高线 → 输出底图 JPEG + base_meta.json

输出 `out/base_map.jpg` + `out/base_meta.json`，投影与窗口信息全部落在 meta 里，
下游（map_svg / render_map_hi）一律从 meta 读，不各自硬编码 z 或 bbox。
"""
import io, json, math, queue, sys, threading, time, urllib.request
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TILE = 256
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import guide_common as GC                                           # noqa: E402

# DEM 瓦片缓存 —— **用户级、跨项目共享**（`~/.workbuddy/cache/hiking-dem/`）。
# 瓦片按 z/x/y 命名，本身就是地球上的唯一格子，所以缓存没有理由放在项目里。
# 旧版放 `scripts/demcache/`：同一片山区做第二条线路时一块都复用不上，
# 又得重下几万块瓦片（冷缓存 100–200 s vs 热缓存 6 s，差别全在这）。
# 换盘/多机用 HIKING_DEM_CACHE 环境变量覆盖。
CACHE = GC.dem_cache_dir()

TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
UA = "hiking-route-guide/1.0 (local offline terrain render)"

# ---- 目标窗口 / 出图参数 ----
# 窗口**不再写死**：由 guide_common.resolve_bbox() 从轨迹包围盒自动推（+ 留白），
# 或由 route_def.CFG['bbox'] 显式覆盖。fetch_osm.py 用同一个函数取同一个窗口，
# 两处从此不可能不一致 —— 旧版各写一份常量，改了这头忘了那头就会整体错位。
# DEM 瓦片级别必须与底图投影一致（map_svg.py 的 projector 用 meta["z"]），
# 否则轨迹与底图会整体错位。terrarium 的最高级别就是 z15（≈4.1 m/px @30.5°）。
DEM_Z = 15
# ---- 超采样（放大不糊的关键）----
# 逻辑宽 = 版式宽（SVG viewBox 用），栅格按 LOGICAL_W × SS 出图。
# SVG 里 <image width="1504"> 会把 3008 的 jpeg 缩回逻辑宽渲染 —— 这等于给浏览器做了 2× 超采样：
#   ① 常规尺寸下等于 4 倍样本降采样 → 线更锐、字更实；
#   ② 放大到 200% 时仍有真实像素，不会立刻糊成一团。
# 代价：底图 jpg 体积约 ×2.5，HTML 变大，但这是"放大能看清"唯一实际有效的做法。
LOGICAL_W = 1504
SS = 2
RASTER_W = LOGICAL_W * SS

CONTOUR_MAJOR = 100.0   # 计曲线间隔（加粗）
CONTOUR_MINOR = 20.0    # 首曲线间隔
AZ, ALT = 315.0, 45.0   # 晕渲光源：西北方向、高度角 45°（制图惯例）
Z_FACTOR = 1.4          # 垂直夸张，让低山也有立体感
UNSHARP = (3, 45, 3)    # USM(半径, 强度%, 阈值)：给晕渲"提锐"，补偿 30 m DEM 的天然柔化
# JPEG 参数：地图全是**彩色细线 + 小字**，必须关掉默认的 4:2:0 色度抽样（subsampling=0 → 4:4:4）。
# 4:2:0 会把色度通道砍到 1/4 分辨率，彩色线条一律发虚发彩边 —— 这是"放大看不清"的隐形主因之一。
JPG = dict(quality=86, optimize=True, progressive=True, subsampling=0)
# 等高线线宽（按栅格像素）：3008 分辨率下的 (首,计)。计曲线是首曲线的 2 倍宽 + 更深色。
# 注意：这里刻意画细 —— 20 m 间距在陡坡会密集成排，线一粗就糊成"棕色泥"，那才是"看不清"的主因。
CONTOUR_DILATE = (0, 1)
# 等高线配色 + alpha 融合：硬像素覆盖会留下高反差硬边，alpha 写回才像"印在纸上"
C_MINOR, C_MAJOR = (172, 155, 136), (139, 111, 84)
A_MINOR, A_MAJOR = 0.55, 0.86
# 缓坡"起云"：垂直夸张会把 30 m DEM 的微起伏一起放大，按真实坡度加权渐回平地基准
FLAT_ANGLE = 2.5
HS_SMOOTH = 3           # 明暗柔化半径（DEM 像素）——柔和明暗 + 锐利线划 = 纸质地形图质感
VOID_MAD = 900.0        # 「远低于局部参考面」多少米才算空洞（见 fill_voids 的教训）

# ---- 等高线高程标注 ----
# 只有计曲线（每 100 m）标数字，首曲线标了就成"数字墙"。
# 所有尺寸都是**逻辑像素**，内部乘 SS 变栅格像素（超采样下同样锐利）。
LABEL_EVERY = 100.0      # 标注哪一级等高线（= 计曲线间隔）
LABEL_GAP = 560          # 同一条等高线上相邻标注的最小弧长间隔
LABEL_SEP = 130          # 任意两个标注之间的最小间距（防聚成堆）
LABEL_MAX_ANGLE = 62     # 局部倾角超过此值就不标（斜着读不出来的不如不标）
LABEL_WINDOW = 26        # 估计局部走向的采样窗口（±窗口长度）
LABEL_FONT = 11          # 字号（逻辑像素）
LABEL_FILL = (105, 79, 55)
LABEL_HALO = (252, 250, 245)
MS_STEP = 1              # marching squares 用的 DEM 抽稀步长（1 = 全分辨率，与栅格线严格同位）

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def _label_font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:                                          # noqa
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ 投影
def lonlat_to_px(lon, lat, z):
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def px_to_lonlat(x, y, z):
    n = TILE * 2 ** z
    return x / n * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


# ------------------------------------------------------------------ 下载
def _get(url, fp, timeout=40, retries=3):
    if fp.exists() and fp.stat().st_size > 500:
        return fp.read_bytes()
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = r.read()
            if d and r.status == 200:
                fp.write_bytes(d)
                return d
        except Exception as e:                     # noqa
            if a == retries - 1:
                print("   FAIL", url.split("/")[-1], repr(e)[:60])
        time.sleep(0.5 * (a + 1))
    return None


def load_dem(z, lon0, lat0, lon1, lat1, workers=24):
    """下载 Terrarium 瓦片 → 拼接 → 裁剪到 bbox。返回 (dem[H,W] 米, meta 片段)。"""
    x0f, y0f = lonlat_to_px(lon0, lat1, z)
    x1f, y1f = lonlat_to_px(lon1, lat0, z)
    tx0, ty0 = int(x0f // TILE), int(y0f // TILE)
    tx1, ty1 = int(x1f // TILE), int(y1f // TILE)
    W, H = (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE
    dem = np.full((H, W), np.nan, np.float32)

    jobs, q = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)], queue.Queue()
    for j in jobs:
        q.put(j)
    got, lock, done = {}, threading.Lock(), [0]

    def work():
        while True:
            try:
                x, y = q.get_nowait()
            except queue.Empty:
                return
            d = _get(TERRARIUM.format(z=z, x=x, y=y), CACHE / f"terr_{z}_{x}_{y}.png")
            with lock:
                got[(x, y)] = d
                done[0] += 1
                if done[0] % 25 == 0:
                    print(f"   dem tiles {done[0]}/{len(jobs)}")

    ths = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    [t.start() for t in ths]
    [t.join() for t in ths]

    miss = 0
    for (x, y), d in got.items():
        if not d:
            miss += 1
            continue
        a = np.asarray(Image.open(io.BytesIO(d)).convert("RGB"), dtype=np.float32)
        e = a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0
        dem[(y - ty0) * TILE:(y - ty0 + 1) * TILE, (x - tx0) * TILE:(x - tx0 + 1) * TILE] = e
    if miss:
        print(f"   WARN 缺失 {miss}/{len(jobs)} 块 DEM 瓦片")
    # 补洞
    if np.isnan(dem).any():
        m = np.isnan(dem)
        dem[m] = np.nanmedian(dem)

    cx0, cy0 = int(round(x0f - tx0 * TILE)), int(round(y0f - ty0 * TILE))
    cx1, cy1 = int(round(x1f - tx0 * TILE)), int(round(y1f - ty0 * TILE))
    sub = dem[cy0:cy1, cx0:cx1]
    print(f"   DEM {sub.shape[1]}x{sub.shape[0]} px, {miss and 'part ' or ''}"
          f"高程 {np.nanmin(sub):.0f}–{np.nanmax(sub):.0f} m, 瓦片 {len(jobs)} 块")
    return sub, {"z": z, "tx0": tx0, "ty0": ty0, "box": [cx0, cy0, cx1, cy1]}


# ------------------------------------------------------------------ 晕渲
def hillshade(dem, z, lat_c, az=AZ, alt=ALT, zf=Z_FACTOR):
    """GDAL Horn 算法。返回 0–1 的明暗值。

    两道后处理（缺一个整张图就会"发脏 / 起云"）：
      * 按**真实坡度**加权：坡度 <FLAT_ANGLE 的缓坡把明暗渐回平地基准，
        否则垂直夸张放大的微起伏会在缓坡上结成灰绿"云斑"与横向细条纹；
      * 明暗柔化 HS_SMOOTH 个 DEM 像素：柔和明暗 + 锐利线划才是纸质地形图的质感。
        PIL 的 GaussianBlur 不支持 "F" 模式，用「降采样 BOX → 升采样 BICUBIC」等效实现。
    """
    res = 156543.03392 * math.cos(math.radians(lat_c)) / (2 ** z)   # m/px（Web Mercator）
    p = np.pad(dem, 1, mode="edge")
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * res)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * res)
    tan_slope = np.hypot(dzdx, dzdy)
    slope = np.arctan(tan_slope * zf)
    aspect = np.arctan2(dzdy, -dzdx)
    azr, altr = math.radians(az), math.radians(alt)
    hs = np.sin(altr) * np.cos(slope) + np.cos(altr) * np.sin(slope) * np.cos(azr - aspect)
    hs = np.clip(hs, 0.0, 1.0)
    w = np.clip(np.arctan(tan_slope) / math.radians(FLAT_ANGLE), 0.0, 1.0)
    hs = hs * w + math.sin(math.radians(ALT)) * (1.0 - w)
    if HS_SMOOTH > 1:
        im = Image.fromarray(hs.astype(np.float32), "F")
        k = max(1, HS_SMOOTH)
        hs = np.asarray(im.resize((max(1, im.width // k), max(1, im.height // k)), Image.BOX)
                        .resize(im.size, Image.BICUBIC), np.float32)
    return np.clip(hs, 0.0, 1.0)


def _jacobi_fill(field, iters):
    """NaN 区域按 4 邻域均值迭代填充（调和插值）。迭代次数够多时得到的是
    平滑的"穹顶"而不是平板 —— 这正是冰川空洞该有的形态。"""
    for _ in range(iters):
        nan = np.isnan(field)
        if not nan.any():
            break
        s = np.zeros_like(field)
        c = np.zeros_like(field)
        for sh in (np.roll(field, 1, 0), np.roll(field, -1, 0),
                   np.roll(field, 1, 1), np.roll(field, -1, 1)):
            v = ~np.isnan(sh)
            s += np.where(v, sh, 0.0)
            c += v.astype(np.float32)
        field = np.where(nan, np.where(c > 0, s / np.maximum(c, 1.0), np.nan), field)
    return field


def _fill_masked(field, valid, iters):
    """只在 valid=False 的位置做 4 邻域均值迭代（保留已有效像元）。
    用于把粗网格填出来的接缝抹平。"""
    for _ in range(iters):
        inv = ~valid
        if not inv.any():
            break
        s = np.zeros_like(field)
        c = np.zeros_like(field)
        for sh in (np.roll(field, 1, 0), np.roll(field, -1, 0),
                   np.roll(field, 1, 1), np.roll(field, -1, 1)):
            s += sh
            c += 1.0
        field = np.where(inv, s / c, field)
    return field


def fill_voids(dem):
    """填 DEM 空洞。

    ⚠⚠ 血泪教训一（2026-10-09 亚丁大转山，差点交付）：**判据必须是"单边偏低"**。
    原代码写的是 `abs(dem - ref) > VOID_MAD` —— 于是"比局部参考面高出 400 m 的
    **真实峰顶**"也被当成异常值填掉。本线实测：被判为空洞的 58293 px 里有 57882 px
    都在 4000 m 以上、最高 5934 m（正是仙乃日/央诺多吉/央迈勇的峰顶！），
    填完之后全域最高点从 5935 m 掉到 5661 m，**三座 5000+ 山峰被削成平滑穹顶**，
    在图上表现为"山顶一块没有等高线、边缘生硬的平板"（用户一眼看出来的就是这个）。
    真实空洞（terrarium 缺数据）只会**偏低**、不会偏高 → 判据改成单边：
        (ref - dem) > VOID_MAD  才有可能是空洞
    另加一条绝对下限兜底（远低于全图中位数），防住那种整片为 0 / 负值的坏瓦片。

    ⚠ 血泪教训二：**大空洞别在全分辨率上迭代**。冰川区空洞可达 2–5 万 px，
    4 邻域 Jacobi 每轮只扩散 1 px，600 轮 ≈ 25 px 半径，空洞内部喂不到，
    最后落到兜底中位数上 → 又是一块平板。改用分尺度（1/4 分辨率迭代 + 全分辨率抹缝）。

    实测提醒：terrarium（SRTM+Copernicus 融合，AWS）在本线**其实没有空洞**
    （单边偏低异常 = 0 px），所以正确行为是"一个都不填"。别的底图源才可能需要。
    """
    p1, p99 = np.nanpercentile(dem, [1.0, 99.0])
    med = float(np.nanmedian(dem))
    im = Image.fromarray(np.clip(dem, p1, p99).astype(np.float32), "F")
    k = 6
    ref = np.asarray(im.resize((max(1, im.width // k), max(1, im.height // k)), Image.BOX)
                     .resize(im.size, Image.BICUBIC), np.float32)
    bad = (ref - dem) > VOID_MAD                  # 单边：只认"远低于参考面"
    bad |= ~np.isfinite(dem) | (dem < med - 2500)  # 坏瓦片兜底
    if not bad.any():
        print(f"   空洞判据：仅认「低于局部参考面 > {VOID_MAD:.0f} m」"
              f"（p1={p1:.0f} p99={p99:.0f}）→ 无空洞，跳过填充")
        return dem
    print(f"   空洞判据：仅认「低于局部参考面 > {VOID_MAD:.0f} m」"
          f"（p1={p1:.0f} p99={p99:.0f}）")
    ys, xs = np.nonzero(bad)
    m = 300
    y0, y1 = max(0, ys.min() - m), min(dem.shape[0], ys.max() + m + 1)
    x0, x1 = max(0, xs.min() - m), min(dem.shape[1], xs.max() + m + 1)
    sub = dem[y0:y1, x0:x1].astype(np.float32)
    valid = ~bad[y0:y1, x0:x1] & np.isfinite(sub)

    # ① 1/4 分辨率上的调和插值（小图迭代 900 轮 ≈ 全分辨率 3600 px 扩散半径）
    f = 4
    H, W = sub.shape
    h2, w2 = max(1, H // f), max(1, W // f)
    sv = valid[:h2 * f, :w2 * f].reshape(h2, f, w2, f)
    sa = np.where(valid[:h2 * f, :w2 * f], sub[:h2 * f, :w2 * f], 0.0).reshape(h2, f, w2, f)
    cnt = sv.sum(axis=(1, 3))
    coarse = np.where(cnt > 0, sa.sum(axis=(1, 3)) / np.maximum(cnt, 1), np.nan).astype(np.float32)
    coarse = _jacobi_fill(coarse, iters=900)
    coarse = np.where(np.isnan(coarse), med, coarse)

    # ② 上采样回全分辨率（BICUBIC → 平滑，无块状阶梯）
    big = np.asarray(Image.fromarray(coarse.astype(np.float32), "F")
                     .resize((W, H), Image.BICUBIC), np.float32)
    filled = np.where(valid, sub, big)

    # ③ 全分辨率少数轮迭代：抹掉粗网格接缝，让边界与真实地形连续
    filled = _fill_masked(filled, valid, iters=120)

    out = dem.copy()
    out[y0:y1, x0:x1] = filled
    print(f"   填补 SRTM 空洞 {int(bad.sum())} px（占 {100*bad.mean():.2f}%；"
          f"1/{f} 分辨率调和插值 + 全分辨率抹缝，中位 {med:.0f} m 基准）")
    return out


# ------------------------------------------------------------------ 分层设色
# ⚠ 血泪教训：这里的色带**不能写死高程档位**（曾照搬党岭 3200–5400 m 的档位到
#   峨眉山 470–3085 m，hypso() 把整个 DEM np.clip 到最低一档 —— 全图变成一片单色，
#   只剩晕渲在撑立体感，"分层设色"名存实亡。而且**不报错、不告警**，极难察觉。
# → 改成通用色带，按**本区实际高程分位**拉伸铺满，换地区自动适配。
#   全程压低饱和度，把"跳出来"的资格留给轨迹与注记。
#   另：色带顶端是否给"雪色"要看本区有没有雪线 —— 亚热带山体（峨眉山 470–3085 m）
#   并没有常年积雪，涂成冷白是误导，故分两套。
_RAMP_SNOW = [(0.00, (148, 170, 138)),   # 谷底灰绿
              (0.16, (176, 192, 148)),   # 低山林
              (0.34, (203, 205, 163)),   # 中山草甸
              (0.52, (222, 208, 172)),   # 亚高山草甸
              (0.70, (226, 196, 166)),   # 流石滩
              (0.84, (212, 178, 160)),   # 裸岩陶土
              (0.93, (214, 204, 202)),   # 近雪线
              (1.00, (232, 236, 244))]   # 雪线冷白
# 无雪线路（峨眉山 470–3085 m 实测）：色带顶端停在裸岩陶土，且**绿段刻意拉长**。
# 依据：本线 46.8 km 里 40 km 都在 700–2100 m 的森林带，若按线性色带铺，
#   海拔刚过 1500 m 就转土黄 —— 全图主色会变成棕黄，既不符合"这座山是绿的"，
#   也会重犯本项目早期"底图发土黄"的老问题。→ 让 0–0.55 都留在绿系，
#   只有接近山顶（约 2300 m 以上）的裸岩带才转陶土。
_RAMP_ALPINE = [(0.00, (150, 172, 140)),   # 谷底灰绿
                (0.22, (170, 188, 145)),   # 低山林
                (0.45, (186, 196, 152)),   # 中山林（仍偏绿）
                (0.62, (203, 202, 160)),   # 中山草甸·微黄
                (0.78, (218, 199, 164)),   # 亚高山草甸
                (0.90, (216, 186, 162)),   # 流石滩
                (1.00, (210, 176, 158))]   # 山顶裸岩陶土
SNOWLINE = 4300.0       # 本区最高点达到此高程，色带才启用雪色端
RAMP_MIN_SPAN = 150.0   # 全域高差小于此值（平地/丘陵线路）就按中心值 ±75 m 展开，避免拉出假色阶


# 雪色端从色带的这个位置起算，之后的色阶单独拉伸到「全域最高点」。
TOP_F = 0.93
# 色带拉伸的顶部分位。⚠ 不能取 p98：本线 p98=5263 m 而最高峰 5935 m，
#   p98 以上 2% 的像元（三座 5000+ 雪山的峰顶）全被 np.clip 压成一个纯白色块，
#   表现就是"山顶一块没有等高线、边缘生硬的平板"（2026-10-09 实测踩坑）。
#   取 p99.7：被截的只剩 0.3%，再配合 TOP_F 之上的"拉伸到最高点"，
#   峰顶就是连续渐变的雪帽，而不是一块平白。
RAMP_TOP_PCT = 99.7


def ramp_stops(dem, verbose=True):
    """按本区 DEM 的 p2–p99.7 把通用色带拉伸成实际色阶表；
    雪色端（TOP_F 以上）再单独拉伸到全域最高点，避免峰顶被截成平顶。"""
    lo, hi = (float(v) for v in np.nanpercentile(dem, (2, RAMP_TOP_PCT)))
    hi = max(hi, float(np.nanpercentile(dem, 98)))     # 兜底：至少不低于 p98
    dmax = float(np.nanmax(dem))
    if hi - lo < RAMP_MIN_SPAN:
        c = (lo + hi) / 2.0
        lo, hi = c - RAMP_MIN_SPAN / 2.0, c + RAMP_MIN_SPAN / 2.0
    ramp = _RAMP_SNOW if hi >= SNOWLINE else _RAMP_ALPINE
    kind = "含雪色端" if ramp is _RAMP_SNOW else "无雪·顶端=裸岩陶土"
    stops = []
    for f, col in ramp:
        if f <= TOP_F or dmax <= hi:
            stops.append((lo + f * (hi - lo), col))
        else:                                          # 雪色端拉伸到最高点
            stops.append((hi + (f - TOP_F) / (1.0 - TOP_F) * (dmax - hi), col))
    if verbose:
        n_clip = int((np.nan_to_num(dem, nan=-1e9) > stops[-1][0]).sum())
        tail = (f"；雪色端 {hi:.0f}→{dmax:.0f} m 单独拉伸"
                if dmax > hi else "")
        print(f"   分层设色 色带区间 {lo:.0f}–{hi:.0f} m"
              f"（通用 ramp × 本区 p2–p{RAMP_TOP_PCT:g}，{kind}{tail}）"
              f"{f'，仍有 {n_clip} px 顶格' if n_clip else ''}")
    return stops


def hypso(dem):
    """高程 → RGB 分层设色（色阶按本区高程自适应，见 _RAMP 的说明）。"""
    stops_tbl = ramp_stops(dem)
    stops = np.array([s[0] for s in stops_tbl], float)
    cols = np.array([s[1] for s in stops_tbl], float)
    d = np.clip(dem, stops[0], stops[-1])
    out = np.zeros(dem.shape + (3,), np.float32)
    for ch in range(3):
        out[..., ch] = np.interp(d, stops, cols[:, ch])
    return out


# 晕渲调制：环境光比例越高整体越亮、对比越弱。
# 太高（>0.75）地形会"平"到看不见，太低（<0.4）背光面发黑、压掉等高线。
AMBIENT, DIRECTIONAL = 0.70, 0.38
HL_CLIP, HL_KEEP = 240.0, 0.35   # 高光软限幅：超 240 只保留 35%，亮而不死白


# ------------------------------------------------------------------ 等高线
def _band_edges(band):
    """栅格化等值线：相邻像素落在不同高程带 → 该处就是一条等高线穿过。"""
    e = np.zeros(band.shape, bool)
    e[1:, :] |= band[1:, :] != band[:-1, :]
    e[:, 1:] |= band[:, 1:] != band[:, :-1]
    return e


def _dilate(m):
    """十字膨胀一格，让计曲线更粗。"""
    o = m.copy()
    o[1:, :] |= m[:-1, :]
    o[:-1, :] |= m[1:, :]
    o[:, 1:] |= m[:, :-1]
    o[:, :-1] |= m[:, 1:]
    return o


def _dilate_n(m, n):
    """十字膨胀 n 次：线宽从 1 px 变为 (1+2n) px。n=0 即 1 px 细线。"""
    for _ in range(int(n)):
        m = _dilate(m)
    return m


def paint_contours(rgb, dem_out, w_minor=0, w_major=2,
                   minor=CONTOUR_MINOR, major=CONTOUR_MAJOR):
    """在输出分辨率上栅格化等高线并写入 RGB 数组。

    原理：band = floor(elev / 间隔)，相邻像素 band 不同即该处有一条等高线。
    全程 numpy 向量化（10^7 像素毫秒级），**不需要 matplotlib / GDAL / skimage**。
    计曲线是首曲线的子集（100 = 5×20），画在上层覆盖。
    """
    b_minor = np.floor(dem_out / minor).astype(np.int32)
    b_major = np.floor(dem_out / major).astype(np.int32)
    m_minor = _dilate_n(_band_edges(b_minor), w_minor)
    m_major = _dilate_n(_band_edges(b_major), w_major)
    # alpha 融合写回（rgb 是 uint8，必须过 float，否则整数回绕会把线画成黑块）
    f = rgb.astype(np.float32)
    for mask, col, a in ((m_minor, C_MINOR, A_MINOR), (m_major, C_MAJOR, A_MAJOR)):
        f[mask] = f[mask] * (1 - a) + np.asarray(col, np.float32) * a
    np.copyto(rgb, np.clip(f, 0, 255).astype(np.uint8))
    n_lv = int((dem_out.max() - dem_out.min()) // minor)
    print(f"   等高线 每 {minor:.0f} m（计曲线每 {major:.0f} m），约 {n_lv} 级，"
          f"线覆盖 {m_minor.mean()*100:.1f}% 像素")
    return rgb


# ------------------------------------------------------------------ 等高线高程标注
# 栅格线是"画出来的"，但要在线上放数字就必须知道**线的走向与顺序**，所以这里再走一遍
# marching squares 取矢量折线（只在活跃格上做循环，13 个 level 秒级）。
# 32 种角点组合里 0/15 无线段，其余查表；5/10 是鞍点歧义，按惯例拆成两段不相连。
_TABLE = {
    1: ((0, 3),), 2: ((0, 1),), 3: ((3, 1),), 4: ((1, 2),),
    5: ((0, 3), (1, 2)), 6: ((0, 2),), 7: ((3, 2),), 8: ((2, 3),),
    9: ((0, 2),), 10: ((0, 1), (2, 3)), 11: ((1, 2),), 12: ((1, 3),),
    13: ((0, 1),), 14: ((0, 3),),
}


def _march(dem, level, step=MS_STEP):
    """marching squares：返回线段列表 [(p0, p1), ...]，坐标为 DEM 像素 (col, row)。"""
    z = (dem[::step, ::step] if step > 1 else dem).astype(np.float32) - np.float32(level)
    v0, v1 = z[:-1, :-1], z[:-1, 1:]
    v2, v3 = z[1:, 1:], z[1:, :-1]
    code = ((v0 >= 0).astype(np.uint8)
            | ((v1 >= 0).astype(np.uint8) << 1)
            | ((v2 >= 0).astype(np.uint8) << 2)
            | ((v3 >= 0).astype(np.uint8) << 3))
    rows, cols = np.nonzero((code > 0) & (code < 15))
    if rows.size == 0:
        return []
    s0, s1 = v0[rows, cols], v1[rows, cols]
    s2, s3 = v2[rows, cols], v3[rows, cols]
    r = rows.astype(np.float32)
    c = cols.astype(np.float32)

    def lerp(va, vb, pa, pb):
        d = va - vb
        safe = np.where(np.abs(d) < 1e-9, 1.0, d)
        t = np.where(np.abs(d) < 1e-9, 0.5, va / safe)
        return pa + (pb - pa) * t

    ep = [np.stack([lerp(s0, s1, c, c + 1), r], 1),            # 0 上边
          np.stack([c + 1, lerp(s1, s2, r, r + 1)], 1),        # 1 右边
          np.stack([lerp(s3, s2, c, c + 1), r + 1], 1),        # 2 下边
          np.stack([c, lerp(s0, s3, r, r + 1)], 1)]            # 3 左边
    code_a = code[rows, cols]
    out = []
    for k, pairs in _TABLE.items():
        sel = np.nonzero(code_a == k)[0]
        if sel.size == 0:
            continue
        for a, b in pairs:
            pa, pb = ep[a][sel], ep[b][sel]
            out.extend(zip(pa.tolist(), pb.tolist()))
    return out


def _join(segs, q=0.05):
    """把零散线段按端点接成折线（端点在 q px 内视为同一顶点）。"""
    if not segs:
        return []
    inv = 1.0 / q

    def key(p):
        return (int(round(p[0] * inv)), int(round(p[1] * inv)))

    ends = {}
    for i, (a, b) in enumerate(segs):
        ends.setdefault(key(a), []).append((i, 0))
        ends.setdefault(key(b), []).append((i, 1))
    used = [False] * len(segs)
    chains = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        fwd = [segs[i][0], segs[i][1]]

        def extend(from_tail, lst, push):
            while True:
                k = key(lst[-1] if from_tail else lst[0])
                nxt = None
                for j, e in ends.get(k, ()):
                    if not used[j]:
                        nxt = (j, e)
                        break
                if nxt is None:
                    return
                j, e = nxt
                used[j] = True
                push(segs[j][1] if e == 0 else segs[j][0])

        extend(True, fwd, fwd.append)
        back = []
        extend(False, fwd, lambda p: back.append(p))
        chains.append(back[::-1] + fwd)
    return chains


def _rdp(pts, eps):
    """Douglas-Peucker（迭代栈，避免递归爆栈）。pts 为 (N,2) 数组。"""
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        p0 = pts[i0]
        dx, dy = pts[i1] - p0
        L = math.hypot(dx, dy)
        seg = pts[i0 + 1:i1]
        if L < 1e-9:
            dist = np.hypot(seg[:, 0] - p0[0], seg[:, 1] - p0[1])
        else:
            dist = np.abs(dx * (seg[:, 1] - p0[1]) - dy * (seg[:, 0] - p0[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > eps:
            m = i0 + 1 + k
            keep[m] = True
            stack.append((i0, m))
            stack.append((m, i1))
    return pts[keep]


def _text_size(font, txt, stroke):
    try:
        bb = font.getbbox(txt, stroke_width=stroke)
    except TypeError:                                              # 位图兜底字体
        bb = (0, 0) + font.getsize(txt)
    return bb[2] - bb[0], bb[3] - bb[1], bb


def draw_contour_labels(pil, dem, scale, lo, hi, avoid=None, verbose=True):
    """在计曲线上断线标注高程数字。

    * 只在**计曲线**（每 LABEL_EVERY m）上标；
    * 沿折线按弧长取候选位，但要求局部接近水平（LABEL_MAX_ANGLE），不然斜着读不出；
    * 与实测轨迹/攻略 POI 冲突的位置直接跳过（`avoid`），免得数字被粗线路切断；
    * 数字用"纸色描边"压掉底下的线 —— 这是纸质地形图的标准断线画法，比加白底方块干净。

    scale: (sx, sy) = DEM 像素 → 栅格像素；avoid: [(x, y, r)] 栅格像素。
    返回：已占位的网格单元集合（cell = 14*SS），供 OSM 注记继续避让。
    """
    sx, sy = scale
    S = SS
    f = _label_font(max(9, int(round(LABEL_FONT * S))))
    # 白描边收细：2.0*S（≈字号 18%）在浅色地形上会糊成"白阴影"（用户反馈），
    # 收到 1.7*S ≈ 14%，读起来是干净的描边；再细就会在深色晕渲上失去可读性。
    stroke = max(2, int(round(1.7 * S)))
    gap, sep, win = LABEL_GAP * S, LABEL_SEP * S, LABEL_WINDOW * S
    W, H = pil.size

    # 避让栅格（cell 与 draw_osm.Labeller 保持一致）
    cell = 14.0 * S
    avoid_cells = set()
    for x, y, r in (avoid or ()):
        for cx in range(int((x - r) // cell), int((x + r) // cell) + 1):
            for cy in range(int((y - r) // cell), int((y + r) // cell) + 1):
                avoid_cells.add((cx, cy))

    def blocked(x0, y0, x1, y1):
        for cx in range(int(x0 // cell), int(x1 // cell) + 1):
            for cy in range(int(y0 // cell), int(y1 // cell) + 1):
                if (cx, cy) in avoid_cells:
                    return True
        return False

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    placed_boxes = []
    grid = set()
    n_lab = n_line = 0
    errs = []

    lv0 = math.ceil(lo / LABEL_EVERY) * LABEL_EVERY
    lv1 = math.floor(hi / LABEL_EVERY) * LABEL_EVERY
    for lv in np.arange(lv0, lv1 + 1e-6, LABEL_EVERY):
        lv = float(lv)
        txt = f"{int(round(lv))}"
        tw, th, _ = _text_size(f, txt, stroke)
        for ch in _join(_march(dem, lv)):
            P = _rdp(np.asarray(ch, np.float32), 0.8 * MS_STEP)
            if len(P) < 2:
                continue
            P = np.stack([P[:, 0] * sx, P[:, 1] * sy], 1)
            dseg = np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1]))
            cum = np.concatenate([[0.0], np.cumsum(dseg)])
            total = float(cum[-1])
            if total < 2 * win + tw + 30:
                continue
            n_line += 1
            s = gap * 0.5
            while s < total - win * 0.6:
                i0 = int(np.searchsorted(cum, max(0.0, s - win)))
                i1 = int(np.searchsorted(cum, min(total, s + win)))
                if i1 - i0 < 2:
                    break
                dx = P[i1, 0] - P[i0, 0]
                dy = P[i1, 1] - P[i0, 1]
                if dx < 0:
                    dx, dy = -dx, -dy
                ang = math.degrees(math.atan2(dy, dx))
                if abs(ang) > LABEL_MAX_ANGLE:
                    s += gap * 0.35
                    continue
                j = min(max(int(np.searchsorted(cum, s)), 1), len(P) - 1)
                t = (s - cum[j - 1]) / max(1e-6, cum[j] - cum[j - 1])
                x = P[j - 1, 0] + (P[j, 0] - P[j - 1, 0]) * t
                y = P[j - 1, 1] + (P[j, 1] - P[j - 1, 1]) * t
                hw, hh = tw * 0.55 + stroke + 2, th * 0.55 + stroke + 2
                if x - hw < 2 or x + hw > W - 2 or y - hh < 2 or y + hh > H - 2:
                    s += gap * 0.35
                    continue
                if blocked(x - hw, y - hh, x + hw, y + hh):
                    s += gap * 0.35
                    continue
                if any(abs(x - px) < hw + phw + sep and abs(y - py) < 26 * S
                       for px, py, phw in placed_boxes):
                    s += gap * 0.3
                    continue
                # 画：先渲到小图再旋转，保证斜线方向的字也是抗锯齿的
                pad = stroke + 3
                tmp = Image.new("RGBA", (int(tw + 2 * pad), int(th + 2 * pad)), (0, 0, 0, 0))
                _, _, bb = _text_size(f, txt, stroke)
                ImageDraw.Draw(tmp).text((pad - bb[0], pad - bb[1]), txt, font=f,
                                         fill=LABEL_FILL + (255,), stroke_width=stroke,
                                         stroke_fill=LABEL_HALO + (255,))
                tmp = tmp.rotate(-ang, resample=Image.BICUBIC, expand=True)
                layer.alpha_composite(tmp, (int(x - tmp.width / 2), int(y - tmp.height / 2)))
                # 自检：数字必须真的落在对应高程的那条线上（DEM 反查，偏差 >8 m 说明错位）
                gy, gx = int(y / sy), int(x / sx)
                if 0 <= gy < dem.shape[0] and 0 <= gx < dem.shape[1]:
                    errs.append(abs(float(dem[gy, gx]) - lv))
                bw, bh = tmp.width / 2 + 2, tmp.height / 2 + 2
                placed_boxes.append((x, y, bw))
                n_lab += 1
                for cx in range(int((x - bw) // cell), int((x + bw) // cell) + 1):
                    for cy in range(int((y - bh) // cell), int((y + bh) // cell) + 1):
                        grid.add((cx, cy))
                s += gap

    if verbose:
        chk = ""
        if errs:
            chk = (f"，自检：数字处实测高程偏差 中位 {np.median(errs):.1f} m / "
                   f"最大 {max(errs):.1f} m")
        print(f"   标注 {n_lab} 个（{n_line} 条计曲线，间隔 {LABEL_EVERY:.0f} m）{chk}")
    return Image.alpha_composite(pil.convert("RGBA"), layer).convert("RGB"), grid


def poi_label_cells(meta, scale_raster, ss, cell):
    """把**攻略自己的 POI 标注框**换算成底图的"禁标网格"。

    ⚠ 2026-10-09 亚丁线实测：底图注记（OSM 峰名/湖名）画在底图上，
    而攻略的 POI 标注是随后叠在 SVG 里、还带白描边 —— 两者一撞，
    底图那条注记会被白描边啃掉半行（现场是「仙乃日」被「珍珠海」压住）。
    所以这里按 route_def 的 dx/dy/anchor 与同款字号，粗算出每个标注框，
    交给 draw_osm 的 Labeller 一起避让。
    """
    try:
        import route_def as _RD
    except Exception:                                              # noqa
        return set()
    ox = meta["tx0"] * TILE + meta["box"][0]
    oy = meta["ty0"] * TILE + meta["box"][1]

    def proj(lon, lat):
        x, y = lonlat_to_px(lon, lat, meta["z"])
        return (x - ox) * scale_raster, (y - oy) * scale_raster

    cells = set()
    fs = 21 * 0.85                       # 与 map_svg 的 POI 字号口径一致
    for d in GC.pois_of(_RD):
        x, y = proj(d["lon"], d["lat"])
        txt = f'{d["name"]} {_RD.fmt_ele(d["ele"])} m'
        w = (GC.text_w(txt, fs) + 26)
        h = fs * 1.6
        tx, ty = x + d["dx"], y + d["dy"]
        anc = d["anchor"]
        x0, x1 = ((tx, tx + w) if anc == "start" else
                  (tx - w, tx) if anc == "end" else (tx - w / 2, tx + w / 2))
        # ⚠ x0/x1 已经是**栅格像素**（proj 乘过 scale_raster），网格单元也是栅格像素，
        #  不要再乘 ss（曾多乘一次 → 禁标区被放大到整张图，山峰名从 12/15 掉到 2/15）。
        for cx in range(int(x0 // cell), int(x1 // cell) + 1):
            for cy in range(int((ty - h / 2) // cell),
                            int((ty + h / 2) // cell) + 1):
                cells.add((cx, cy))
    return cells


def avoid_points(meta, scale_raster, ss):
    """需要避让的栅格像素点：实测轨迹 + 攻略 POI。

    轨迹线在底图之上还要被攻略的粗实线覆盖，标注压在它上面会被切断；
    POI 同理（还有图标）。所以两者都作为"禁标区"。
    """
    ox = meta["tx0"] * TILE + meta["box"][0]
    oy = meta["ty0"] * TILE + meta["box"][1]

    def proj(lon, lat):
        x, y = lonlat_to_px(lon, lat, meta["z"])
        return (x - ox) * scale_raster, (y - oy) * scale_raster

    pts = []
    for cand in (OUT / "track_real.json", HERE / "track_real.json",
                 HERE / "kml" / "track_real.json"):
        if not cand.exists():
            continue
        try:
            tr = json.loads(cand.read_text(encoding="utf-8"))
            for p in tr["pts"]:
                x, y = proj(p["lon"], p["lat"])
                pts.append((x, y, 7.0 * ss))
            break
        except Exception as e:                                     # noqa
            print(f"   （轨迹避让读取失败：{e}）")
    try:
        sys.path.insert(0, str(HERE))
        import route_def as _RD
        for d in GC.pois_of(_RD):
            x, y = proj(d["lon"], d["lat"])
            pts.append((x, y, 20.0 * ss))
    except Exception:                                              # noqa
        pass
    return pts


# ------------------------------------------------------------------ 主流程
def build_terrain():
    LON0, LAT0, LON1, LAT1, src = GC.resolve_bbox()
    GC.assert_bbox_covers((LON0, LAT0, LON1, LAT1), "make_terrain")
    print(f"[1/6] 地图窗口（{src}）: {LON0}, {LAT0} → {LON1}, {LAT1}", flush=True)
    print(f"[1/6] 下载 DEM（terrarium z{DEM_Z}）…")
    dem, meta = load_dem(DEM_Z, LON0, LAT0, LON1, LAT1)
    dem = fill_voids(dem)
    lat_c = (LAT0 + LAT1) / 2
    lo, hi = float(np.nanmin(dem)), float(np.nanmax(dem))

    dw, dh = dem.shape[1], dem.shape[0]
    scale_logical = LOGICAL_W / dw                 # 逻辑像素/瓦片像素（SVG 层用）
    scale_raster = RASTER_W / dw                   # 栅格像素/瓦片像素（底图用）
    LOGICAL_H = round(dh * scale_logical)
    RASTER_H = round(dh * scale_raster)

    print(f"[2/6] 晕渲（GDAL Horn，光源 西北 315°/45°，z_factor {Z_FACTOR}）…")
    hs = hillshade(dem, meta["z"], lat_c)

    print("[3/6] 分层设色 + 合成 …")
    rgb = hypso(dem)
    # 制图惯例：设色打底 + 晕渲塑形（环境光 + 方向光，背光面不死黑）
    img = rgb * (AMBIENT + DIRECTIONAL * hs)[..., None]
    over = img > HL_CLIP
    img[over] = HL_CLIP + (img[over] - HL_CLIP) * HL_KEEP     # 高光软限幅，亮而不白
    img = np.clip(img, 0, 255).astype(np.uint8)
    # BICUBIC 而非 LANCZOS：LANCZOS 在陡崖等高反差处会产生振铃，被随后的 USM 放大成"颗粒噪点"。
    pil = Image.fromarray(img, "RGB").resize((RASTER_W, RASTER_H), Image.BICUBIC)
    # USM 提锐：30 m DEM 在 z15 已被过采样 7×，天生柔；提锐才能让山脊/冲沟"立"起来
    pil = pil.filter(ImageFilter.UnsharpMask(radius=UNSHARP[0], percent=UNSHARP[1],
                                             threshold=UNSHARP[2]))

    print(f"[4/7] 等高线（{CONTOUR_MINOR:.0f} m / 计曲线 {CONTOUR_MAJOR:.0f} m，"
          f"线宽 {CONTOUR_DILATE}@{RASTER_W}px）…")
    dem_out = np.asarray(Image.fromarray(dem, "F").resize((RASTER_W, RASTER_H), Image.LANCZOS),
                         dtype=np.float32)
    arr = np.asarray(pil).copy()
    paint_contours(arr, dem_out, *CONTOUR_DILATE)
    pil = Image.fromarray(arr, "RGB")

    print(f"[5/7] 等高线高程标注（计曲线每 {LABEL_EVERY:.0f} m）…")
    scale_raster_y = RASTER_H / dh
    pil, seed_cells = draw_contour_labels(
        pil, dem, (scale_raster, scale_raster_y), lo, hi,
        avoid=avoid_points(meta, scale_raster, SS))

    print(f"[6/7] OSM 矢量叠加 + 落盘（超采样 {SS}× → 栅格 {RASTER_W}px / 逻辑 {LOGICAL_W}px）…")
    pil.save(OUT / "base_terrain.jpg", **JPG)
    m = {"size": [LOGICAL_W, LOGICAL_H], "scale": scale_logical,
         "raster": [RASTER_W, RASTER_H], "ss": SS,
         "box": meta["box"], "bbox": [LON0, LAT0, LON1, LAT1], "z": meta["z"],
         "tx0": meta["tx0"], "ty0": meta["ty0"],
         "source": f"本地自渲染：地形 DEM z{DEM_Z} + OSM 矢量（非瓦片服务）",
         "attribution": "Elevation: SRTM/Copernicus DEM (AWS Open Data, 免费开放) · "
                        "Map data: © OpenStreetMap contributors (ODbL) · "
                        "本地生成，无瓦片服务速率限制。"}
    (OUT / "base_meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    try:
        sys.path.insert(0, str(HERE))
        from draw_osm import draw_overlay
        # 攻略自己的 POI 名要抑制底图重复注记：重名时海拔口径会打架
        # （OSM 十王峰 1344.4 vs 实测轨迹 1345.5），一律以实测轨迹为准。
        # ⚠ 只按全名匹配会漏 —— route_def 的 POI 常带后缀（如「万佛顶 · 全程最高」
        #   「雷洞坪 · 观光车终点」），而 OSM 的注记是「万佛顶」「雷洞坪」，
        #   全名比对不成立，底图上会并排出现两条同地注记。
        #   → 除全名外，再把「·」前的核心地名也加进跳过表。
        try:
            import route_def as _RD
            skip = set()
            for d in GC.pois_of(_RD):
                skip.add(d["name"])
                skip.add(d["name"].split("·")[0].strip())
        except Exception:                                          # noqa
            skip = set()
        pil = draw_overlay(pil, m, OUT / "osm.json", skip_names=skip,
                           px_scale=SS, proj_scale=scale_raster,
                           seed_cells=(seed_cells
                                       | poi_label_cells(meta, scale_raster, SS, 14 * SS)))
    except FileNotFoundError:
        pass
    pil.save(OUT / "base_map.jpg", **JPG)
    print(f"[7/7] 完成 栅格 {pil.size} / 逻辑 {LOGICAL_W}×{LOGICAL_H}  高程 {lo:.0f}–{hi:.0f} m  "
          f"jpg {(OUT/'base_map.jpg').stat().st_size/1048576:.2f} MB")


if __name__ == "__main__":
    build_terrain()

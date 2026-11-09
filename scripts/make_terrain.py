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
import io, json, math, os, queue, sys, threading, time, urllib.request
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
# ⚠ DEM 缩放要按窗口大小**自适应降档**（2026-10-10 狼塔 C+V 实测）：
#   底图最终只有 LOGICAL_W(1504) 逻辑宽，DEM 分辨率再高都是浪费。
#   但 z15 对**长线**是灾难：狼塔窗口 0.75°×0.57° → DEM 约 17600×20000 px ≈ 3.5 亿像素，
#   单个 float32 数组就 1.4 GB，`paint_contours` 还要在上百个高程层上各扫一遍全图 →
#   实测 **25 分钟 + 峰值内存 11.5 GB**（差点把机器拖进 swap），而输出只有 3008 px 宽。
#   → 超过 DEM_MAX_PX 就逐级降到 z14/z13：瓦片数按 4 倍递减，质量对 1504 宽的成品没有可见损失
#     （z13 ≈ 19 m/px，仍远细于成品的 50–100 m/px）。
#   ⚠ 2026-11-03 由 40 M 提到 45 M：念青东波密北线窗口 1.19°×0.92°（122×102 km）在 z13 是
#     42.6 M px，卡在 40 M 上限外会被再降一档到 z12（34 m/px）—— 对 4512 px 成品来说
#     晕渲与等高线会明显发软。45 M 只多 5 M（float32 +20 MB），仍在安全区。
DEM_MAX_PX = 45_000_000


def _window_px(z, lon0, lat0, lon1, lat1):
    """该窗口在 z 级下的 DEM 像素数（估算，用于决定要不要降档）。

    投影委托 `GC.mercator_px` —— 原先这里内联了另一套写法（`tan + 1/cos` 形式，
    数学上等价，但那是本文件里的**第 3 份**公式抄写）。
    """
    x0, y0 = GC.mercator_px(lon0, lat1, z, TILE)
    x1, y1 = GC.mercator_px(lon1, lat0, z, TILE)
    return abs(x1 - x0) * abs(y1 - y0)


# 默认起档：**14**（2026-11-03 定）。数据源最高 z15 只在个别小窗口（< 20 km）才有实际意义，
# 而 z15 的像素数是 z14 的 4 倍 —— 长线窗口动辄十几分钟、几 GB 内存，收益却是零
# （4512–6016 px 的成品上，z14 的 8 m/px 已经有 1.5–2 倍余量）。
# 需要更细的线路仍可用 CFG["dem_z"]=15 显式要，**但要知道自己在干什么**。
DEM_Z_DEFAULT = min(DEM_Z, 14)


def pick_dem_z(lon0, lat0, lon1, lat1, z=DEM_Z_DEFAULT):
    """按窗口大小把 DEM 缩放降到合理的档位（返回实际使用的 z）。

    ⚠ 大窗口（> DEM_MAX_PX）仍会自动降档 —— 这是内存/耗时护栏，不是质量问题：
      122×102 km 的窗口在 z14 是 171 M px（约 9 分钟），在 z12 只有 11 M px。
      确实需要 z14 的长线，用 CFG["dem_z"]=14 绕过本函数（见 build_terrain 的分支）。
    """
    while z > 12 and _window_px(z, lon0, lat0, lon1, lat1) > DEM_MAX_PX:
        z -= 1
    return z


def cap_aspect(lon0, lat0, lon1, lat1, max_ratio):
    """把窗口的「东西:南北」像素比钳到 max_ratio 以内（只扩不缩，避免裁掉轨迹）。

    背景：底图固定逻辑宽 LOGICAL_W（→ 栅格宽 RASTER_W 固定），高度由窗口南北跨度按比例
    推出。东西跨度远大于南北的线路（如横向切脊的贡嘎南山脊线，真实约 3.3∶1）会被拉成极扁的
    横图（≈2.2∶1），与竖图为主的其它线路放在一起很突兀。此函数在「图太宽」时把南北外扩，
    让宽高比回到上限值，图面更协调；图本来就窄/方则原样不动。

    像素横纵比与 DEM 级别 z 无关（横纵都按 2^z 缩放，互相抵消），所以直接用
    `GC.mercator_px` 取一个参考 z 算 Δx/Δy 即可；横纵比确定后，把南北跨度按倍率 f 线性
    外扩（线路跨度小，墨卡托在此范围内近似线性，误差可忽略）。
    """
    import math
    z_ref = DEM_Z
    x0, y0 = GC.mercator_px(lon0, lat1, z_ref, TILE)   # 左上（纬度高 → y 小）
    x1, y1 = GC.mercator_px(lon1, lat0, z_ref, TILE)   # 右下（纬度低 → y 大）
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    if dy <= 0:
        return lon0, lat0, lon1, lat1
    ratio = dx / dy                                      # = 出图宽高比（z 无关）
    if ratio <= max_ratio:
        return lon0, lat0, lon1, lat1
    f = ratio / max_ratio                                # 南北需放大的倍率
    lat_mid = (lat0 + lat1) / 2.0
    new_span = (lat1 - lat0) * f
    return lon0, lat_mid - new_span / 2.0, lon1, lat_mid + new_span / 2.0
# ---- 超采样（放大不糊的关键）----
# 逻辑宽 = 版式宽（SVG viewBox 用），栅格按 LOGICAL_W × SS 出图。
# SVG 里 <image width="1504"> 会把 3008 的 jpeg 缩回逻辑宽渲染 —— 这等于给浏览器做了 2× 超采样：
#   ① 常规尺寸下等于 4 倍样本降采样 → 线更锐、字更实；
#   ② 放大到 200% 时仍有真实像素，不会立刻糊成一团。
# 代价：底图 jpg 体积约 ×2.5，HTML 变大，但这是"放大能看清"唯一实际有效的做法。
LOGICAL_W = 1504
# 超采样倍率：`CFG["map_ss"]` 可按线路覆盖（**默认 4** → 6016 px，2026-11-03 由 3 上调）。
#   配合 `CONTOUR_WIDTH=(1,2)`（单位栅格 px、不随超采样变）→ 线宽在 6016 px 上反而是"更细"，
#   视觉上等高线退到背景、晕渲与设色主导，这是用户要的"清爽地形图"口径。
# 放大窗口后"每公里像素变少 → 看着糊"的唯一正解是提高这个倍率：
#   · 只影响栅格与独立高清图，HTML 内嵌仍降到 EMBED_MAX(1600)，页面体积不变；
#   · 代价是 make_terrain 耗时/内存按像素数走（SS=3 约 2.2 倍）。
SS = 4
try:
    SS = max(2, int(GC.cfg("map_ss", SS) or SS))
except Exception:                                              # noqa
    pass
# 一次性倍率覆盖（`HIKING_MAP_SS=13 python make_terrain.py`）：只为"这一次出超大图"，
# 不写进项目 CFG —— 否则以后每次重建都要按这个倍率跑（耗时/内存按像素数走）。
# ⚠ `render_map_hi.py` 必须读**同一个**环境变量：它的 W = IW×SS，两边不一致就会
#   把 19552 px 的底图塞进 4512 px 的画布（等于白升）。
_env_ss = os.environ.get("HIKING_MAP_SS", "").strip()
if _env_ss:
    SS = max(2, int(_env_ss))
RASTER_W = LOGICAL_W * SS

# ---- 等高距：**按窗口自适应**，不写死 ----
# ⚠ 血泪教训（2026-10-10 狼塔 C+V）：等高距写死 20 m，在小窗口（亚丁 0.19°×0.15°）
#   线覆盖率 21%、像纸质地形图；换到长线窗口（狼塔 0.81°×0.62°）**同一 20 m 间隔**
#   覆盖率飙到 66% —— 整张图糊成一片棕色，用户原话"比例尺越大，等高线越密，地图很难看"。
#   物理原因：等高线的疏密 = 每像素真实高差 / 等高距。窗口一大，同样的地表起伏被压进
#   同样 3008 px，每像素跨过的等高线就成倍增加 → 必然糊。
#   → 判据落到**实测线覆盖率**：从最细的候选间隔往上试，第一个覆盖率 ≤ 目标值的就是它。
#     小窗口仍得 20 m（保持细节），长窗口自动退到 50/100 m（保持可读）。
CONTOUR_NICE = (10, 20, 25, 50, 100, 200, 250, 500)   # 候选等高距（米，都是好记的整数）
CONTOUR_TARGET_COV = 0.20   # 线覆盖率上限（2026-10-25 用户定：全局默认对齐 37 条线路在用的 0.20 口径）
CONTOUR_COV_MIN = 0.15      # 覆盖率下限（2026-10-25 用户定）：与上限组成 [0.15, 0.20] 动态区间，
                            # 区间内取最接近中点的一档；缺省 None = 旧的"≤上限"单阈值行为
# contour_coverage()（band 边缘估计，**不扣陡坡省略**）→ 真实像素线覆盖 的折算系数。
# 2026-10-25 起选档改用 `contour_actual_coverage()`（逐像素复刻 paint_contours 的判据，
# 含陡坡省略），这个常数只留给"拿不到 m_per_px 的老调用/自检"当兜底。
# 为什么废掉它当主判据：它是**地形相关的**——陡坡线省略多的高山区实际只有 0.66–0.75，
# 而巴丹吉林这种缓沙丘几乎没有省略、比值 ≈1.0，用一个常数 0.82 必然两头都不准
# （2026-10-25 普查：6 条高山线本该选更细一档被挡掉，1 条沙漠线被低估）。
COV_EST_TO_ACTUAL = 0.82
# 上限的**容忍带**：覆盖率允许超出 `contour_target_cov` 这么多（绝对值，3 个百分点）。
# 为什么要容忍：候选等高距是离散规范档，20.3% 与 10.2% 之间没有中间态——
# 死守 20% 会把密度砍掉一半（他念-梅里、狼塔、KBC 等实测 10.2–11.4%，
# 而更细一档只超 0.3–2.7 pp）。取 3 pp 而不是 2.5 pp：北天山中线更细一档是 22.66%，
# 2.5 pp 时以 0.16 pp 之差被挡在外面（实测仍是 50 m / 11.4%），同一批线路里
# 22.16% 的梅里北坡却能进 —— 阈值卡在数据堆中间，纯粹是指标巧合，不是制图判断。
COV_OVER_TOL = 0.03
# ⚠ 该阈值可**按线路覆盖**：`route_def.CFG["contour_target_cov"]`。
#   2026-10-16 希夏邦马实测：本区 20 m 覆盖率 28.6%（虽在 30% 以内，但山体陡坡处线仍成排、
#   雪原上偏抢眼），用户要求"再细一些" → 本线把目标收到 **0.26**，自动退到 25 m（23.8%）。
#   2026-10-25 用户拍板：全局默认收紧到 0.20 + 下限 0.15（= 37 条线路已在用的口径），
#   "最坏情况仍可读"交给下限兜底；狼塔等自有显式配置的线路不受影响。
#   仍需更疏/更密的线路继续在线路级覆盖（上限下限都可单独配）。
# 下面两个只作**兜底缺省值**（调用方没传间隔时用），实际间隔由 pick_contour_interval() 定。
CONTOUR_MINOR = 20.0    # 首曲线缺省间隔
CONTOUR_MAJOR = 100.0   # 计曲线缺省间隔（正常由 nice_major() 从首曲线推：20→100 是 ×5，25→100 是 ×4）
AZ, ALT = 315.0, 45.0   # 主光方向：西北、高度角 45°（制图惯例）
# 多向晕渲（MDOW，Multi-Directional Oblique Weighting）：单向光会把地貌压成"半边亮半边黑"，
# 背光侧的冲沟/山脊全部消失。改成四向**加权**叠加 —— 主光仍取西北（符合制图惯例、保住立体感），
# 另外补上正北/正西/西南让背光面的地形结构显出来。权重和为 1。
MDOW = ((225.0, 0.20), (270.0, 0.20), (315.0, 0.40), (360.0, 0.20))
# 垂直夸张：让低山也立得起来。1.4 在雪线以上大面积的地形会偏软（贡嘎实测），
# 1.5–1.55 更接近「真山」；可按线路 CFG['z_factor'] 覆盖。
Z_FACTOR = 1.5
UNSHARP = (3, 45, 3)    # USM(半径, 强度%, 阈值)：给晕渲"提锐"，补偿 30 m DEM 的天然柔化
# JPEG 参数：地图全是**彩色细线 + 小字**，必须关掉默认的 4:2:0 色度抽样（subsampling=0 → 4:4:4）。
# 4:2:0 会把色度通道砍到 1/4 分辨率，彩色线条一律发虚发彩边 —— 这是"放大看不清"的隐形主因之一。
# 质量可按线路调高（`CFG["jpg_quality"]`，默认 86）：线和小字多的图，
# 86 的块效应在 200% 放大时最明显（"放大看不清"的另一半原因）。
JPG = dict(quality=int(GC.cfg("jpg_quality", 86) or 86),
           optimize=True, progressive=True, subsampling=0)
# 等高线线宽（按栅格像素）：3008 分辨率下的 (首,计)。计曲线是首曲线的 2 倍宽 + 更深色。
# 注意：这里刻意画细 —— 等高距再自适应，陡坡处仍会成排，线一粗就糊成"棕色泥"。
# ⚠ 2026-10-16 希夏邦马：计曲线从 3 px 收到 **1 px**（与首曲线同宽），两者的区分
#   不再靠"粗细"，改由派生色的明度差承担。理由：这片图 15% 是冰川白雪、山体等高线
#   成排出现，3 px 的计曲线在雪原上连成"灰带"，把等高线读成了一片色块而不是线。
# 分层设色的量化步长（米）：同一等高带内颜色必须一致（2026-10-20 用户要求）。
# 由 build_terrain 在选出等高距后覆写为「首曲线间隔」；0 = 关闭量化。
# 雪线以上「按坡度混岩」（2026-10-20）：雪线不是一刀切的白 —— 陡壁挂不住雪，露出岩体。
# 0 = 关闭（回到二值：线以上全白）。斜率为 tan 值：35° ≈ 0.70、50° ≈ 1.19。
SNOW_ROCK_MIX = 0.42          # 陡壁最多混多少岩色（0.62→0.42：岩只作一层淡影，不再发脏）
SNOW_ROCK_T = (1.05, 1.75)    # 46° 起混 / 60° 全岩（只有真陡壁才露岩）
# 岩色必须是冷浅灰：暖褐 (122,116,110) 混进白雪里读作「脏」，不是「岩」。
SNOW_ROCK_RGB = (156, 156, 160)
TINT_BAND_M = 0.0
CONTOUR_DILATE = (0, 0)
# 线宽默认（首, 计），单位**基准栅格 px**（= 4512 px 画幅上的 px，见 CONTOUR_REF_SS），
# 不随超采样倍率变化（2026-10-28 用户定默认）。出图时掩膜放大 → 线宽跟着等比放大。
# 奇数宽走十字膨胀、偶数宽走方形膨胀（见 `_widen`），所以 2 px 也是精确 2 px。
# 线路级覆盖：CFG["contour_width"] = (2, 3)；显式写 None 退回旧行为
# （首线恒 1 px、计线 max(0, 3-2) 补偿）。
CONTOUR_WIDTH = (1, 2)
# ---- 等高线几何基准栅格（2026-11-07 用户：「等高线不随超采变动」）----
# 超采样 SS 只该是"清晰度旋钮"，不该改等高线的**几何**。但旧实现把一切建在
# `dem_out`（= RASTER_W 宽）上：选档覆盖率 ∝ 1/画幅高度、线宽是固定栅格 px、
# 雪线虚线节距也是固定栅格 px —— 于是同一条线 SS=3 选 25 m、SS=4 选 20 m、SS=13 选 10 m
# （珠峰东坡实测 25 m 覆盖 18.5% → 13.8% → 4.0%，level 数直接翻倍），
# 线还越画越细、雪线虚线糊成实线。这就是 2026-11-07 那次大画幅"必须手钉 contour_minor" 的根因。
# → 现在所有像素级几何（等高距选档、陡坡省略、线宽、虚线节距）一律在**基准栅格**上定义，
#   出图只把掩膜放大到 RASTER_W（放大不改变相对线宽）。
#   基准取 3 = 4512 px：0.15/0.20 覆盖率区间（2026-10-25）与 (1,2) 线宽（10-28）
#   都是在这个画幅上定的；换基准等于把 37 条线路的选档结果集体推翻。
CONTOUR_REF_SS = 3
# 陡坡省略阈值（tan，不是角度）：超过就不画该档等高线。理由见 paint_contours 的注释。
# 45° = 1.0；63° = 2.0。可按线路在 route_def.CFG 覆盖。
STEEP_SKIP_MINOR = 1.0
STEEP_SKIP_MAJOR = 2.0
# 等高线配色 + alpha 融合：硬像素覆盖会留下高反差硬边，alpha 写回才像"印在纸上"。
# ⚠ 血泪教训（2026-10-10 用户反馈"等高线的颜色也不好看"）：原配色是暖橙棕
#   (172,155,136)/(139,111,84)，而底图分层设色本身就是"绿→土黄→陶土"的暖色带 ——
#   暖线画在暖底上，色相差几乎为零，只剩明度差，于是整张图读起来是"发黄的旧报纸"。
#   → 换成**中性灰棕**：亮度仍分两档（首线浅、计线深）保住层次，
#     但把橙味抽掉，让等高线读起来是"灰笔线"，把彩度完全让给海拔色带和轨迹。
# ⚠ 但中性灰棕也是"全图一条色"（2026-10-16 希夏邦马，用户要求"配色与该区域渲染
#   颜色接近"）：雪线以上是冷白、谷地是草绿、裸岩是陶土，同一条灰线压在这三种底上
#   —— 在白雪上显得脏、在草甸上显得跳。**正解是派生色**（CONTOUR_DERIVED=True）：
#   线色由该像素的底图色算出（同色相 + 降饱和 + 明度分档），线就永远"长在地里"。
C_MINOR, C_MAJOR = (152, 144, 132), (100, 90, 80)     # 仅 CONTOUR_DERIVED=False 时用
A_MINOR, A_MAJOR = 0.15, 0.30                          # 派生色融合度（2026-10-28 用户定默认 = 更轻的一档）
#   历史：0.19/0.47（2026-10-25）→ 0.15/0.30。降 alpha 只影响"线压上去有多重"，
#   不动几何；明度差兜底由 CONTOUR_MIN_DELTA (15,34) 保证，浅底上仍读得出。
# --- 派生色参数（首, 计）---
CONTOUR_DERIVED   = True      # True = 随底图派生（推荐）；False = 用上面的固定灰棕（旧行为）
CONTOUR_DARKEN    = (0.74, 0.46)   # 亮底（亮度 ≥ 阈值）处的明度系数：越高压得越狠
CONTOUR_LIGHTEN   = (16, 38)       # 暗底处的**提亮**量：阴影里的线再压暗就成黑泥，反过来提亮
CONTOUR_LIGHT_T   = 110.0          # 亮/暗底分界（0–255 亮度）
CONTOUR_MIN_DELTA = (15, 34)       # 与底图的最小明度差：保证中间调处也读得出线
# 雪面（亮度 >200）的压暗系数上限：首 0.80 / 计 0.66。
# 雪原上的线要看得见但别抢戏：原 0.74/0.46 在纯白底上是近黑硬线，一密就成网纹。
SNOW_CONTOUR_DARKEN = (0.80, 0.66)
CONTOUR_DESAT     = 0.30           # 线色向同明度中性灰靠的比例（彩度让给底图与轨迹）
# 缓坡"起云"：垂直夸张会把 30 m DEM 的微起伏一起放大，按真实坡度加权渐回平地基准
FLAT_ANGLE = 2.5
HS_SMOOTH = 3           # 明暗柔化半径（DEM 像素）——柔和明暗 + 锐利线划 = 纸质地形图质感
VOID_MAD = 900.0        # 「远低于局部参考面」多少米才算空洞（见 fill_voids 的教训）

# ---- 等高线高程标注 ----
# 只有计曲线（每 100 m）标数字，首曲线标了就成"数字墙"。
# 所有尺寸都是**逻辑像素**，内部乘 SS 变栅格像素（超采样下同样锐利）。
LABEL_EVERY = 100.0      # 缺省标注级（实际 = 本次选定的计曲线间隔，由 build_terrain 覆写）
LABEL_GAP = 560          # 同一条等高线上相邻标注的最小弧长间隔
LABEL_SEP = 130          # 任意两个标注之间的最小间距（防聚成堆）
LABEL_MAX_ANGLE = 62     # 局部倾角超过此值就不标（斜着读不出来的不如不标）
LABEL_WINDOW = 26        # 估计局部走向的采样窗口（±窗口长度）
LABEL_FONT = 8.2         # 字号（逻辑像素）：9.5→8.2（2026-10-21 用户：等高线字太粗太大）
#   ⚠ 2026-10-17 希夏邦马：11 → 9.5。本窗口 25 m 首曲线下等高线数字约 100+ 个，
#     11 px 时与峰名/湖名在同一片山脊上争位（用户反馈"字太大、重叠"）。
#     别再往下压：低于 9 后缩到长图（~0.4×）里就只剩一个色点了。
LABEL_FILL = (96, 84, 72)
LABEL_HALO = (252, 250, 245)
MS_STEP = 1              # marching squares 用的 DEM 抽稀步长（1 = 全分辨率，与栅格线严格同位）

# 字体清单已收敛到 guide_common.FONT_CANDIDATES（跨平台共用 + 按 (size,bold) 缓存），
# 那份清单的取舍说明见 guide_common 里的注释。
# ⚠ 首位是**常规**字重 msyh.ttc，不是原来的 msyhbd —— 这才真正兑现了 9a03d70
#   「等高线字太粗、全局字体要精细」的意图（那次只改了列表第二位，首位粗体没动）。
FONT_CANDIDATES = GC.FONT_CANDIDATES


def _label_font(size):
    return GC.label_font(size)


# ------------------------------------------------------------------ 投影
def lonlat_to_px(lon, lat, z):
    """经纬度 → 全球 Web Mercator 像素。

    公式收敛在 `GC.mercator_px`（全技能唯一权威实现，见那里的说明）；
    保留同名函数是为了不重写调用点。
    """
    return GC.mercator_px(lon, lat, z, TILE)


def px_to_lonlat(x, y, z):
    """`lonlat_to_px` 的逆。公式收敛在 `GC.mercator_ll`。"""
    return GC.mercator_ll(x, y, z, TILE)


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


def snap_peaks_to_dem(osm_path, dem, meta, lat_c, radius_m=800.0,
                      min_gain=200.0, tol=100.0, max_gap=400.0,
                      max_shift_m=400.0, allow_shift=None, verbose=True):
    """把 OSM 峰点吸附到 DEM 的局部山顶，返回 `{"lon,lat": [lon, lat]}`。

    为什么需要（2026-10-23 博格达实测，用户指着截图问"这个三角标的不准确"）
    ------------------------------------------------------------------------
    `natural=peak` 是**人工录入**的点，坐标常有几百米漂移；而等高线与晕渲是 DEM 画的。
    于是三角符号落在山腰、名字漂在坡上，和地形对不上。实测 7 个具名峰里 3 个明显偏：

        博格达三峰 5288：点在 4362 的山腰，800 m 内山顶 4810（离 797 m）
        博格达五峰 5213：4802 → 5158（离 508 m）
        博格达六峰 5180：4332 → 4784（离 380 m）
        Nangfeng  4683：4372 → 4594（离 347 m）
        博格达峰   5445：5148 就是邻域最高（离 7 m）—— 点位本来就准，绝不能动

    判据（三条同时满足才动）
    ------------------------
    ① 邻域最高点比**当前点**的 DEM 高 ≥ `min_gain`（200 m）→ 点在坡上不在顶；
    ② 邻域最高点不得高过标称 `ele + tol`（默认 +100 m）→ 否则说明窗口里装进了
       **另一座更高的峰**（实测「4488 那座」800 m 内就是 5175 的主峰，硬吸会张冠李戴），
       这时把半径减半重试；
    ③ 只在 `radius_m` 内找，找不到就**不动** —— 宁可保留 OSM 原点，也不造位置；
    ④ 山顶 DEM 不得比标称 ele 低过 `max_gap`（400 m）→ 差太多说明找到的是**卫峰**
       而不是本峰（博格达三峰 5288 周围 800 m 最高只有 4810，差 478 m：宁可不动）。

    只借 DEM 定位置，不借它改高程：DEM 对 5000 m 级雪峰普遍低估 200–400 m
    （雪面反照 + 30 m 分辨率抹平尖顶），图上印的仍是 OSM 的 `ele`。

    ⑤ 位移不得超过 `max_shift_m`（默认 400 m）→ 超出说明找到的是**另一座山**。
       逐峰可用 `CFG["peak_snap_allow"] = {"峰名": 600}` 单独放宽这一条（只放宽上限）。
       2026-10-25 全库核验：把 `min_gain` 降到 50 后，多数线路会冒出 800–1100 m 的
       "吸附"，且几乎全是 `(无名)` 峰点 —— 那是 800 m 方窗的角落（对角线 1131 m），
       即邻峰顶，不是校正。真正该修的是几十到两百米的小漂移；几百米以上的属于
       OSM 坐标本身录错，脚本不应替它搬家。
    """
    try:
        d = json.loads(Path(osm_path).read_text(encoding="utf-8"))
    except Exception:                                              # noqa
        return {}
    z, tx0, ty0 = meta["z"], meta["tx0"], meta["ty0"]
    cx0, cy0 = meta["box"][0], meta["box"][1]
    H, W = dem.shape
    mpp = 156543.03392 * math.cos(math.radians(lat_c)) / (2 ** z)
    out, moved, skipped_gap, near_miss = {}, [], [], []
    skipped_rad = []
    skipped_shift = []

    # 其它具名峰的原点：吸附目标不许落在它们 300 m 内 —— 否则就是"张冠李戴"，
    # 把 A 峰的三角挪到 B 峰的头上（峰群密集区 800 m 窗口里常有邻峰）。
    # ⚠ 自己是按 `lon,lat` **键**排除的，不能按"距离≈0"排除：
    #   曾经写 `math.hypot(nx - xi, ny - yi) > 1e-6`，而 nx,ny 是未取整浮点、
    #   xi,yi 是 `int(round(...))`，两者恒差 0.0–0.5 px（远大于 1e-6）→ 自己永远
    #   不算"自己"。后果：候选山顶落在**本点 300 m 内**的峰全部被判"会盖住邻峰"，
    #   半径一路减半到 <60 px 后**静默放弃**（2026-10-25 冈仁波齐：主峰只需移 175 m
    #   就被自己的原坐标挡下，`peak_snap.json` 写成 `{}`，图上三角漂在山肩）。
    named_px = []
    for e in d.get("elements", []):
        t = e.get("tags", {})
        if t.get("natural") != "peak" or "lon" not in e:
            continue
        if not (t.get("name") or t.get("name:en")):
            continue
        gx, gy = lonlat_to_px(e["lon"], e["lat"], z)
        named_px.append((f"{e['lon']:.7f},{e['lat']:.7f}",
                         gx - tx0 * 256 - cx0, gy - ty0 * 256 - cy0))

    for e in d.get("elements", []):
        t = e.get("tags", {})
        if t.get("natural") != "peak" or "lon" not in e:
            continue
        try:
            ele = float(str(t.get("ele", "")).replace("m", "").strip())
        except ValueError:
            ele = None
        gx, gy = lonlat_to_px(e["lon"], e["lat"], z)
        xi, yi = int(round(gx - tx0 * 256 - cx0)), int(round(gy - ty0 * 256 - cy0))
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        base = float(dem[yi, xi])
        # 没有 ele 的裸峰点：DEM 普遍低估雪峰高程，没标称高程就**无法**判断
        # "吸过去是不是换了一座更高的峰"，所以只用更保守的半径（350 m）+ 纯 DEM 判据
        # （节点处 DEM 比局部最高低 ≥min_gain 才动）。亚丁本窗口 18 个峰点里 14 个
        # 没录 ele，靠这一步把三角从山腰吸到山顶（2026-10-24 用户：三角不在等高线高点）。
        rad = (radius_m if ele is not None else min(radius_m, 350.0))
        r = int(rad / mpp)
        hit = None
        nm0 = t.get("name") or t.get("name:en") or "(无名)"
        for _ in range(4):                       # 最多缩 3 次
            if r < 60:
                # 半径缩到 <60 px 仍被挡 → 静默放弃曾经完全不可见（冈仁波齐踩过：
                # 主峰被自己的坐标挡下，日志只说"无需校正"）。留一条记录便于复查。
                skipped_rad.append(nm0)
                break
            win = dem[max(0, yi - r):yi + r, max(0, xi - r):xi + r]
            iy, ix = np.unravel_index(np.nanargmax(win), win.shape)
            mx = float(win[iy, ix])
            ax, ay = ix + max(0, xi - r), iy + max(0, yi - r)
            if ele is not None and mx > ele + tol:
                r //= 2                           # 窗口里有更高的别的峰 → 缩半径
                continue
            # 目标点离"别的具名峰原点"太近 → 说明吸过去会盖在邻峰上，缩半径重试
            # （按 lon,lat 键排除自己，见 above：不要用像素距离判"是不是自己"）
            _self_key = f"{e['lon']:.7f},{e['lat']:.7f}"
            if any(k != _self_key and math.hypot(ax - nx, ay - ny) * mpp < 300.0
                   for k, nx, ny in named_px):
                r //= 2
                continue
            # 位移护栏：几百米以上的"最高点"通常已经是另一座山，宁可不动。
            # 不缩半径重试 —— 缩完找到的会是坡上随便一个点，比原点更离谱。
            _shift = math.hypot(ax - xi, ay - yi) * mpp
            # 逐峰例外（CFG["peak_snap_allow"] = {"大雪山": 600}）：全局护栏 400 m 是为了
            # 防止"吸到邻峰"（三连山中央峰 501 m 就会落到西北峰那个顶上），但个别峰的
            # OSM 坐标就是偏在几百米外的同一座山上 —— 这类只能逐峰点名放行，且只改上限、
            # 不改其它四条判据（多高、多远、跳过邻峰、max_gap 全部照旧）。
            _lim = float(max_shift_m) if max_shift_m is not None else None
            if allow_shift and nm0 in allow_shift:
                _v = allow_shift[nm0]
                _lim = float(_v) if _v is not None else None
            if _lim is not None and _shift > _lim:
                skipped_shift.append((nm0, _shift))
                break
            gain = mx - base
            if gain >= min_gain:
                if ele is None:
                    hit = (ax, ay, mx)            # 无 ele：纯 DEM 判据，直接吸到局部最高
                elif ele - mx > max_gap:
                    gap = (t.get("name") or t.get("name:en") or "(无名)", ele, mx)
                    skipped_gap.append(gap)
                else:
                    hit = (ax, ay, mx)
            elif 0 < gain < min_gain:
                # 节点比局部最高低，但不到 min_gain 阈值 —— 往往是"在坡上但没到山腰"，
                # 记录出来看量级（2026-10-24 用户：部分三角不在等高线高点）。
                near_miss.append((nm0, base, mx, gain, r * mpp))
            break
        nm = t.get("name") or t.get("name:en") or "(无名)"
        if not hit:
            continue
        px_, py_, mx = hit
        lo, la = px_to_lonlat(px_ + tx0 * 256 + cx0, py_ + ty0 * 256 + cy0, z)
        dist = math.hypot(px_ - xi, py_ - yi) * mpp
        out[f"{e['lon']:.7f},{e['lat']:.7f}"] = [round(lo, 7), round(la, 7)]
        moved.append((nm, base, mx, dist))

    if verbose:
        if moved:
            print(f"   峰点校正：{len(moved)} 个 OSM 点漂在山腰 → 吸附到 DEM 山顶"
                  f"（位移 {min(m[3] for m in moved):.0f}–{max(m[3] for m in moved):.0f} m）")
            for nm, base, mx, dist in moved:
                print(f"     · {nm:16s} DEM {base:5.0f} → {mx:5.0f} m   移 {dist:4.0f} m")
        else:
            print("   峰点校正：本窗口无需校正（所有峰点都在各自山顶附近）")
        if near_miss:
            print(f"     · 近未校正 {len(near_miss)} 个（节点比局部最高低 "
                  f"{min(n[3] for n in near_miss):.0f}–{max(n[3] for n in near_miss):.0f} m，"
                  f"阈值 {min_gain:.0f} m 未触发）：")
            for nm, base, mx, gain, radm in sorted(near_miss, key=lambda n: -n[3]):
                print(f"       - {nm:16s} DEM {base:5.0f} → 局部最高 {mx:5.0f} m"
                      f"   差 {gain:4.0f} m（半径 {radm/1000:.1f} km 内）")
        for nm, ele, mx in skipped_gap:
            print(f"     ~ {nm:16s} 800 m 内最高只有 {mx:.0f} m（标称 {ele:.0f}），"
                  f"差的太多 —— 像是卫峰，保持 OSM 原点")
        if skipped_rad:
            print(f"     ~ 半径缩到 <60 px 仍被邻峰挡住 {len(skipped_rad)} 个（保持原点）："
                  f"{'、'.join(skipped_rad)}")
        if skipped_shift:
            print(f"     ~ 位移超 {max_shift_m:.0f} m 未吸附 {len(skipped_shift)} 个"
                  f"（多半是 OSM 坐标本身录错，需人工核对）：")
            for nm, sh in sorted(skipped_shift, key=lambda x: -x[1])[:8]:
                print(f"       - {nm:16s} 最近山顶在 {sh:.0f} m 外")
    return out


# ------------------------------------------------------------------ 晕渲
def hillshade(dem, z, lat_c, az=None, alt=ALT, zf=Z_FACTOR):
    """GDAL Horn 算法。返回 0–1 的明暗值。

    az=None（默认）走**多向晕渲 MDOW**：按 MDOW 权重把 4 个方位的明暗加权叠加。
    单向光虽然"立体感"强，但会把地貌压成半边亮、半边死的剪影 —— 背光侧的冲沟、
    山脊、崖壁全部糊成一团黑，这正是"不够真实"的主因。四向加权后：主光仍是西北 315°
    （制图惯例 + 保住立体感），同时补出正北/正西/西南，让背光面的地形结构显形。
    传入具体 az 则退化为原来的单向晕渲（保留旧行为，便于对比/调试）。

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
    # 坡向（0=北，顺时针）。⚠⚠ 2026-10-20 修 bug（贡嘎大环）：原写法 arctan2(dzdy, -dzdx)
    # 在图像坐标系（y 轴向下）里把坡向沿 45° 轴**镜像**了 —— 实测「面向西北的坡亮度 0.293、
    # 面向东南的坡 0.787」，即主光跑到了东南 135°，与设定 NW 315° 完全相反。
    # 后果不是"光从哪边来"这么轻：谷地两侧的受光墙与背光墙会整体对调，整条谷读成一道
    # 亮脊（用户原话"谷底看起来更像山脊"），而且所有山体的阴坡阳坡全反。
    # 正确写法 arctan2(-dzdx, dzdy)：
    #   北坡(dzdy>0,dzdx=0)→0°  东坡(dzdx<0)→90°  南坡(dzdy<0)→180°  西坡(dzdx>0)→270°
    aspect = np.arctan2(-dzdx, dzdy)

    def _one(az_deg):
        azr, altr = math.radians(az_deg), math.radians(alt)
        v = (math.sin(altr) * np.cos(slope)
             + math.cos(altr) * np.sin(slope) * np.cos(azr - aspect))
        return np.clip(v, 0.0, 1.0)

    if az is None:
        hs = np.zeros(dem.shape, np.float32)
        for a_deg, w in MDOW:
            hs += w * _one(a_deg)
    else:
        hs = _one(az)

    # 坡度加权只依赖坡度（与方位无关），权重和为 1 时"逐向加权"与"先合并再加权"等价
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
    # ⚠ 2026-10-17 尼泊尔四线（EBC / KBC）实测：原式 `dem < med - 2500` 在**高海拔窗口会翻车**。
    #   昆布窗口高程中位 4837 m → 阈值 2337 m，于是 1800–2337 m 的整条 Dudh Koshi 河谷
    #   被判成坏瓦片（288,915 px = 1.58%），再被调和插值抹成一块没有起伏的死平绿面。
    #   "坏瓦片"的真正表现是 NaN / 负高程，正常河谷（> 0 m）绝不能被牵连 ——
    #   → 绝对下限改成"负高程"。详见 references/pitfalls-terrain.md §8a。
    bad |= ~np.isfinite(dem) | (dem < 0)
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
    # ⚠ 通用护栏：空洞是"缺数据"，填它绝不会降低全域最高点。
    #   如果填完最高点掉了，说明被判为空洞的其实是真峰顶 —— 任何线路都适用这条不变量。
    pre_max = float(np.nanmax(dem))
    post_max = float(np.nanmax(out))
    if post_max < pre_max - 5:
        print(f"   ⚠⚠ 填充后全域最高点从 {pre_max:.0f} 掉到 {post_max:.0f} m —— "
              f"疑似把真峰顶当空洞填平了！请调大 VOID_MAD（当前 {VOID_MAD:.0f}）后重跑")
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
# 沙漠 / 干旱区专用色带（`route_def.CFG["terrain_palette"] = "desert"` 时启用）。
# ⚠ 为什么必须单独一套：沙漠线路海拔 1000–1400 m、高差不足 100 m，套 _RAMP_ALPINE
#   会把整片沙丘涂成"谷底灰绿 → 中山草甸绿"——用户一眼就看出"这不是沙漠"。
#   沙漠只有沙色与盐碱白两族：湖盆/盐壳最浅，沙丘随高程加深，立体感交给晕渲。
#   参考 references/china-terrain.md §2「沙漠（沙丘）#EAD9A0」。
_RAMP_DESERT = [(0.00, (236, 231, 219)),   # 湖盆 / 盐碱地：浅灰白
                (0.14, (238, 227, 190)),   # 沙丘间低地：淡沙黄
                (0.38, (232, 212, 160)),   # 沙丘主体：米黄  ≈ #EAD9A0
                (0.64, (224, 198, 138)),   # 沙丘中上部：暖沙
                (0.84, (214, 184, 120)),   # 沙丘脊线：深沙
                (1.00, (204, 172, 108))]   # 最高的沙丘脊：沙棕
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
    palette = str(_route_cfg("terrain_palette", "") or "").strip().lower()
    if palette == "desert":
        ramp = _RAMP_DESERT
        kind = "沙漠专用（沙丘米黄 / 盐碱灰白，无绿）"
    else:
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


def hypso(dem, snowline=None):
    """高程 → RGB 分层设色。
    区域跨越雪线 → 绝对高程地貌带（belt_stops：雪线以上积雪白，与雪线虚线严格同高程）；
    否则 → 通用 percentile 色带（ramp_stops：色阶按本区高程分位自适应，非雪线区用）。
    snowline=None 时自动读 route_def.CFG['snowline_m']（动态联网值）。"""
    if snowline is None:
        snowline = _snowline_from_cfg()
    snowline = float(snowline)
    stops_tbl = belt_stops(dem, snowline)
    if stops_tbl is None:
        stops_tbl = ramp_stops(dem)
    stops = np.array([s[0] for s in stops_tbl], float)
    cols = np.array([s[1] for s in stops_tbl], float)
    # ⚠ 2026-10-20 用户要求「同一等高线内的色阶必须一致」：
    #   连续插值会让一个等高带内部还有渐变，两条等高线之间就成了"看不出分界的糊坡"。
    #   按**等高距**量化高程（取带中心值）后再查色，带内完全同色，带与带之间才跳一档 ——
    #   这才是分层设色的本义。TINT_BAND_M=0 可关闭（回到连续插值）。
    if TINT_BAND_M > 0:
        dem = np.floor(dem / TINT_BAND_M) * TINT_BAND_M + TINT_BAND_M * 0.5
    d = np.clip(dem, stops[0], stops[-1])
    out = np.zeros(dem.shape + (3,), np.float32)
    for ch in range(3):
        out[..., ch] = np.interp(d, stops, cols[:, ch])
    return out


# ------------------------------------------------------------------ 地貌垂直带色带（绝对高程）
# 用户要求：底图配色贴近真实地貌（草原 / 戈壁 / 森林 / 砾石 / 雪 / 冰川），过渡要自然。
#  上面的 percentile 色带按高程**分布**拉伸，色阶位置随轨迹分布漂移 —— 表现为
#  "雪线"这条绝对等高线与底色对不上（6000 m 处可能还是陶土色）、峰顶白块位置不定。
# → 喜马拉雅型线路（区域跨越雪线）改走**绝对高程地貌带**，带位与雪线严格对齐：
#   高原草原 → 高寒草原 → 草甸/砾石过渡 → 高山砾石（流石滩）→ 裸岩寒漠
#   → 季节性积雪过渡带 → **雪线以上常年积雪白**。
#   带位参照喜马拉雅北坡垂直带谱 + 本区 DEM 实测（4125–8047 m）。
#   （戈壁 / 森林带在本区不存在；低海拔林区线路仍走上面的 percentile 通用色带。）
# ⚠ 仅当区域够到雪线带（dem_max ≥ snowline−250）时启用；
#   峨眉山（470–3085 m）/ 党岭（3200–5400 m，默认雪线 6000）等不跨雪线区域
#   自动回退上面的 percentile 通用色带，行为不变。
# 低海拔段（森林 / 灌丛）给绝对高程带：本窗口含南部深切河谷（DEM 实测低至 ~2900 m，
#  吉隆沟方向是有林区），只画 4200 m 以上会把整片南谷 clip 成一块平色。
# 季节档（2026-10-20 用户要求：后续所有出图都基于夏季地貌）
#   summer = 植被更绿；雪线偏移看 CFG["snow_month"]（**默认 7 月档 −150 m**，
#     见 MONTH_SNOW_SHIFT / DEFAULT_SNOW_MONTH）；annual = 旧的年常态配色（月份键不生效）。
#   CFG["terrain_season"] 可按线路覆盖。
SEASON = "summer"
SUMMER_SNOW_SHIFT = 350.0
# 按月份取雪线偏移（相对"现代雪线"= 消融季末的平衡线 ELA，即 8–9 月的值）。
# 依据：6 月仍处残雪期，季节性雪线比 ELA 低 300–400 m（喜马拉雅/天山实测同期差 300–500）；
# 7 月消融加速、8 月末到达最高。徒步最常走的 6–9 月用这张表即可。
#   CFG["snow_month"] = 6  →  偏移 −350 m（比 9 月多约 550 m 的雪）
MONTH_SNOW_SHIFT = {
    5: -500.0,   # 5 月：积雪大面积未消，雪线很低
    6: -350.0,   # 6 月：残雪期，垭口普遍有雪
    7: -150.0,   # 7 月：消融中
    8: 0.0,      # 8 月末 ≈ 现代雪线本值
    9: 200.0,    # 9 月：最少年积雪
    10: 350.0,   # 10 月：新雪开始，视觉雪线回升
}
# 未配 CFG["snow_month"] 时的默认档（2026-10-25 用户定：**默认 7 月**，−150 m）。
# 例外在线路级单独配：孟克特-那拉提 / 喀拉峻 / 托乎拉苏赛里木湖山脊线 = 6 月（残雪期）。
# ⚠ terrain_season="annual" 时月份键整体不生效（走年常态，无偏移）。
DEFAULT_SNOW_MONTH = 7
BELT_DENSIFY_M = 100.0   # 密化步长：段间每 ~100 m 插一个中间色，过渡更自然


def _ramp_check(stops, snowline, verbose=True):
    """色带自检：雪线以下不允许出现「G 通道相对 R 回升」的折返（肉眼就是"又黄又绿又黄"）。

    判据用 G−R：越正越绿、越负越暖。地貌垂直带从谷底到雪线应当**单调**变暖变灰，
    任何一段回升超过 6 个色阶都算配色错误 —— 北天山 3200→3700 那处回升 +18 就是这样抓出来的。
    """
    lo = [(e, c) for e, c in stops if e <= snowline + 1]
    bad = []
    for (e0, c0), (e1, c1) in zip(lo, lo[1:]):
        d0, d1 = c0[1] - c0[0], c1[1] - c1[0]
        if d1 - d0 > 6:
            bad.append((e0, e1, d0, d1))
    if verbose:
        if bad:
            print("   ⚠ 色带折返 %d 处：%s" % (len(bad), "，".join(
                f"{a:.0f}→{b:.0f} m（G−R {c:+d}→{d:+d}）" for a, b, c, d in bad[:4])))
        else:
            print("   色带自检：雪线以下 G−R 单调递减，无「黄→绿→黄」折返")
    return not bad


def regional_snow_shift(dem, bbox, verbose=True):
    """按区域返回「等效高程偏移」数组（米）；未配置或失败返回 None。

    CFG["snowline_regions"] = {
        "border_file": "out/borders.json",   # [{"pts": [[lon,lat],...]}]
        "south": 5000,                        # 南坡雪线（图的下方）
        "north": 6000,                        # 北坡雪线（图的上方）
        "blend_deg": 0.06,                    # 边界过渡宽度（度）
    }
    返回 shift：北坡 = north − south，南坡 = 0；边界处线性过渡。
    用法：dem_eff = dem − shift，然后用 south 雪线的一套色表查色。
    """
    reg = _route_cfg("snowline_regions", None)
    if not reg:
        return None
    try:
        import json as _json
        bp = reg.get("border_file", "out/borders.json")
        # 容错：既接受 "out/borders.json"（相对项目），也接受裸文件名
        cands = [Path(bp), GC.OUT / Path(bp).name, GC.ROOT / bp if hasattr(GC, "ROOT") else Path(bp)]
        fb = next((c for c in cands if c.exists()), None)
        if fb is None:
            if verbose:
                print(f"   ⚠ 找不到国界文件 {bp}，区域雪线不生效")
            return None
        bj = _json.loads(fb.read_text(encoding="utf-8"))
        seg = bj["pts"] if isinstance(bj, dict) else bj[0]["pts"]
        lons = np.array([pt[0] for pt in seg], float)
        lats = np.array([pt[1] for pt in seg], float)
        keep = np.isfinite(lons) & np.isfinite(lats)
        lons, lats = lons[keep], lats[keep]
        o = np.argsort(lons)
        lons, lats = lons[o], lats[o]
        x0, y0, x1, y1 = bbox
        H, W = dem.shape
        lon_axis = np.linspace(x0, x1, W)
        lat_axis = np.linspace(y1, y0, H)
        b_lat = np.interp(lon_axis, lons, lats)          # 每列的边界纬度
        d = lat_axis[:, None] - b_lat[None, :]           # >0 = 边界以北
        bw = max(float(reg.get("blend_deg", 0.06)), 1e-3)
        w = np.clip(d / (2 * bw) + 0.5, 0.0, 1.0)        # 0=南坡 1=北坡
        delta = float(reg["north"]) - float(reg["south"])
        shift = (delta * w).astype(np.float32)
        if verbose:
            print(f"   区域雪线：北坡 {reg['north']:.0f} m / 南坡 {reg['south']:.0f} m"
                  f"（Δ{delta:+.0f} m，过渡 {bw:.2f}°）")
        return shift
    except Exception as e:                                          # noqa
        if verbose:
            print(f"   ⚠ 区域雪线计算失败：{type(e).__name__}: {e}")
        return None


def belt_stops(dem, snowline, verbose=True, season=None):
    """按雪线对齐的绝对高程地貌带生成色阶表；区域够不到雪线带返回 None（调用方回退）。

    ⚠ 2026-10-20 重写（北天山实测）：旧实现把 **veg（绝对高程锚点）** 与
    **alpine（雪线相对锚点）** 直接合并排序，两条曲线在高程上会**互相穿插** ——
    北天山雪线 3800 时，alpine 到 3100 m 已压成裸岩褐 (126,118,106)，
    而 veg 的 3200/3700 锚点又把黄绿 (166,170,116)/(190,184,130) 插了回来，
    色带因此出现「黄绿 → 褐 → 黄绿」的折返（用户原话："不正常色阶"）。
    → 现在 veg 一律**按比例收进 [dmin, s − 1300]**，末端颜色与 alpine 首锚点相接，
      两条曲线只在 s−1300 处交接、不再交叉，整条色带绿度单调递减。

    season：
      · "summer"（默认）：植被更绿更饱和；雪线按 CFG["snow_month"] 偏移
        （默认 7 月档 −150 m，见 DEFAULT_SNOW_MONTH）；
      · "annual"：旧的年常态配色（无月份偏移）。
    """
    dmin, dmax = float(np.nanmin(dem)), float(np.nanmax(dem))
    s = float(snowline)                       # 调用方已按季节偏移过
    if dmax < s - 250:
        return None
    season = (season or SEASON).lower()
    if season == "summer":
        # 夏季：整条绿带往"冷绿/森林绿"拉，只在最上一档转黄绿；
        # 旧值 (172,182,124)/(176,182,126) 的 R≈G，整片山读起来就是黄的。
        # ⚠ 蓝通道是关键：B≈106 时读起来是"橄榄/土黄"，B≥126 才是"草绿"。
        #   参照贡嘎的观感 (150,172,132)，夏季绿带统一把 B 抬到 118–140，
        #   黄味只在最上一档（高寒草甸）保留一点。
        veg_cols = [(58, 96, 72), (74, 116, 86), (92, 134, 96),
                    (112, 148, 104), (132, 160, 112), (148, 168, 118)]
        # ⚠ 2026-10-21 用户"有些地方裸岩太多、不好看"：根因不是混岩，是这一带的
        #   橄榄褐 (140,138,120)/(132,128,120) 在白雪山体里读作"脏土"。
        #   改为**冷中性灰**（R≈G≈B，略偏蓝），保留立体感但不脏。
        # 2026-10-21「整体偏灰」：亚丁 67% 的面积落在 4200–4800 的岩壁带，
        # 旧值 (146,146,142) 在晕渲后压成中灰，整幅发闷。改为：
        #   ① 草甸绿多留一档（前两档仍带绿）
        #   ② 岩色提亮到 158 左右、并带一点暖（R≥G≥B），像被阳光照着的碎石坡
        # 高山带前两档仍是「高寒草甸绿」，到 55% 才转灰 ——
        # 雪线−700 到雪线之间那 700 m，实景一半是草甸、一半是碎石，不该整段发灰。
        alp_cols = [(152, 162, 126), (148, 156, 122), (152, 152, 138),
                    (160, 158, 152), (172, 174, 176), (240, 243, 247)]
    else:
        veg_cols = [(86, 102, 80), (100, 120, 88), (120, 140, 98),
                    (140, 154, 106), (156, 168, 116), (170, 178, 124)]
        alp_cols = [(170, 178, 124), (172, 172, 120), (156, 148, 118),
                    (138, 132, 114), (150, 149, 146), (234, 237, 242)]

    # 植被段顶端：夏季 s−700（把徒步实际活动的 3900–4600 m 都留在绿带里），
    # 年常态 s−1300。旧版一律 s−1300 —— 格聂雪线 5350 时顶端只有 4050，
    # 整条路线落在"高山草甸→砾石"的黄绿段，用户看到的就是"夏天怎么这么黄"。
    # 顶端优先取**林线**（CFG['treeline_m']）：固定 s−700 对"山体只比雪线高一点点"的线路
    # （年宝玉则 3800–5369、雪线 4650）会把整片 4000–4650 草甸划进高山带 → 82% 画面发灰。
    # 林线是独立于雪线的第二条界线（天山 ≈2700、喜马拉雅 ≈4000、横断山 ≈4100–4300）。
    _tl = _route_cfg("treeline_m", None)
    # ⚠ 用 max()：林线只用来**抬高**植被顶端，绝不降低。
    #   林线以上仍是高山草甸（绿的），不是碎石；若取 min()，天山这类林线 2700、
    #   雪线−700=3150 的线路会被砍掉 450 m 的绿带（实测乌孙绿占比 45%→18%）。
    _base_top = s - (700.0 if season == "summer" else 1300.0)
    top = min(max(float(_tl), _base_top) if _tl else _base_top, s - 150.0)
    # ⚠ 2026-10-21 改为**雪线相对高程**定位植被带（每档 ~600–800 m）。
    #   旧做法把整张植被色表压进 [dmin, top]：亚丁窗口从 3573 m 起，
    #   600 m 内塞完「深谷→森林→灌丛→草甸」，谷底直接取到 (58,96,72) 近黑绿，
    #   而 4110–4810 m 的大片高山带是中性灰 —— 整图偏灰、七月河谷不绿
    #   （用户原话："整体会不会有点儿偏灰""7 月河谷的草还没绿吗"）。
    #   相对高程后：3600 m 落在中绿草甸档、4200 m 才转黄绿，与实景一致。
    offs = (-3800.0, -3000.0, -2400.0, -1800.0, -1000.0, -700.0)
    veg = [(s + o, veg_cols[i]) for i, o in enumerate(offs)]
    veg = [(e, c) for e, c in veg if e <= top]            # 顶端以上的锚点丢掉
    if not veg:
        veg = [(top, veg_cols[-1])]
    if veg[0][0] > dmin:                                  # 最低锚点高于窗口底 → 补平到窗口底
        veg = [(dmin, veg[0][1])] + veg

    # ⚠ 高山段必须从植被顶端（top）开始、按比例铺到雪线 —— 不能写死 s−1300：
    #   夏季 top = s−700，写死的 s−1300/s−1000 会落进植被段里，两条曲线重叠，
    #   4050–4650 被灰褐"洗"回去（用户看到的"夏季发黄"就是这么来的）。
    alp_off = (0.0, 0.30, 0.55, 0.75, 0.90, 1.0)          # 占 (s − top) 的比例
    alpine = [(top + f * (s - top), alp_cols[i]) for i, f in enumerate(alp_off)]
    alpine += [
        (s + 400,  (242, 244, 249)),
        (s + 1000, (247, 248, 252)),
        (s + 2100, (250, 251, 254)),
    ]
    raw = sorted(veg + alpine, key=lambda t: t[0])
    stops = [raw[0]]
    for (e0, c0), (e1, c1) in zip(raw, raw[1:]):
        n = max(1, int(round((e1 - e0) / BELT_DENSIFY_M)))
        for i in range(1, n + 1):
            f = i / n
            stops.append((e0 + (e1 - e0) * f,
                          tuple(c0[k] + (c1[k] - c0[k]) * f for k in range(3))))
    if verbose:
        print(f"   地貌垂直带（{season}）：{dmin:.0f}–{dmax:.0f} m · 雪线 {s:.0f} m 以上转积雪白")
    _ramp_check(stops, s, verbose)
    return stops


# 晕渲调制：环境光比例越高整体越亮、对比越弱。
# 太高（>0.75）地形会"平"到看不见，太低（<0.4）背光面发黑、压掉等高线。
AMBIENT, DIRECTIONAL = 0.60, 0.50
HL_CLIP, HL_KEEP = 240.0, 0.35   # 高光软限幅：超 240 只保留 35%，亮而不死白
# 阴阳分色（瑞士晕渲的看家手法）：受光面微微偏暖、背光面微微偏冷。
# 纯灰阶晕渲会让人误以为"纸是灰的"，加一点点色温差，山体立刻像有阳光与阴影 ——
# 幅度必须很小（≤0.25），大了就成了"蓝紫色的假雪"。0 = 关闭。
TINT_COOL = 0.20     # 背光偏冷强度
TINT_WARM = 0.10     # 受光偏暖强度
TINT_COOL_RGB = (0.90, 0.95, 1.04)
TINT_WARM_RGB = (1.05, 1.00, 0.95)


def shade_rgb(rgb, hs, snow_w=None, snow_shape=(0.58, 0.60)):
    """分层设色 × 晕渲 → 明暗塑形 +（可选）阴阳分色。

    ⚠ 2026-11-04 **按行分块**实现：248 M px 的大窗口下，单个 (H,W,3) float32 临时数组
    就是 2.78 GB，原式 `img*(1-w) + img*c*w` 同时要 3 个 → 念青东五线总览实测连续 OOM。
    分块后峰值降到 ~1/6，代价只是多几次 for 循环（H=12012 时 6 块）。

    snow_w：雪线以上**强塑形**权重（0–1，由高程渐入）。方向是"雪面用更狠的
    环境光/方向光配比（0.58/0.60）塑形"—— 受光近白、背光中灰，冰面才有立体感。
    """
    rgb32 = np.asarray(rgb, np.float32)
    hs32 = np.asarray(hs, np.float32)
    sw = None if snow_w is None else np.asarray(snow_w, np.float32)
    H = rgb32.shape[0]
    img = np.empty_like(rgb32)
    CH = 2048                                    # 每块行数（块内临时数组 ≈ 0.47 GB）
    cool = np.asarray(TINT_COOL_RGB, np.float32) - 1.0
    warm = np.asarray(TINT_WARM_RGB, np.float32) - 1.0
    for y0 in range(0, H, CH):
        y1 = min(H, y0 + CH)
        r = rgb32[y0:y1]
        h = hs32[y0:y1]
        out = r * (AMBIENT + DIRECTIONAL * h)[..., None]
        if TINT_COOL > 0:
            t = np.clip((0.5 - h) * 2.0, 0, 1).astype(np.float32)[..., None]
            out *= 1.0 + cool * (TINT_COOL * t)      # 等价 img*(1-w) + img*c*w
            del t
        if TINT_WARM > 0:
            t = np.clip((h - 0.5) * 2.0, 0, 1).astype(np.float32)[..., None]
            out *= 1.0 + warm * (TINT_WARM * t)
            del t
        over = out > HL_CLIP
        out[over] = HL_CLIP + (out[over] - HL_CLIP) * HL_KEEP    # 高光软限幅
        if sw is not None:
            w = sw[y0:y1][..., None]
            shaped = np.clip(r * (snow_shape[0] + snow_shape[1] * h)[..., None], 0, 255).astype(np.float32)
            shaped -= out
            shaped *= w
            out += shaped
            del shaped, w
        img[y0:y1] = out
        del out, r, h
    return img


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


def _box_dilate(m, w):
    """方形（w×w）膨胀：线在**两个方向**都加粗 w px，宽度精确。

    为什么不能靠"整体错开 1 px"补偶数宽度：那样竖直线只会变长、不会变宽。
    """
    w = int(w)
    if w <= 1:
        return m
    H, W = m.shape
    p = np.zeros((H + w, W + w), bool)
    p[w:, w:] = m
    out = np.zeros_like(m)
    for dy in range(w):
        for dx in range(w):
            out |= p[dy:dy + H, dx:dx + W]
    return out


def _widen(m, w_px):
    """把 1 px 的线加宽到**精确** w_px 宽（单位：基准栅格像素）。

    为什么要有它：`_dilate_n` 是十字膨胀，宽度只能是奇数（1 / 3 / 5 …）。
    但「计曲线 2 px、首曲线 1 px，且**不随超采样变化**」这种诉求需要偶数宽度
    （2026-10-27 用户口径）。奇数宽度仍走十字膨胀（保持老观感），
    偶数宽度走方形膨胀（w×w，两个方向都精确 w px）。
    """
    w = int(w_px)
    if w <= 1:
        return m
    if w % 2 == 1:
        return _dilate_n(m, (w - 1) // 2)
    return _box_dilate(m, w)


def contour_ref_size(dem_shape):
    """等高线几何的**基准栅格**尺寸 (W, H) —— 宽恒 = LOGICAL_W × CONTOUR_REF_SS。

    与出图超采样倍率无关：超采样只决定"把这张掩膜放大几倍"，
    不参与等高距/线宽/陡坡省略的任何计算（见 CONTOUR_REF_SS 的说明）。
    """
    w = LOGICAL_W * CONTOUR_REF_SS
    h = max(1, int(round(dem_shape[0] * w / float(dem_shape[1]))))
    return w, h


def _upsample_mask(m, shape):
    """把基准栅格的线掩膜放大到出图栅格，**线占比守恒**。

    为什么不是简单阈值：双线性放大后取固定 128 会系统性变细（4× 实测 4.35% → 3.81%，
    掉 12%）—— 放大反而把线抹细，正是要避免的那种"随超采样变动"。
    这里用直方图反解阈值：选一个 thr 让放大后的线像素数等于基准栅格的线像素数，
    于是"放大 N 倍"严格等于"线宽放大 N 倍"。

    双线性而不是最近邻：SS=13 时每级放大 4.3×，最近邻会让斜向等高线变成 4 px 楼梯。
    """
    shape = (int(shape[0]), int(shape[1]))
    if tuple(shape) == tuple(m.shape):
        return m
    if not m.any():
        return np.zeros(shape, bool)
    im = Image.fromarray((m * 255).astype(np.uint8), "L")
    a = np.asarray(im.resize((shape[1], shape[0]), Image.BILINEAR))
    hist = np.bincount(a.ravel(), minlength=256)
    keep = a.size - np.cumsum(hist)          # keep[k] = 像素值 > k 的个数
    want = m.mean() * a.size                 # 目标线像素数（基准栅格的占比）
    thr = int(np.argmin(np.abs(keep - want)))
    return a > thr


def contour_plan(dem, lat_c, z, verbose=True):
    """等高线几何方案 —— **完全不看出图栅格 / 超采样倍率**。

    返回 (dem_ref, m_per_px, minor, major, cov, ref_w)。

    这是 2026-11-07「等高线不随超采变动」的落点：等高距选档、陡坡省略、
    线宽、雪线虚线节距全部只依赖这份 dem_ref（基准栅格），出图时才把掩膜放大。
    搬到这里而不是留在 build_terrain 里，是为了让 selftest 能在**不同
    HIKING_MAP_SS 下跑同一份地形**并断言选档结果逐字相等（回归守卫）。
    """
    ref_w, ref_h = contour_ref_size(dem.shape)
    dem_ref = np.asarray(
        Image.fromarray(np.asarray(dem, np.float32), "F").resize((ref_w, ref_h),
                                                                Image.LANCZOS),
        dtype=np.float32)
    # m_per_px 必须**先算**：选档判据要用它复刻陡坡省略（2026-10-25），否则算出来的
    # 覆盖率是"没省略的线数"，高山线会系统性低估实际密度、选粗一档。
    m_per_px = (156543.03392 * math.cos(math.radians(lat_c)) / (2 ** z)) / (ref_w / dem.shape[1])
    minor, major, cov = pick_contour_interval(dem_ref, verbose=verbose, m_per_px=m_per_px)
    return dem_ref, m_per_px, minor, major, cov, ref_w


def contour_coverage(dem_out, interval):
    """给定等高距，返回线像元占比（0–1）。就是 paint_contours 里那套 band 边缘。"""
    b = np.floor(dem_out / interval).astype(np.int32)
    e = np.zeros(b.shape, bool)
    e[1:, :] |= b[1:, :] != b[:-1, :]
    e[:, 1:] |= b[:, 1:] != b[:, :-1]
    return float(e.mean())


def _steep_keep(dem_out, m_per_px):
    """陡坡省略的**保留掩膜**（True = 该像元的首曲线仍画）。与 paint_contours 同一判据。"""
    gy, gx = np.gradient(dem_out.astype(np.float32))
    tan_slope = np.hypot(gx, gy) / float(m_per_px)
    return tan_slope < STEEP_SKIP_MINOR


def _cov_masked(dem_out, interval, keep=None):
    """band 边缘覆盖率；给了 keep 就只数保留下来的像元。"""
    b = np.floor(dem_out / interval).astype(np.int32)
    e = np.zeros(b.shape, bool)
    e[1:, :] |= b[1:, :] != b[:-1, :]
    e[:, 1:] |= b[:, 1:] != b[:, :-1]
    if keep is not None:
        e &= keep
    return float(e.mean())


def contour_actual_coverage(dem_out, interval, m_per_px=None, keep=None):
    """给定等高距，返回**图上真实的**线像元占比 —— 逐像素复刻 paint_contours 的判据。

    与 `contour_coverage()` 的唯一差别：扣掉陡坡省略（tan ≥ `STEEP_SKIP_MINOR` 不画首曲线）。
    2026-10-25 改用这个做选档判据，理由见 `COV_EST_TO_ACTUAL` / `COV_OVER_TOL` 的注释：
    缓坡地区两者几乎相等，陡峭山区能差 3–8 个百分点，用一个常数折算必然两头都不准。
    只算首曲线（与 paint_contours 打印的「线覆盖 X% 像素」同口径，计曲线是它的子集）。
    """
    if keep is None and m_per_px:
        keep = _steep_keep(dem_out, m_per_px)
    return _cov_masked(dem_out, interval, keep)


def _route_cfg(key, default):
    """读 route_def.CFG 里的线路级渲染配置（取不到就回退默认，脚本可独立运行）。

    实现已收敛到 `guide_common.cfg`（全技能唯一入口，见那里的说明）；
    保留这层薄封装，是为了不重写已有的十几处调用点。
    """
    return GC.cfg(key, default)


def nice_major(minor, ratio=5):
    """由首曲线间隔推**规范的计曲线间隔**：优先 minor×5，退而求其次 ×4、×3。

    为什么不能死守 ×5（2026-10-16 希夏邦马实测）：本区首曲线自适应到 **25 m** 时，
    25×5 = **125 m** 计曲线不是地形图的规范值（读数要写成"125 的倍数"），
    而 25×4 = **100 m** 既在 `CONTOUR_NICE` 里、又是 25 的整数倍，
    高程注记间隔也跟着回到 100 m —— 读者视角就是"每 100 m 一条粗线"，与常用地形图一致。
    20 m → 100（×5）、25 m → 100（×4）、50 m → 250（×5）、10 m → 50（×5）都落在 NICE 里。
    """
    for k in (ratio, ratio - 1, ratio - 2):
        if k >= 2 and minor * k in CONTOUR_NICE:
            return minor * k
    return minor * ratio


def pick_contour_interval(dem_out, verbose=True, m_per_px=None):
    """按窗口自适应选等高距（首曲线, 计曲线, 覆盖率）。

    ⚠ 这是"比例尺越大、等高线越密、地图越难看"的正解（2026-10-10 狼塔 C+V 踩坑）：
    等高距**不能写死**。等高线疏密 ≈ 每像素真实高差 / 等高距 —— 窗口一大，
    同样的地表起伏被压进同样多的输出像素，每像素跨过的等高线成倍增加，20 m 间隔
    必然糊成一片棕。判据直接落到可测的「线覆盖率」上：从最细候选往上试，
    第一个 ≤ 目标覆盖率的胜出。
    * 小窗口（亚丁 0.19°）：20 m → 21%，保留细节；
    * 长线（狼塔 0.81°）：20 m → 66%（糊），自动退到 50 m → 30%。
    * 高海拔雪原线（希夏邦马，2026-10-16）：20 m → 28.6% 仍在默认阈值内，
      但陡坡线成排、雪原上偏抢眼 → 线路级把目标收到 **0.26** → 退到 25 m（23.8%）。

    两级可配置（都在 `route_def.CFG`；2026-10-25 起全局默认即 0.20/0.15，
    未配的线路也走同一区间，线路级覆盖只为个别调整）：
      * `contour_target_cov`：本次的覆盖率**上限**（缺省 `CONTOUR_TARGET_COV`=0.20）；
      * `contour_minor`：直接指定首曲线间隔，跳过自适应（缺省 None = 自适应）；
      * `contour_cov_min`（2026-10-24 新增）：覆盖率**下限**（缺省 `CONTOUR_COV_MIN`=0.15）。
        配了之后不是"越细越好"，而是**在 [下限, 上限] 区间里挑一个覆盖率最接近中点的等高距**
        —— 疏密随地形自适应，但被夹在一个可读区间里（用户 2026-10-24："线覆盖上限 20%、
        下限 15%，动态调整"）。
        区间内没有候选时退到"≤ 上限里最接近下限的一档"；连最小间隔都超上限时取最粗的一档并告警。
    计曲线由 `nice_major()` 取规范值（不一定是 ×5，见该函数注释）。

    `m_per_px`（栅格米/像素）**决定判据口径**：给了就用 `contour_actual_coverage()`
    逐像素复刻绘制判据（含陡坡省略），这是唯一与"图上看到的密度"一致的口径；
    不给（老调用/自检）退回 band 边缘估计 × `COV_EST_TO_ACTUAL`，只作兜底。
    2026-10-25 普查结论：`COV_EST_TO_ACTUAL` 是**地形相关的**，用常数必然偏差
    —— 6 条高山线本该进更细一档、3 条该退档，都是它算错的。
    """
    target = float(_route_cfg("contour_target_cov", CONTOUR_TARGET_COV))
    forced = _route_cfg("contour_minor", None)
    if forced:
        iv = float(forced)
        cov = contour_actual_coverage(dem_out, iv, m_per_px)
        if verbose:
            print(f"   等高距由线路指定 → 首曲线 {iv:g} m / 计曲线 {nice_major(iv):g} m，"
                  f"线覆盖 {cov * 100:.1f}%")
        return iv, nice_major(iv), cov
    # 判据用**图上真实的**线覆盖（含陡坡省略）。拿不到 m_per_px 时退回旧估计 × 常数折算。
    if m_per_px:
        _keep = _steep_keep(dem_out, m_per_px)          # 斜率只算一次，8 个候选共用
        cands = [(iv, _cov_masked(dem_out, iv, _keep)) for iv in CONTOUR_NICE]

        def _act(cv):
            return cv                                   # 已经是实测口径，不再折算
    else:
        cands = [(iv, contour_coverage(dem_out, iv)) for iv in CONTOUR_NICE]

        def _act(cv):
            return cv * COV_EST_TO_ACTUAL
    # ⚠ 缺省必须是 CONTOUR_COV_MIN（0.15），不能是 None：2026-10-25 首发版在这里
    # 写了 None，未配 contour_cov_min 的 18 条线路静默走回旧单阈值行为，
    # 「下限 0.15」对它们形同未配（报告口径与实物不符）。
    lo = _route_cfg("contour_cov_min", CONTOUR_COV_MIN)
    if lo is not None:
        lo = float(lo)
        if 0 < lo < target:
            # 上限带容忍：`target + COV_OVER_TOL` 也算候选（见该常量注释）。
            hi = target + COV_OVER_TOL
            inside = [(iv, cv) for iv, cv in cands if lo <= _act(cv) <= hi]
            if inside:
                # 排序中点仍取**目标区间**的中点（不含容忍带）：
                # 容忍带只负责"别把超 1–2 pp 的好档扔掉"，不负责把最优往右拉。
                mid = (lo + target) * 0.5
                iv, cov = min(inside, key=lambda x: abs(_act(x[1]) - mid))
                if verbose:
                    _o = "（超上限 ≤%.1f pp，在容忍带内）" % (COV_OVER_TOL * 100) \
                        if _act(cov) > target else ""
                    print(f"   等高距动态 → 首曲线 {iv:g} m / 计曲线 {nice_major(iv):g} m，"
                          f"预计实际线覆盖 {_act(cov) * 100:.1f}%"
                          f"（区间 {lo * 100:.0f}–{target * 100:.0f}%，取最接近中点 {mid * 100:.0f}% 的一档{_o}）")
                return iv, nice_major(iv), cov
            below = [(iv, cv) for iv, cv in cands if _act(cv) < lo]
            if below:
                iv, cov = max(below, key=lambda x: _act(x[1]))   # 最接近下限的一档
                if verbose:
                    print(f"   等高距动态 → 首曲线 {iv:g} m / 计曲线 {nice_major(iv):g} m，"
                          f"预计实际线覆盖 {_act(cov) * 100:.1f}%；区间 {lo * 100:.0f}–"
                          f"{target * 100:.0f}% 内无候选，取 ≤ 上限里最接近下限的一档")
                return iv, nice_major(iv), cov
            iv, cov = min(cands, key=lambda x: _act(x[1]))          # 连最小间隔都超上限 → 取最粗
            if verbose:
                print(f"   ⚠ 等高距动态：所有候选间隔的线覆盖都 > {target * 100:.0f}%，"
                      f"取最粗的一档 {iv:g} m（预计实际线覆盖 {_act(cov) * 100:.1f}%）")
            return iv, nice_major(iv), cov
    fallback = (CONTOUR_NICE[-1], nice_major(CONTOUR_NICE[-1]), 1.0)
    for iv in CONTOUR_NICE:
        cov = contour_actual_coverage(dem_out, iv, m_per_px)
        if _act(cov) <= target:
            if verbose:
                tag = "（缺省）" if iv == CONTOUR_MINOR else \
                    f"（{CONTOUR_MINOR:.0f} m 覆盖率 " \
                    f"{_act(contour_actual_coverage(dem_out, CONTOUR_MINOR, m_per_px)) * 100:.0f}% 超出目标，自动退到 {iv:g} m）"
                print(f"   等高距自适应 → 首曲线 {iv:g} m / 计曲线 {nice_major(iv):g} m，"
                  f"线覆盖 {_act(cov) * 100:.1f}% ≤ 目标 {target * 100:.0f}% {tag}")
            return iv, nice_major(iv), cov
    return fallback


def _contour_tint(base, mode, dmin=None):
    """由底图颜色**派生**等高线颜色：同色相 + 降饱和 + 明度分档（暗处自动提亮）。

    dmin : 覆盖 CONTOUR_MIN_DELTA[mode]。线路级 `CFG["contour_min_delta"]` 用它把
           "保证看得见"的最小明度差压小 —— 这是**降低等高线存在感**最直接的一刀
           （线是否"跳"首先取决于它和底图差多少级明度，其次才是 alpha）。


    base : float32 (H, W, 3)  —— 已合成好、尚未画线的底图
    mode : 0 = 首曲线（浅），1 = 计曲线（深）

    为什么不能给全图一条固定色（2026-10-16 希夏邦马实测）：
    分层设色把本区铺成了"谷地草绿 → 灌丛黄绿 → 裸岩陶土 → 雪线以上冷白"，
    一条中性灰棕压上去，在冷白雪原上显得脏、在草甸上显得跳 —— 因为线的色相
    与所在区域的实际地貌色毫不相干。**线色随底图走，等高线才像"长在这片地上"**。

    明度仍是唯一的分级手段（首浅计深）：
      * 亮底（亮度 ≥ CONTOUR_LIGHT_T）：乘系数压暗 —— 雪原上压出冷灰蓝，读得出；
      * 暗底（背光面 / 深谷阴影）：改为**提亮** —— 这里有旧方案的隐藏坑：
        固定色在阴影里比底图还暗，一排线连起来就是"黑泥"，把晕渲的立体感吃掉；
      * CONTOUR_MIN_DELTA 兜底：中间调单纯乘法可能只差 5–6 级明度，肉眼读不出，
        强制拉开到最小差值，保证"处处看得到线"。
    """
    # ⚠ 2026-11-04 **按行分块**：230 M px 画幅下 `lum[bool]` 这类布尔索引一次就要
    #   783 MiB（实测直接 ArrayMemoryError），而且原写法同时要 3 个 (H,W)。
    #   分块后每块的临时数组 ≈ 160 MB，39 级等高线叠加也不再累积。
    k0, add, _dm = (CONTOUR_DARKEN[mode], CONTOUR_LIGHTEN[mode], CONTOUR_MIN_DELTA[mode])
    dmin = _dm if dmin is None else float(dmin)
    H = base.shape[0]
    CH = 2048
    wcol = np.asarray([0.299, 0.587, 0.114], np.float32)
    for y0 in range(0, H, CH):
        y1 = min(H, y0 + CH)
        blk = base[y0:y1]
        lum = blk @ wcol
        snow = np.clip((lum - 200.0) / 45.0, 0.0, 1.0)
        k = k0 + (SNOW_CONTOUR_DARKEN[mode] - k0) * snow
        del snow
        bright = lum >= CONTOUR_LIGHT_T
        tgt = lum * k
        alt = lum + add
        np.maximum(alt, lum + dmin, out=alt)                 # 暗底：提亮到至少 +dmin
        np.minimum(tgt, lum - dmin, out=tgt)                 # 亮底：压暗到至少 −dmin
        np.copyto(tgt, alt, where=~bright)
        del alt, bright
        np.clip(tgt, 6.0, 252.0, out=tgt)
        ratio = tgt / np.maximum(lum, 1.0)                   # 保住底图色相
        del lum
        blk *= ratio[:, :, None]
        del ratio
        blk *= (1 - CONTOUR_DESAT)                           # 彩度让给底图
        blk += tgt[:, :, None] * CONTOUR_DESAT
        del tgt
        np.clip(blk, 0, 255, out=blk)
    return base          # 就地改的是 base 的视图，调用方仍按返回值取色（别丢这一行）

def paint_contours(rgb, dem_out, w_minor=0, w_major=2,
                   minor=CONTOUR_MINOR, major=CONTOUR_MAJOR,
                   derived=CONTOUR_DERIVED, m_per_px=None,
                   alpha=None, dmin=None, w_px=None, raster_shape=None):
    """在输出分辨率上栅格化等高线并写入 RGB 数组。

    原理：band = floor(elev / 间隔)，相邻像素 band 不同即该处有一条等高线。
    全程 numpy 向量化（10^7 像素毫秒级），**不需要 matplotlib / GDAL / skimage**。
    计曲线是首曲线的子集（100 = 5×20），画在上层覆盖。

    derived=True 时线色由底图逐像素派生（见 `_contour_tint`），
    这是"等高线配色与该区域渲染颜色接近"的实现处。

    `dem_out` 必须是**基准栅格**的 DEM（见 `contour_ref_size`），不是出图栅格 ——
    线宽、陡坡省略、覆盖率因此与超采样倍率脱钩（2026-11-07）。给了
    `raster_shape=(H, W)` 就把掩膜放大到出图尺寸；不给则假定 `rgb` 与 `dem_out` 同尺寸。
    """
    b_minor = np.floor(dem_out / minor).astype(np.int32)
    b_major = np.floor(dem_out / major).astype(np.int32)
    # `w_px` 给的是**固定栅格 px** 的线宽（不随超采样倍率变）；
    # 不给时走老路径（十字膨胀次数，首线恒 1 px、计线随 SS 补偿）。
    if w_px is None:
        m_minor = _dilate_n(_band_edges(b_minor), w_minor)
        m_major = _dilate_n(_band_edges(b_major), w_major)
    else:
        m_minor = _band_edges(b_minor)
        m_major = _band_edges(b_major)
    n_skip = 0
    if m_per_px:
        # ---- 陡坡省略首曲线（2026-10-20 贡嘎大环）----
        # 25 m 等高距在 ~11 m/px 栅格上，45° 坡处相邻两线只隔 2.3 px —— 画出来不是等高线，
        # 是"布纹"：整片雪原/陡壁变成均匀灰网纹。制图惯例本就是"陡坡处等高线自然合并"：
        #   tan ≥ 1.0(45°) → 不画首曲线（只留计曲线，100 m 间距仍有 ~9 px）
        #   tan ≥ 2.0(63°) → 计曲线也不画（陡崖区用断崖符号表达）
        gy, gx = np.gradient(dem_out.astype(np.float32))
        tan_slope = np.hypot(gx, gy) / float(m_per_px)
        keep_minor = tan_slope < STEEP_SKIP_MINOR
        keep_major = tan_slope < STEEP_SKIP_MAJOR
        n_skip = int((m_minor & ~keep_minor).sum())
        m_minor &= keep_minor
        m_major &= keep_major
        # 立即释放：这三个 float32 全幅数组 ≈ 3.4 GB/张（16000×18770），
        # 下面 f / base / col 还要各占一张同尺寸的图 —— 不删就会撞进程内存上限。
        del gy, gx, tan_slope, keep_minor, keep_major
    if w_px is not None:
        # 陡坡省略先作用在 1 px 原始边线上，再按线路指定的固定 px 宽加粗 ——
        # 这样"陡坡省略"仍然生效（只是加宽后的线会向省略区伸出 1 px，属正常制图行为）。
        m_minor = _widen(m_minor, int(w_px[0]))
        m_major = _widen(m_major, int(w_px[1]))
    if raster_shape is not None:
        _rs = (int(raster_shape[0]), int(raster_shape[1]))
        if _rs != m_minor.shape:
            # 基准栅格掩膜 → 出图栅格：超采样只放大掩膜，不改几何（见 CONTOUR_REF_SS）。
            # 放大在加宽之后做，所以线宽的相对值是"基准 px / 基准宽"，与 SS 无关。
            m_minor = _upsample_mask(m_minor, _rs)
            m_major = _upsample_mask(m_major, _rs)
    # alpha 融合写回（rgb 是 uint8，必须过 float，否则整数回绕会把线画成黑块）
    f = rgb.astype(np.float32)
    base = f.copy() if derived else None
    # alpha / dmin 可被线路级 CFG 覆盖（`contour_alpha` / `contour_min_delta`）：
    # 用户说"等高线太明显"时，这两个是**只调视觉强度、不动几何**的旋钮。
    # 优先级：alpha 参数 > CFG > 全局常量。
    if alpha is None:
        alpha = _route_cfg("contour_alpha", None) or (A_MINOR, A_MAJOR)
    if dmin is None:
        dmin = _route_cfg("contour_min_delta", None)
    _a = (float(alpha[0]), float(alpha[1]))
    _d = None if dmin is None else tuple(dmin)
    for mask, mode, a in ((m_minor, 0, _a[0]), (m_major, 1, _a[1])):
        if derived:
            col = _contour_tint(base, mode, None if _d is None else _d[mode])[mask]
        else:
            col = np.asarray(C_MINOR if mode == 0 else C_MAJOR, np.float32)
        f[mask] = f[mask] * (1 - a) + col * a
    # ⚠ 就地 clip + 先出 uint8 再 copyto：`np.clip(f,0,255).astype(uint8)` 会同时
    #   持有 f(3.36 GB) + clip 副本(3.36 GB) + uint8(1.1 GB)。全幅大图（>250 MP）
    #   上这就是压垮进程的那一根稻草（2026-11-05 伯舒拉岭三线总览实测）。
    np.clip(f, 0, 255, out=f)
    np.copyto(rgb, f.astype(np.uint8))
    n_lv = int((dem_out.max() - dem_out.min()) // minor)
    print(f"   等高线 每 {minor:.0f} m（计曲线每 {major:.0f} m），约 {n_lv} 级，"
          f"线覆盖 {m_minor.mean()*100:.1f}% 像素"
          + (f"，陡坡省略 {n_skip*100.0/max(1, m_minor.size):.1f}%" if n_skip else ""))
    return rgb


# ------------------------------------------------------------------ 雪线
# 2026-10-16 联网核实（来源清单见 route_def.py 头部）：希夏邦马峰区域
# **南坡雪线 ≈5000 m、北坡 ≈6000 m**（西藏在线 / 维基 / 冰川群资料口径一致；
# 野博康加勒冰川雪线即 6000 m、末端 5530 m）。
# 画法：在 DEM 的 6000 m 等高线上叠一条**蓝色虚线** + 沿线注记。
# 为什么只画 6000 这一条：本线绝大多数路程位于北坡；南坡的 5000 m 只写进注记
# 文字 —— 否则读者会在北坡地形上把随处可见的 5000 m 等高线误读成雪线。
# ⚠ 线路级常量：换线路必须按山脉查表重设（当前残留的是珠峰线取值）。
#    中国各山脉现代雪线见 references/china-terrain.md §4（阿尔泰 3000–3200 / 天山 3600–4400 /
#    祁连 4300–5240 / 喜马拉雅南坡≈4500、北坡 5900–6000 …），绝不能沿用上一座山的值。
SNOWLINE_LEVEL = 6000.0     # ← fallback 值，且**只在你确认过新线路雪线时配 CFG['snowline_m']**。
# ⚠ 缺省文案必须带 {:.0f} 占位符（或干脆不带米数），**不能写死某座山的高度** ——
#   否则换线路改了 CFG['snowline_m']，虚线画在新高度上，字却还写着旧米数（图文打架）。
#   正确做法：在 route_def.CFG 里给 snowline_label / snowline_note（写清坡向与数据出处）；
#   不配就用这里的通用模板，米数由实际 level 生成，永远与画的线一致。
SNOWLINE_LABEL = "雪线 ≈{:.0f} m"
SNOWLINE_NOTE = ""
C_SNOWLINE = (26, 106, 186)
A_SNOWLINE = 0.88


def _snowline_texts(level):
    """雪线注记的 (主标, 副注)。

    主标：CFG['snowline_label']（含 `{}` 时按 level 格式化）→ 缺省用 SNOWLINE_LABEL 模板。
    副注：CFG['snowline_note'] → 退到 CFG['snowline_src']（自动包括号）→ 缺省 SNOWLINE_NOTE。
    取不到 route_def 也不报错（脚本可独立运行）。
    """
    cfg = {}
    try:
        import route_def as _RD
        cfg = getattr(_RD, "CFG", None) or {}
    except Exception:                                              # noqa
        pass
    label = cfg.get("snowline_label") or SNOWLINE_LABEL
    try:
        label = label.format(level) if "{" in label else label
    except Exception:                                              # noqa
        label = f"雪线 ≈{level:.0f} m"
    note = cfg.get("snowline_note")
    if note is None:
        src = cfg.get("snowline_src")
        note = f"（{src}）" if src else SNOWLINE_NOTE
    return label, note


def _snowline_from_cfg():
    """雪线高度优先取 route_def.CFG['snowline_m']（动态联网值），取不到回退内置默认。
    雪线是年际变化量：换地区 / 数据更新只改 route_def，不必动渲染脚本。"""
    try:
        import route_def as _RD
        cfg = getattr(_RD, "CFG", None) or {}
        v = cfg.get("snowline_m")
        if v:
            return float(v)
    except Exception:
        pass
    return SNOWLINE_LEVEL


def _box_blur_f32(a, r, passes=3):
    """可分离盒式模糊（cumsum 实现，不依赖 scipy）。

    三次盒式 ≈ 一次高斯（中心极限），用于**平滑高程面再取阈值**：
    雪线掩膜是「below/above 的交界」，高原面上 5350 m 这条面会绕出上千个小闭环，
    叠上 18 px 的虚线节距就是满屏蓝色方块麻点（2026-11-07 实测）。先低频再取等值线，
    得到的就是一条能读的地貌界线 —— 这正是 1:50 万地形图上雪线的画法。
    """
    r = int(r)
    if r < 1:
        return a
    out = a.astype(np.float32, copy=True)
    H, W = out.shape
    for _ in range(passes):
        # 行方向
        p = np.pad(out, ((0, 0), (r + 1, r)), mode="edge")
        cs = np.cumsum(p, axis=1, dtype=np.float32)
        out = (cs[:, 2 * r + 1:] - cs[:, :W]) / (2 * r + 1)
        # 列方向
        p = np.pad(out, ((r + 1, r), (0, 0)), mode="edge")
        cs = np.cumsum(p, axis=0, dtype=np.float32)
        out = (cs[2 * r + 1:, :] - cs[:H, :]) / (2 * r + 1)
    return out


def paint_snowline(arr, dem_out, level=SNOWLINE_LEVEL, raster_shape=None, smooth_px=0):
    """雪线：DEM level 等高线处叠蓝色虚线（arr 原地改）。返回 mask；高度不在窗口内返回 None。

    与 paint_contours 同一套 band-edge 原理，但用横向条纹打掩码变成虚线，
    并加粗一格 —— 缩图进长图后仍要看得出来。

    `dem_out` 与 `paint_contours` 一样必须是**基准栅格**的 DEM：
    虚线节距 9/18 px 也是"基准 px"，出图时随掩膜一起放大（否则 SS=13 时
    18 px 节距只有 1.4 逻辑 px，虚线糊成实线）。给了 `raster_shape=(H, W)` 就放大。
    """
    below = (dem_out if not smooth_px else _box_blur_f32(dem_out, smooth_px)) < level
    cross = np.zeros(dem_out.shape, bool)
    cross[:-1, :] |= below[:-1, :] != below[1:, :]
    cross[:, :-1] |= below[:, :-1] != below[:, 1:]
    cross = _dilate(cross)
    if not cross.any():
        return None
    # 虚线节距（基准栅格 px）：14 周期里亮 8。原来是 18/9 —— 在大窗口总览上
    # 一个「实段」折到图上近 4 km 长，雪线看起来像一串蓝色小方块而不是一根线。
    _sp = int(_route_cfg("snowline_dash_period", 14) or 14)
    _so = int(_route_cfg("snowline_dash_on", 8) or 8)
    stripe = (np.arange(cross.shape[1])[None, :] % max(_sp, 4)) < max(1, min(_so, _sp - 1))
    mask = cross & stripe
    if raster_shape is not None:
        _rs = (int(raster_shape[0]), int(raster_shape[1]))
        if _rs != mask.shape:
            mask = _upsample_mask(mask, _rs)
    f = arr.astype(np.float32)
    # 强度可按线路覆盖（CFG["snowline_alpha"]）：0.88 是"单线雪线要一眼看见"的口径；
    # 大窗口总览上雪线如果和轨迹同权重，整幅会被蓝虚线框满（2026-11-10 GHT 实测），
    # 降到 0.6–0.7 后它退成"地貌界线"而不是前景符号。
    _sa = float(_route_cfg("snowline_alpha", A_SNOWLINE) or A_SNOWLINE)
    f[mask] = f[mask] * (1 - _sa) + np.asarray(C_SNOWLINE, np.float32) * _sa
    # ⚠ 就地 clip：`np.clip(f,…).astype(uint8)` 在 226 MP 上要额外 900 MB 的 clip 副本
    #   （与 paint_contours 同一个坑，见那里的注释）。
    np.clip(f, 0, 255, out=f)
    np.copyto(arr, f.astype(np.uint8))
    return mask


def draw_snowline_label(pil, dem, level=SNOWLINE_LEVEL, scale=(1.0, 1.0), avoid=None,
                        verbose=True):
    """沿雪线放一处注记：蓝字 + 纸色描边、随线旋转，下方附口径小字（不旋转）。

    几何与 draw_contour_labels 同源（_march → _join → _rdp），但只标一次、
    倾角容忍放宽到 45°（名字不像高程数字那样必须水平才读得出）。
    返回 (pil, 占位网格) —— 网格交给 OSM 注记继续避让。
    """
    sx, sy = scale
    S = SS
    # 注记文案按**实际画的这条线的高度**生成，杜绝"线在新高度、字写旧米数"。
    _lab, _note = _snowline_texts(level)
    f = _label_font(max(11, int(round(LABEL_FONT * 1.25 * S))))
    fn = _label_font(max(8, int(round(LABEL_FONT * 0.8 * S))))
    stroke = max(2, int(round(1.15 * S)))   # 白描边 1.7→1.15*S
    W, H = pil.size
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

    _w_init(dem)
    polys = _w_level(level)
    if not polys:
        if verbose:
            print(f"   雪线 {level:.0f} m：窗口内无折线，跳过注记")
        return pil, set()

    tw, th, _ = _text_size(f, _lab, stroke)
    nw, nh, _ = _text_size(fn, _note, 1)
    best = None
    for P0 in polys:
        P = np.stack([P0[:, 0] * sx, P0[:, 1] * sy], 1)
        dseg = np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(dseg)])
        total = float(cum[-1])
        need = tw + 40 * S
        if total < need:
            continue
        s = need * 0.5
        while s < total - need * 0.4:
            i0 = int(np.searchsorted(cum, max(0.0, s - 30 * S)))
            i1 = int(np.searchsorted(cum, min(total, s + 30 * S)))
            if i1 - i0 < 2:
                break
            dx, dy = P[i1, 0] - P[i0, 0], P[i1, 1] - P[i0, 1]
            if dx < 0:
                dx, dy = -dx, -dy
            ang = math.degrees(math.atan2(dy, dx))
            if abs(ang) > 45:
                s += 40 * S
                continue
            j = min(max(int(np.searchsorted(cum, s)), 1), len(P) - 1)
            t = (s - cum[j - 1]) / max(1e-6, cum[j] - cum[j - 1])
            x = P[j - 1, 0] + (P[j, 0] - P[j - 1, 0]) * t
            y = P[j - 1, 1] + (P[j, 1] - P[j - 1, 1]) * t
            hw, hh = tw / 2 + 8 * S, th / 2 + 16 * S
            if x - hw < 4 or x + hw > W - 4 or y - hh < 4 or y + hh > H - 4:
                s += 40 * S
                continue
            if blocked(x - hw, y - hh, x + hw, y + hh):
                s += 40 * S
                continue
            best = (x, y, ang)
            break
        if best:
            break

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not best:
        if verbose:
            print(f"   雪线 {level:.0f} m：没找到合适注记位（避让约束太紧），仅画虚线")
        return Image.alpha_composite(pil.convert("RGBA"), layer).convert("RGB"), set()

    x, y, ang = best
    pad = stroke + 4
    tmp = Image.new("RGBA", (int(tw + 2 * pad), int(th + 2 * pad)), (0, 0, 0, 0))
    _, _, bb = _text_size(f, _lab, stroke)
    ImageDraw.Draw(tmp).text((pad - bb[0], pad - bb[1]), _lab, font=f,
                             fill=C_SNOWLINE + (255,), stroke_width=stroke,
                             stroke_fill=LABEL_HALO + (255,))
    tmp = tmp.rotate(-ang, resample=Image.BICUBIC, expand=True)
    layer.alpha_composite(tmp, (int(x - tmp.width / 2), int(y - tmp.height / 2)))
    # 口径小字跟在主标下方，不旋转（小字旋转后难读）
    ImageDraw.Draw(layer).text((int(x - nw / 2), int(y + th * 0.9 + 6 * S)),
                               _note, font=fn, fill=C_SNOWLINE + (255,),
                               stroke_width=max(1, int(round(1.2 * S))),
                               stroke_fill=LABEL_HALO + (255,))
    bw = max(tmp.width, nw) / 2 + 6 * S
    bh = tmp.height / 2 + nh + 12 * S
    grid = set()
    for cx in range(int((x - bw) // cell), int((x + bw) // cell) + 1):
        for cy in range(int((y - bh) // cell), int((y + bh) // cell) + 1):
            grid.add((cx, cy))
    if verbose:
        print(f"   雪线 {level:.0f} m：蓝色虚线 + 注记「{_lab}」"
              f"@({x:.0f},{y:.0f}) {_note}")
    return Image.alpha_composite(pil.convert("RGBA"), layer).convert("RGB"), grid


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


# ---- 标注几何两阶段并行化（2026-10-11）----
# 每个等级的 marching→join→rdp 相互独立且占大头（实测 ~12s/19 级），
# 拆成「并行算几何 → 按原序串行占位」：占位逻辑一行不改，输出与纯串行完全一致。
_W_DEM = None


def _w_init(dem):
    """worker 初始化：**只传路径，不传数组**。

    ⚠ 2026-11-09 实测：旧写法 `initargs=(dem,)` 会把整个 DEM（大窗口 z14 时 1.2 GB）
    pickle 给 4 个子进程。Windows 上这条管道传不完 → `UnpicklingError: pickle data
    was truncated`，异常被外层 `except` 吞掉后**静默退回串行**，日志只留一行
    「并行几何不可用」。GHT 全线（19552 px）每次跑都命中，等于白丢 3 倍速度。
    → 改成主进程 np.save 一次，worker 用 `mmap_mode="r"` 只读映射（不复制数据）。
    串行兜底路径仍可直接传 ndarray。
    """
    global _W_DEM
    if isinstance(dem, (str, bytes)) or hasattr(dem, "__fspath__"):
        _W_DEM = np.load(str(dem), mmap_mode="r")
    else:
        _W_DEM = dem


def _w_level(lv):
    """worker：单一计曲线等级的几何（DEM 坐标折线列表）。"""
    out = []
    for ch in _join(_march(_W_DEM, lv)):
        P = _rdp(np.asarray(ch, np.float32), 0.8 * MS_STEP)
        if len(P) >= 2:
            out.append(P)
    return out


def draw_contour_labels(pil, dem, scale, lo, hi, avoid=None, verbose=True, every=None):
    """在计曲线上断线标注高程数字。

    * 只在**计曲线**（每 `every` m，缺省 LABEL_EVERY）上标；
    * 沿折线按弧长取候选位，但要求局部接近水平（LABEL_MAX_ANGLE），不然斜着读不出；
    * 与实测轨迹/攻略 POI 冲突的位置直接跳过（`avoid`），免得数字被粗线路切断；
    * 数字用"纸色描边"压掉底下的线 —— 这是纸质地形图的标准断线画法，比加白底方块干净。

    scale: (sx, sy) = DEM 像素 → 栅格像素；avoid: [(x, y, r)] 栅格像素。
    every: 计曲线间隔（= 本次自适应选定的值），必须与 paint_contours 用的 major 一致，
           否则数字会落在没有加粗线的位置上（"数字浮在图上"）。
    返回：已占位的网格单元集合（cell = 14*SS），供 OSM 注记继续避让。
    """
    every = float(every if every else LABEL_EVERY)
    sx, sy = scale
    S = SS
    # 计曲线数字字号：默认 LABEL_FONT（8.2 逻辑 px）。CFG["contour_label_font"]
    # 可按线路覆写（逻辑 px）—— 大等高距合幅（500 m 档）里 8.2 会与峰名同大、
    # 比 OSM 地名还抢眼，全线实测收到 6.4 后层次恢复（2026-11-08）。
    _lf = _route_cfg("contour_label_font", None)
    f = _label_font(max(7, int(round((float(_lf) if _lf else LABEL_FONT) * S))))
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

    lv0 = math.ceil(lo / every) * every
    lv1 = math.floor(hi / every) * every
    levels = [float(v) for v in np.arange(lv0, lv1 + 1e-6, every)]

    # 阶段 A：各等级几何并行算（进程池；任何异常都退回串行——结果一致，只是慢）
    per_lv = None
    if len(levels) >= 3:
        _tmp_npy = OUT / "_dem_worker.npy"
        try:
            import multiprocessing as mp
            n = min(4, mp.cpu_count() or 2)
            if n >= 2:
                # DEM 落盘一次、worker mmap 只读映射（详见 _w_init 的注释：
                # 直接传数组会在 Windows 上截断后静默退回串行）。
                np.save(_tmp_npy, dem)
                with mp.Pool(n, initializer=_w_init, initargs=(str(_tmp_npy),)) as pool:
                    per_lv = pool.map(_w_level, levels, chunksize=1)
        except Exception as e:
            print(f"   （并行几何不可用：{type(e).__name__}: {e} —— 退回串行，结果不变）")
            per_lv = None
        finally:
            try:
                _tmp_npy.unlink()
            except OSError:
                pass
    if per_lv is None:
        _w_init(dem)
        per_lv = [_w_level(lv) for lv in levels]

    # 阶段 B：按原序贪心占位（与旧串行版逐位一致）
    for lv, polys in zip(levels, per_lv):
        txt = f"{int(round(lv))}"
        tw, th, _ = _text_size(f, txt, stroke)
        for P0 in polys:
            P = np.stack([P0[:, 0] * sx, P0[:, 1] * sy], 1)
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
        print(f"   标注 {n_lab} 个（{n_line} 条计曲线，间隔 {every:g} m）{chk}")
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


# ------------------------------------------------------------------ 高海拔水面过滤

# ---- OSM「可绘制集」瘦身（2026-11-09）----
# 背景：大窗口（8.4°×3.4°）的 osm.json 是 749 MB / 47.8 万要素，其中**真正会画的不到
# 3 万**。这不只是磁盘问题 —— 下游有四道工序各自 `json.loads(read_text())` 整读一遍
# （水面过滤 → 峰点人工修正 → 峰点吸附 → draw_overlay），每遍 13 s + 3–4 GB 常驻，
# 再各自整写一份 780 MB 副本。
# → 这里一次性筛出「bucket() 可能画到的」要素，坐标压到 5 位小数（≈1 m，远小于 1 px），
#   写成 osm_slim.json；后面四道全部只吃这份（预计 749 MB → 30–60 MB）。
#   白名单是 bucket() 判据的**超集**，宁可多留不可漏：判据一旦比 bucket 窄，
#   就会出现「要素在文件里、图上却没了」的静默丢图层。
_SLIM_ROAD = None          # 延迟到运行时从 draw_osm 取（避免循环导入）


def _slim_sig():
    """瘦身口径指纹：类别白名单一变，缓存必须失效（否则改了 CFG 图却没变）。"""
    _r = GC.cfg("osm_road_classes", None)
    _t = GC.cfg("osm_trail_classes", None)
    _w = GC.cfg("osm_water_classes", None)
    return json.dumps([sorted(_r) if _r else None,
                       sorted(_t) if _t else None,
                       sorted(_w) if _w else None], ensure_ascii=False)


def _slim_osm(src, dst, verbose=True):
    """把 osm.json 裁成「**本次绘制口径**真正会用到」的要素集。

    ⚠ 白名单必须**跟随 CFG**，不能只按样式表：ROAD_STYLE/TRAIL_STYLE 里含
    residential / track / path 等，按样式表留等于什么都没裁 —— 第一版实测
    749 MB → 653 MB（只掉 13%），因为 43 万个 highway way 几乎全被留下。
    2026-11-10 改为读 osm_road_classes / osm_trail_classes / osm_water_classes，
    与 draw_osm.bucket() 同一口径（超集由这三个开关本身保证）。
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return src
    sig = _slim_sig()
    sig_p = dst.with_suffix(".sig")
    try:
        fresh = (dst.exists() and sig_p.exists()
                 and dst.stat().st_mtime >= src.stat().st_mtime
                 and sig_p.read_text(encoding="utf-8") == sig)
        if fresh:
            if verbose:
                print(f"   复用瘦身要素集 {dst.name} "
                      f"（{dst.stat().st_size / 1048576:.0f} MB，口径与源均未变）")
            return dst
    except OSError:
        pass

    from draw_osm import ROAD_STYLE, TRAIL_STYLE
    _rc = GC.cfg("osm_road_classes", None)
    _tc = GC.cfg("osm_trail_classes", None)
    _wc = GC.cfg("osm_water_classes", None)
    _rc = None if _rc is None else set(_rc)
    _tc = None if _tc is None else set(_tc)
    _wc = None if _wc is None else set(_wc)
    _node_keys = ("place", "natural", "tourism", "amenity", "man_made",
                  "historic", "railway", "aeroway", "highway", "waterway",
                  "mountain_pass", "ele", "boundary")

    def keep(e):
        t = e.get("tags") or {}
        if not t:
            return False
        if e.get("type") == "node":
            return any(k in t for k in _node_keys)
        if t.get("boundary") == "administrative" and str(t.get("admin_level")) == "2":
            return True
        h = t.get("highway")
        if h in ROAD_STYLE and (_rc is None or h in _rc):
            return True
        if h in TRAIL_STYLE and (_tc is None or h in _tc):
            # CFG 未配（None）= 与 draw_osm 一样"全画"；配成 [] 则一条不留。
            return True
        w = t.get("waterway")
        if w and (_wc is None or w in _wc):
            return True
        if t.get("natural") in ("water", "glacier", "wood"):
            return True
        if t.get("landuse") in ("reservoir", "forest", "meadow"):
            return True
        return False

    def rnd(x):
        return None if x is None else round(x, 5)

    t0 = time.time()
    d = json.loads(src.read_text(encoding="utf-8"))
    els = d.get("elements") or []
    out = []
    for e in els:
        if not keep(e):
            continue
        if "lat" in e:
            e["lat"], e["lon"] = rnd(e["lat"]), rnd(e["lon"])
        g = e.get("geometry")
        if g:
            for q in g:
                q["lat"], q["lon"] = rnd(q.get("lat")), rnd(q.get("lon"))
        out.append(e)
    d["elements"] = out
    dst.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    try:
        sig_p.write_text(sig, encoding="utf-8")
    except OSError:
        pass
    if verbose:
        print(f"   瘦身要素集：{len(els)} → {len(out)} 条 · "
              f"{src.stat().st_size / 1048576:.0f} → "
              f"{dst.stat().st_size / 1048576:.0f} MB · {time.time() - t0:.1f}s")
    del d, els, out
    return dst


def _drop_high_alt_water(osm_path, dem, bbox, snow_m, out_path):
    """雪线以上的小水面直接剔除（仅大窗口总览图启用）。

    OSM 在青藏高原/喜马拉雅标了上千个冰面湖/冰面塘（supraglacial pond）。在
    几百 km 的幅面上，它们就是雪山里一个深色麻点，读者会当成渲染错误
    （2026-10-21 尼泊尔总览用户圈图，雪线以上水面 1391 个）。

    判据：水面重心所在栅格海拔 > 雪线 − 300 m 的一律不画；但**有名湖/大湖保留**
    （浪强错 4570 m ≈ 42 km² 就在雪线上沿，是希夏邦马一带的地标）。佩枯错、
    玛旁雍错等大湖海拔低于雪线，天然不受影响。
    """
    import json as _json
    x0, y0, x1, y1 = bbox
    H, W = dem.shape
    d = _json.loads(Path(osm_path).read_text(encoding="utf-8"))
    keep, dropped = [], 0
    for e in d.get("elements", []):
        t = e.get("tags", {})
        is_w = (t.get("natural") == "water" or t.get("landuse") == "reservoir"
                or t.get("waterway") == "riverbank")
        if is_w and isinstance(t, dict):
            pts = []
            def _walk(x):
                if isinstance(x, dict):
                    if "lat" in x:
                        pts.append((x["lon"], x["lat"]))
                    else:
                        for v in x.values():
                            _walk(v)
                elif isinstance(x, list):
                    for v in x:
                        _walk(v)
            _walk(e.get("geometry") or e.get("members") or [])
            if len(pts) >= 3:
                lon = sum(q[0] for q in pts) / len(pts)
                lat = sum(q[1] for q in pts) / len(pts)
                px, py = int((lon - x0) / (x1 - x0) * W), int((y1 - lat) / (y1 - y0) * H)
                if 0 <= py < H and 0 <= px < W and dem[py, px] > snow_m - 300:
                    # 保护：有名字的湖、或面积够大的湖是地标，删了反而是信息损失；
                    # 要删的是无名的冰面小塘。
                    _xs = [q[0] for q in pts]
                    _ys = [q[1] for q in pts]
                    _area = (max(_xs) - min(_xs)) * (max(_ys) - min(_ys))
                    if not (t.get("name") or _area > 0.0015):      # ≈ 20 km²
                        dropped += 1
                        continue
        keep.append(e)
    _json.dump({"elements": keep}, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
    if dropped:
        print(f"   雪线以上水面 {dropped} 个 → 已剔除")
    return out_path


# ------------------------------------------------------------------ 主流程
def _overlay(pil, m, dem, meta, bbox, SS, scale_raster, seed_cells, snow_m):
    """把 OSM 矢量层叠到地形层上（含水面过滤 / 峰点修正与吸附）并返回 pil。

    抽成函数是为了让 `--osm-only` 能复用已经算好的地形层：改注记、线宽、类别口径时
    不必重跑 DEM 下载、分层设色、晕渲、雪线（那几步占一次出图的 ~80% 时间）。
    """
    LON0, LAT0, LON1, LAT1 = bbox
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
            # 2026-10-17 念青东：音译差异会让同一个山/村在 OSM 与本线 POI 下各画一遍
            # （OSM「巴拉拉垭口」vs 攻略「巴巴拉垭口」）。route_def 可配 OSM_SKIP
            # 显式抑制 OSM 那一条 —— 全串精确匹配拦不住音译，只能手工列。
            for _s in (getattr(_RD, "OSM_SKIP", []) or []):
                skip.add(str(_s).strip())
        except Exception:                                          # noqa
            skip = set()
        # 先把 749 MB 裁成「会画到的几万条」，后面水面过滤/峰点修正/吸附/绘制
        # 四次整读都只吃这份小的（详见 _slim_osm 的注释）。
        _osm_draw = OUT / "osm.json"
        try:
            _osm_draw = _slim_osm(_osm_draw, OUT / "osm_slim.json")
        except Exception as _e:                                        # noqa
            print(f"   （要素集瘦身跳过：{_e}）")
            _osm_draw = OUT / "osm.json"
        try:
            if (LON1 - LON0) > 1.5:      # 只在"大窗口总览图"启用
                _osm_draw = _drop_high_alt_water(_osm_draw, dem, (LON0, LAT0, LON1, LAT1),
                                                 snow_m, OUT / "osm_draw.json")
        except Exception as _e:                                        # noqa
            print(f"   （水面过滤跳过：{_e}）")
        # ---- 峰点人工修正（CFG["peak_fix"] = {"峰名": [lon, lat]}）----
        # 用在**OSM 录入明显错、自动吸附又被 400 m 护栏挡住**的那几个峰上：
        # 把人工核对过的坐标写死，其余要素原样。与 peak_snap 不冲突 —— 改完坐标后
        # 吸附在新位置重新判一次，差值 <min_gain 就自动不动。
        # ⚠ 只按 `natural=peak` + 名字全串匹配，别指望它改路/村。
        try:
            import route_def as _RD4
            _fix = (getattr(_RD4, "CFG", None) or {}).get("peak_fix") or {}
        except Exception:                                          # noqa
            _fix = {}
        if isinstance(_fix, dict) and _fix:
            try:
                _dd = json.loads(_osm_draw.read_text(encoding="utf-8"))
                _nfix = 0
                for _e in _dd.get("elements") or []:
                    _t = _e.get("tags") or {}
                    if _t.get("natural") != "peak":
                        continue
                    _nm = (_t.get("name") or _t.get("name:en") or "").strip()
                    if _nm not in _fix:
                        continue
                    _lo, _la = float(_fix[_nm][0]), float(_fix[_nm][1])
                    print(f"   峰点人工修正：{_nm}  {_e.get('lon'):.5f},{_e.get('lat'):.5f}"
                          f" → {_lo:.5f},{_la:.5f}")
                    _e["lon"], _e["lat"] = _lo, _la
                    _nfix += 1
                if _nfix:
                    _fixed = OUT / "osm_fixed.json"
                    _fixed.write_text(json.dumps(_dd, ensure_ascii=False), encoding="utf-8")
                    _osm_draw = _fixed
                else:
                    print("   ⚠ CFG['peak_fix'] 里的名字在本窗口一个都没匹配上（检查措辞）")
            except Exception as _e:                                # noqa
                print(f"   （峰点人工修正跳过：{_e}）")
        # 峰点校正：OSM 的人工峰点常漂在山腰（博格达 7 个具名峰里 3 个偏 350–800 m），
        # 而等高线是 DEM 画的 —— 不校正就会出现"三角符号不在地形顶上"。
        snap = {}
        if GC.cfg_flag("peak_snap", True):
            try:
                # 校正灵敏度可由线路覆盖：CFG["peak_snap_min_gain"]（默认 200 m）。
                # 200 m 太宽松 —— 节点在坡上、离真顶差 50–150 m 也不会动（亚丁 9 个峰点
                # 实测差 12–137 m）。用户要求"三角在等高线高点"，收到 50 m 更贴合。
                _min_gain = float(GC.cfg("peak_snap_min_gain", 200.0) or 200.0)
                # 位移上限：默认 400 m，`None` 表示不限（单线验证过再开 —— 冈仁波齐
                # 9 个峰点位移 93–1081 m 全是真实漂移）。
                # ⚠ 这里**不能**用 GC.cfg()：它把显式 None 当"没配"返回 default，
                #   于是"写 None 想关掉护栏"会静默变回 400。必须查键是否存在。
                try:
                    import route_def as _RD
                    _raw = getattr(_RD, "CFG", None) or {}
                    _max_shift = _raw["peak_snap_max_shift"] \
                        if "peak_snap_max_shift" in _raw else 400.0
                except Exception:                                  # noqa
                    _max_shift = 400.0
                _allow = {}
                try:
                    import route_def as _RD2
                    _allow = (getattr(_RD2, "CFG", None) or {}).get("peak_snap_allow") or {}
                    if not isinstance(_allow, dict):
                        _allow = {}
                except Exception:                                  # noqa
                    _allow = {}
                if _allow:
                    print(f"   逐峰例外：{len(_allow)} 个（"
                          + "、".join(f"{k}≤{v:g} m" for k, v in _allow.items()) + "）")
                snap = snap_peaks_to_dem(_osm_draw, dem, meta, (LAT0 + LAT1) / 2.0,
                                         min_gain=_min_gain, max_shift_m=_max_shift,
                                         allow_shift=_allow)
                (OUT / "peak_snap.json").write_text(
                    json.dumps(snap, ensure_ascii=False, indent=0), encoding="utf-8")
            except Exception as _e:                                    # noqa
                print(f"   （峰点校正跳过：{_e}）")
        pil = draw_overlay(pil, m, _osm_draw, skip_names=skip,
                           px_scale=SS, proj_scale=scale_raster,
                           peak_snap=snap,
                           seed_cells=(seed_cells
                                       | poi_label_cells(meta, scale_raster, SS, 14 * SS)))
    except FileNotFoundError:
        pass
    return pil


def build_terrain():
    LON0, LAT0, LON1, LAT1, src = GC.resolve_bbox()
    # 宽高比钳制：图太宽（东西≫南北）时把南北外扩到上限，图面更协调（详见 cap_aspect）。
    # 默认把宽高比钳到 1.5:1（2026-11-03 定）：东西向长线（如萨普 128×50 km = 2.54:1）
    # 会出成一根扁带子，放进竖版攻略里很突兀。cap_aspect 只扩南北、不裁轨迹，
    # 本来就窄/方的线路（比例 < 1.5）原样不动 —— 所以这是个"只治过宽"的安全默认。
    # 想要其他上限：CFG["map_aspect_max"]=1.3/1.7；设 0/False 明确关闭。
    _cap = _route_cfg("map_aspect_max", 1.5)
    if _cap is not None and float(_cap or 0) > 0:
        _cap = float(_cap)
        LON0, LAT0, LON1, LAT1 = cap_aspect(LON0, LAT0, LON1, LAT1, _cap)
        src = f"{src} · 宽高比钳到 {_cap:g}∶1"
    GC.assert_bbox_covers((LON0, LAT0, LON1, LAT1), "make_terrain")
    print(f"[1/6] 地图窗口（{src}）: {LON0}, {LAT0} → {LON1}, {LAT1}", flush=True)
    # 线路级可强制 DEM 级别（CFG["dem_z"]）：z14 的平滑会吞掉冰川面的细节，
    # 博格达主峰北壁实测被渲染成一条无特征灰带。**绕过 DEM_MAX_PX 上限**——
    # pick_dem_z 的降档循环会把强制值又降回去（博格达实测：传 z=15 仍被降到 14）。
    # 只允许 ≤ DEM_Z（不超出数据源）。代价：内存/耗时按像素数走，z15 大窗口可能要几分钟。
    _z_req = _route_cfg("dem_z", None)
    if _z_req:
        z_use = min(int(_z_req), DEM_Z)
        _px = _window_px(z_use, LON0, LAT0, LON1, LAT1)
        print(f"[1/6] 线路强制 dem_z={z_use}"
              + (f"（{_px / 1e6:.0f} M px，超默认上限 {DEM_MAX_PX / 1e6:.0f} M —— 记得预期变慢）"
                 if _px > DEM_MAX_PX else ""))
    else:
        z_use = pick_dem_z(LON0, LAT0, LON1, LAT1)
    if z_use != DEM_Z:
        print(f"[1/6] 窗口较大（{_window_px(DEM_Z, LON0, LAT0, LON1, LAT1) / 1e6:.0f} M px @z{DEM_Z} "
              f"> 上限 {DEM_MAX_PX / 1e6:.0f} M px）→ DEM 自动降到 z{z_use}"
              f"（约 {_window_px(z_use, LON0, LAT0, LON1, LAT1) / 1e6:.0f} M px）")
    print(f"[1/6] 下载 DEM（terrarium z{z_use}）…")
    dem, meta = load_dem(z_use, LON0, LAT0, LON1, LAT1)
    dem = fill_voids(dem)
    lat_c = (LAT0 + LAT1) / 2
    lo, hi = float(np.nanmin(dem)), float(np.nanmax(dem))

    dw, dh = dem.shape[1], dem.shape[0]
    scale_logical = LOGICAL_W / dw                 # 逻辑像素/瓦片像素（SVG 层用）
    scale_raster = RASTER_W / dw                   # 栅格像素/瓦片像素（底图用）
    LOGICAL_H = round(dh * scale_logical)
    RASTER_H = round(dh * scale_raster)

    zf = float(_route_cfg("z_factor", Z_FACTOR))
    print(f"[2/6] 晕渲（GDAL Horn，多向 MDOW 主光 西北 315°/45°，z_factor {zf:g}）…")
    hs = hillshade(dem, meta["z"], lat_c, zf=zf)

    print("[3/6] 分层设色 + 合成 …")
    snow_m = _snowline_from_cfg()
    # 夏季地貌：季节性积雪退到高处 → 雪线整体上移（雪线虚线、设色、雪面塑形同步用这个值）
    _season = str(_route_cfg("terrain_season", SEASON) or SEASON).lower()
    if _season == "summer":
        _m = _route_cfg("snow_month", None)
        if _m is not None:
            _m = int(_m)
            _shift = float(MONTH_SNOW_SHIFT.get(_m, MONTH_SNOW_SHIFT[9]))
            print(f"   月份档 {_m} 月：雪线偏移 {_shift:+.0f} m（相对现代雪线）")
        else:
            # 2026-10-25 用户定：未配月份 = 默认 7 月档（−150 m）。
            # 旧行为是沿用 SUMMER_SNOW_SHIFT(+350，≈10 月)，会把雪线画得过高。
            _shift = float(_route_cfg("summer_snow_shift",
                                      MONTH_SNOW_SHIFT[DEFAULT_SNOW_MONTH]))
        _why = "季节性积雪已退" if _shift >= 0 else "仍处残雪期，雪线低于平衡线"
        print(f"   季节档 summer：雪线 {snow_m:.0f} → {snow_m + _shift:.0f} m"
              f"（{_shift:+.0f} m，{_why}）")
        snow_m = snow_m + _shift
    if (_route_cfg("snowline_m", None) is None
            and str(_route_cfg("terrain_palette", "") or "").strip().lower() != "desert"):
        # 不配就用残留默认值：高度大概率是错的，而图上会一本正经地画出这条线。
        # 沙漠线路本来就没有雪线，不刷这条警告（否则每条沙线都报一次假警）。
        print(f"   ⚠ 未配 CFG['snowline_m']，沿用脚本残留默认 {snow_m:.0f} m —— "
              f"换线路必须按山脉查表重设（references/china-terrain.md §4）；"
              f"若本线最高峰远低于雪线，删掉这个键更合适")
    global TINT_BAND_M
    # 色阶分级步长（米）：0 = **连续渐变**（默认）。
    # ⚠ 2026-10-20 用户先要"同一等高带内一致"、后要"过渡不能僵硬、要渐变"——
    #   这两件事互斥：量化到 100 m 会得到一层层硬边色块（像海报化），
    #   连续插值才是自然地貌的样子。**最终选渐变**，分级只作为可选旋钮：
    #   CFG["tint_band_m"] = 100 可切回分层填色（分界线落在计曲线上）。
    try:
        TINT_BAND_M = float(_route_cfg("tint_band_m", 0.0))
    except Exception:                                              # noqa
        TINT_BAND_M = 0.0
    # 区域雪线（南/北坡）：把雪线差折成等效高程偏移，后续全部用同一套色表
    _rs = regional_snow_shift(dem, (LON0, LAT0, LON1, LAT1), verbose=True)
    if _rs is None:
        dem_tint = dem
    else:
        # ⚠ 不能整体下移 dem：北坡 −1000 m 会把 5200 m 的高原压成 4200 m 的"草甸绿"，
        #   图上出现一块块绿色斑点（2026-10-21 用户截图）。正确做法是**只拉伸高山带**：
        #   把 [林线, 本坡雪线] 这段线性映射回 [林线, 基准雪线]，色序不变、比例不变。
        _tl2 = _route_cfg("treeline_m", None)
        _top = float(_tl2) if _tl2 else snow_m - 700.0     # 高山带下界
        _top = min(_top, snow_m - 150.0)
        _t = np.clip((dem - _top) / np.maximum(snow_m + _rs - _top, 1.0), 0.0, 1.2)
        dem_tint = np.where(dem > _top, _top + _t * (snow_m - _top), dem).astype(np.float32)
    rgb = hypso(dem_tint, snowline=snow_m)

    # 制图惯例：设色打底 + 晕渲塑形（环境光 + 方向光，背光面不死黑）+ 阴阳分色
    # ⚠ 2026-10-20（贡嘎大环）重做：原来这里是「向无山影的设色回抬」（600 m 内 0→0.6），
    #   本意是"山影别把雪涂灰"，实际把雪线以上的晕渲压掉六成；而峰顶设色本身已接近纯白，
    #   两者叠加 → 雪原变成"白纸 + 等高线"，贡嘎主峰群 2400 m 高差全是平的。
    #   现在改为：**雪面用更强的明暗塑形**（见 shade_rgb 的 shaped），只保留一点点回抬。
    # 雪线以上按坡度混岩：陡壁露岩、缓坡积雪（比"线以上全白"真实得多）
    # 雪面口径可按线路覆盖（2026-11-04）：
    #   "snow_rock_mix" 陡壁露岩比例 —— 区域总览这种 1:20 万尺度上，0.42 会让雪区布满
    #     灰岩条纹，远看像"花斑"；调小后雪原才连片。
    #   "snow_shape"    雪面明暗配比 (ambient, directional)。单线 1:5 万用 0.58/0.60 出立体感；
    #     总览尺度上对比过强会把雪切成一块块亮斑，用 0.80/0.26 更接近"雪原"。
    _srm = _route_cfg("snow_rock_mix", SNOW_ROCK_MIX)
    _sshape = _route_cfg("snow_shape", (0.58, 0.60))
    if _srm > 0:
        gy, gx = np.gradient(dem)
        res_m = 156543.03392 * np.cos(np.radians(lat_c)) / (2 ** meta["z"])   # DEM 像元边长（米）
        slope = np.sqrt(gx ** 2 + gy ** 2) / max(res_m, 1e-6)
        t0, t1 = SNOW_ROCK_T
        rock = np.clip((slope - t0) / max(t1 - t0, 1e-6), 0.0, 1.0)
        above = np.clip((dem_tint - (snow_m - 120)) / 260.0, 0.0, 1.0)  # 残雪过渡带 120 m
        # ⚠ 全程 float32 + 就地：w 由 float64 除法得来，一旦让 numpy 自动提升，
        #   (12012, 20680, 3) 的临时数组就是 5.55 GB（2026-11-04 实测 OOM）。
        w = (above * rock * _srm).astype(np.float32)[..., None]
        rgb *= (1.0 - w)
        rgb += np.asarray(SNOW_ROCK_RGB, np.float32) * w
        # ⚠ 这 5 个都是 **DEM 分辨率**的 float32 全幅数组（z14 / 320 M px 时各 1.28 GB，
        #   合计 6.4 GB），用完不删会一路活到 paint_contours —— 那里光 rgb/f/base/tint
        #   就要 11 GB，加起来直接撞进程内存上限（2026-11-05 伯舒拉岭三线总览实测 OOM）。
        del w, gy, gx, slope, rock, above
    snow_w = np.clip((dem_tint - snow_m) / 500.0, 0.0, 1.0) * 0.85
    img = shade_rgb(rgb, hs, snow_w=snow_w, snow_shape=_sshape)
    # ⚠ 大画幅（24000 px / 334 M px）下这两步各要 3.7 GB：就地 clip + 立刻释放
    #   rgb/hs/snow_w，否则 paint_contours 还要 copy 一份时必 OOM（2026-11-04 实测）。
    del rgb, hs, snow_w
    np.clip(img, 0, 255, out=img)
    img = img.astype(np.uint8)
    # BICUBIC 而非 LANCZOS：LANCZOS 在陡崖等高反差处会产生振铃，被随后的 USM 放大成"颗粒噪点"。
    pil = Image.fromarray(img, "RGB").resize((RASTER_W, RASTER_H), Image.BICUBIC)
    # USM 提锐：30 m DEM 在 z15 已被过采样 7×，天生柔；提锐才能让山脊/冲沟"立"起来
    pil = pil.filter(ImageFilter.UnsharpMask(radius=UNSHARP[0], percent=UNSHARP[1],
                                             threshold=UNSHARP[2]))

    # 等高线几何一律在**基准栅格**上算（宽 4512 px，与超采样 SS 无关）：
    # 超采样只负责"把这张掩膜放大几倍"，不参与等高距/线宽/陡坡省略/虚线节距
    # 的任何计算（2026-11-07 用户「等高线不随超采变动」，详见 CONTOUR_REF_SS）。
    # 顺带把旧的 dem_out（RASTER_W 宽 float32，SS=13 时 900 MB）整块省掉。
    # ---- 等高线总开关（CFG["contours"]，默认 True = 旧行为）----
    # 1:50 万级总览图（0.54 km/px）上，500 m 等高距的相邻线平均只隔 1–2 px：
    # 画出来既不是「线」（读不出高程关系）也不是「色带」（不连续），只能靠把它加粗到
    # 4.33 px 假装成纹理 —— 那不是地形图语法。这条尺度上的通行做法是**分层设色 + 晕渲**，
    # 等高线只出现在分幅图（0.1 km/px，500 m 间距 5 px）里。
    # 关掉时同时省掉 contour_plan（要跑覆盖率选档，是这一步最贵的一环）与高程标注。
    _CONTOUR_ON = bool(_route_cfg("contours", True))
    arr = np.asarray(pil).copy()
    if _CONTOUR_ON:
        dem_ref, _m_per_px, c_minor, c_major, c_cov, _ref_w = contour_plan(dem, lat_c, meta["z"])
        # 计曲线间隔也可单独指定：CFG["contour_major"]。
        # 为什么需要：首曲线被固定成非整数倍时（如 15 m），`nice_major()` 只能给 75 m ——
        # 高程注记变成 75/150/225 这种不好读的数字。单独指定 100 m 就回到规范值。
        _cmaj = _route_cfg("contour_major", None)
        if _cmaj:
            c_major = float(_cmaj)
            print(f"   计曲线间隔由线路指定：{c_major:g} m")
        # 老路径（contour_width=None）的计线补偿也按**基准 px**：SS-2 在 SS=13 时是 11 px，
        # 放到基准栅格上等于 11/4512 的粗线（整幅糊成灰带）。基准口径恒为 3-2=1。
        _w_major = max(CONTOUR_DILATE[1], CONTOUR_REF_SS - 2)
        # 线宽默认 CONTOUR_WIDTH=(1,2)，可按线路用 CFG["contour_width"] 覆盖，
        # 单位**基准栅格 px**（= 4512 px 画幅上的 px），不随超采样变；
        # 显式 None 退回老行为（首线 1 px、计线基准 1 px 补偿）。
        _cw = _route_cfg("contour_width", CONTOUR_WIDTH)
        _w_px = None
        if _cw:
            _w_px = (int(_cw[0]), int(_cw[1]))
        # 两种线宽口径（2026-11-07 用户先选「逻辑宽恒定」，随后改为「输出 px 固定」）：
        #   · 默认 False → `contour_width` 单位是**基准栅格 px**，出图时掩膜整体放大 ×SS/3，
        #     所以逻辑宽恒定（4512 图 1/2 px，19552 图 4.33/8.66 px，换算逻辑宽仍 1/3、2/3）。
        #   · True       → `contour_width` 单位是**最终图上的实际像素**，无论 SS 多大都恒
        #     为首 1 / 计 2 px。代价：线覆盖率 ∝ 1/放大倍数（缩图会变淡），且与选档判据
        #     （在基准栅格上测）脱钩。默认关，别影响既有线路。
        _abs_px = bool(_route_cfg("contour_width_absolute", False))
        _k = RASTER_W / _ref_w
        if _abs_px:
            _wp = _w_px or (int(CONTOUR_WIDTH[0]), int(CONTOUR_WIDTH[1]))
            print(f"[4/7] 等高线（首曲线 {c_minor:g} m / 计曲线 {c_major:g} m，"
                  f"线宽 {_wp[0]}/{_wp[1]} px@出图，固定不随超采变化）…")
        elif _w_px:
            print(f"[4/7] 等高线（首曲线 {c_minor:g} m / 计曲线 {c_major:g} m，"
                  f"线宽 {_w_px[0]}/{_w_px[1]} px@{_ref_w}px 基准 → ×{_k:.2f} 出图）…")
        else:
            print(f"[4/7] 等高线（首曲线 {c_minor:g} m / 计曲线 {c_major:g} m，"
                  f"线宽 {_w_major} px@{_ref_w}px 基准 → ×{_k:.2f} 出图）…")
        # 线色来源可按线路切：派生色（`CONTOUR_DERIVED`，随底图同色相）在**大窗口总览图**
        # 上会"消失"—— 线是提亮型的浅色，与晕渲受光坡同色相、亮度也接近，实测 19552 px
        # 全幅 4.33 px 宽、alpha 0.30 仍读不出（2026-11-09 GHT 全线）。总览图改 `False`
        # 走固定灰棕，才是地形图里那种"深色细线"，雪区/草甸的"跳"在这个尺度反而无妨。
        _derived = bool(_route_cfg("contour_derived", CONTOUR_DERIVED))
        if not _derived:
            print(f"   等高线线色：固定灰棕（CFG['contour_derived']=False，不走派生色）")
        if _abs_px:
            # 只在**出图栅格**上重算 band edges（1 px 原始边线），再按固定 px 加宽；
            # 等高距、陡坡省略的物理坡度仍来自基准 plan（m_per_px 同比缩小即可）。
            # ⚠ 这张 dem_raster 用完立刻 del —— 226 MP 画幅下它是 900 MB，
            #   留着会与 paint_contours 的 f / base 一起撞内存上限。
            dem_raster = np.asarray(
                Image.fromarray(dem, "F").resize((RASTER_W, RASTER_H), Image.LANCZOS))
            _wp = _w_px or (int(CONTOUR_WIDTH[0]), int(CONTOUR_WIDTH[1]))
            paint_contours(arr, dem_raster, 0, 0, minor=c_minor, major=c_major,
                           m_per_px=_m_per_px / _k, w_px=_wp, derived=_derived)
            del dem_raster
        else:
            paint_contours(arr, dem_ref, CONTOUR_DILATE[0], _w_major, minor=c_minor, major=c_major,
                           m_per_px=_m_per_px, w_px=_w_px, derived=_derived,
                           raster_shape=(RASTER_H, RASTER_W))
    else:
        # 只造基准栅格 DEM：雪线掩膜要用它（等高距/线宽一律不参与）。
        _ref_w, _ref_h = contour_ref_size(dem.shape)
        dem_ref = np.asarray(
            Image.fromarray(dem, "F").resize((_ref_w, _ref_h), Image.BILINEAR))
        _m_per_px, c_minor, c_major = 0.0, 0.0, 0.0
        _k = RASTER_W / _ref_w
        print("[4/7] 等高线：CFG['contours']=False → 不画线、不标高程"
              "（1:50 万口径：分层设色 + 晕渲 + 雪线/冰川面）")
    # 雪线：联网核实的北坡 6000 m（南坡 5000 m 写进注记），叠在 DEM 等高线上
    # ⚠ CFG["snowline_draw"]=False 只关**这条蓝虚线**，地貌垂直带的雪色照旧按 snow_m 分档。
    #   为什么需要这个开关（2026-11-07 GHT 全幅实测）：雪线掩膜是「任何 below/above 交界」，
    #   在地势平缓的高原面上，5350 m 等高线会绕出上千个小闭环 → 虚线节距又被放大到
    #   117/234 输出 px，整片高原变成蓝色方块麻点。窗口跨到整条喜马拉雅时只留色带更干净。
    scale_raster_y = RASTER_H / dh
    _snow_draw = _route_cfg("snowline_draw", True)
    if _snow_draw:
        # 平滑半径按基准栅格 px（4512 宽）。总览尺度上 4 px ≈ 0.8 km，
        # 足以吃掉高原面的小闭环又不动山脊走向；分幅图配 0（逐像素、贴合真实雪线）。
        _sm = int(_route_cfg("snowline_smooth_px", 0) or 0)
        if _sm:
            print(f"   雪线掩膜：先平滑 {_sm} px（基准栅格）再取等高，避免高原面碎闭环")
        snow_mask = paint_snowline(arr, dem_ref, level=snow_m,
                                   raster_shape=(RASTER_H, RASTER_W), smooth_px=_sm)
    else:
        snow_mask = None
    pil = Image.fromarray(arr, "RGB")
    snow_cells = set()
    if snow_mask is None:
        if _snow_draw:
            print(f"   雪线 {snow_m:.0f} m：不在本窗口高程范围内，跳过")
        else:
            print(f"   雪线 {snow_m:.0f} m：CFG['snowline_draw']=False → 只留色带，不画蓝虚线")
    else:
        pil, snow_cells = draw_snowline_label(
            pil, dem, snow_m, (scale_raster, scale_raster_y),
            avoid=avoid_points(meta, scale_raster, SS))

    if _CONTOUR_ON:
        print(f"[5/7] 等高线高程标注（计曲线每 {c_major:g} m）…")
        pil, seed_cells = draw_contour_labels(
            pil, dem, (scale_raster, scale_raster_y), lo, hi,
            avoid=avoid_points(meta, scale_raster, SS), every=c_major)
        seed_cells = seed_cells | snow_cells
    else:
        # 不画线就不标配数字：只留雪线名与峰顶高程（1:50 万图的注记就这两类）。
        print("[5/7] 等高线高程标注：跳过（无等高线可依）")
        seed_cells = set(snow_cells)

    print(f"[6/7] OSM 矢量叠加 + 落盘（超采样 {SS}× → 栅格 {RASTER_W}px / 逻辑 {LOGICAL_W}px）…")
    pil.save(OUT / "base_terrain.jpg", **JPG)
    m = {"size": [LOGICAL_W, LOGICAL_H], "scale": scale_logical,
         "raster": [RASTER_W, RASTER_H], "ss": SS,
         "box": meta["box"], "bbox": [LON0, LAT0, LON1, LAT1], "z": meta["z"],
         "tx0": meta["tx0"], "ty0": meta["ty0"],
         # 本次自适应选定的等高距；`ref_w` 记几何基准宽（超采样再怎么变，这个值不变，
         # 排查"等高距怎么跟上次不一样"时先看它是不是同一个基准）。
         "contour": {"minor": c_minor, "major": c_major, "ref_w": _ref_w},
         # 出图雪线（已含月档偏移）：图例 desc 是手写的，没有这个键就没法自动比对 ——
         # 2026-10-25 贡嘎 fork 实测 desc 写 5100 / 图上画 4950 的图文打架就是这么来的。
         # 下游 build_guide / map_svg / qa_guide 经 GC.check_snowline_legend 比对告警。
         "snowline": snow_m,
         "source": f"本地自渲染：地形 DEM z{z_use} + OSM 矢量（非瓦片服务）",
         "attribution": "Elevation: SRTM/Copernicus DEM (AWS Open Data, 免费开放) · "
                        "Map data: © OpenStreetMap contributors (ODbL) · "
                        "本地生成，无瓦片服务速率限制。"}
    (OUT / "base_meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    pil = _overlay(pil, m, dem, meta, (LON0, LAT0, LON1, LAT1),
                   SS, scale_raster, seed_cells, snow_m)
    # 署名水印（栅格层）：烧在最终底图上 —— HTML 内嵌地图与长图都吃这一张，
    # 所以在渲染期加，而不是让人事后用图片工具去盖。高清图另有独立一份（render_map_hi）。
    pil = GC.burn_watermark(pil)
    pil.save(OUT / "base_map.jpg", **JPG)
    print(f"[7/7] 完成 栅格 {pil.size} / 逻辑 {LOGICAL_W}×{LOGICAL_H}  高程 {lo:.0f}–{hi:.0f} m  "
          f"jpg {(OUT/'base_map.jpg').stat().st_size/1048576:.2f} MB")


def build_overlay_only():
    """只重跑 OSM 叠层：复用 base_terrain.jpg + base_meta.json。

    判据：改注记/线宽/类别口径时，DEM 下载 + 分层设色 + 晕渲 + 雪线（约占一次出图
    80% 的时间）**结果完全不变**，没有理由重算。地形层一旦定稿，后续迭代表只该碰
    叠层这一层（2026-11-10）。
    """
    meta_p, terr = OUT / "base_meta.json", OUT / "base_terrain.jpg"
    if not meta_p.exists() or not terr.exists():
        raise SystemExit("缺 base_terrain.jpg / base_meta.json —— 先完整跑一次 make_terrain.py")
    m = json.loads(meta_p.read_text(encoding="utf-8"))
    LON0, LAT0, LON1, LAT1 = m["bbox"]
    pil = Image.open(terr).convert("RGB")
    print(f"[osm-only] 复用地形层 {pil.size} · 逻辑 {m['size'][0]}×{m['size'][1]}"
          f" · bbox {m['bbox']}")
    dem, meta = load_dem(int(m["z"]), LON0, LAT0, LON1, LAT1)
    dem = fill_voids(dem)
    scale_raster = m["raster"][0] / dem.shape[1]
    t0 = time.time()
    pil = _overlay(pil, m, dem, meta, (LON0, LAT0, LON1, LAT1),
                   int(m.get("ss", 1)), scale_raster, set(), float(m["snowline"]))
    pil = GC.burn_watermark(pil)
    pil.save(OUT / "base_map.jpg", **JPG)
    print(f"[osm-only] 完成 → base_map.jpg {pil.size}  "
          f"{time.time() - t0:.1f}s  高程不改（沿用 base_meta）")


if __name__ == "__main__":
    if "--osm-only" in sys.argv:
        build_overlay_only()
    else:
        build_terrain()

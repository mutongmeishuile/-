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
import json
import math
import re

import guide_common as GC
import route_def as RD

if not getattr(RD, "TRACK_READY", True):
    raise SystemExit("轨迹数据未就绪：先跑 python prep_track.py <你的.kml>")

meta = GC.load_meta()
IW, IH = meta["size"]
PROJ = GC.projector_of(meta)
M_PER_PX = GC.m_per_px(meta)

INK = "#1F2430"
# ---- 家具缩放系数（要求「缩小」，2026-10-09 按用户反馈两次收档）----
FURN_LG = 0.55        # 图例：566 基准宽 → 311（面积 ≈ 0.30）
FURN_CP = 0.58        # 指北针：半径 42 → 24
FURN_SB = 0.62        # 比例尺：只作用于字与内衬，**不作用于色条长度**（见 §12）

_LG_S = FURN_LG       # 图例**实际**缩放（build_map_overlay 按高度预算/重试动态设定）

EMBED_MAX = 1600      # 网页内嵌底图上限；高清地图用 embed_max=0
FONT_STACK = "Microsoft YaHei,PingFang SC,Noto Sans CJK SC,Source Han Sans SC,sans-serif"
PS_BASE = 0.66        # 点位符号／标注基准缩放（0.85 → 0.74 → 0.66：图钉再收两档）
MFS_M = 1.9           # 手机版地图统一放大系数（图被缩到 ~0.25 倍，必须放大）
MFS_S = 2.8           # 手机版里程点/线宽的额外放大

class _LazyCfg:
    """`route_def.CFG` 的惰性代理 —— **取值那一刻才去读**，不在 import 时快照。

    2026-10-22：这里原先是 `_CFG = getattr(RD, "CFG", {})`，import 的那一瞬间就把
    配置固化成普通 dict 了；而 draw_osm / make_terrain 都是运行时现取 ——
    同一份配置在两个脚本里语义不同（详见 guide_common.cfg 的说明）。
    改成代理后，下面 `_CFG.get(k, d)` 的写法一个字都不用改，行为就跟其它脚本一致了。
    """

    def get(self, key, default=None):
        return GC.cfg(key, default)

    def __getitem__(self, key):
        v = GC.cfg(key, None)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return GC.cfg(key, None) is not None


_CFG = _LazyCfg()
# ⚠ 下面这 4 个仍是**模块级**求值（import 时取一次）：它们被当成"配色常量"用，
#   语义上就是固定值，保持原样。真正需要跟着配置变的是函数内的那些 _CFG.get()。
D1_C = _CFG.get("d1_color", "#E4572E")
D2_C = _CFG.get("d2_color", "#1E9E76")
D1_T = _CFG.get("d1_text", "#B23A1C")
D2_T = _CFG.get("d2_text", "#0F6E5C")

PS = PS_BASE
_PAD = 18
_PIN_R = 15
_OCC, _LABELS, LABEL_OF = [], [], {}
_TRK, _TRK_HW = [], 0.0     # 轨迹点与半宽（_layout 用来避让线）
_lg_rect = _sb_rect = _cp_rect = None


def P(lon, lat):
    x, y = PROJ(lon, lat)
    return round(x, 1), round(y, 1)


def d_of(pts):
    return "M " + " L ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)


# ---- 地图朝向：默认「一页宽、长度不限」----
# 若线路长轴明显是横向 (E-W)，旋转 90° 让长轴转竖直、缩放填满页宽；纵向 (N-S) 线路保持正上不变。
# 指北针随真实北向旋转（见下方 compass 绘制）。PHI=0 时本段为恒等变换（与旧版逐字节一致）。
def _rot_pt(x, y, deg, cx, cy):
    a = math.radians(deg)
    dx, dy = x - cx, y - cy
    return (cx + dx * math.cos(a) - dy * math.sin(a),
            cy + dx * math.sin(a) + dy * math.cos(a))


def _track_aspect():
    lons = [p[0] for p in RD.TRACK]
    lats = [p[1] for p in RD.TRACK]
    midlat = (min(lats) + max(lats)) / 2
    wm = (max(lons) - min(lons)) * 111320 * math.cos(math.radians(midlat))
    hm = (max(lats) - min(lats)) * 110540
    return wm, hm


_WM, _HM = _track_aspect()
PHI = 0
if _WM > _HM * 1.15:                # 明显横向 → 旋转 90° 使长轴竖直
    cx, cy = IW / 2, IH / 2
    sx0, sy0 = P(*RD.TRACK[0])       # 让「起点」落在画布顶部
    best_y = None
    for _cand in (90, -90):
        _, y = _rot_pt(sx0, sy0, _cand, cx, cy)
        if best_y is None or y < best_y:
            PHI, best_y = _cand, y


def _upright_text(svg, phi):
    """旋转地图后，把地理标注文字反向旋转回正立（仅 phi≠0 时生效）。"""
    if phi == 0 or not svg:
        return svg
    import re
    def _w(m):
        x, y = m.group(1), m.group(2)
        return f'<g transform="rotate({-phi:.2f} {x} {y})"><text x="{x}" y="{y}"'
    svg = re.sub(r'<text x="([\d.]+)" y="([\d.]+)"', _w, svg)
    return svg.replace('</text>', '</text></g>')


# ---------------------------------------------------------------- 标注避让（动态定位）
def _layout(x, y, dx, dy, anc, w, fs, commit=True):
    """给标注找一个位置，返回 (tx, ty, anchor, box, overlap_area)。

    `area == 0` = 完全不压任何已占用矩形（完美落位）；`> 0` = 压到的面积（px²）。
    **纯函数**（除 commit 外无副作用）：调用方（build_map_overlay 的字号阶梯）
    需要反复试探不同字号，若每次试探都往 _OCC 里登记，落选的方案会把后来的
    标注全部挤走——2026-10-17 北天山中线实测踩过这个坑。

    位置候选（"动态合理"）：
      ① route_def 里配置的 (dx, dy, anchor) 永远排最前 —— 作者意图优先；
      ② 其后是 8 方向 × 4 档半径的搜索，锚向随所在侧自动选（右侧 start /
         左侧 end / 正上下 middle），半径以行高 h 为单位，最多外扩 ~2.8h。
    ⚠ 旧实现只沿纵向往复试 0/±1h/±2h…：标注一多（本线 26 个）就互相压。
    """
    h = fs * 1.6
    plans = [(dx, dy, anc)]
    if anc == "middle":
        plans += [(w / 2 + 34, dy, "start"), (-w / 2 - 34, dy, "end")]
    for r in (0.7 * h, 1.4 * h, 2.1 * h, 2.8 * h):
        for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
            a = "start" if sx > 0 else ("end" if sx < 0 else "middle")
            plans.append((sx * r, sy * r + (h * 0.5 if sy else 0.0), a))

    def boxof(ox, oy, a):
        tx, ty = x + ox, y + oy
        x0, x1 = ((tx, tx + w) if a == "start" else
                  (tx - w, tx) if a == "end" else (tx - w / 2, tx + w / 2))
        return tx, ty, (x0, ty - h / 2, x1, ty + h / 2)

    def dist(box):
        return max(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))

    free, loose = None, None
    for ox, oy, a in plans:
        tx, ty, box = boxof(ox, oy, a)
        x0, y0, x1, y1 = box
        if x0 < _PAD or x1 > IW - _PAD or y0 < _PAD or y1 > IH - _PAD:
            continue
        area = sum(GC.rect_overlap_area(box, o) for o in _OCC)
        # 压到轨迹（含白晕）= 不可接受：2026-10-21 实测 POI 标注会正正落在
        # 白色线道上（"措学仁玛营地"横穿白带），因为 _OCC 里只有图钉与家具，
        # 线本身从来没进过占位表。给一个极大的罚值，让候选位一律先避开线。
        if area == 0 and _TRK and _near_track(box, _TRK, _TRK_HW):
            area = 1e6
        if area == 0:
            # 完美落位里也排个序：配置位 > 离点位近 > 离配置位近
            key = (0 if (ox, oy, a) == (dx, dy, anc) else 1, round(dist(box)),
                   round((ox - dx) ** 2 + (oy - dy) ** 2))
            if free is None or key < free[0]:
                free = (key, tx, ty, a, box, 0.0)
        elif loose is None or (area, round(dist(box))) < loose[0]:
            loose = ((area, round(dist(box))), tx, ty, a, box, area)
    best = free or loose
    if best is not None:
        _, tx, ty, a, box, area = best
        if commit:
            _OCC.append(box)
            _LABELS.append(box)
        return round(tx, 1), round(ty, 1), a, box, area
    # 一个候选都放不下（图太小）：退回配置位置，强行落位
    tx = min(max(x + dx, _PAD + w if dx >= 0 else _PAD), IW - _PAD)
    ty = min(max(y + dy, _PAD), IH - _PAD)
    box = (tx - w, ty - h / 2, tx + w, ty + h / 2)
    if commit:
        _LABELS.append(box)
    return round(tx, 1), round(ty, 1), anc, box, 1e18


# ---------------------------------------------------------------- 点位符号
def mk_pin(x, y, kind):
    # 2026-10-17 用户反馈：POI 图钉在轨迹上过大，遮挡地名/道路。
    # 统一从 r≈19–20 缩到 r≈14–15，字号同步缩小，与地名同量级。
    if kind == "start":
        r = 14 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#D93B2B" stroke="#fff" '
                f'stroke-width="{4*PS:.1f}"/>'
                f'<text x="{x}" y="{y+6*PS:.1f}" text-anchor="middle" font-size="{16*PS:.1f}" '
                f'font-weight="800" fill="#fff">起</text>')
    if kind == "end":
        r = 14 * PS
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#0F6E5C" stroke="#fff" '
                f'stroke-width="{4*PS:.1f}"/>'
                f'<text x="{x}" y="{y+6*PS:.1f}" text-anchor="middle" font-size="{15*PS:.1f}" '
                f'font-weight="800" fill="#fff">终</text>')
    if kind in ("peak", "peak_hi"):
        # 山峰：与营地 / 水源 / 村落的构造完全同源 —— 白底圈 + 色盘 + 白色三角。
        # 2026-10-24 改：旧版是「白晕 + 裸色三角」（peak_hi r=14 深红 / peak r=11 灰），
        # 与其它符号两种画风，且同类山峰一大一小。现在两种 kind 几何一致，
        # 若要强调主峰请只改 label 颜色，不要改符号尺寸。
        return (f'<circle cx="{x}" cy="{y}" r="{15*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{11.6*PS:.1f}" fill="#4A5261"/>'
                f'<path d="M {x-7.2*PS:.1f},{y+5.2*PS:.1f} L {x},{y-6.6*PS:.1f} '
                f'L {x+7.2*PS:.1f},{y+5.2*PS:.1f} Z" fill="#fff"/>')
    if kind == "temple":
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PS:.1f}" fill="#5A4A8A"/>'
                f'<path d="M {x-7*PS:.1f},{y-0.8*PS:.1f} L {x},{y-7.8*PS:.1f} '
                f'L {x+7*PS:.1f},{y-0.8*PS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-4.6*PS:.1f}" y="{y+0.8*PS:.1f}" width="{9.2*PS:.1f}" '
                f'height="{5.6*PS:.1f}" fill="#fff"/>')
    if kind == "camp":
        return (f'<circle cx="{x}" cy="{y}" r="{15*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{11.6*PS:.1f}" fill="{D1_C}"/>'
                f'<path d="M {x-7.5*PS:.1f},{y+6*PS:.1f} L {x},{y-6.7*PS:.1f} '
                f'L {x+7.5*PS:.1f},{y+6*PS:.1f} Z" fill="#fff"/>')
    if kind == "water":
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PS:.1f}" fill="#0F8A9B"/>'
                f'<path d="M {x},{y-6.6*PS:.1f} C {x+6.6*PS:.1f},{y+0.7*PS:.1f} '
                f'{x+5.2*PS:.1f},{y+6.6*PS:.1f} {x},{y+6.6*PS:.1f} C {x-5.2*PS:.1f},{y+6.6*PS:.1f} '
                f'{x-6.6*PS:.1f},{y+0.7*PS:.1f} {x},{y-6.6*PS:.1f} Z" fill="#fff"/>')
    if kind == "warn":
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PS:.1f}" fill="#D0871B"/>'
                f'<text x="{x}" y="{y+5.4*PS:.1f}" text-anchor="middle" '
                f'font-size="{15*PS:.1f}" font-weight="900" fill="#fff">!</text>')
    if kind == "pass":
        # 垭口：倒三角 + 横杠（山鞍形）
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<path d="M {x-9*PS:.1f},{y-5*PS:.1f} L {x+9*PS:.1f},{y-5*PS:.1f} '
                f'L {x},{y+8*PS:.1f} Z" fill="#8B5E3C"/>'
                f'<rect x="{x-7*PS:.1f}" y="{y-8*PS:.1f}" width="{14*PS:.1f}" '
                f'height="{3*PS:.1f}" rx="{1.5*PS:.1f}" fill="#8B5E3C"/>')
    if kind == "ford":
        # 涉滩：水滴 + 波纹
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PS:.1f}" fill="#2E86AB"/>'
                f'<path d="M {x-6*PS:.1f},{y-1*PS:.1f} Q {x-3*PS:.1f},{y-5*PS:.1f} '
                f'{x},{y-1*PS:.1f} Q {x+3*PS:.1f},{y+3*PS:.1f} {x+6*PS:.1f},{y-1*PS:.1f}" '
                f'stroke="#fff" stroke-width="{2.2*PS:.1f}" fill="none" stroke-linecap="round"/>'
                f'<path d="M {x-6*PS:.1f},{y+3.5*PS:.1f} Q {x-3*PS:.1f},{y-0.5*PS:.1f} '
                f'{x},{y+3.5*PS:.1f} Q {x+3*PS:.1f},{y+7*PS:.1f} {x+6*PS:.1f},{y+3.5*PS:.1f}" '
                f'stroke="#fff" stroke-width="{2.2*PS:.1f}" fill="none" stroke-linecap="round"/>')
    if kind == "poi":
        # 地标：菱形
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<path d="M {x},{y-10*PS:.1f} L {x+7.5*PS:.1f},{y} '
                f'L {x},{y+10*PS:.1f} L {x-7.5*PS:.1f},{y} Z" fill="#6B4FA0"/>')
    if kind == "village":
        # 村落：小房子
        return (f'<circle cx="{x}" cy="{y}" r="{14*PS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PS:.1f}" fill="#C4703A"/>'
                f'<path d="M {x-7*PS:.1f},{y-0.5*PS:.1f} L {x},{y-7*PS:.1f} '
                f'L {x+7*PS:.1f},{y-0.5*PS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-5*PS:.1f}" y="{y-0.5*PS:.1f}" width="{10*PS:.1f}" '
                f'height="{6*PS:.1f}" fill="#fff"/>')
    return ""


# ---------------------------------------------------------------- 三件套
def _legend_km_txt(s):
    """把 "14.40 km" 归一成 "14.4"。

    单位统一提到图例标题（「分日路段 · km」），省掉每行 3 个字符宽 ——
    7 天就省 21 字符宽，卡片能窄一截（2026-10-23 精细化）。
    """
    t = str(s or "").strip()
    m = re.match(r"^([\d.]+)\s*km$", t, re.I)
    if not m:
        return t
    v = float(m.group(1))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _legend_rows():
    rows = []
    # 样本线宽：跟字号同量级即可（字 9.35 逻辑 px）。6.5 的粗横杠会盖过文字，
    # 用户要求"线条也细一点儿" → 主线段 3.8、虚线步道 1.5。
    for d in _CFG.get("days", []):
        tag = d.get("tag", "D")
        desc = str(d.get("legend", "") or "").strip()
        # ⚠ 「D1 D1 鸡丑垭口 → 两河口营地」：CFG 的 legend 常自带日标前缀，而前面
        #   还有一列 tag —— 直接拼就是重复两遍。这里把重复前缀剔掉（只剔一次，且要求
        #   后面紧跟空格，避免把真的以 D1 开头的专名误伤）。
        if desc.startswith(tag + " "):
            desc = desc[len(tag) + 1:]
        rows.append((d.get("color", D1_C), tag, desc,
                     _legend_km_txt(d.get("dist_label", "")), 3.8, ""))
    # 附加行：CFG["legend_extra"] 列表优先（冰川面 / 雪线 / 步道可并存），
    # 缺省退回旧的单行 extra_legend_* 键。
    extra = _CFG.get("legend_extra")
    if extra:
        for it in extra:
            if "雪线" in str(it.get("tag", "")):
                GC.check_snowline_legend(it.get("desc", ""), "（地图图例）")
            if "步道" in str(it.get("tag", "")):
                GC.check_trail_legend(it.get("color", GC.C_TRAIL_LEGEND), "（地图图例）")
            rows.append((it.get("color", GC.C_TRAIL_LEGEND), it.get("tag", ""),
                         it.get("desc", ""), _legend_km_txt(it.get("dist_label", "")),
                         it.get("width", 1.5), it.get("dash", ' stroke-dasharray="8 5"')))
    elif _CFG.get("extra_legend_desc"):
        GC.check_trail_legend(_CFG.get("extra_legend_color", GC.C_TRAIL_LEGEND), "（地图图例·旧键）")
        rows.append((_CFG.get("extra_legend_color", GC.C_TRAIL_LEGEND),
                     _CFG.get("extra_legend_tag", "步道"),
                     _CFG["extra_legend_desc"], "", 1.5, ' stroke-dasharray="8 5"'))
    return rows


# 图例里的**符号**说明：只列本图真的用到的 kind，两列紧凑排布。
# 用户要求「图中所涉及的图例最好在图例区域都展示出来，不要占地过大」
# → 不再只画分日线段，把起点/终点/营地/水源/垭口/高峰的图形样本也摆进去，
#   但用两列网格 + 缩小系数控制面积。名称可用 CFG["legend_kind_names"] 覆盖。
_KIND_ORDER = ["start", "end", "camp", "water", "warn", "pass", "peak_hi", "peak", "village", "ford", "poi", "temple"]
# ---- 图例排版常量（单位 = 1×图例缩放 `_LG_S`，`_legend_size()` 与渲染代码共用）----
# 2026-10-23 精细化：原 30/60/38/38/46 的组合在 7 天线路上一张卡高 482（×0.55 = 265），
# 占 684 高的图面 39%，压掉将近半个角落。行距与边距整体收紧 15% 左右，
# 字号不动（字是信息，行距是装饰），高 408（×0.55 = 224，33%）。
# ⚠ 改这些值必须同时改渲染代码里的 `+11*S` 基线偏移与符号 +7*S —— 都走同一组常量。
LG_TITLE = 26         # 卡片顶 → 标题基线
LG_ROW0 = 52          # 卡片顶 → 第一行的"行位"
LG_BASE = 11          # 行位 → 文字基线（⚠ 分隔线要**从基线**再往下让，见 LG_DIV_GAP）
LG_PITCH = 32         # 分日行行距
LG_DIV_GAP = 10       # 最后一行**文字基线** → 分隔线（旧版按"行位"算，线正划在字上）
LG_SYM0 = 24          # 分隔线 → 第一行符号中心
LG_SYM_PITCH = 34     # 符号行行距
LG_BOT = 40           # 最后一行内容 → 卡片底（脚注落在这段，太小会压到末行符号！实测踩坑）
LG_FOOT = 13          # 卡片底 → 脚注基线
LG_GLYPH = 0.55       # 符号样本缩放（0.42 时起点/终点只剩一颗小点，图例反而看不出是什么）
LG_MIN_TAIL = 12      # 脚注与末行内容的最小安全间距（自检用）
# 图例卡片宽度：**按内容算，不写死**。566 是旧的固定值，7 天线里有一半是空白；
# 另一方面描述一长（"温泉营地 → 废弃牧屋营地"）又会顶到右边框。
LG_W_MIN, LG_W_MAX = 300, 700     # 逻辑 px 上下限（×S 之前）
LG_DESC_X = 132       # 描述列起点（示例线 18→78、日标 88、描述 132）


def _legend_w():
    """按内容算卡片宽度（单位 = 1×S）。取四段里最宽的那段。"""
    rows = _legend_rows()
    title = _legend_title_txt()
    need = [18 + GC.text_w(title, 22) + 18]
    for _c, _tag, desc, km, _w, _d in rows:
        x = LG_DESC_X + GC.text_w(desc, 17)
        if km:
            x += 14 + GC.text_w(km, 17)
        need.append(x + 18)
    syms = _legend_symbols()
    if syms:
        # 两列网格：每列 = 符号 34 + 间距 22 + 名称
        half = max(34 + 22 + GC.text_w(nm, 17) for _k, nm in syms)
        need.append(2 * half + 36)
    foot = _CFG.get("legend_foot", "全线为实测轨迹 · 非导航用图")
    if foot:
        need.append(18 + GC.text_w(foot, 16.5) + 18)
    return max(LG_W_MIN, min(LG_W_MAX, max(need)))


def _legend_title_txt():
    """图例标题：有里程列时自动补单位（每行就不再重复写 km）。"""
    t = _CFG.get("legend_title", "分日路段")
    has_km = any(r[3] for r in _legend_rows())
    return f"{t}（km）" if has_km and "km" not in t else t


def _legend_geom():
    """算图例的纵向排版（单位 = 1×S）。渲染与尺寸共用，避免两套算法打架。"""
    rows = len(_legend_rows())
    syms = len(_legend_symbols())
    last_row = LG_ROW0 + LG_PITCH * max(0, rows - 1)
    end = last_row + LG_BASE + 4                           # 末行的下半行高（基线下方 4 px）
    if syms:
        div = last_row + LG_BASE + LG_DIV_GAP
        n_sym_rows = (syms + 1) // 2
        last_sym = div + LG_SYM0 + LG_SYM_PITCH * max(0, n_sym_rows - 1)
        end = last_sym + 14
    h = end + LG_BOT
    return {"rows": rows, "last_row": last_row, "end": end, "h": h,
            "w": _legend_w(),
            "foot": h - LG_FOOT, "tail": h - LG_FOOT - end}
_KIND_NAME = {"start": "起点", "end": "终点", "camp": "营地", "water": "水源 · 海子",
              "warn": "注意点", "peak_hi": "主峰 · 最高点", "peak": "山峰", "temple": "寺庙",
              "pass": "垭口", "ford": "涉滩 / 过河", "poi": "地标", "village": "村落 · 补给"}


def _legend_symbols():
    names = dict(_KIND_NAME)
    names.update(_CFG.get("legend_kind_names", {}) or {})
    seen, used = set(), []
    for d in GC.pois_of(RD):
        k = d["kind"]
        if k and k not in seen:
            seen.add(k)
            used.append(k)
    used.sort(key=lambda k: _KIND_ORDER.index(k) if k in _KIND_ORDER else 99)
    return [(k, names.get(k, k)) for k in used]


def _report_poi_density(POIS, pin_xy):
    """点位密度预检：**摆位之前**就把"哪两个点近到抢地方"算出来，别等出图再肉眼找。

    为什么要有（2026-10-23 大横断海子线实测，为此删点删了三轮）
    --------------------------------------------------------
    1504 逻辑 px 的图上 POI 标签约 150 px 宽、图钉半径 19 px。中段 6 个点
    （黑海子 41.4 / 黑海子垭口 42.6 / 白海子 43.7 / 石头垭口 45.6 /
    长海子 47.2 / 长海垭口 48.8）挤在 7 km 内，图上满屏长引线，
    最后删掉两个垭口点位才干净 —— 而这本来是一个 for 循环就能算出来的。

    **分两档报，别只报一个阈值**（只报一档时要么全是噪音、要么漏掉真问题）：
      · 硬档 `d < 2×图钉半径`（默认 38 px）：两个符号本身会叠在一起，必须处理；
      · 软档 `d < 100 px`：标注会互相挤，Placer 一般靠"一左一右"摆得开，只报数量。
    实测校准：本线 18 点里有 4 对落在硬档（温泉营地×荷花海 19 px 等）——
    出图后确认过：符号是上下贴着，但两条标注分摆两侧、可读，属于"可接受"。
    所以硬档的措辞是"确认要不要合并"，不是"必须删"。
    """
    try:
        pin_r = float(_CFG.get("poi_pin_px", 19.0))
        soft = float(_CFG.get("poi_min_gap_px", 100.0))
    except Exception:                                              # noqa
        pin_r, soft = 19.0, 100.0
    hard = pin_r * 2.0

    pairs = []
    n = len(POIS)
    for i in range(n):
        for j in range(i + 1, n):
            (x1, y1), (x2, y2) = pin_xy[i], pin_xy[j]
            d = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
            if d < soft:
                pairs.append((d, POIS[i]["name"], POIS[j]["name"]))
    pairs.sort()
    if not pairs:
        print(f"   点位密度预检：{n} 个点位，最近间距均 ≥ {soft:.0f} px ✓")
        return []

    h = [p for p in pairs if p[0] < hard]
    s = [p for p in pairs if p[0] >= hard]
    print(f"   点位密度预检：{n} 个点位 · 最近 {pairs[0][0]:.0f} px")
    if h:
        print(f"     · 符号会叠（< {hard:.0f} px = 一个图钉直径）：{len(h)} 对 —— "
              + "、".join(f"{a}×{b} {d:.0f}px" for d, a, b in h))
        print("       → 确认这两条是否值得各占一个图钉；要清爽就合并（如把垭口并入邻近营地）")
    if s:
        print(f"     · 标注偏挤（{hard:.0f}–{soft:.0f} px）：{len(s)} 对 —— "
              + "、".join(f"{a}×{b} {d:.0f}px" for d, a, b in s[:6])
              + ("…" if len(s) > 6 else ""))
        print("       → 通常摆得开（Placer 会分摆两侧）；出图后瞄一眼有没有长引线")
    return pairs


def _anchor_name(x, y, w, h, placer):
    """反查"这块家具最后落在哪个锚位"—— 只为打一行日志，让"动态摆位"可核对。

    Placer 只返回坐标，不返回它选了哪个锚点；而排查"图例为什么跑到这儿"时，
    没有这一行就只能靠量坐标反推。这里按 8 个锚位（含 0/14/28 三档内缩）取曼哈顿最近的那个。
    """
    best, bd = "?", 1e18
    for nm in GC.Placer.ANCHORS:
        for ins in (0, 14, 28):
            try:
                ax, ay = placer._anchor_xy(nm, w, h, ins)      # noqa: SLF001
            except Exception:                                  # noqa
                continue
            d = abs(ax - x) + abs(ay - y)
            if d < bd:
                bd, best = d, nm
    return best


def _legend_scale(map_h):
    """图例缩放系数：默认 FURN_LG，超出「图高 42%」预算时收缩，下限 0.34。

    ⚠ 2026-10-17 北天山中线：9 个日程行 + 6 类符号 → 图例基准高约 560 px，
    固定 0.55 时高达 308 px，在 2240 px 宽的图上还算能放，但日程更多的线
    （10+ 天）会顶到边、甚至被 placer 判为放不下而**整块丢弃**。
    这里按可用高度预算动态缩，宽高与内部排版全部走同一个系数（_LG_S），
    缩的是"整套"，不会出现字号和行距各缩一半的走形。
    """
    g = _legend_geom()
    need = g["h"] * FURN_LG
    budget = max(180.0, map_h * 0.42)
    if need <= budget:
        return FURN_LG
    return max(0.34, FURN_LG * budget / need)


def _legend_size(scale=None):
    """图例尺寸。⚠ 与渲染共用 `_legend_geom()` 的几何，避免"尺寸公式"和"实际排版"各走一套
    （曾经出现脚注压住最后一行符号 —— 卡片太矮，而自检看不出来）。"""
    global _LG_S
    _LG_S = FURN_LG if scale is None else float(scale)
    g = _legend_geom()
    if g["tail"] < LG_MIN_TAIL:                       # 开发期护栏：宁可吵，不要悄悄压字
        print(f"   ⚠ 图例几何异常：脚注与末行间距仅 {g['tail'] * _LG_S:.1f} px"
              f"（应 ≥ {LG_MIN_TAIL * _LG_S:.1f}）—— 调大 LG_BOT")
    return (round(g["w"] * _LG_S), round(g["h"] * _LG_S))


def _nice_bar_km(m_per_px, iw, target_frac=0.16):
    """按底图比例自适应选一个"整数好看"的比例尺长度（km）。

    ⚠ 写死长度是不通用的：短线路（bbox 只有几公里）会被 2 km 的条占满整幅，
    长线路（上百公里）又会显得太小。这里取"地图宽度的 target_frac"当目标像素长，
    再吸附到 0.1/0.2/0.5/1/2/5/… 的整数档。
    """
    target_m = iw * target_frac * m_per_px
    cand = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
    km = min(cand, key=lambda k: abs(k * 1000 - target_m))
    return km


# ---- 全局字号口径（2026-10-24）----
# 用户要求：① 全图字体统一（底图地名 8 px）；② 轨迹标注只比底图地名大 1 px。
# 两个值都可在 route_def.CFG 覆盖。深度/层级改由**颜色与白晕**承担，不再靠字号拉开。
_FS_BASE = float(GC.cfg("label_fs_base", 8) or 8)
_FS_POI = float(GC.cfg("label_fs_poi", _FS_BASE + 1) or (_FS_BASE + 1))


def _load_base_labels():
    """读取 draw_osm 记下的地名锚点 + 样式（栅格 px → 逻辑 px）。

    只取数据，不排位置 —— 位置统一由 `_place_base_labels()` 动态求解。
    """
    try:
        lb = json.loads((GC.OUT / "label_boxes.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("   ⚠ 没有 out/label_boxes.json —— 底图地名不会出现在图上（先跑 make_terrain.py）")
        return []
    except Exception as e:                                         # noqa
        print(f"   ⚠ 底图地名加载失败：{type(e).__name__}: {e}")
        return []
    if not lb.get("labels"):
        print("   ⚠ label_boxes.json 里没有 labels 字段 —— 底图地名不会出现")
        return []
    ss = float(lb.get("ss", 1.0)) or 1.0
    out = []
    for it in lb["labels"]:
        s0 = it["s"] / ss
        # 统一到 3 档字号：14+ 主峰 / 11.5–14 中档 / 其余小号。原来的 22–31（栅格）
        # 换算成逻辑像素是 11–15.5，跟 POI 标注（16）几乎同级，图上一片一样重。
        # 2026-10-24 用户要求「全局字体大小统一、再小一些」：底图地名**只用一个字号**
        # （CFG["label_fs_base"]，默认 10.5），不再按文字宽度分 11.4/10.6/10.0 三档 ——
        # 三档同屏时 0.6–1.4 px 的差异既不构成层级、又让同类地物看着不一样大。
        fs = _FS_BASE
        out.append({
            # 用 draw_osm 记录的**要素真实锚点** ax/ay 当基准（不再用文字框位置当锚点，
            # 否则峰名/湖名的避让起点、引线落点都会偏到文字位置 → "明显偏移"）。
            "t": it["t"], "x": (it.get("ax", it["x"])) / ss, "y": (it.get("ay", it["base"])) / ss,
            "fs": fs, "w": it["s"] / ss, "pri": s0,
            "fill": it["f"], "stroke": it["st"],
        })
    # 重要度按"徒步关心什么"排（2026-10-21）：垭口 > 地名/湖 > 峰 > 河。
    # 拥挤时才需要取舍，所以这个顺序只在"实在没地方"时决定谁被丢。
    def _rank(d):
        f = tuple(d["fill"])
        if f == (150, 78, 24):
            return 1000.0                     # 垭口：路线必经点
        if f[0] == f[1] == f[2] or (abs(f[0] - f[1]) < 6 and abs(f[1] - f[2]) < 6):
            return 800.0 + d["pri"]           # 村/地名
        if f[2] > f[0] + 40:
            return 700.0 + d["pri"]           # 湖 / 河
        return 500.0 + d["pri"] * 8           # 峰：越高越靠前
    for d in out:
        d["rank"] = _rank(d)
    out.sort(key=lambda d: -d["rank"])
    return out


def _near_track(box, track, half_w):
    """矩形是否压到轨迹带（含白色外晕）。"""
    x0, y0, x1, y1 = box
    for x, y in track:
        if x0 - half_w <= x <= x1 + half_w and y0 - half_w <= y <= y1 + half_w:
            return True
    return False


def _poi_dup(it, pois, pin_xy):
    """底图地名与攻略 POI 是同一个地方？→ 丢掉底图那条。

    OSM 的村/营地名与 POI 名往往只差后缀（「措学仁玛」vs「措学仁玛营地」、
    「热嘎」vs「热嘎营地」），draw_osm 的 skip_names 全串匹配拦不住，
    图上就会在同一个点上印两行名字 —— 2026-10-21 实测一处不落。
    判据：名字互相包含（≥2 字）且锚点相距 < 60 逻辑 px。
    """
    t = it["t"].split(" ")[0]
    if len(t) < 2:
        return False
    for d, (px, py) in zip(pois, pin_xy):
        nm = d["name"].split(" · ")[-1]
        if not (t in nm or nm in t):
            continue
        if (it["x"] - px) ** 2 + (it["y"] - py) ** 2 < 60 ** 2:
            return True
    return False


def _place_base_labels(track, half_w, pois=(), pin_xy=()):
    """OSM 地名：矢量层 + 动态避让。

    为什么在**最后**排（2026-10-21）：
      · 先排会让一堆底图地名把好位置占满，POI 营地/垭口标注只能被挤到边上 ——
        而攻略图的主体是轨迹与营地，底图地名应当让路。
      · 先排时 `_OCC` 里还没有图钉、里程点、POI 标注，避让等于没做。
    策略：按重要度从高到低，围绕 draw_osm 给的原锚点试 8 个方向 × 3 档半径；
    找到零重叠的位置就落，找不到就**丢掉这一个**（稀疏区基本不会丢，
    拥挤区先丢次要的湖名/次峰名）。这就实现了"不拥挤区域不做取舍"。
    """
    items = _load_base_labels()
    if not items:
        return ""
    if pois:
        items = [it for it in items if not _poi_dup(it, pois, pin_xy)]
    out, dropped = [], 0
    for it in items:
        fs = it["fs"]
        w = GC.text_w(it["t"], fs) + 4
        h = fs * 1.22
        pad = fs * 0.55
        # 默认位（2026-10-24 用户要求）：**锚点右侧、垂直居中**。只有放不下才动态避让。
        # x0 是文字左边界（text-anchor 默认 start），所以要摆到左边得用 -gap-w。
        gap = 3.2
        vmid = fs * 0.34            # 基线偏移：让文字中线对准锚点
        tried = [(gap, vmid)]
        for k in (0, 1, 2):
            r = fs * 0.8 * (k + 1)
            tried += [(gap, vmid - r), (gap, vmid + r),                      # 右侧上/下错开
                      (-gap - w, vmid),                                      # 左侧水平
                      (-gap - w, vmid - r), (-gap - w, vmid + r),
                      (-w * 0.5, vmid - fs * 1.15 - r * 0.55),               # 正上方
                      (-w * 0.5, vmid + fs * 1.35 + r * 0.55)]               # 正下方
        for dx, dy in tried:
            x0 = it["x"] + dx
            yb = it["y"] + dy
            box = (x0 - pad, yb - fs * 1.02, x0 + w + pad, yb + fs * 0.26)
            if box[0] < 3 or box[2] > IW - 3 or box[1] < 3 or box[3] > IH - 3:
                continue
            if any(GC.rect_overlap_area(box, o) > 0 for o in _OCC):
                continue
            if _near_track(box, track, half_w):
                continue
            _OCC.append(box)
            f, st = it["fill"], it["stroke"]
            c1 = "#%02x%02x%02x" % (f[0], f[1], f[2])
            c2 = "#%02x%02x%02x" % (st[0], st[1], st[2])
            # 2026-10-24 用户反馈「看不出这个地名标的是哪个地方」：避让把字推离锚点后，
            # 图上一根线都没有，读者无从对应。→ 位移 > 5 px 就补一条 0.5 px 细引线。
            # 峰/垭口本身有三角符号（栅格层画的），不再叠圆点，否则像脏点。
            _ax, _ay = it["x"], it["y"]
            _cx = min(max(_ax, box[0]), box[2])
            _cy = min(max(_ay, box[1]), box[3])
            _is_peak = f[0] - f[2] > 12 and f[0] < 110     # 棕色系 = 峰名，锚点已有三角
            if not _is_peak:
                # **每个**地名都在锚点上打一个小圆 —— 这是"这个字标的是哪"最直接的答案。
                # 只给被推远的那几个画圆是不够的：紧贴锚点的名字反而更让人怀疑它指哪儿。
                out.append(f'<circle cx="{_ax:.1f}" cy="{_ay:.1f}" r="1.3" '
                           f'fill="{c1}" fill-opacity="0.9" stroke="#fff" '
                           f'stroke-width="0.7"/>')
            if math.hypot(_cx - _ax, _cy - _ay) > 5:
                out.append(f'<line x1="{_ax:.1f}" y1="{_ay:.1f}" x2="{_cx:.1f}" '
                           f'y2="{_cy:.1f}" stroke="{c1}" stroke-width="0.5" '
                           f'stroke-opacity="0.55"/>')
            t = it["t"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            wt = 700          # 字号统一后不再靠字重区分主次（白晕 + 颜色承担）
            out.append(f'<text x="{x0:.1f}" y="{yb:.1f}" font-size="{fs:.1f}" '
                       f'font-weight="{wt}" fill="{c1}" stroke="{c2}" stroke-width="{max(0.8, fs * 0.13):.2f}" '
                       f'stroke-opacity="0.92" paint-order="stroke" '
                       f'stroke-linejoin="round">{t}</text>')
            break
        else:
            dropped += 1
    if dropped:
        print(f"   底图地名：落位 {len(out)} 条 · 拥挤丢弃 {dropped} 条")
    return "".join(out)


def build_map_overlay(mobile=False):
    """返回地图 SVG 的内容（不含 <svg> 外壳与底图）。"""
    global PS, _PAD, _PIN_R
    global _lg_rect, _sb_rect, _cp_rect
    M = MFS_M if mobile else 1.0
    PS = PS_BASE * M
    _PIN_R = 19 * M
    _PAD = 22
    _OCC.clear()
    global _TRK, _TRK_HW
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
    # ⚠ 线宽与符号要成比例：主线太粗会盖住底图等高线、也压掉点位符号。
    #   2026-10-09 亚丁线「线太粗」→ 5.4/8.6 降到 4.0/6.2；
    #   2026-10-21 珠峰东坡扩窗后线相对更粗、压地名 → 再收到 3.0/4.9。
    ROUTE_W, HALO_W = 3.0 * M, 4.9 * M
    _TRK, _TRK_HW = XY, (ROUTE_W + HALO_W) / 2 + 1.0
    o.append(f'<path d="{d_of(XY)}" fill="none" stroke="#FFFFFF" stroke-width="{HALO_W}" '
             f'stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/>')
    # ---- 分日配色：支持 N 天。优先 CFG["days"] 的 color + CFG["day_bounds"]（km），
    #      没有 day_bounds 时退回单分界 split1（两天）的老行为。
    #      2026-10-25：缺 color 的按 DAY_PALETTE 回退（原先 d.get("color", D1_C)
    #      让 17 条没写色的线路全日同一个橙，分日界在图上完全看不见）。
    _day_cols, _day_txts = GC.day_colors(_CFG.get("days", []))
    _day_cols = _day_cols or [D1_C, D2_C]
    _bounds_km = [0.0] + [float(b) for b in _CFG.get("day_bounds", [])] + [float(TOTAL_KM)]
    if len(_bounds_km) == 2:                      # 无 day_bounds → 老的两段行为
        _bounds_km = [0.0, float(KM[SPLIT_I]), float(TOTAL_KM)]
    while len(_day_cols) < len(_bounds_km) - 1:   # 分段多于颜色 → 沿用同族末色
        _day_cols.append(_day_cols[-1])

    def _idx_km(kmv):
        for i, k in enumerate(KM):
            if k >= kmv:
                return i
        return len(KM) - 1

    _split_is = [_idx_km(b) for b in _bounds_km]
    for si in range(len(_bounds_km) - 1):
        i0, i1 = _split_is[si], _split_is[si + 1]
        seg = XY[i0:max(i1 + 1, i0 + 2)]
        if len(seg) < 2:
            continue
        o.append(f'<path d="{d_of(seg)}" fill="none" stroke="{_day_cols[si]}" '
                 f'stroke-width="{ROUTE_W}" stroke-linecap="round" stroke-linejoin="round"/>')

    # ---- 底图地名（矢量层）：占位记在这里，最后统一摆位后再插回来 ----
    #   （要等图钉 / 里程点 / POI 标注都摆完，_OCC 才完整，避让才算数）
    _LBL_AT = len(o)

    # 每 5 km 里程点
    # ⚠ 三位数（105 km 等）在固定 r=7.4 的圆里必然溢出 → 用户看到「105 挤在圈里」。
    #   按位数自适应：三位数加大半径 + 略缩字号，保证文字完整落在圆内。
    for target in range(5, int(TOTAL_KM) + 1, 5):
        i = min(range(len(KM)), key=lambda j: abs(KM[j] - target))
        x, y = XY[i]
        z = MFS_S if mobile else 1.0
        nd = len(str(target))
        rr = (7.4 if nd <= 2 else 9.8) * z
        fs = (10.0 if nd <= 2 else 8.8) * z
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr:.1f}" fill="#FFFFFF" '
                 f'stroke="#3A4250" stroke-width="{2*M:.1f}"/>'
                 f'<text x="{x:.1f}" y="{y+fs*0.38:.1f}" text-anchor="middle" '
                 f'font-size="{fs:.1f}" font-weight="800" '
                 f'fill="#3A4250">{target}</text>')

    # 分日界短线（每个分界一条；无 day_bounds 时只画 split1）
    for _bi in _split_is[1:-1] if len(_split_is) > 2 else [SPLIT_I]:
        sx, sy = XY[_bi]
        # ⚠ 2026-10-21：原来这条分日界是"白色胶囊底 + 2.4 粗虚线"，线宽收到 3.0 之后
        #   它反而比主线还抢眼（图上像一排白色药丸）。改成细虚线 + 极窄白底。
        o.append(f'<line x1="{sx-36*M:.0f}" y1="{sy:.1f}" x2="{sx+36*M:.0f}" y2="{sy:.1f}" '
                 f'stroke="#FFFFFF" stroke-width="{2.4*M:.1f}" stroke-linecap="round" '
                 f'opacity="0.85"/>'
                 f'<line x1="{sx-36*M:.0f}" y1="{sy:.1f}" x2="{sx+36*M:.0f}" y2="{sy:.1f}" '
                 f'stroke="#6C7480" stroke-width="{1.15*M:.1f}" '
                 f'stroke-dasharray="{7*M:.0f} {5*M:.0f}" stroke-linecap="round"/>')

    pin_xy = [P(d["lon"], d["lat"]) for d in POIS]
    if not mobile:
        _report_poi_density(POIS, pin_xy)

    # ---- 家具先摆（硬约束：不压轨迹、互不重叠）；标注随后绕开它们 ----
    placer = GC.Placer(IW, IH, track=XY, pad=_PAD, track_half_w=(ROUTE_W + HALO_W) / 2 + 2)
    placer.pins = pin_xy
    for px, py in pin_xy:
        _OCC.append((px - _PIN_R, py - _PIN_R, px + _PIN_R, py + _PIN_R))

    lg_rows = _legend_rows()
    lg_pad = round(12 * M)
    # 图例动态缩小：先按图高预算算一档，放不下再逐档收缩重试（2026-10-17）
    _lg_scale = _legend_scale(IH)
    LG_W, LG_H = _legend_size(_lg_scale)
    # ⚠ 比例尺：色条长度**绝不能被 FURN_* 缩放**（否则"1 km"就不等于 1 km 了）。
    #   而且底衬框必须比色条宽（含内衬），否则色条会戳出框外 ——
    #   2026-10-09 实测就是"白底方框比色条窄、色条压在底图花纹上"，用户一眼看出不协调。
    # 比例尺长度**按底图比例自适应**（取整数档，目标 ≈ 地图宽度的 16%）。
    # 本图 1 km ≈ 80 逻辑 px，自动选出的正是 2 km —— 但换一条线路（bbox 大小不同）时
    # 写死 2 km 就会要么占满整幅、要么小到看不清。档位标签统一 0 / 一半 / 全长。
    sb_km = _nice_bar_km(M_PER_PX, IW)
    sb_bar = sb_km * 1000 / M_PER_PX
    sb_fs = 12 * M                                 # 数字字号（信息，不缩放）
    sb_th = 4.4 * M                                # 色条高度
    sb_padx = round(11 * M * FURN_SB)              # 左右内衬（装饰，可缩）
    SB_W = round(sb_bar + 2 * sb_padx)
    sb_top = round(4 * M)
    sb_gap = round(4.5 * M)
    SB_H = round(sb_top + sb_fs + sb_gap + sb_th + 6 * M)
    CP_R = round(42 * M * FURN_CP)          # 42 → 24
    CP_W = CP_H = CP_R * 2 + 16 * M

    placed = {}
    if not mobile and _legend_place() == "map":   # 手机版过小走页内图例；band/title 另画
        # 落点首选顺序：默认 tl→tr→br→bl（旧行为）。用户点名"图例放左下角"这类要求时
        # 用 CFG["legend_corner"]="bl" 把它提到最前 —— **只改顺序、不改避让规则**：
        # 硬约束仍是"不压轨迹、不压标注"，左下角真放不下才会退到下一角（不会硬摆压线）。
        _corner = str(_CFG.get("legend_corner") or "").strip().lower()
        _default = ["tl", "tr", "br", "bl", "ml", "mr", "tc", "bc"]
        _pref = ([_corner] + [c for c in _default if c != _corner]) if _corner else _default
        # ⚠ 图例放不下时**缩到能放下为止**，不要直接丢弃（旧行为：一次失败就整块没了，
        #   用户看到"图例呢？"）。逐档收缩重试，_legend_size() 会同步刷新 _LG_S，
        #   渲染代码读同一个 _LG_S，尺寸与排版永远不会打架。
        for _mult in (1.0, 0.86, 0.74, 0.62, 0.5):
            LG_W, LG_H = _legend_size(_lg_scale * _mult)
            got = placer.place(LG_W + 2 * lg_pad, LG_H + 2 * lg_pad, prefer=tuple(_pref))
            if got:
                # place() 返回 (x, y, rect)；家具占位登记的是 rect（含内衬那一圈）
                _lg_rect = (got[0] + lg_pad, got[1] + lg_pad, got[0] + lg_pad + LG_W,
                            got[1] + lg_pad + LG_H)
                _OCC.append(got[2])
                placed["lg"] = got
                _an = _anchor_name(got[0], got[1], LG_W + 2 * lg_pad,
                                   LG_H + 2 * lg_pad, placer)
                print(f"   图例：{_an} 角 · {LG_W}×{LG_H} px · 缩放 {_lg_scale * _mult:.2f}"
                      f"（日程 {len(lg_rows)} 行 + 符号 {len(_legend_symbols())} 类）"
                      + ("· 已缩档才放下" if _mult < 1.0 else ""))
                break
                break
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

    # ---- 标注：越挤越先摆；字号动态收缩 + 位置动态避让 ----
    # ⚠ 2026-10-17 北天山中线（26 个标注、143 km 长线）实测：固定字号 + 只试纵向偏移时，
    #   后摆的标注必然压先摆的。改成两级动态：
    #   ① **字号阶梯**：从基准字号逐档缩小（1.0 → 0.6），取第一个能"零重叠"落位的档；
    #      全都放不下时用重叠面积最小的那档（并偏向大字号）。
    #   ② **位置搜索**：见 _layout() 的 8 方向 × 4 档半径，配置的 dx/dy 仍排首选。
    #   灰线指引（pin → label）保留：位置外扩后没有它就读不出标注指向哪。
    FS_LADDER = (1.0, 0.92, 0.84, 0.76, 0.68, 0.6)

    def _crowd(i):
        px, py = pin_xy[i]
        return min(((px - pin_xy[j][0]) ** 2 + (py - pin_xy[j][1]) ** 2) ** 0.5
                   for j in range(len(pin_xy)) if j != i)

    for i in sorted(range(len(POIS)), key=_crowd):
        d = POIS[i]
        name, ele, k = d["name"], d["ele"], d["kind"]
        dx, dy, anc = d["dx"], d["dy"], d["anchor"]
        x, y = pin_xy[i]
        # 标注字号**不跟符号缩放走**：图钉要小（0.66），字要读得清。
        # 2026-10-21 实测：字跟着 PS 缩到 9.6 之后，比底图地名（10–11.4）还小，
        # 白晕又占掉 1/4 笔画 —— POI 名看着发虚发灰，主次颠倒。
        base = _FS_POI          # 全图统一口径：轨迹标注 = 底图地名 + 2 px（起终点也不例外）
        col = D2_T if k in ("start", "end") else INK
        if mobile:
            txt = getattr(RD, "SHORT", {}).get(name, name)
        else:
            txt = f"{name} {RD.fmt_ele(ele)} m"
        best = None
        for sc in FS_LADDER:
            fs = base * sc
            w = GC.text_w(txt, fs) + 26 * M
            r = _layout(x, y, dx * M, dy * M, anc, w, fs * 0.86, commit=False)
            # 优先零重叠；都不零重叠时取面积最小、并列取字号大
            if best is None or r[4] < best[4] - 1e-6 or \
               (abs(r[4] - best[4]) <= 1e-6 and sc > best[5]):
                best = r + (sc,)
            if r[4] == 0:
                break
        tx, ty, ta, box, area, sc = best
        fs = base * sc
        t = txt
        if not mobile:
            t = (f'{name}<tspan font-size="{fs*0.78:.1f}" font-weight="700" fill="#5F6875"> '
                 f'{RD.fmt_ele(ele)} m</tspan>')
        _OCC.append(box)                      # 只有最终选中的方案才占位
        _LABELS.append(box)                   # ⚠ 自检读 _LABELS：漏登记会变成"0 标注 = 0 重叠"的假通过
        LABEL_OF[name] = box
        gap = max(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))
        if gap > 34 * M:
            px = min(max(x, box[0]), box[2])
            py = min(max(y, box[1]), box[3])
            o.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="#FFFFFF" stroke-width="{2.6*M:.1f}" stroke-linecap="round"/>'
                     f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="{col}" stroke-width="{1.4*M:.1f}" stroke-linecap="round" '
                     f'opacity="0.75"/>')
        # ⚠ 白描边要"细而净"：5.4*PS（≈4.6 逻辑 px / 9 栅格 px）在浅色地形上会糊成
        #   一圈白阴影（用户原话"字体看上去有白色的阴影"）。收到 2.4*PS ≈ 字号的 13%，
        #   读起来是"描边"而不是"发光"，同时仍能压住底图等高线。
        o.append(f'<text x="{tx}" y="{ty+7.4*PS:.1f}" text-anchor="{ta}" font-size="{fs:.1f}" '
                 f'font-weight="700" fill="{col}" stroke="#FFFFFF" stroke-width="{max(1.1, fs * 0.17):.2f}" '
                 f'stroke-opacity="0.92" paint-order="stroke" stroke-linejoin="round">{t}</text>')

    # ---- 落位绘制 ----
    if "lg" in placed:
        lx, ly = _lg_rect[0], _lg_rect[1]
        S = _LG_S                                # 与 _legend_size() 同一系数（动态缩小后仍一致）
        g = [f'<g class="il"><rect x="{lx}" y="{ly}" width="{LG_W}" height="{LG_H}" rx="{round(14*S)}" '
             f'fill="#FFFFFF" opacity="0.96" stroke="#D9D2C6" stroke-width="1.4"/>',
             f'<text x="{lx+18*S:.0f}" y="{ly+LG_TITLE*S:.0f}" font-size="{round(22*S)}" '
             f'font-weight="800" fill="{INK}">{_legend_title_txt()}</text>']
        for i, (col, tag, desc, km, w, dash) in enumerate(lg_rows):
            ry = ly + (LG_ROW0 + LG_PITCH * i) * S
            # ⚠ 样本线要压在文字的**视觉中线**上，不能随手给个负偏移：
            #   文字基线在 ry+11*S、字号 19*S，字形中线 ≈ 基线 − 0.35×字号 ≈ ry+4.4*S。
            #   原先写 ry-7*S，比文字中心高 11*S（≈6 逻辑 px）—— 看上去就是"线条错位"。
            line_y = ry + (LG_BASE - 6.6) * S
            g.append(f'<line x1="{lx+18*S:.0f}" y1="{line_y:.0f}" x2="{lx+78*S:.0f}" y2="{line_y:.0f}" '
                     f'stroke="{col}" stroke-width="{w:.1f}" stroke-linecap="round"{dash}/>')
            g.append(f'<text x="{lx+88*S:.0f}" y="{ry+LG_BASE*S:.0f}" font-size="{round(19*S)}" '
                     f'font-weight="800" fill="{col}">{tag}</text>'
                     f'<text x="{lx+LG_DESC_X*S:.0f}" y="{ry+LG_BASE*S:.0f}" font-size="{round(17*S)}" '
                     f'font-weight="600" fill="#4A5261">{desc}</text>')
            if km:
                g.append(f'<text x="{lx+LG_W-18*S:.0f}" y="{ry+LG_BASE*S:.0f}" text-anchor="end" '
                         f'font-size="{round(17*S)}" font-weight="800" fill="#3A4250">{km}</text>')

        # ---- 符号说明（两列网格）：让图上出现的每种符号都在图例里有样本 ----
        syms = _legend_symbols()
        if syms:
            # 分隔线 / 符号行位置全部走 LG_* 常量，与 _legend_size() 严格同源
            last_row_y = ly + (LG_ROW0 + LG_PITCH * max(0, len(lg_rows) - 1)) * S
            sy = last_row_y + (LG_BASE + LG_DIV_GAP) * S
            g.append(f'<line x1="{lx+18*S:.0f}" y1="{sy:.0f}" x2="{lx+LG_W-18*S:.0f}" y2="{sy:.0f}" '
                     f'stroke="#E2DACD" stroke-width="{1.4*S:.1f}"/>')
            col_w = (LG_W - 36 * S) / 2.0
            glyph_scale = LG_GLYPH
            for i, (kind, nm) in enumerate(syms):
                cx = lx + 34 * S + (i % 2) * col_w
                cy = sy + (LG_SYM0 + LG_SYM_PITCH * (i // 2)) * S
                # 复用真实符号：整体 scale 缩小，保证"图例样本 = 图上符号"
                g.append(f'<g transform="translate({cx:.1f},{cy:.1f}) scale({glyph_scale})">'
                         f'{mk_pin(0, 0, kind)}</g>')
                g.append(f'<text x="{cx+22*S:.0f}" y="{cy+7*S:.0f}" font-size="{round(17*S)}" '
                         f'font-weight="700" fill="#4A5261">{nm}</text>')
        g.append(f'<text x="{lx+18*S:.0f}" y="{ly+LG_H-14*S:.0f}" font-size="{round(16.5*S)}" '
                 f'font-weight="600" fill="#7A8290">{_CFG.get("legend_foot", "全线为实测轨迹 · 非导航用图")}</text>')
        g.append("</g>")
        o.append("".join(g))

    if "cp" in placed:
        _, _, cr = placed["cp"]
        gx, gy = (cr[0] + cr[2]) / 2, (cr[1] + cr[3]) / 2
        R = CP_R
        o.append(f'<g><circle cx="{gx:.0f}" cy="{gy:.0f}" r="{R+M:.0f}" fill="#FFFFFF" opacity="0.95"/>'
                 f'<path d="M {gx:.0f},{gy-R*0.71:.0f} L {gx+R*0.26:.0f},{gy+R*0.29:.0f} '
                 f'L {gx:.0f},{gy+R*0.12:.0f} L {gx-R*0.26:.0f},{gy+R*0.29:.0f} Z" fill="#3A4250"/>'
                 f'<text x="{gx:.0f}" y="{gy+R*0.9:.0f}" text-anchor="middle" '
                 f'font-size="{round(14*M):d}" font-weight="800" fill="#3A4250">N</text></g>')

    if "sb" in placed:
        sx0, sy0, srect = placed["sb"]
        bw, bh = srect[2] - srect[0], srect[3] - srect[1]
        fs = sb_fs                                   # 与卡高同一口径（见上面的说明）
        th = sb_th
        bx = sx0 + (bw - sb_bar) / 2.0               # 色条在底衬框内居中
        num_y = sy0 + sb_top + fs * 0.95             # 数字基线：留出字高，不溢出卡顶
        bar_top = num_y + sb_gap
        bar_bottom = bar_top + th
        o.append(f'<g><rect x="{sx0}" y="{sy0}" width="{bw:.0f}" height="{bh:.0f}" '
                 f'rx="{round(6*M)}" fill="#FBFAF7" opacity="0.94" stroke="#CFC7B9" '
                 f'stroke-width="{1.1*M:.1f}"/>'
                 f'<rect x="{bx:.1f}" y="{bar_top:.1f}" width="{sb_bar/2:.1f}" '
                 f'height="{th:.1f}" fill="#3A4250"/>'
                 f'<rect x="{bx+sb_bar/2:.1f}" y="{bar_top:.1f}" width="{sb_bar/2:.1f}" '
                 f'height="{th:.1f}" fill="#FFFFFF" stroke="#3A4250" stroke-width="{1.1*M:.1f}"/>'
                 f'<line x1="{bx+sb_bar/2:.1f}" y1="{bar_top-1.6*M:.1f}" '
                 f'x2="{bx+sb_bar/2:.1f}" y2="{bar_bottom+1.6*M:.1f}" stroke="#3A4250" '
                 f'stroke-width="{1.1*M:.1f}"/>'
                 f'<text x="{bx:.1f}" y="{num_y:.1f}" font-size="{fs:.1f}" '
                 f'font-weight="700" fill="#3A4250">0</text>'
                 f'<text x="{bx+sb_bar/2:.1f}" y="{num_y:.1f}" text-anchor="middle" '
                 f'font-size="{fs:.1f}" fill="#3A4250">{sb_km/2:g}</text>'
                 f'<text x="{bx+sb_bar:.1f}" y="{num_y:.1f}" text-anchor="end" '
                 f'font-size="{fs:.1f}" font-weight="700" fill="#3A4250">{sb_km:g} km</text></g>')
    o.insert(_LBL_AT, _place_base_labels(XY, (ROUTE_W + HALO_W) / 2 + 2,
                            POIS, pin_xy))
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
    # ⚠ 护栏：标注数必须等于 POI 数。_LABELS 漏登记时自检会报「0 标注 · 0 重叠」——
    #   看着全绿，其实一个标注都没查（2026-10-17 北天山中线实测踩过：
    #   _layout 改成纯函数后调用方忘了补 _LABELS.append，自检假通过了一整轮）。
    _n_poi = len(GC.pois_of(RD))
    if n != _n_poi:
        print(f"      ⚠ 标注数 {n} ≠ POI 数 {_n_poi} —— 自检样本不完整，"
              f"重叠/越界结论不可信（查 _LABELS 登记）")
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


TITLE_H = 132               # 名称带高度（逻辑像素），仅独立高清图使用
# 图例塞进标题带时带子要加高：132 px 只够把 3 行图例顶到数据行鼻子上
# （2026-10-23 用户实测"融在一起了"）。加高 16 px 只影响用 legend_place="title" 的线，
# 其他模式的画布高一个字都不变。地图本身大小不变，只是它在画布里的 y 起点下移。
TITLE_H_LEGEND = 158

# ---- 标题带 / 图例带配色 ----
# 原来用暖纸色 #F6F0E3，和底图（冷调植被带，采样中位 (125,139,109)）放一起发闷、发"脏"。
# 现在取**同色相、高明度、低饱和**的一档（G 略高于 R、B 最低），等于把地形色提亮压灰，
# 带上带下是一套色。文字也跟着换：小字从 #8A7A66（对旧底只有 3.66:1，本来就不达标）
# 换成 #5F6B60（对 #E9EEE7 是 4.75:1）。
TITLE_BG = "#E9EEE7"
TITLE_LINE = "#C6D0C4"
TITLE_INK_MUTED = "#5F6B60"


def out_size(with_title=False):
    """输出逻辑尺寸。带名称带时高度加 TITLE_H —— render_map_hi 要按它算画布高。

    `legend_outside` 打开时，独立高清图下方还要再加一条图例带（高度由
    `_legend_band_geom()` 算）。⚠ 画布高与 viewBox 高必须同源，否则会被 letterbox。
    """
    h = IH + (_title_h() if with_title else 0)
    if with_title and _legend_outside():
        h += _legend_band_geom()["h"]
    return IW, h


def _legend_place():
    """图例放哪：`"map"`（图面内，默认）/ `"band"`（图下方图例带）/ `"title"`（标题带里）。

    优先级：显式 `CFG["legend_place"]` > 旧的 `CFG["legend_outside"]`（→"band"）> "map"。
    保留 legend_outside 是为了老线路不用改配置（珠峰东坡等已在用它）。
    """
    p = str(_CFG.get("legend_place") or "").strip().lower()
    if p in ("map", "band", "title"):
        return p
    return "band" if _legend_outside() else "title"


def _title_legend(x_right, y_bottom):
    """标题带里的紧凑图例（`CFG["legend_place"]="title"`）。

    为什么要有（2026-10-23 用户指着截图问「能不能放这片区域」）
    ---------------------------------------------------------
    满幅地形图上，图例卡压哪儿都盖一片山；而**标题带中间本来就是空的**（左边标题、
    右边数据口径）。把它放进去，图面一点不浪费；位置按用户要求"靠右下角"——
    右缘与数据行对齐（`x_right`），底缘贴带子下沿（`y_bottom`）。

    排版"两簇并排、底边对齐"：左簇符号说明（3×n 网格），右侧上为分日格子（最多 4 列），
    下为附加线（雪线 / 步道）横排一行。
    ⚠ **必须留出标题块的宽度**：`title_lite` 一长就会和图例第一列撞在一起
    （2026-10-23 实测「（贡雅线 鸡荷段 · 7 天 6 晚 · 重装野线）」直接压在「起点 终点」上）。
    这里先量标题块的右缘，算 `avail = x_right - 标题右缘`，整块按 `k = avail/需要的宽`
    等比缩放（下限 0.62），再靠右下角定位 —— 换条线标题再长也不会撞。
    """
    day_rows, extra_rows = _legend_split_rows()
    syms = _legend_symbols()

    FS, FS_T, FS_K = 11.0, 11.5, 10.5
    PITCH, GAP_COL, GAP_BLOCK = 26.0, 22.0, 34.0
    LINE_W, TAG_DX, DESC_GAP = 22.0, 28.0, 8.0

    def entry_w(row):
        _c, tag, desc, km = row[0], row[1], row[2], row[3]
        w = TAG_DX + GC.text_w(tag, FS_T) + DESC_GAP + GC.text_w(desc, FS)
        if km:
            w += 16 + GC.text_w(km, FS_K)   # 里程与描述之间留 16 px：8 px 时中文描述的实际字形宽会把空隙吃掉
        return w

    # ---- 分日格子：最多 4 列；附加行（雪线 / 步道）单独占最后一行 ----
    # ⚠ 早先把附加行也塞进同一网格：它们的描述特别长（"OSM 测绘的既有步道 / 牧道 /
    #   机耕路（非本次轨迹）"），会把整列撑宽 —— 同列的 D1/D4 就跟里程数字拉开一大截空，
    #   博格达实测 100+ px，看着像漏打了个字。现在附加行自成一行，列宽只由分日行决定。
    days = list(day_rows)
    # ---- 每行放几天：**动态算，不写死 4 列** ----
    # 用户口径（2026-10-23）："较多空白，可以动态调整每行放的天数，最后整体右移"。
    # 5 天按 4 列排 → 第二行只剩 D5，右边 3 格全空（博格达实测一大片空白）；
    # 按 3 列排 → 3 + 2，只空 1 格，且整块窄了、右对齐后自然整体右移。
    # 判据：先在**行数预算**内（多了会顶到数据行）挑"空格最少"的列数，同分再挑列多的（更矮）。
    _avail_h = y_bottom - 68                       # 68 = 数据行基线 52 + 下缘 16
    _max_rows = max(1, int(_avail_h // PITCH))
    best = None
    for _c in range(1, 5):
        if not days:
            break
        _rows = -(-len(days) // _c)
        if _rows + (1 if extra_rows else 0) > _max_rows:
            continue
        _score = (_rows * _c - len(days), _rows)   # ①空格少 ②行少
        if best is None or _score < best[0]:
            best = (_score, _c)
    ncol = best[1] if best else min(4, max(1, len(days))) if days else 1
    nrow = -(-len(days) // ncol) if days else 0
    cols = [[] for _ in range(ncol)]
    for i, r in enumerate(days):
        cols[i % ncol].append(r)                        # 列优先：横读是 D1 → D2 → D3 → D4
    col_w = [max([entry_w(r) for r in c] or [0]) for c in cols]
    day_w = sum(col_w) + GAP_COL * (ncol - 1)

    ext_w = 0.0
    for i, r in enumerate(extra_rows):
        ext_w += entry_w(r) + (GAP_COL * 2 if i else 0)

    nrow_all = nrow + (1 if extra_rows else 0)
    # 符号簇的列数**跟着分日格子的行数走**：两簇行数不一致时（比如 5 类符号 3 列 2 行、
    # 分日 2 行 + 附加 1 行 = 3 行），底边对齐会让符号簇孤零零吊在下面、上方一片空。
    # 按 "ceil(符号数 / 行数)" 反算列数，两簇就等高，读起来是一个整体。
    sym_cols = min(4, max(1, -(-len(syms) // max(1, nrow_all)))) if syms else 1
    sym_cols = min(sym_cols, max(1, len(syms)))
    sym_rows = -(-len(syms) // sym_cols)
    sym_cell = max([16 + 6 + GC.text_w(nm, FS) for _k, nm in syms] or [0]) + 14
    sym_w = sym_cell * sym_cols

    grid_w = max(day_w, ext_w)
    total = sym_w + GAP_BLOCK + grid_w
    h = max(nrow_all, sym_rows) * PITCH

    # 标题块右缘：只算**和图例同高**的两行（主标题 82 / 副题 112）。
    # badge 在 y=30、图例顶边在 y≈50 以下，纵向不重叠，不必为它让宽 ——
    # 早先把 badge 也算进来（且 `&nbsp;` 被当成 6 个字量宽），avail 直接被压到 535 px。
    x_left = 40
    t = _CFG.get("title") or _CFG.get("file_stem") or ""
    sub = _CFG.get("title_lite") or ""
    _right = x_left + max(GC.text_w(t, 44) + 2, GC.text_w(sub, 16))
    avail = x_right - (_right + 26)
    # 宽度和高度都要过：行数多到顶住数据行时，也等比缩下去（别只按宽度算）
    k = max(0.62, min(1.0, avail / total, _avail_h / h)) if total else 1.0

    # 本地坐标原点 = 块左上角；整块 translate 到右下角落点再 scale
    ox = x_right - total * k
    oy = y_bottom - h * k
    o = [f'<g transform="translate({ox:.1f},{oy:.1f}) scale({k:.3f})">']

    for i, (kind, nm) in enumerate(syms):
        cx = (i % sym_cols) * sym_cell + 7
        cy = (nrow_all - sym_rows + i // sym_cols) * PITCH + 15
        o.append(f'<g transform="translate({cx:.1f},{cy:.1f}) scale(0.52)">{mk_pin(0, 0, kind)}</g>')
        o.append(f'<text x="{cx + 16:.1f}" y="{cy + 4:.1f}" font-size="{FS:.1f}" '
                 f'font-weight="700" fill="#4A5261">{_esc(nm)}</text>')

    def _entry(row, lx, base, w_col):
        """画一格：色标 + 日标 + 描述 + 右对齐里程（`w_col` = 本列右缘）。"""
        c, tg, desc, km = row[0], row[1], row[2], row[3]
        g = [f'<line x1="{lx:.1f}" y1="{base - 4:.1f}" x2="{lx + LINE_W:.1f}" '
             f'y2="{base - 4:.1f}" stroke="{c}" stroke-width="3" stroke-linecap="round"/>',
             f'<text x="{lx + TAG_DX:.1f}" y="{base:.1f}" font-size="{FS_T:.1f}" '
             f'font-weight="800" fill="{c}">{_esc(tg)}</text>']
        tx = lx + TAG_DX + GC.text_w(tg, FS_T) + DESC_GAP
        g.append(f'<text x="{tx:.1f}" y="{base:.1f}" font-size="{FS:.1f}" '
                 f'font-weight="600" fill="#4A5261">{_esc(desc)}</text>')
        if km:
            g.append(f'<text x="{lx + w_col:.1f}" y="{base:.1f}" text-anchor="end" '
                     f'font-size="{FS_K:.1f}" font-weight="800" fill="#6B7280">{_esc(km)}</text>')
        return "".join(g)

    dx = sym_w + GAP_BLOCK
    for ci, col in enumerate(cols):
        for ri, row in enumerate(col):
            lx = dx + sum(col_w[:ci]) + GAP_COL * ci
            o.append(_entry(row, lx, ri * PITCH + 15, col_w[ci]))

    # 附加行（雪线 / 步道）：分日格子下面横排一行，不参与列宽计算
    if extra_rows:
        lx = dx
        for row in extra_rows:
            w = entry_w(row)
            o.append(_entry(row, lx, nrow * PITCH + 15, w))
            lx += w + GAP_COL * 2
    o.append('</g>')
    if k < 0.999:
        print(f"   标题带图例：按 {k:.2f} 缩放到 {total * k:.0f}×{h * k:.0f} px"
              f"（需要 {total:.0f}，可用 {avail:.0f}）")
    return "".join(o)


def _title_h():
    """名称带当前高度：图例搬进带子里时要更高一点（见 TITLE_H_LEGEND 的注释）。"""
    return TITLE_H_LEGEND if _legend_place() == "title" else TITLE_H


def _title_band():
    """地图名称带（2026-10-20 与贡嘎大环对齐）：整幅图**正上方**一条标题栏。

    为什么放在图外而不是压在图上：满幅都是等高线与地名，任何角标都会挡东西；
    单独一条带既给出线路名称，也顺手把里程/爬升口径写在图上，截图发出去也知道这是什么线。
    HTML 内嵌版不要它（页面已有 <h1>），由 render_map_hi 传 with_title=True 打开。
    """
    t = _CFG.get("title") or _CFG.get("file_stem") or "全线地图"
    sub = _CFG.get("title_lite") or _CFG.get("sub") or ""
    tag = _CFG.get("badge") or _CFG.get("meta") or "全线地图"
    tag = tag.replace("<b>", "").replace("</b>", "").replace("<em>", "").replace("</em>", "")
    x0 = 40
    # ⚠ 名称带底色不能用近白（#FFFDF8）：render_map_hi 的满幅校验把"整行全白"判为裁切，
    #   一条纯白的带会直接让 16 条线路全部报错。用暖纸色，既过校验也更像地图边框。
    _th = _title_h()
    # 三行文字的纵向节奏（2026-10-23 用户要求"最美观 + 动态随带高"）：
    #   badge(15px) → title(44px) → subtitle(16px)
    #   视觉净空比 = badge→title : title→subtitle ≈ 1.55 : 1（疏→密，标题组更紧凑）
    #   基线按 H 的 0.18 / 0.58 / 0.84 锚定，字号不缩放、间隙等比。
    #   H=158 → y = 28/92/133（净空 27/18）；H=132 → y = 24/77/111（净空 16/11）
    _y_badge = round(_th * 0.18)
    _y_title = round(_th * 0.58)
    _y_sub   = round(_th * 0.84)
    o = [f'<rect x="0" y="0" width="{IW}" height="{_th}" fill="{TITLE_BG}"/>',
         f'<text x="{x0}" y="{_y_badge}" font-size="15" font-weight="700" fill="{TITLE_INK_MUTED}" '
         f'letter-spacing="2.5">全线地图 · {tag}</text>',
         f'<text x="{x0 - 2}" y="{_y_title}" font-size="44" font-weight="900" fill="#1F2430" '
         f'letter-spacing="1">{_esc(t)}</text>']
    if sub:
        o.append(f'<text x="{x0}" y="{_y_sub}" font-size="16" font-weight="600" fill="#55605A">'
                 f'{_esc(sub)}</text>')
    # 数据口径（可选）：CFG["title_stats"] 一行右对齐 —— 这张图会被单独发出去，
    # 光有名字没有里程/爬升，读者还是要回头问"多长、多高"。
    # 不配时自动从 route_def 生成「总里程 · +爬升 / −下降」。
    _stats = _CFG.get("title_stats")
    if not _stats:
        try:
            _stats = f"{RD.TOTAL_KM:.1f} km · +{RD.ASC} / −{RD.DESC} m"
        except Exception:
            _stats = None
    if _stats:
        o.append(f'<text x="{IW - 40}" y="52" text-anchor="end" font-size="17" '
                 f'font-weight="700" fill="#4A5261">{_esc(_stats)}</text>')
    # 图例放标题带时整块搬进来（右缘贴数据行、底缘贴带子下沿）；否则只留分日色条
    if _legend_place() == "title":
        # 底缘留 8 px（带子下沿之上）—— 用户口径：默认 8 px，再少就顶到边了
        o.append(_title_legend(IW - 40, _th - 8))
    # 分日色条：与图例同色，先给读者一个"这是几天线"的暗示
    days = _CFG.get("days", [])
    if days and _legend_place() != "title":
        bx = IW - 40 - len(days) * 34
        for i, d in enumerate(days):
            o.append(f'<rect x="{bx + i * 34:.0f}" y="96" width="28" height="6" rx="3" '
                     f'fill="{d.get("color", "#888")}"/>'
                     f'<text x="{bx + i * 34 + 14:.0f}" y="116" text-anchor="middle" '
                     f'font-size="10" font-weight="700" fill="{TITLE_INK_MUTED}">{d.get("tag", "")}</text>')
    o.append(f'<line x1="0" y1="{_th - 1}" x2="{IW}" y2="{_th - 1}" '
             f'stroke="{TITLE_LINE}" stroke-width="2"/>')
    return "".join(o)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------- 图例带（可选）
# CFG["legend_outside"] = True → 图例**不压在图面上**，改画在图下方一条带里。
# 为什么要有它：本技能的地图是满幅地形，四角没有空白，图例压哪儿都会盖掉一片山 ——
# 2026-10-21 珠峰东坡实测，图例卡正压在珠峰东坡—马卡鲁北侧的冰川群上，
# 而那恰恰是徒步者最想看的一块（用户原话「图例遮住了想看的山峰冰川区域」）。
# 带子与标题带同底色（TITLE_BG），视觉上是海报的下缘，不是"盖在图上的一块白"。
# HTML 内嵌版：图例改由页面文字图例承担（build_guide._legend_html），图上不再画卡片。
BD_ROW0 = 52          # 带内第一行基线（标题行之下）
BD_PITCH = 29         # 行距（密排：同内容比两栏版矮一半以上）
BD_PADT = 14
BD_PADB = 16
BD_COLS = 4           # 通栏列数（可用 CFG["legend_band_cols"] 覆盖）


def _legend_outside():
    return bool(_CFG.get("legend_outside"))


def _legend_split_rows():
    """把图例行拆成「分日路段」与「附加线（雪线 / 步道）」两段。

    `_legend_rows()` 的顺序是「先分日、后附加」，附加行由 `CFG["legend_extra"]` 提供，
    所以按 `len(CFG["days"])` 切开即可，不依赖行的内容。
    """
    rows = _legend_rows()
    n = min(len(rows), len(_CFG.get("days", [])) or len(rows))
    return rows[:n], rows[n:]


def _legend_band_items():
    """图例带的条目，一律「色标 + 名称 + （里程）」，供通栏网格排布。

    ⚠ 分日行的 `legend` 文本自带前缀（CFG 里写的是 "D1 优帕村 → 晓乌措营地"），
    而色标旁已经有一个 D1，**必须把前缀剥掉**，否则会印成「D1 D1 优帕村…」
    （2026-10-21 两栏版实测就是这么印的，只是块大看着不明显）。
    """
    day_rows, extra_rows = _legend_split_rows()
    items = []
    for col, tag, desc, km, w, dash in day_rows:
        t = desc.strip()
        if t.startswith(tag):
            t = t[len(tag):].lstrip(" 　·-→")
        items.append(("day", col, tag, t, km, None))
    for kind, nm in _legend_symbols():
        items.append(("sym", None, "", nm, "", kind))
    # 附加行（雪线 / 步道）说明偏长，图例带里用 CFG 给的 `short` 版，
    # 完整口径留给 HTML 页面的文字图例（那里横排宽度够）。
    short = {it.get("tag"): it.get("short")
             for it in (_CFG.get("legend_extra") or []) if it.get("short")}
    for col, tag, desc, km, w, dash in extra_rows:
        items.append(("line", col, tag, short.get(tag) or desc, "", None))
    return items


def _legend_band_geom():
    """图例带排版（逻辑 px，S=1）。

    版面 = **通栏密排网格**（BD_COLS 列）：色标 + 名称 + 里程一行说清。
    上一版是"左半区一列到底 + 右半区符号网格"，为了容纳 9 个分日行把带子撑到
    360 逻辑 px（占整图 32%），用户反馈「下半区域图例占比多大，喧宾夺主」。
    改密排后同样内容 ≈160 px，占整图 16% 左右。
    """
    items = _legend_band_items()
    cols = max(1, int(_CFG.get("legend_band_cols", BD_COLS)))
    n_rows = max(1, (len(items) + cols - 1) // cols)
    h = BD_PADT + BD_ROW0 + BD_PITCH * (n_rows - 1) + BD_PADB
    return {"h": h, "cols": cols, "n_rows": n_rows}


def _legend_band_svg():
    g = _legend_band_geom()
    BH = g["h"]
    items = _legend_band_items()
    cols = g["cols"]
    x0 = 40
    cw = (IW - 2 * x0) / cols
    o = [f'<rect x="0" y="0" width="{IW}" height="{BH}" fill="{TITLE_BG}"/>',
         f'<line x1="0" y1="1" x2="{IW}" y2="1" stroke="{TITLE_LINE}" stroke-width="2"/>',
         f'<text x="{x0}" y="{BD_PADT + 16}" font-size="14" font-weight="800" '
         f'fill="{TITLE_INK_MUTED}" letter-spacing="2">{_esc(_CFG.get("legend_title", "分日路段"))}</text>',
         f'<text x="{IW - x0}" y="{BD_PADT + 16}" text-anchor="end" font-size="13.5" '
         f'font-weight="600" fill="#7A8290">'
         f'{_esc(_CFG.get("legend_foot", "全线为实测轨迹 · 非导航用图"))}</text>']

    for i, (kind, col, tag, text, km, glyph) in enumerate(items):
        cx = x0 + (i % cols) * cw
        ry = BD_PADT + BD_ROW0 + BD_PITCH * (i // cols)
        if kind == "sym":
            o.append(f'<g transform="translate({cx + 8:.1f},{ry - 5:.1f}) scale(0.42)">'
                     f'{mk_pin(0, 0, glyph)}</g>')
            o.append(f'<text x="{cx + 30:.0f}" y="{ry}" font-size="13.5" font-weight="700" '
                     f'fill="#4A5261">{_esc(text)}</text>')
            continue
        dash = ' stroke-dasharray="7 4"' if kind == "line" else ""
        o.append(f'<line x1="{cx}" y1="{ry - 4.5:.0f}" x2="{cx + 30}" y2="{ry - 4.5:.0f}" '
                 f'stroke="{col}" stroke-width="{2.8 if kind == "day" else 2.2:.1f}" '
                 f'stroke-linecap="round"{dash}/>')
        o.append(f'<text x="{cx + 38:.0f}" y="{ry}" font-size="14" font-weight="800" '
                 f'fill="{col}">{tag}</text>'
                 f'<text x="{cx + 68:.0f}" y="{ry}" font-size="13.5" font-weight="600" '
                 f'fill="#4A5261">{_esc(text)}</text>')
        if km:
            o.append(f'<text x="{cx + cw - 14:.0f}" y="{ry}" text-anchor="end" '
                     f'font-size="13.5" font-weight="800" fill="#3A4250">{km}</text>')
    return "".join(o)



def build_svg(mobile=False, embed_image=True, embed_max=EMBED_MAX, with_title=False):
    """返回 (svg 字符串, 自检信息)。embed_max=0 表示不降采样（高清地图用）。

    with_title=True：在整幅图上方加一条名称带（独立高清图用，HTML 内嵌版不用）。
    """
    overlay, placed = build_map_overlay(mobile)
    img = ""
    if embed_image:
        img = (f'<image x="0" y="0" width="{IW}" height="{IH}" '
               f'href="data:image/jpeg;base64,{_embed_jpeg(embed_max)}"/>')
    cls = "mapsvg map-p" if mobile else "mapsvg map-d"
    band = _title_h() if with_title else 0
    lb = _legend_band_geom()["h"] if (with_title and _legend_outside()) else 0
    vh = IH + band + lb
    # 字体**只在根节点声明一次**：这样 OSM 地名、POI 标注、里程数字全部同一套字，
    # 不再出现"底图地名是黑体、POI 标注落到浏览器默认衬线"的混排（2026-10-21 用户指出）。
    head = (f'<svg class="{cls}" viewBox="0 0 {IW} {vh}" xmlns="http://www.w3.org/2000/svg" '
            f'font-family="{FONT_STACK}" '
            f'role="img" aria-label="{_CFG.get("aria_map", "全线地图")}">')
    if with_title:
        tail = (f'<g transform="translate(0,{band + IH})">{_legend_band_svg()}</g>'
                if lb else "")
        return (f'{head}{_title_band()}<g transform="translate(0,{band})">'
                f'{img}{overlay}</g>{tail}</svg>', placed)
    return f'{head}{img}{overlay}</svg>', placed

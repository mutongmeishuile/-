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
import os
import json
import math
import re
from pathlib import Path

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
# ---- 线条与符号默认口径（2026-11-03 用户定；均为**逻辑 px**，与页面内嵌图同一坐标系，
#      所以换 map_ss 只会改变字面像素数、不改变视觉权重）----
ROUTE_W_DEF = 1.5       # 当日色主线（2026-11-24 用户定案：3.0×0.5 → 1.5 逻辑 px；
                        # 出图 = 1.5 × min(SS,cap)，地面口径 SS=10 时 15 px，白晕仍每侧 1 px）
ROUTE_HALO_SIDE_DEF = 1.0   # 主线**每侧**白色描边宽度（总衬底 = 主线 + 2×此值）
DAY_DASH_W_DEF = 1.0    # 分日界虚线
PIN_D_DEF = 5.0         # 图钉可见外径（起点/终点/营地/水源/海子/垭口同尺寸）
# 图钉内部字形按同一系数等比缩（基准是「外径 19.8」那版几何）。线路级可用 CFG["pin_d"] 覆盖。

# ---- 高清图视觉元素统一倍率（2026-11-10 GHT 全线定案）----
# 画幅按 SS 超采（长边 ~20000 px），但字号 / 符号只按 min(SS, cap) 放大
# （cap 由线路 CFG 给；不配 = 不封顶、k=1）：
#   出图栅格 px = 设计基准 px × min(SS, cap)
# 换算到 SVG 逻辑坐标（viewBox 宽 IW=1504）：逻辑 px = 设计基准 px × k，k = min(SS,cap)/SS。
# visual 由 render_map_hi 显式传入 build_svg；网页内嵌图 / 其余线路不传 → k=1、白晕走旧口径，
# 逐字保持旧行为（共享脚本默认不得改动其余线路）。
_VIS = {"ss": 1.0, "k": 1.0, "halo_out_px": None}


def _vk():
    return float(_VIS["k"])


def _vp(target, ratio=0.0, fs=None):
    """目标物理 px → 逻辑 px（尺度自适应唯一入口，2026-11-25）。

    · 物理目标优先：`target / SS`，所以画幅放大多少倍，符号在成品图上都是同一尺寸；
    · 地面口径下再用 `ratio × 字高` 兜底，避免小画幅时符号比字还小；
    · 物理上限 GC.SYMBOL_MAX_PX 兜底防失控。
    显式配了 label_fs_base 的老线路 `_GROUND_FS=False` → 这里仍只按物理目标算，
    但调用点会把老口径作为 upper/lower bound，逐字不漂移。
    """
    v = GC.visual_px(target, _VIS["ss"])
    if ratio and fs:
        v = max(v, float(ratio) * float(fs))
    return min(v, GC.visual_px(GC.SYMBOL_MAX_PX, _VIS["ss"]))


def _label_lead_max():
    """标注引线 / 标注到锚点距离的硬上限（逻辑 px）= K × 倍数。

    K = GC.VIS_CAP（统一系数上限，默认 3）。2026-11-18 用户定案倍数 2 → 6 px。
    线路可 CFG["label_leader_max_k"] 覆盖；与底图地名引线同一口径。
    """
    try:
        _k = float(GC.cfg("label_leader_max_k", GC.LABEL_LEADER_MAX_K)
                   or GC.LABEL_LEADER_MAX_K)
    except Exception:                                              # noqa
        _k = GC.LABEL_LEADER_MAX_K
    _lead = _k * GC.VIS_CAP
    # 地面口径（2026-11-22 大字号 / 2026-11-24 引线连通）：上限至少 1.05 个字高，
    # 并覆盖正位贴点落位距离（_PIN_R + 6）——否则「文字贴正右/正左、gap≈半径+6」的
    # 候选全被 6 px 上限拦下、引线一根都不画（用户 2026-11-24："应有标注点位指向名称"）。
    if _GROUND_FS:
        _lead = max(_lead, 1.05 * _FS_BASE, _PIN_R + 7.0)
    return _lead


def _halo_out():
    """白底单侧外扩量（逻辑 px）= halo_out_px 输出 px / SS，固定不随超采。

    未启用 visual 时返回 None（各调用点走旧口径）。
    """
    o = _VIS.get("halo_out_px")
    return None if o is None else float(o) / float(_VIS["ss"])


def _sw(old):
    """任意白色描边：启用 visual 时统一为「外围 +halo_out_px 输出 px」。
    SVG stroke 居中绘制，描边宽度 = 2 × 单侧外扩量。
    """
    ho = _halo_out()
    return 2.0 * ho if ho is not None else old


def _halo_sw_text(fs):
    """文字白晕描边宽度（逻辑 px）。"""
    ho = _halo_out()
    if ho is not None:
        # 2026-11-18 用户：底图地名白描边 = 1×halo_out（原来 2×，JPG 里 20 px 太糊）
        return 1.0 * ho
    return max(0.8, fs * 0.13)


def _halo_sw_sym():
    """符号（峰三角 / 锚点圆）白色描边宽度（逻辑 px）。"""
    return _sw(0.7)


def _pin_d(k=None):
    kk = _vk() if k is None else float(k)
    old = float(_CFG.get("pin_d") or PIN_D_DEF) * kk
    # 显式配了 pin_d 的老线路逐字不变；地面口径线路换用目标物理直径口径。
    if not _CFG.get("pin_d", None) and _GROUND_FS:
        return _vp(GC.VISUAL_PX["poi_pin"], 1.2, _FS_BASE)
    return old


MFS_M = 1.9           # 手机版地图统一放大系数（图被缩到 ~0.25 倍，必须放大）
MFS_S = 2.8           # 手机版里程点/线宽的额外放大

# 里程圈统一圆径的口径在 guide_common.km_circle_metrics（跨模块唯一实现）。

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
_DBG_TAG = "desk"       # 落位 dump 区分桌面/手机（两者坐标口径不同，别混着看）
_TRK, _TRK_HW = [], 0.0     # 轨迹点与半宽（_layout 用来避让线）
_lg_rect = _sb_rect = _cp_rect = _bd_rect = None


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
    """给 POI 标注找一个位置，返回 (tx, ty, anchor, box, overlap_area)。

    `area == 0` = 完全不压任何已占用矩形（完美落位）；`> 0` = 压到的面积（px²）。
    **纯函数**（除 commit 外无副作用）：调用方（build_map_overlay 的字号阶梯）
    需要反复试探不同字号，若每次试探都往 _OCC 里登记，落选的方案会把后来的
    标注全部挤走——2026-10-17 北天山中线实测踩过这个坑。

    位置候选（2026-11-18 用户二次定案）：
      ① 正右侧、垂直居中；② 正左侧、垂直居中；
      ③ 作者 dy 斜向微调（仍受上限约束）；④ 四周（上/右上/下/右下/左上/左下）。
    ⚠ 所有候选「锚点到字框最近距离」硬限 K×2 逻辑 px（K=VIS_CAP=3 → 6 px），
      从源头杜绝「字被避让甩到另一个山头、一根长引线横穿图面」。
    """
    h = fs * 1.6
    _cap = _label_lead_max()
    # 正位紧贴图钉：图钉半径 + 2 px，不越上限
    _gap = min(max(_PIN_R + 2.0, 2.0), _cap)
    plans = [(_gap, 0.0, "start"), (-_gap, 0.0, "end")]
    if dx or dy:
        plans.append((math.copysign(min(abs(dx), _cap), dx),
                      math.copysign(min(abs(dy), _cap), dy), anc))
    # 四周：半径按上限分三档，不再用 h 的倍数（h≈1.6×字号，必然远越上限）
    for r in (_cap * 0.45, _cap * 0.75, _cap):
        for sx, sy in ((0, -1), (1, -1), (0, 1), (-1, -1),
                       (1, 1), (-1, 1), (1, 0), (-1, 0)):
            a = "start" if sx > 0 else ("end" if sx < 0 else "middle")
            plans.append((sx * r, sy * r, a))

    def boxof(ox, oy, a):
        tx, ty = x + ox, y + oy
        x0, x1 = ((tx, tx + w) if a == "start" else
                  (tx - w, tx) if a == "end" else (tx - w / 2, tx + w / 2))
        return tx, ty, (x0, ty - h / 2, x1, ty + h / 2)

    def dist(box):
        return max(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))

    free, loose = None, None
    for _pi, (ox, oy, a) in enumerate(plans):
        tx, ty, box = boxof(ox, oy, a)
        # 硬上限：字离锚点超过 K×2 px 的候选一律不参与（无论重叠多少）
        if dist(box) > _cap + 0.5:
            continue
        x0, y0, x1, y1 = box
        if x0 < _PAD or x1 > IW - _PAD or y0 < _PAD or y1 > IH - _PAD:
            continue
        area = sum(GC.rect_overlap_area(box, o) for o in _OCC)
        # ⚠ 2026-11-18 用户二次定案：标注必须贴锚点（cap=K×2 逻辑 px），而轨迹线
        # 本来就穿过图钉 —— 贴着摆必然压线。旧版给压轨迹 1e6 罚值，等于把所有
        # 正右/正左候选全否掉，只能退到 loose 里缩字号/甩远（起点终点字号差一倍）。
        # 现在压轨迹只做**同分软偏好**：绝不并入 `area`（那是硬重叠，>0 就进不了
        # free），否则贴点候选全被误判成"有重叠"又退化成缩字号（高清 k=0.5 实测）。
        _on_track = 1 if (area == 0 and _TRK and _near_track(box, _TRK, _TRK_HW)) else 0
        if area == 0:
            # 完美落位里按 plans 顺序取（正右 > 正左 > 作者微调 > 四周），同序优先不压线
            key = (_pi, _on_track, round(dist(box)))
            if free is None or key < free[0]:
                free = (key, tx, ty, a, box, 0.0)
        elif loose is None or (area, _on_track, round(dist(box))) < loose[0]:
            loose = ((area, _on_track, round(dist(box))), tx, ty, a, box, area)
    best = free or loose
    if best is not None:
        _, tx, ty, a, box, area = best
        if commit:
            _OCC.append(box)
            _LABELS.append(box)
        return round(tx, 1), round(ty, 1), a, box, area
    # 极端：所有候选越界 → 仍在 cap 内强行落位（右优先、左次之）
    if x + _gap + w <= IW - _PAD:
        tx, ty, a = x + _gap, y, "start"
    elif x - _gap - w >= _PAD:
        tx, ty, a = x - _gap, y, "end"
    else:
        tx, ty, a = min(max(x, _PAD), IW - _PAD), y, "middle"
    box = boxof(tx - x, ty - y, a)[2]
    if commit:
        _LABELS.append(box)
    return round(tx, 1), round(ty, 1), a, box, 1e18


# ---------------------------------------------------------------- 点位符号
def mk_pin(x, y, kind, k=None):
    # 2026-10-17 用户反馈：POI 图钉在轨迹上过大，遮挡地名/道路。
    # 统一从 r≈19–20 缩到 r≈14–15，字号同步缩小，与地名同量级。
    # 2026-11-03 用户定口径：**外径恒 = PIN_D（默认 15 逻辑 px）**，起点/终点/营地/水源/
    #   海子/垭口同尺寸。内部字形按同一系数 PINS 等比缩，所以只改 PIN_D 一处即可整体收放。
    PINS = PS * _pin_d(k) / (2 * 15 * PS_BASE)
    _R = 15 * PINS          # 外圈半径（所有 kind 统一）
    # 2026-11-03：外径收到 5 之后，内部字形只剩 2–3 px、只会糊成脏点 ——
    # 低于 8 px 自动退化成「白色细环 + 单色实心点」，可辨识度反而更高。
    if _pin_d(k) < 8 * (_vk() if k is None else float(k)):
        _dc = {"start": "#D93B2B", "end": "#0F6E5C", "camp": D1_C, "water": "#0F8A9B",
               "peak": "#4A5261", "peak_hi": "#4A5261", "temple": "#5A4A8A",
               "warn": "#D0871B", "pass": "#8B5E3C", "ford": "#2E86AB",
               "poi": "#6B4FA0", "village": "#C4703A"}.get(kind)
        if _dc:
            return (f'<circle cx="{x}" cy="{y}" r="{_R:.1f}" fill="{_dc}" stroke="#FFFFFF" '
                    f'stroke-width="{_sw(max(0.8, _R * 0.36)):.4f}"/>')
        return ""

    if kind == "start":
        r = _R
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#D93B2B" stroke="#fff" '
                f'stroke-width="{_sw(4 * PINS):.4f}"/>'
                f'<text x="{x}" y="{y+6*PINS:.1f}" text-anchor="middle" font-size="{16*PINS:.1f}" '
                f'font-weight="800" fill="#fff">起</text>')
    if kind == "end":
        r = _R
        return (f'<circle cx="{x}" cy="{y}" r="{r:.1f}" fill="#0F6E5C" stroke="#fff" '
                f'stroke-width="{_sw(4 * PINS):.4f}"/>'
                f'<text x="{x}" y="{y+6*PINS:.1f}" text-anchor="middle" font-size="{15*PINS:.1f}" '
                f'font-weight="800" fill="#fff">终</text>')
    if kind in ("peak", "peak_hi"):
        # 山峰：与营地 / 水源 / 村落的构造完全同源 —— 白底圈 + 色盘 + 白色三角。
        # 2026-10-24 改：旧版是「白晕 + 裸色三角」（peak_hi r=14 深红 / peak r=11 灰），
        # 与其它符号两种画风，且同类山峰一大一小。现在两种 kind 几何一致，
        # 若要强调主峰请只改 label 颜色，不要改符号尺寸。
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{11.6*PINS:.1f}" fill="#4A5261"/>'
                f'<path d="M {x-7.2*PINS:.1f},{y+5.2*PINS:.1f} L {x},{y-6.6*PINS:.1f} '
                f'L {x+7.2*PINS:.1f},{y+5.2*PINS:.1f} Z" fill="#fff"/>')
    if kind == "temple":
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PINS:.1f}" fill="#5A4A8A"/>'
                f'<path d="M {x-7*PINS:.1f},{y-0.8*PINS:.1f} L {x},{y-7.8*PINS:.1f} '
                f'L {x+7*PINS:.1f},{y-0.8*PINS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-4.6*PINS:.1f}" y="{y+0.8*PINS:.1f}" width="{9.2*PINS:.1f}" '
                f'height="{5.6*PINS:.1f}" fill="#fff"/>')
    if kind == "camp":
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{11.6*PINS:.1f}" fill="{D1_C}"/>'
                f'<path d="M {x-7.5*PINS:.1f},{y+6*PINS:.1f} L {x},{y-6.7*PINS:.1f} '
                f'L {x+7.5*PINS:.1f},{y+6*PINS:.1f} Z" fill="#fff"/>')
    if kind == "water":
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PINS:.1f}" fill="#0F8A9B"/>'
                f'<path d="M {x},{y-6.6*PINS:.1f} C {x+6.6*PINS:.1f},{y+0.7*PINS:.1f} '
                f'{x+5.2*PINS:.1f},{y+6.6*PINS:.1f} {x},{y+6.6*PINS:.1f} C {x-5.2*PINS:.1f},{y+6.6*PINS:.1f} '
                f'{x-6.6*PINS:.1f},{y+0.7*PINS:.1f} {x},{y-6.6*PINS:.1f} Z" fill="#fff"/>')
    if kind == "warn":
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PINS:.1f}" fill="#D0871B"/>'
                f'<text x="{x}" y="{y+5.4*PINS:.1f}" text-anchor="middle" '
                f'font-size="{15*PINS:.1f}" font-weight="900" fill="#fff">!</text>')
    if kind == "pass":
        # 垭口：倒三角 + 横杠（山鞍形）
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<path d="M {x-9*PINS:.1f},{y-5*PINS:.1f} L {x+9*PINS:.1f},{y-5*PINS:.1f} '
                f'L {x},{y+8*PINS:.1f} Z" fill="#8B5E3C"/>'
                f'<rect x="{x-7*PINS:.1f}" y="{y-8*PINS:.1f}" width="{14*PINS:.1f}" '
                f'height="{3*PINS:.1f}" rx="{1.5*PINS:.1f}" fill="#8B5E3C"/>')
    if kind == "ford":
        # 涉滩：水滴 + 波纹
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PINS:.1f}" fill="#2E86AB"/>'
                f'<path d="M {x-6*PINS:.1f},{y-1*PINS:.1f} Q {x-3*PINS:.1f},{y-5*PINS:.1f} '
                f'{x},{y-1*PINS:.1f} Q {x+3*PINS:.1f},{y+3*PINS:.1f} {x+6*PINS:.1f},{y-1*PINS:.1f}" '
                f'stroke="#fff" stroke-width="{_sw(2.2 * PINS):.4f}" fill="none" stroke-linecap="round"/>'
                f'<path d="M {x-6*PINS:.1f},{y+3.5*PINS:.1f} Q {x-3*PINS:.1f},{y-0.5*PINS:.1f} '
                f'{x},{y+3.5*PINS:.1f} Q {x+3*PINS:.1f},{y+7*PINS:.1f} {x+6*PINS:.1f},{y+3.5*PINS:.1f}" '
                f'stroke="#fff" stroke-width="{_sw(2.2 * PINS):.4f}" fill="none" stroke-linecap="round"/>')
    if kind == "poi":
        # 地标：菱形
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<path d="M {x},{y-10*PINS:.1f} L {x+7.5*PINS:.1f},{y} '
                f'L {x},{y+10*PINS:.1f} L {x-7.5*PINS:.1f},{y} Z" fill="#6B4FA0"/>')
    if kind == "village":
        # 村落：小房子
        return (f'<circle cx="{x}" cy="{y}" r="{15*PINS:.1f}" fill="#fff" opacity="0.95"/>'
                f'<circle cx="{x}" cy="{y}" r="{10.8*PINS:.1f}" fill="#C4703A"/>'
                f'<path d="M {x-7*PINS:.1f},{y-0.5*PINS:.1f} L {x},{y-7*PINS:.1f} '
                f'L {x+7*PINS:.1f},{y-0.5*PINS:.1f} Z" fill="#fff"/>'
                f'<rect x="{x-5*PINS:.1f}" y="{y-0.5*PINS:.1f}" width="{10*PINS:.1f}" '
                f'height="{6*PINS:.1f}" fill="#fff"/>')
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
            tag = str(it.get("tag", ""))
            desc = it.get("desc", "")
            if "雪线" in tag:
                GC.check_snowline_legend(desc, "（地图图例）")
            # 2026-10-25：步道类行色块一律跟 TRAIL_STYLE 芯色（图上无 per-route 覆盖）
            is_trail = GC.is_trail_legend(tag, desc)
            if is_trail:
                GC.check_trail_legend(it.get("color", GC.C_TRAIL_LEGEND), "（地图图例）")
            color = GC.C_TRAIL_LEGEND if is_trail else it.get("color", GC.C_TRAIL_LEGEND)
            rows.append((color, tag,
                         desc, _legend_km_txt(it.get("dist_label", "")),
                         it.get("width", 1.5), it.get("dash", ' stroke-dasharray="8 5"')))
        # 列表里没有步道行 → 用旧单行键回补（图上紫线在图例里必须有名字）
        if not any(GC.is_trail_legend(it.get("tag"), it.get("desc", "")) for it in extra):
            leg = GC.legacy_trail_legend(_CFG)
            if leg:
                GC.check_trail_legend(leg["color"], "（地图图例·旧键回补）")
                rows.append((leg["color"], leg["tag"], leg["desc"], "", 1.5,
                             ' stroke-dasharray="8 5"'))
    elif _CFG.get("extra_legend_desc"):
        GC.check_trail_legend(_CFG.get("extra_legend_color", GC.C_TRAIL_LEGEND), "（地图图例·旧键）")
        rows.append((GC.C_TRAIL_LEGEND,
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
        # 两列网格：每列 = 符号（按真实图钉直径）+ 间距 + 名称
        half = max(_pin_d() + 14 + GC.text_w(nm, 17) for _k, nm in syms)
        need.append(2 * half + 36)
    foot = GC.author_legend(_CFG.get("legend_foot", "仅供参考 · 非导航用途"))
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
      · 硬档 `d < 2×预留足迹`（默认 38 px，经验保守值，**不等于图钉实际半径** —
        后者是 `_pin_d()/2`）：两个符号会挤在一起，必须处理；
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
        print(f"     · 符号会叠（< {hard:.0f} px = 两个预留足迹相切，图钉外径 {_pin_d():.0f} px）：{len(h)} 对 —— "
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
    2026-11-10 起档位表抽到 `GC.scale_bar_km`（draw_osm 的近轨迹走廊共用同一口径）。
    """
    return GC.scale_bar_km(m_per_px, iw, target_frac)


# ---- 全局字号口径（2026-10-24）----
# 用户要求：① 全图字体统一（底图地名 8 px）；② 轨迹标注只比底图地名大 1 px。
# 两个值都可在 route_def.CFG 覆盖。深度/层级改由**颜色与白晕**承担，不再靠字号拉开。
_FS_BASE = float(GC.cfg("label_fs_base", 4) or 4)
_FS_POI = float(GC.cfg("label_fs_poi", _FS_BASE * 1.125) or (_FS_BASE * 1.125))

# 「每个字压住 N 米」倒算（2026-11-22 用户定案，技能默认口径）：CFG["label_ground_m"] 默认 150。
# 显式手填了 label_fs_base / label_fs_poi 的线路照旧优先（逐字保持旧行为）。
_GROUND_FS = GC.label_fs_from_ground(IW, meta.get("bbox"),
                                     GC.cfg("label_ground_m", GC.LABEL_GROUND_M))
if _GROUND_FS:
    if not GC.cfg("label_fs_base", None):
        _FS_BASE = _GROUND_FS
    if not GC.cfg("label_fs_poi", None):
        _FS_POI = _GROUND_FS * 1.125
    print(f"  字号（地面口径）：每字 {GC.cfg('label_ground_m', GC.LABEL_GROUND_M):g} m → "
          f"底图 {_FS_BASE:.1f} / POI {_FS_POI:.1f} 逻辑 px "
          f"（出图 {_FS_BASE * GC.font_scale(_VIS['ss']):.0f} px）")


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
        # 底图地名**只用一个字号**（CFG["label_fs_base"]，默认 **8 逻辑 px**，
        # 最终输出约 14.4 px）。2026-10-24 用户要求「全局字体大小统一、再小一些」，
        # 原来是按文字宽度分 11.4/10.6/10.0 三档（更早是 14+/11.5–14/其余 三档），
        # **三档已废弃** —— 同屏 0.6–1.4 px 的差异既不构成层级、又让同类地物看着不一样大。
        # 调字号就从 8 起算；POI 标注 = 基准 +1（label_fs_poi）。
        fs = _FS_BASE * _vk()
        # 地面口径在 HTML 小画幅（SS=1、无 visual）下会算出 1.x 逻辑 px —— 缩略后
        # 几乎看不见。给小画幅一个显示字号下限；高清图 SS=10 时不生效（目标 1.1 px
        # 低于地面倒算的 1.3 px）。手机图同样受益。
        if _GROUND_FS and _VIS.get("halo_out_px") is None:
            fs = max(fs, GC.HTML_MIN_FS_LOG)
        out.append({
            # 用 draw_osm 记录的**要素真实锚点** ax/ay 当基准（不再用文字框位置当锚点，
            # 否则峰名/湖名的避让起点、引线落点都会偏到文字位置 → "明显偏移"）。
            "t": it["t"], "x": (it.get("ax", it["x"])) / ss, "y": (it.get("ay", it["base"])) / ss,
            "fs": fs, "w": it["s"] / ss, "pri": s0,
            "fill": it["f"], "stroke": it["st"],
            # 近轨迹走廊标记（draw_osm 2026-11-10）：缺省 0 = 老缓存/老线路逐字不变。
            # 有标记的注记在 `_place_base_labels` 里多试几圈「零重叠」位置。
            "near": int(it.get("near", 0) or 0),
            # 要素类别（draw_osm 2026-11-19）：peak / saddle / glacier / road。
            # 缺省 "" = 老缓存，仍按字色判（向后兼容）。
            "k": str(it.get("k") or ""),
        })
    # 重要度按"徒步关心什么"排（2026-10-21）：垭口 > 地名/湖 > 峰 > 河。
    # 拥挤时才需要取舍，所以这个顺序只在"实在没地方"时决定谁被丢。
    def _rank(d):
        f = tuple(d["fill"])
        if d.get("k") == "road":
            # 道路编号/路名：和峰名同在第一遍，但要排在峰名之后（峰锚点不可替代）
            return 400.0 + d["pri"]
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
    # 2026-11-18 用户：同名标注不该重复出现。
    # OSM 同一地物常被 way+node 各标一次（「歌連臣角砲台探射燈碉堡」两条锚点只差
    # 0.7 逻辑 px）；也有同一山头被标成两个点、相距十几 px（「標尖角頂」26 px）。
    # 规则：**同名且锚点相距 < 30 逻辑 px** 只留 rank 最高的一条；
    # 不同山头的同名（相距远，通常 >50 px）照常保留。
    _kept = {}
    _uniq = []
    for d in out:
        _prev = _kept.setdefault(_norm_name(d["t"]), [])
        if any(math.hypot(d["x"] - px, d["y"] - py) < 30.0 for px, py in _prev):
            continue
        _prev.append((d["x"], d["y"]))
        _uniq.append(d)
    return _uniq


def _near_track(box, track, half_w):
    """矩形是否压到轨迹带（含白色外晕）。"""
    x0, y0, x1, y1 = box
    for x, y in track:
        if x0 - half_w <= x <= x1 + half_w and y0 - half_w <= y <= y1 + half_w:
            return True
    return False


_T2S = {"灣": "湾", "營": "营", "頂": "顶", "嶺": "岭", "觀": "观", "龍": "龙",
        "鳳": "凤", "島": "岛", "崗": "岗", "頭": "头", "徑": "径", "環": "环",
        "錦": "锦", "鉛": "铅", "礦": "矿", "瀝": "沥", "燈": "灯", "碉": "碉",
        "門": "门", "園": "园", "場": "场", "橋": "桥", "鐵": "铁", "銀": "银",
        "銅": "铜", "廟": "庙", "壇": "坛", "墳": "坟", "墻": "墙", "樹": "树",
        "灣": "湾", "灘": "滩", "溝": "沟", "澗": "涧", "瀉": "泻", "滙": "汇"}


def _norm_name(s):
    """把常见繁体地名用字收敛成简体，仅用于**重名判定**（不改变显示文字）。

    2026-11-18 用户实测：POI「浪茄湾营地」与 OSM「浪茄灣營地」是同一处，
    但 `_poi_dup` 的名字包含/相似度都因为简繁差异没命中，图上印了两行。
    """
    return "".join(_T2S.get(c, c) for c in s)


def _poi_dup(it, pois, pin_xy):
    """底图地名与攻略 POI 是同一个地方？→ 丢掉底图那条。

    OSM 的村/营地名与 POI 名往往只差后缀（「措学仁玛」vs「措学仁玛营地」、
    「热嘎」vs「热嘎营地」），draw_osm 的 skip_names 全串匹配拦不住，
    图上就会在同一个点上印两行名字 —— 2026-10-21 实测一处不落。
    判据：名字互相包含（≥2 字）且锚点相距 < 60 逻辑 px。
    """
    t = _norm_name(it["t"].split(" ")[0])
    if len(t) < 2:
        return False
    for d, (px, py) in zip(pois, pin_xy):
        nm = _norm_name(d["name"].split(" · ")[-1])
        if not (t in nm or nm in t):
            if len(t) < 3 or len(nm) < 3:
                continue
            _sa, _sb = set(t), set(nm)
            if len(_sa & _sb) / max(len(_sa), len(_sb)) < 0.55:
                continue
        if (it["x"] - px) ** 2 + (it["y"] - py) ** 2 < 60 ** 2:
            return True
    return False


def _is_peak_item(it):
    """这条底图地名是不是「山峰名」。

    判据与下面的引线判据同源：填充色是棕色系（draw_osm 给峰名的颜色），且比蓝色系暗。
    ⚠ 2026-11-19：道路编号/路名也是深棕（88,60,40 / 104,78,58），纯按字色判会把
      "G219""喀东线"当成峰名 —— 既抢"峰名优先"的位，又在前面补一个 ▲（用户截图
      「G219 前面标记的是山峰三角」）。draw_osm 现在把类别写进记录 k，有 k 就听它的。
    """
    k = str(it.get("k") or "")
    if k:
        return k == "peak"
    f = tuple(it["fill"])
    return f[0] - f[2] > 12 and f[0] < 110


def _base_label_sym(fill, sym_on_raster, kind=""):
    """底图地名锚点由谁画什么符号 —— 返回 'peak' / 'saddle' / 'glacier' / 'dot' / None。

    2026-11-18 用户定案：**默认 False（矢量补画）**。原因是"符号必须跟着最终落位
    的名字走"。栅格层（draw_osm.Labeller）与矢量层（本函数所在模块）是两套独立避让，
    栅格实测落位 2182 条、矢量只落 1128 条 —— 差出来的约 1000 个符号全成了图上
    无字的孤三角/孤圆点（用户："丢失了很多记录"）。改由矢量在**落位成功处**补画，
    符号与字同生同灭，也不会双画（栅格此时不画）。

    · sym_on_raster=True（显式开启，总览 / build_bigmap 通道）：返回 None，
      符号由 draw_osm 烤进栅格。
    · sym_on_raster=False（默认）：按字色分流 —— 峰棕=三角、垭口橙=倒三角、
      冰川蓝=菱形、其余=小圆点。

    与 draw_osm 的口径对应：`lab.draw_symbols = lab.draw_pixels or labels_symbol_on_raster`。
    两边必须读同一个 CFG 键，否则又回到"栅格画 + 矢量补"的双画。
    """
    if sym_on_raster:
        return None
    k = str(kind or "")
    if k == "peak":
        return "peak"
    if k == "saddle":
        return "saddle"
    if k == "glacier":
        return "glacier"
    if k == "road":
        return None              # 道路编号/路名：不挂锚点符号（2026-11-19）
    # 2026-11-25 水系语义修复：旧实现把水系字色 (36,84,128) 当冰川画成菱形，
    # 而冰川真色是 C_GLACIER_LB=(24,92,146)。改为按类别分流（draw_osm 给水系写
    # k="water"、冰川写 k="glacier"），颜色兜底也一并纠正 —— 水系圆点、冰川菱形。
    if k == "water":
        return "dot"
    f = tuple(fill)[:3]
    if f == (150, 78, 24):
        return "saddle"          # 垭口：橙
    if f == (24, 92, 146):
        return "glacier"         # 冰川名：深冰蓝 C_GLACIER_LB
    if f == (36, 84, 128):
        return "dot"             # 水系名：深青蓝（旧记录无 k 时靠颜色兜底也正确）
    return "peak" if (f[0] - f[2] > 12 and f[0] < 110) else "dot"


def _first_pass_item(it):
    """进「第一遍（优先层）」的条目：山峰名 + 道路编号/路名。

    峰名先排是为了守住山顶锚点（用户 2026-10-27 口径）；道路编号跟着进这一层，
    是因为它落在路中段、被挤走等于这条国道在地图上消失（旧实现它按「棕字色」
    混进峰名层，两件事碰巧都成立）。两者都不再靠猜：类别由 draw_osm 写入 k。
    """
    if it.get("k"):
        return it["k"] in ("peak", "road")
    return _is_peak_item(it)


def _is_camp_item(it):
    """OSM 营地注记：draw_osm 的 extras(camp) 用 fill=(60,92,60)。"""
    return tuple(it.get("fill") or ()) == (60, 92, 60)


def _camp_pass_cfg():
    """官方营地专用落位通道的参数：(轨迹附近半径 km, 官方名集合)；0 = 关闭。

    CFG["camp_near_route_km"] > 0 才启用；CFG["camp_official_names"] 非空时
    只放名单内的营地（简繁归一后比对），其余 OSM 营地仍走原来的 rest 通道。
    """
    try:
        km = float(GC.cfg("camp_near_route_km", 0) or 0)
    except Exception:                                               # noqa
        km = 0.0
    if km <= 0:
        return 0.0, set()
    names = GC.cfg("camp_official_names", None)
    want = {_norm_name(str(x)) for x in names} if names else set()
    return km, want


def _place_base_labels(track, half_w, pois=(), pin_xy=(), only=None):
    """OSM 地名：矢量层 + 动态避让。

    为什么默认在**最后**排（2026-10-21）：
      · 先排会让一堆底图地名把好位置占满，POI 营地/垭口标注只能被挤到边上 ——
        而攻略图的主体是轨迹与营地，底图地名应当让路。
      · 先排时 `_OCC` 里还没有图钉、里程点、POI 标注，避让等于没做。
    策略：按重要度从高到低，围绕 draw_osm 给的原锚点试 8 个方向 × 3 档半径；
    找到零重叠的位置就落，找不到就**丢掉这一个**（稀疏区基本不会丢，
    拥挤区先丢次要的湖名/次峰名）。这就实现了"不拥挤区域不做取舍"。

    `only` 用于「山峰名优先」（`CFG["label_priority"]="peak"`，2026-10-27）：
      · "peak" —— 只排山峰名，并且在 POI 标注**之前**调用，于是峰名先占位、
        POI 标注绕开它（用户口径：「OSM 山峰的名字定位应该优先于轨迹上标注名称」——
        峰名的锚点就是山顶，被推走或丢掉就失去意义；POI 标注本来就可以挪）。
      · "rest" —— 排其余地名，仍在 POI 之后（村/湖/河继续给轨迹让路）。
      · None   —— 老行为，一次排完（其它线路不受影响）。
    """
    _camp_km, _camp_names = _camp_pass_cfg()
    if only == "camp" and _camp_km <= 0:
        return ""                      # 线路没开官方营地通道：什么都不画
    items = _load_base_labels()
    _camp_pass = (only == "camp" and _camp_km > 0)
    if _camp_pass:
        # 官方营地通道：只取「名单内 + 轨迹附近」的 OSM 营地。
        # 营地本来就在轨迹旁边，旧的「字框不许压轨迹带」会把它全部丢掉。
        items = [it for it in items if _is_camp_item(it)]
        if _camp_names:
            items = [it for it in items if _norm_name(it["t"]) in _camp_names]
        if track:
            _r = _camp_km * 1000.0 / max(M_PER_PX, 1e-6)
            items = [it for it in items
                     if min(math.hypot(it["x"] - px, it["y"] - py) for px, py in track) <= _r]
    elif only == "peak":
        items = [it for it in items if _first_pass_item(it)]
    elif only == "rest":
        items = [it for it in items if not _first_pass_item(it)]
        # 专用通道已接管的营地不再排第二遍，否则同名会印两行。
        if _camp_km > 0:
            items = [it for it in items if not _is_camp_item(it)]
    if not items:
        return ""
    if pois:
        items = [it for it in items if not _poi_dup(it, pois, pin_xy)]
    # 近轨迹走廊（2026-11-10 GHT 全线）：**不插队**。near 项只多试几个零重叠位置，
    # 顺序仍按原来的 rank；一旦把 near 提前，会抢占走廊外地名的位置
    # （用户口径：其他地区保持不变）。
    out, dropped, n_placed = [], 0, 0
    _drop_names = []
    # 2026-11-25：同名去重挡不住「谢家沟片区 / 谢家沟社区」这种一字之差、锚点只差
    # 11 px 的点对 —— 各画一个 26 px 圆点 + 白晕就成了双重圆圈。文字都保留，
    # 但符号在同一处只画一个（谁先落位谁占坑）。
    _sym_xy = []
    # 落位诊断（MAP_LBL_DUMP=1）：把每个地名的「锚点 / 最终字位 / 命中第几个候选」
    # dump 成 JSON，用于回答"为什么这个字离锚点这么远"——不靠肉眼猜。
    _dbg = [] if os.environ.get("MAP_LBL_DUMP") else None
    # 符号归属：2026-11-18 默认 False = 矢量层在**落位成功处**补画。
    # 理由见 _base_label_sym：栅格/矢量两套落位差 ~1000 条，栅格画符号会产生
    # 大量无字孤三角。总览（build_bigmap）显式开 True 保持烤栅格。
    _sym_on_raster = GC.cfg_flag("labels_symbol_on_raster", False)
    # 引线最小位移（2026-11-17 用户：很多短引线没必要）。逻辑 px，线路可
    # CFG["label_leader_min_px"] 覆盖；0/None = 回到旧的「>5 px 就画」。
    try:
        _leader_min = float(GC.cfg("label_leader_min_px", GC.LABEL_LEADER_MIN_PX)
                            or GC.LABEL_LEADER_MIN_PX)
    except Exception:                                              # noqa
        _leader_min = GC.LABEL_LEADER_MIN_PX
    # 引线长度上限（2026-11-17 用户）：只保留 3×K px（K=VIS_CAP=3 → 9 px），
    # 超长的线不画（字仍落位，杜绝「字在another山头、一根线横穿地图」）。
    # 逻辑 px 口径与 _leader_min 一致；线路可 CFG["label_leader_max_k"] 改倍数。
    try:
        _leader_max_k = float(GC.cfg("label_leader_max_k", GC.LABEL_LEADER_MAX_K)
                              or GC.LABEL_LEADER_MAX_K)
    except Exception:                                              # noqa
        _leader_max_k = GC.LABEL_LEADER_MAX_K
    _leader_max = _leader_max_k * GC.VIS_CAP
    for it in items:
        fs = it["fs"]
        w = GC.text_w(it["t"], fs) + 4 * _vk()
        h = fs * 1.22
        pad = fs * 0.55
        # 默认位（2026-10-24 用户要求）：**锚点右侧、垂直居中**。只有放不下才动态避让。
        # x0 是文字左边界（text-anchor 默认 start），所以要摆到左边得用 -gap-w。
        gap = 3.2 * _vk()
        vmid = fs * 0.34            # 基线偏移：让文字中线对准锚点
        tried = [(gap, vmid)]
        _dense = bool(it.get("near"))
        # 2026-11-18 用户定案：优先**正右/正左**，斜向放最后。
        # 旧顺序把「右上/右下」排在「左侧」之前、且第一档 r=fs*0.8（6.4 px），
        # 于是轻微冲突就把字推到斜上方 20+ px（截图里「明显错位」的成因）。
        # 新顺序：正右 → 正右水平推远 → 正左 → 正左水平推远 → 同侧上下错开
        #         → 正上/正下 → 斜向（仅走廊内）。
        _rstep = fs * 0.55
        # 1) 贴身正右 / 正左
        tried += [(-gap - w, vmid)]
        # 2) 同侧微小上下错开（仍在贴身范围，避免为躲一个字的缺口就横移很远）
        for k in ((1, 2, 3) if _dense else (1, 2)):
            r = _rstep * k
            tried += [(gap, vmid - r), (gap, vmid + r),
                      (-gap - w, vmid - r), (-gap - w, vmid + r)]
        # 3) 正右 / 正左 水平推远（用户口径：宁可远一点，也别偏到斜上方）
        for k in (1, 2, 3):
            r = _rstep * k
            tried += [(gap + r, vmid), (-gap - w - r, vmid)]
        # 4) 正上 / 正下
        tried += [(-w * 0.5, vmid - fs * 1.25),
                  (-w * 0.5, vmid + fs * 1.45)]
        # 5) 斜向（最后手段，仅走廊内多试）
        if _dense:
            for k in (1, 2, 3, 4, 5):
                r = _rstep * k
                tried += [(gap + r, vmid - r), (gap + r, vmid + r),
                          (-gap - w - r, vmid - r), (-gap - w - r, vmid + r)]
        # 6) 兜底：旧版完整候选集（含大环、大斜向）。上面的贴身顺序找不到位置时
        #    还有退路 —— 保证「只改偏好、不减少落位数」（实测新顺序会把
        #    「吹筒坳」这类被邻居围住的字直接丢掉）。
        _legacy = []
        for k in ((0, 1, 2, 3, 4, 5) if _dense else (0, 1, 2)):
            r = fs * 0.8 * (k + 1)
            _legacy += [(gap, vmid - r), (gap, vmid + r),
                        (-gap - w, vmid), (-gap - w, vmid - r), (-gap - w, vmid + r),
                        (-w * 0.5, vmid - fs * 1.15 - r * 0.55),
                        (-w * 0.5, vmid + fs * 1.35 + r * 0.55)]
        if _dense:
            for k in (2, 3, 4, 5):
                r = fs * 0.8 * (k + 1)
                _legacy += [(gap + r, vmid - r), (gap + r, vmid + r),
                            (-gap - w - r, vmid - r), (-gap - w - r, vmid + r)]
        _seen = set(tried)
        for c in _legacy:
            if c not in _seen:
                _seen.add(c)
                tried.append(c)
        _rs = {"occ": 0, "trk": 0, "oob": 0} if _dbg is not None else None
        _first_hit = None
        for _ti, (dx, dy) in enumerate(tried):
            x0 = it["x"] + dx
            yb = it["y"] + dy
            box = (x0 - pad, yb - fs * 1.02, x0 + w + pad, yb + fs * 0.26)
            if box[0] < 3 or box[2] > IW - 3 or box[1] < 3 or box[3] > IH - 3:
                if _rs is not None:
                    _rs["oob"] += 1
                continue
            _hit = None
            for o in _OCC:
                if GC.rect_overlap_area(box, o) > 0:
                    _hit = o
                    break
            if _hit is not None:
                if _rs is not None:
                    _rs["occ"] += 1
                if _first_hit is None:
                    _first_hit = _hit
                continue
            # 官方营地贴着轨迹，字框落在轨迹带上是**预期**：只避已占框，
            # 不再因压线丢掉（其余地名逐字保持旧行为）。
            if not _camp_pass and _near_track(box, track, half_w):
                if _rs is not None:
                    _rs["trk"] += 1
                continue
            _OCC.append(box)
            n_placed += 1
            if _dbg is not None:
                _dbg.append({"t": it["t"], "ax": it["x"], "ay": it["y"],
                             "x": x0, "y": yb, "dx": dx, "dy": dy, "try": _ti,
                             "d": math.hypot(x0 - it["x"], yb - it["y"]),
                             "w": w, "fs": fs, "n_cand": len(tried),
                             "rs": _rs,
                             "first_hit": None if _first_hit is None
                             else [round(v, 1) for v in _first_hit]})
            f, st = it["fill"], it["stroke"]
            c1 = "#%02x%02x%02x" % (f[0], f[1], f[2])
            c2 = "#%02x%02x%02x" % (st[0], st[1], st[2])
            # 2026-10-24 用户反馈「看不出这个地名标的是哪个地方」：避让把字推离锚点后，
            # 图上一根线都没有，读者无从对应。→ 位移 > 5 px 就补一条 0.5 px 细引线。
            _ax, _ay = it["x"], it["y"]
            _cx = min(max(_ax, box[0]), box[2])
            _cy = min(max(_ay, box[1]), box[3])
            # 符号：2026-11-18 默认由矢量层在**落位成功处**补画（_sym_on_raster=False），
            # 符号与字同生同灭 —— 栅格层那套多落位的符号不再单独出现（无字孤三角）。
            _sym = _base_label_sym(f, _sym_on_raster, it.get("k"))
            # 2026-11-25 尺度自适应：符号半宽按**目标物理 px**，再以字高兜底。
            _sym_key = {"peak": "osm_peak_half", "saddle": "osm_saddle_half",
                        "glacier": "osm_glacier_half", "dot": "osm_dot_r"}.get(_sym, "")
            _sr_sym = _vp(GC.VISUAL_PX[_sym_key], 0.5, fs) if _sym_key else 0.0
            # 同一处只画一个符号：两条不同名的 OSM 点锚点挨得很近时，
            # 两个圆点 + 白晕会叠成"双重圆圈"。文字都保留，符号先到先得。
            _draw_sym = bool(_sym)
            if _draw_sym:
                if any(math.hypot(_ax - sx, _ay - sy) < _sr_sym + s0
                       for sx, sy, s0 in _sym_xy):
                    _draw_sym = False
                else:
                    _sym_xy.append((_ax, _ay, _sr_sym))
            if _draw_sym:
                # 2026-11-18 用户实测：POI 标注压在峰三角上，看起来"营地被标成了山峰"。
                # 原因是 _OCC 里只有文字框，符号画在锚点却从不占位。补一个小方框，
                # 让后续的 POI 标注 / 其他地名绕开已画出的符号。
                _sr = 3.4 * _vk()
                _OCC.append((_ax - _sr, _ay - _sr, _ax + _sr, _ay + _sr))
            if _draw_sym and _sym == "peak":
                # 峰：正三角（目标物理半宽 9 px，SS=10 → 0.9 逻辑 px）
                _tr_ = _vp(GC.VISUAL_PX["osm_peak_half"], 0.55, fs)
                out.append(f'<path d="M {_ax:.3f},{_ay - _tr_:.3f} '
                           f'L {_ax + _tr_ * 0.92:.3f},{_ay + _tr_ * 0.62:.3f} '
                           f'L {_ax - _tr_ * 0.92:.3f},{_ay + _tr_ * 0.62:.3f} Z" '
                           f'fill="{c1}" stroke="#FFFFFF" stroke-width="{_halo_sw_sym():.4f}" '
                           f'stroke-linejoin="round"/>')
            elif _draw_sym and _sym == "saddle":
                # 垭口：倒三角（尖端向下）+ 米色填充，与峰名三角明确区分
                _tr_ = _vp(GC.VISUAL_PX["osm_saddle_half"], 0.55, fs)
                out.append(f'<path d="M {_ax - _tr_ * 0.95:.3f},{_ay - _tr_ * 0.72:.3f} '
                           f'L {_ax + _tr_ * 0.95:.3f},{_ay - _tr_ * 0.72:.3f} '
                           f'L {_ax:.3f},{_ay + _tr_:.3f} Z" '
                           f'fill="#FFF4E8" stroke="{c1}" stroke-width="{_halo_sw_sym():.4f}" '
                           f'stroke-linejoin="round"/>')
            elif _draw_sym and _sym == "glacier":
                # 冰川 / 水系：菱形（语义错配另记，不在本轮改形状）
                _tr_ = _vp(GC.VISUAL_PX["osm_glacier_half"], 0.5, fs)
                out.append(f'<path d="M {_ax:.3f},{_ay - _tr_:.3f} L {_ax + _tr_:.3f},{_ay:.3f} '
                           f'L {_ax:.3f},{_ay + _tr_:.3f} L {_ax - _tr_:.3f},{_ay:.3f} Z" '
                           f'fill="#E9F4FC" stroke="{c1}" stroke-width="{_halo_sw_sym():.4f}" '
                           f'stroke-linejoin="round"/>')
            elif _draw_sym and _sym == "dot":
                # 地名 / 湖 / 河：小圆点 —— "这个字标的是哪"的答案。
                out.append(f'<circle cx="{_ax:.1f}" cy="{_ay:.1f}" '
                           f'r="{_vp(GC.VISUAL_PX["osm_dot_r"], 0.5, fs):.3f}" '
                           f'fill="{c1}" fill-opacity="0.9" stroke="#fff" '
                           f'stroke-width="{_halo_sw_sym():.4f}"/>')
            # 引线独立于符号归属：符号在栅格时同样需要它指认"字标的是哪"。
            # 但只有**真被推远**的才画：麦径实测 844 条里中位位移仅 23 px，
            # 短引线看着像脏线，反而干扰阅读（2026-11-17 用户）。
            if _leader_min < math.hypot(_cx - _ax, _cy - _ay) <= _leader_max:
                out.append(f'<line x1="{_ax:.1f}" y1="{_ay:.1f}" x2="{_cx:.1f}" '
                           f'y2="{_cy:.1f}" stroke="{c1}" stroke-width="0.1" '
                           f'stroke-opacity="0.55"/>')
            t = it["t"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            wt = 700          # 字号统一后不再靠字重区分主次（白晕 + 颜色承担）
            out.append(f'<text x="{x0:.3f}" y="{yb:.3f}" font-size="{fs:.3f}" '
                       f'font-weight="{wt}" fill="{c1}" stroke="{c2}" stroke-width="{_halo_sw_text(fs):.4f}" '
                       f'stroke-opacity="0.92" paint-order="stroke" '
                       f'stroke-linejoin="round">{t}</text>')
            break
        else:
            dropped += 1
            _drop_names.append(it["t"])
    if dropped:
        # ⚠ 统计必须用 n_placed（真实落位条数），不能用 len(out)：out 是 SVG 片段列表，
        #   每个名字可能含「符号 + 引线 + text」多个元素，开着矢量符号时会翻倍。
        print(f"   底图地名：落位 {n_placed} 条 · 拥挤丢弃 {dropped} 条")
        # 丢的是谁？"落位 131 / 丢弃 11" 这种汇总数看不出丢掉的是主峰还是次要湖名 ——
        # 用户报「图上怎么没有珠峰」时，第一件事就是确认它是否在这一行里。
        if os.environ.get("MAP_LBL_DEBUG"):
            print("      丢弃：" + "、".join(_drop_names))
    if _dbg is not None:
        try:
            (GC.OUT / f"_label_dump_{only or 'all'}_{_DBG_TAG}.json").write_text(
                json.dumps(_dbg, ensure_ascii=False), encoding="utf-8")
        except Exception:                                          # noqa
            pass
    return "".join(out)


# ---------------------------------------------------------------- 署名徽标（可选）
# `CFG["map_badge"] = True`（或 {"size": 78, "gap": 9}）→ 在比例尺旁画一枚圆形署名徽标。
#   素材：`scripts/assets/avatar_ring.png`（或技能目录 assets/，缺了自动不画、不报错）。
#   落点（2026-10-26 用户指定）：**首选比例尺正上方、水平居中**，直径 ≈ 比例尺卡宽的 42%
#   （视觉重量与比例尺相当）。若压轨迹 / 图钉 / 已摆家具 / 底图地名锚点，按候选序列外移
#   —— 这是"遮住主要地形就动态挪位"的落地方式：地形本身无法量化，用**可量化的地物**
#   （轨迹、点位、地名锚点、标高）当代理指标。
def _badge_cfg():
    """署名徽标开关。**默认开启**（2026-10-26 起是技能默认出品的一部分）。

    `route_def.CFG["map_badge"]` 三态：
      · 缺键 / True        → 用默认（直径 = 卡宽×0.62 夹逼 120–140）
      · dict               → 覆盖 `{"size": 150, "gap": 14}`
      · False / None 显式   → 不画
    素材缺失只是不画、不报错（`author_asset()` 先找项目 assets、再回落技能目录）。
    """
    v = _CFG.get("map_badge", True)
    if v is False or v is None:
        return None
    return v if isinstance(v, dict) else {}


_BADGE_DOC = None


def _badge_doc():
    """读精简版矢量徽标（`assets/avatar_ring.svg`），并按需改写根节点 / 加 id 前缀。

    为什么要矢量（2026-10-26 用户反馈「名称和小红书账号放大后模糊」）：
    原先用 384 px 的 PNG，高清图里只显示 201 px，放大一档就是糊的；而这份 SVG 的
    环与文字都是矢量，只有中间那张照片是位图（已降到 640 JPEG，整体 115 KB，
    比 384 PNG 的 base64 还小）。内联进地图 SVG 后**任意倍率都锐利**。
    """
    global _BADGE_DOC
    if _BADGE_DOC is not None:
        return _BADGE_DOC
    p = GC.author_asset("avatar_ring.svg")
    if p is None:
        _BADGE_DOC = ""
        return ""
    try:
        s = Path(p).read_text(encoding="utf-8")
    except Exception:                                            # noqa: BLE001
        _BADGE_DOC = ""
        return ""
    # id 加前缀：同一份地图里若还嵌了别的东西，avatarClip / scrim 这类通用名会撞车
    for _id in ("avatarClip", "scrim", "vignette", "softShadow"):
        s = s.replace(f'id="{_id}"', f'id="bd{_id}"').replace(f"url(#{_id})", f"url(#bd{_id})")
    # 根节点只保留 xmlns + viewBox，尺寸交给调用方（x/y/width/height 由 _badge_svg 填）
    s = re.sub(r"<svg\b[^>]*>", '<svg xmlns="http://www.w3.org/2000/svg" '
               'viewBox="0 0 1200 1200">', s, count=1)
    _BADGE_DOC = s
    return s


def _badge_data_uri():
    """位图回退口径（矢量版缺失时用）。"""
    p = GC.author_asset("avatar_ring.png")
    return GC._data_uri(p) if p is not None else None


_OSM_PTS = None


def _osm_barrier_pts():
    """OSM 线状地物（步道 / 公路 / 水系）的逻辑坐标点，专供徽标避让。

    为什么需要（2026-10-26 峨眉山实测）：图上那条紫色步道是画在**栅格底图**里的，
    SVG 层根本没有它的坐标；徽标只避让轨迹时，有一小段步道正好从头像顶上穿过去。
    这里把 osm.json 的 way 几何投影一遍，作为"次要地物"障碍（权重低于轨迹）。
    """
    global _OSM_PTS
    if _OSM_PTS is not None:
        return _OSM_PTS
    pts = []
    try:
        d = json.loads((GC.OUT / "osm.json").read_text(encoding="utf-8"))
    except Exception:                                           # noqa: BLE001
        _OSM_PTS = pts
        return pts
    for e in d.get("elements", []):
        if e.get("type") != "way":
            continue
        t = e.get("tags") or {}
        if not any(k in t for k in ("highway", "waterway", "railway")):
            continue
        for g in (e.get("geometry") or []):
            if "lon" not in g or "lat" not in g:
                continue
            x, y = P(g["lon"], g["lat"])
            if -20 <= x <= IW + 20 and -20 <= y <= IH + 20:
                pts.append((x, y))
    _OSM_PTS = pts
    return pts


def _badge_place(sb, D, gap, track, pins, anchors):
    """挑一个最不碍事的落点，返回 (box, where, detail)。首选正上方，冲突才外移/抬高。

    ⚠ 候选必须是"上方一个 **二维网格**"，不能只在正中线上抬高（2026-10-26 峨眉山实测：
    徽标放大到 99 px 后，正中线的四个高度全被那条斜穿的步道 / 地名挡住，程序只好一路
    抬到 2.3 倍直径处 —— 图上看着像飘在山坡中间，不再像是"比例尺的署名"）。
    横向 5 档 × 纵向 4 档能吃到"斜线旁边的空档"：优先最低的一排，同一排里从左到右试。
    """
    x0, y0, x1, y1 = sb
    sbw = x1 - x0
    mid = (y0 + y1) / 2.0 - D / 2.0
    # CFG["badge_above"]=True → 只放「正上方居中」这一个位置。
    # 用户口径（2026-11-04 郭喀拉 29 天）：「徽标就摆在比例尺正上方」——宁可压住底图路网，
    # 也不要为了让开一条乡道而横移/抬高（那会看着像飘在山坡中间，不像比例尺的署名）。
    if _CFG.get("badge_above"):
        _bx = x0 + (sbw - D) / 2.0
        _by = y0 - gap - D
        _box = (_bx, _by, _bx + D, _by + D)
        if (_box[0] >= _PAD and _box[1] >= _PAD
                and _box[2] <= IW - _PAD and _box[3] <= IH - _PAD):
            return _box, "正上方（强制）", "居中"
    cands = []
    # 档位代价按 k² 长：**"紧贴比例尺"比"不压路网"更重要** —— 比例尺在城区/山脚时，
    # 上方整片都是 OSM 路网（实测一个 99 px 圆里 106 个节点），若按线数重罚就必然一路抬高，
    # 那时徽标已飘到山坡中间、根本不像"比例尺的署名"。硬约束只留给轨迹/点位/地名。
    for k, yy in enumerate((gap, gap + D * 0.42, gap + D * 1.0, gap + D * 1.75)):
        for fi, frac in enumerate((0.5, 0.0, 1.0, 0.25, 0.75)):
            cands.append((f"上方 {k + 1} 档·{int(frac * 100)}%",
                          (x0 + frac * (sbw - D), y0 - yy - D),
                          k * k * 400.0 + fi * 2.0))
    cands += [
        ("左侧", (x0 - gap - D, mid), 900.0),
        ("右侧", (x1 + gap, mid), 900.0),
        ("下方", (x0 + (sbw - D) / 2.0, y1 + gap), 900.0),
        ("左侧外移", (x0 - 2 * gap - D, mid), 1400.0),
        ("右侧外移", (x1 + 2 * gap, mid), 1400.0),
        ("左上角", (x0 - 2 * gap - D, y0 - gap - D), 1400.0),
        ("右下角", (x1 + 2 * gap, y1 + gap), 1400.0),
    ]
    osm = _osm_barrier_pts()
    best = None
    for nm, (bx, by), pen in cands:
        box = (bx, by, bx + D, by + D)
        if box[0] < _PAD or box[1] < _PAD or box[2] > IW - _PAD or box[3] > IH - _PAD:
            continue
        n_trk = sum(1 for x, y in track
                    if box[0] - 3 <= x <= box[2] + 3 and box[1] - 3 <= y <= box[3] + 3)
        n_pin = sum(1 for x, y in pins if box[0] <= x <= box[2] and box[1] <= y <= box[3])
        n_occ = sum(1 for ob in _OCC if GC.rect_overlap_area(box, ob) > 0)
        # 底图地名锚点：垭口/村落（rank 高）比次峰更不该被压
        n_lab = sum(1 for it in anchors if box[0] <= it["x"] <= box[2] and box[1] <= it["y"] <= box[3])
        # 步道/公路/水系：软指标（贴纸盖住路网是正常的，不能因此把徽标推到别处）
        n_osm = sum(1 for x, y in osm if box[0] <= x <= box[2] and box[1] <= y <= box[3])
        s = pen + 1000.0 * n_trk + 400.0 * n_pin + 250.0 * n_occ \
            + 90.0 * n_lab + 4.0 * n_osm
        if best is None or s < best[0] - 1e-9:
            det = [f"{k} {n}" for k, n in (("轨迹", n_trk), ("点位", n_pin),
                                           ("家具", n_occ), ("地名", n_lab), ("线状地物", n_osm)) if n]
            best = (s, box, nm, "、".join(det))
        if os.environ.get("MAP_BADGE_DEBUG"):
            print(f"      [badge] {nm:>16} 分 {s:8.1f}  "
                  f"轨迹{n_trk} 点位{n_pin} 家具{n_occ} 地名{n_lab} 线{n_osm}")
    if best is None:
        return None
    return best[1], best[2], best[3]


def _badge_svg(box, uri):
    x, y, x1, y1 = box
    D = x1 - x
    r = D / 2.0
    doc = _badge_doc()
    if doc:
        # 矢量版：嵌套 <svg> 直接带 x/y/width/height → 文字/环随放大保持锐利
        core = re.sub(r"<svg\b[^>]*>",
                      f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 1200" '
                      f'x="{x:.1f}" y="{y:.1f}" width="{D:.1f}" height="{D:.1f}">',
                      doc, count=1)
    else:
        core = (f'<image x="{x:.1f}" y="{y:.1f}" width="{D:.1f}" height="{D:.1f}" '
                f'href="{uri}" preserveAspectRatio="xMidYMid meet"/>')
    # 一圈白环：让徽标读成"贴上去的署名"，而不是地形上的一个脏点
    return (core + f'<circle cx="{x + r:.1f}" cy="{y + r:.1f}" r="{r - 1.0:.1f}" fill="none" '
            f'stroke="#FFFFFF" stroke-width="2.0" opacity="0.85"/>')


def build_map_overlay(mobile=False):
    """返回地图 SVG 的内容（不含 <svg> 外壳与底图）。"""
    global PS, _PAD, _PIN_R, _DBG_TAG
    global _lg_rect, _sb_rect, _cp_rect, _bd_rect
    M = MFS_M if mobile else 1.0
    _DBG_TAG = "mobile" if mobile else "desk"
    PS = PS_BASE * M
    # 图钉占位半径 = **符号实际半径**（2026-11-18）。原来写死 19*M，与 mk_pin
    # 真正的半径（_pin_d()/2，高清图 k=3 → 22.5）不一致：框比符号小 → 文字能压到
    # 图钉上；框比符号大 → 正位被冤枉挡掉、标注被推远。统一取实际半径。
    _PIN_R = _pin_d() / 2.0
    _PAD = 22
    _OCC.clear()
    global _TRK, _TRK_HW
    _LABELS.clear()
    LABEL_OF.clear()
    # ⚠ 三件套矩形必须在每轮开头清零：手机版**不摆图例**，若不清零，
    #   它读到的是上一轮（桌面版）留下的 _lg_rect —— 自检会凭空报一条「图例 × 比例尺」重叠，
    #   而图上根本没有图例。这类"幽灵家具"只看图是发现不了的。
    _lg_rect = _sb_rect = _cp_rect = _bd_rect = None

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
    # 2026-11-10：主线也走「设计基准 px × min(SS,cap)」→ cap=3 时 2.0×3 = 6 输出 px。
    if _CFG.get("route_w", None) is None and _GROUND_FS:
        # 2026-11-25 尺度自适应：主线走目标物理 px（SS=10 → 0.6 逻辑 px），
        # 不再写死 1.5×SS 变成 15 px 的粗线；显式 route_w 的老线路逐字不变。
        ROUTE_W = max(_vp(GC.VISUAL_PX["route_w"]), 0.5 * _FS_BASE * _vk()) * M
    else:
        ROUTE_W = float(_CFG.get("route_w") or ROUTE_W_DEF) * M * _vk()
    # 「白色描边」按**每侧**宽度定义：总衬底 = 主线 + 2×描边。
    # 若按总宽写（1.0 < 2.0），白边会被主线完全盖住、且不报错 —— 所以这里显式换算。
    # 启用 visual 时：白底每侧恒 halo_out_px 输出 px（不随超采，全线 = 1 px）。
    if _CFG.get("route_halo_side", None) is None and _GROUND_FS:
        _halo_side = _vp(GC.VISUAL_PX["route_halo_side"])
    else:
        _halo_side = float(_CFG.get("route_halo_side") or ROUTE_HALO_SIDE_DEF)
    _ho = _halo_out()
    if _ho is not None:
        _halo_side = _ho
    HALO_W = (ROUTE_W / M + 2 * _halo_side) * M
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
    if not _CFG.get("days"):
        # 无分日的线路（2026-10-27）：整条线一个颜色，不再按 SPLIT_I 劈两段
        _bounds_km = [0.0, float(TOTAL_KM)]
    elif len(_bounds_km) == 2:                    # 无 day_bounds → 老的两段行为
        _bounds_km = [0.0, float(KM[SPLIT_I]), float(TOTAL_KM)]
    while len(_day_cols) < len(_bounds_km) - 1:   # 分段多于颜色 → 沿用同族末色
        _day_cols.append(_day_cols[-1])

    def _idx_km(kmv):
        for i, k in enumerate(KM):
            if k >= kmv:
                return i
        return len(KM) - 1

    _split_is = [_idx_km(b) for b in _bounds_km]
    # 折返型线路（原路上、原路下）：后一天的下撤段与前几天的上升段**完全重合**，
    # 按天序绘制会让最后一天的颜色把整条线盖成一个色（2026-10-28 金银山实测：
    # D4 从 C2 冲顶再退回公路，整条 32 km 只剩 D4 的洋红）。
    # CFG["day_draw_reverse"]=True 时倒序绘制 → 每段显示"它第一次被走过的那天"的颜色。
    _n_seg = len(_bounds_km) - 1
    _order = (range(_n_seg - 1, -1, -1) if _CFG.get("day_draw_reverse") else range(_n_seg))
    for si in _order:
        i0, i1 = _split_is[si], _split_is[si + 1]
        seg = XY[i0:max(i1 + 1, i0 + 2)]
        if len(seg) < 2:
            continue
        o.append(f'<path d="{d_of(seg)}" fill="none" stroke="{_day_cols[si]}" '
                 f'stroke-width="{ROUTE_W}" stroke-linecap="round" stroke-linejoin="round"/>')

    # ---- 底图地名（矢量层）：占位记在这里，最后统一摆位后再插回来 ----
    #   （要等图钉 / 里程点 / POI 标注都摆完，_OCC 才完整，避让才算数）
    _LBL_AT = len(o)

    # 每 5 km 里程点：统一圆径（km_circle_metrics），撞图钉沿轨迹微调而不是丢号。
    pin_xy = [P(d["lon"], d["lat"]) for d in POIS]
    # 里程点间隔：默认 5 km。**长线（>200 km）必须放大**，否则 469 km 会画 94 个圆圈、
    # 整条线变成一串珠子（2026-11-04 郭喀拉29天实测）。CFG["km_step"] 覆盖。
    _km_step = max(1, int(GC.cfg("km_step", 5) or 5))
    # 字号基准：本线路最大里程圈位数（2026-11-24 用户定案）。所有圈传同一个 nd，
    # 字号取「最大位数动态填充后」的最小值 —— 5/10/15 km 数字完全一致，
    # 不再出现一位数撑满、两位数偏小；短线路（<100 km）也不会白白缩到三位数档。
    _km_nd = max(1, len(str(int(TOTAL_KM))))
    for target in range(_km_step, int(TOTAL_KM) + 1, _km_step):
        i = min(range(len(KM)), key=lambda j: abs(KM[j] - target))
        z = MFS_S if mobile else 1.0
        k = _vk()
        rr, fs, sw = GC.km_circle_metrics(_km_nd, M_PER_PX, z, float(_VIS["ss"]), M)
        # 撞图钉时**沿轨迹前后微调**（2026-11-24 用户定案）：原来直接 continue，
        # 20/30/85 km 整号消失、5 km 节奏断档。现在在 ±0.4 km 窗口里找离目标
        # 最近且不压图钉的位置；只有整段都被占才放弃该号。
        _shift_km = 0.4
        _cands = [i]
        for _j in range(i - 1, -1, -1):
            if KM[i] - KM[_j] > _shift_km:
                break
            _cands.append(_j)
        for _j in range(i + 1, len(KM)):
            if KM[_j] - KM[i] > _shift_km:
                break
            _cands.append(_j)
        _cands.sort(key=lambda _j: abs(KM[_j] - target))
        for _j in _cands:
            x, y = XY[_j]
            if not any((x - px) ** 2 + (y - py) ** 2
                       < (rr + (_PIN_R + 2) * k) ** 2 for px, py in pin_xy):
                break
        else:
            continue
        # 里程点是**固定锚点**（画在轨迹上），登记进 _OCC 让地名/POI 标注绕开它 ——
        # 否则会出现「195」圆圈压住垭口三角、或者地名贴着数字写（2026-10-27 实测）。
        _OCC.append((x - rr - k, y - rr - k, x + rr + k, y + rr + k))
        o.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{rr:.3f}" fill="#FFFFFF" '
                 f'stroke="#3A4250" stroke-width="{sw:.3f}"/>'
                 f'<text x="{x:.3f}" y="{y+fs*0.38:.3f}" text-anchor="middle" '
                 f'font-size="{fs:.3f}" font-weight="800" '
                 f'fill="#3A4250">{target}</text>')

    # 分日界短线（每个分界一条；无 day_bounds 时只画 split1）
    # 2026-11-18 用户定案：不再水平横贯图幅 —— 总长限 3×K px（K=VIS_CAP=3 → 9px），
    # 方向取分界点前后约 1 km 轨迹趋势的法线（大致垂直于轨迹走向）。
    _dash_w = float(_CFG.get("day_dash_w") or DAY_DASH_W_DEF)
    if _CFG.get("day_dash_w", None) is None and _GROUND_FS:
        _dash_core = _vp(GC.VISUAL_PX["day_dash"]) * M
    else:
        _dash_core = _dash_w * M * _vk()
    _dash_halo = (_dash_core + GC.visual_px(2.0, _VIS["ss"])) \
        if _ho is None else _dash_core + 2 * _ho
    try:
        _dash_max_k = float(GC.cfg("day_dash_max_k", GC.DAY_DASH_MAX_K)
                            or GC.DAY_DASH_MAX_K)
    except Exception:                                              # noqa
        _dash_max_k = GC.DAY_DASH_MAX_K
    _dash_half = _dash_max_k * GC.VIS_CAP / 2.0        # 半长（逻辑 px）
    _TREND_KM = 1.0                                    # 局部趋势窗口（前后各 1 km）
    for _bi in _split_is[1:-1] if len(_split_is) > 2 else [SPLIT_I]:
        sx, sy = XY[_bi]
        _i0, _i1 = _bi, _bi
        while _i0 > 0 and KM[_bi] - KM[_i0] < _TREND_KM:
            _i0 -= 1
        while _i1 < len(XY) - 1 and KM[_i1] - KM[_bi] < _TREND_KM:
            _i1 += 1
        _tx = XY[_i1][0] - XY[_i0][0]
        _ty = XY[_i1][1] - XY[_i0][1]
        _tn = math.hypot(_tx, _ty)
        if _tn > 1e-9:
            _nx, _ny = -_ty / _tn, _tx / _tn           # 轨迹法线 = 垂直于局部走向
        else:
            _nx, _ny = 0.0, 1.0                        # 退化（原地折返）：竖直短线
        _x1, _y1 = sx - _nx * _dash_half, sy - _ny * _dash_half
        _x2, _y2 = sx + _nx * _dash_half, sy + _ny * _dash_half
        o.append(f'<line x1="{_x1:.1f}" y1="{_y1:.1f}" x2="{_x2:.1f}" y2="{_y2:.1f}" '
                 f'stroke="#FFFFFF" stroke-width="{_dash_halo:.4f}" stroke-linecap="round" '
                 f'opacity="0.85"/>'
                 f'<line x1="{_x1:.1f}" y1="{_y1:.1f}" x2="{_x2:.1f}" y2="{_y2:.1f}" '
                 f'stroke="#6C7480" stroke-width="{_dash_core:.4f}" '
                 f'stroke-dasharray="{max(1.5, _dash_half):.1f} {max(1.5, _dash_half):.1f}" '
                 f'stroke-linecap="round"/>')

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
    # 比例尺长度**按底图比例自适应**（取整数档，目标 ≈ 地图宽度的 _SB_FRAC）。
    # 本图 1 km ≈ 80 逻辑 px，自动选出的正是 2 km —— 但换一条线路（bbox 大小不同）时
    # 写死 2 km 就会要么占满整幅、要么小到看不清。档位标签统一 0 / 一半 / 全长。
    # ⚠ 2026-10-29 用户反馈南山脊线比例尺太大：新增 CFG["scale_bar_frac"] 旋钮
    #   （默认 0.16），让过宽的线路把比例尺收到图宽的一成左右。
    _SB_FRAC = float(_CFG.get("scale_bar_frac", 0.16) or 0.16)
    sb_km = _nice_bar_km(M_PER_PX, IW, target_frac=_SB_FRAC)
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

    # ---- 署名徽标（可选）：贴在比例尺旁，压地物才挪位 ----
    #   必须在 POI 标注之前算：徽标占位要先登记进 _OCC，标注才会绕开它。
    _bd_svg = ""
    _bcfg = _badge_cfg()
    if _bcfg is not None and not mobile and "sb" in placed:
        # 优先矢量版（avatar_ring.svg，文字放大不糊）；没有才回退位图 PNG
        _uri = None if _badge_doc() else _badge_data_uri()
        if not _uri and not _BADGE_DOC:
            print("   ⚠ 配了 map_badge，但找不到 assets/avatar_ring.svg|png —— 本轮不画徽标")
        else:
            _sbr = placed["sb"][2]
            _sbw = _sbr[2] - _sbr[0]
            # 直径默认 = 比例尺卡宽 × 0.62，并**夹逼到 120–140**：
            # 52 条线路的比例尺卡宽差 2 倍（160–337），不夹逼时直径会从 98 漂到 209 ——
            # 209 占图宽 13.9%（比比例尺还抢眼），98 在 3 倍超采样下账号字只有 11 px（仍糊）。
            # 夹逼后 120×3=360 ~ 140×3=420 px 直径，账号字 13.8–16.1 px，各线观感一致。
            _D = float(_bcfg.get("size") or min(140.0, max(120.0, round(_sbw * 0.62))))
            _gap = float(_bcfg.get("gap") or 9)
            _res = _badge_place(_sbr, _D, _gap, XY, pin_xy, _load_base_labels())
            if _res:
                _box, _where, _det = _res
                _bd_rect = _box
                _OCC.append(_box)
                _bd_svg = _badge_svg(_box, _uri)
                print(f"   署名徽标：{_D:.0f} px · {_where}"
                      + (f"（已避让 {_det}）" if _det else "（无冲突）"))
            else:
                print("   ⚠ 署名徽标：候选位全放不下，本轮不画")

    for d in POIS:
        o.append(mk_pin(*P(d["lon"], d["lat"]), d["kind"]))

    # ---- 山峰名优先（CFG["label_priority"] = "peak"）----
    # 峰名锚点就是山顶本身：被 POI 标注挤走或挤丢，读者就再也对不上"这是哪座山"。
    # 所以先让峰名占位，轮到 POI 标注时它会自己绕开（_layout 有 8 方向 × 4 档半径 + 字号阶梯）。
    _peak_lbl_svg = ""
    # 2026-10-28 起这是**默认**：峰名锚点就是山顶，被 POI 标注挤走/挤丢就再也对不上"这是哪座山"；
    # 轨迹上的 POI 标注本来就可以挪（有 8 方向 × 4 档半径 + 引线）。
    # 想退回老顺序（底图地名一律给轨迹让路）写 "label_priority": "none"。
    _peak_first = str(_CFG.get("label_priority", "peak") or "").strip().lower() in ("peak", "peaks", "summit")
    if _peak_first:
        _peak_lbl_svg = _place_base_labels(XY, (ROUTE_W + HALO_W) / 2 + 2,
                                           POIS, pin_xy, only="peak")

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
        base = _FS_POI * _vk()  # 地面口径倒算 / 显式 9 px 基准 × min(SS,cap)
        if _GROUND_FS and _VIS.get("halo_out_px") is None:
            base = max(base, GC.HTML_MIN_FS_LOG * 1.125)
        col = D2_T if k in ("start", "end") else INK
        if mobile:
            txt = getattr(RD, "SHORT", {}).get(name, name)
        else:
            txt = f"{name} {RD.fmt_ele(ele)} m"
        best = None
        for sc in FS_LADDER:
            fs = base * sc
            w = GC.text_w(txt, fs) + 26 * M
            # 首选偏移（作者的 dx/dy）：**跟着图钉实际尺寸走**。
            # 图钉半径 = _pin_d()/2（高清图 k=3 → 22.5、HTML k=1 → 7.5），
            # 文字左缘 = 半径 + 6 间隙。写死 22 时比高清图钉半径(22.5)还小，
            # 正位永远被占位框挡掉 → 标注被甩到远处（2026-11-18 用户："还是这么远"）。
            _gap_poi = _PIN_R + 6.0
            _dxo = math.copysign(min(abs(dx) * M, _gap_poi), dx) if dx else 0.0
            _dyo = math.copysign(min(abs(dy) * M, _gap_poi), dy) if dy else 0.0
            r = _layout(x, y, _dxo * M, _dyo * M, anc, w, fs * 0.86, commit=False)
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
        # 2026-11-18 用户：引线长度硬上限 K×2 逻辑 px（默认 6 px）。
        # 落位已被 _layout 限在 cap 内，这里再卡一道，杜绝旧版 34*M 无上限的长线。
        if 2.0 * M < gap <= _label_lead_max():
            px = min(max(x, box[0]), box[2])
            py = min(max(y, box[1]), box[3])
            # 2026-11-25：引线走目标物理 px（2.5 px 芯 + 细白边），旧口径
            # 1.4×_vk() 在 SS=10 下变成 14 px 实线 / 16 px 白晕，像一根胶囊。
            if _GROUND_FS:
                _lead_core = _vp(GC.VISUAL_PX["leader_core"]) * M
                _lead_halo = _lead_core + 2 * (_ho if _ho is not None
                                              else GC.visual_px(1.0, _VIS["ss"]))
            else:
                _lead_core = 1.4 * M * _vk()
                _lead_halo = 2.6 * M if _ho is None else _lead_core + 2 * _ho
            o.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="#FFFFFF" stroke-width="{_lead_halo:.4f}" stroke-linecap="round"/>'
                     f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                     f'stroke="{col}" stroke-width="{_lead_core:.4f}" stroke-linecap="round" '
                     f'opacity="0.75"/>')
        # ⚠ 白描边要"细而净"：5.4*PS（≈4.6 逻辑 px / 9 栅格 px）在浅色地形上会糊成
        #   一圈白阴影（用户原话"字体看上去有白色的阴影"）。收到 2.4*PS ≈ 字号的 13%，
        #   读起来是"描边"而不是"发光"，同时仍能压住底图等高线。
        # 文字垂直居中于点位（2026-11-24 用户定案）：baseline 必须跟字号走。
        # 旧版写死 ty+7.4*PS（PS=0.66 → 下沉 4.88 逻辑 px），是按字号 9 px 设计的；
        # 地面口径把 POI 字号缩到 5 px 后，4.88 ≈ 字高本身，文字整体偏下、引线够不着。
        # 0.35×fs 是基线相对字框中心的常规下沉，任意字号都居中（点位↔引线↔字心共线）。
        _baseline = ty + 0.35 * fs
        o.append(f'<text x="{tx}" y="{_baseline:.1f}" text-anchor="{ta}" font-size="{fs:.1f}" '
                 f'font-weight="700" fill="{col}" stroke="#FFFFFF" stroke-width="{_sw(max(1.1, fs * 0.17)):.4f}" '
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
            # 符号样本与图面**同尺寸**（2026-11-18 用户口径）：走 `k=_vk()`、不再乘 LG_GLYPH。
            # 旧写法 `scale(0.55)` + `k=1.0` 会让图例里的钉子是图面上的 0.55×，读起来像另一个符号。
            _glyph_i = _pin_d()
            for i, (kind, nm) in enumerate(syms):
                cx = lx + 34 * S + (i % 2) * col_w
                cy = sy + (LG_SYM0 + LG_SYM_PITCH * (i // 2)) * S
                g.append(f'<g transform="translate({cx:.1f},{cy:.1f})">'
                         f'{mk_pin(0, 0, kind, k=_vk())}</g>')
                g.append(f'<text x="{cx + _glyph_i / 2 + 7:.0f}" y="{cy+7*S:.0f}" font-size="{round(17*S)}" '
                         f'font-weight="700" fill="#4A5261">{nm}</text>')
        g.append(f'<text x="{lx+18*S:.0f}" y="{ly+LG_H-14*S:.0f}" font-size="{round(16.5*S)}" '
                 f'font-weight="600" fill="#7A8290">{GC.author_legend(_CFG.get("legend_foot", "仅供参考 · 非导航用途"))}</text>')
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
    if _bd_svg:
        o.append(_bd_svg)
    # 官方营地（2026-11-23）：在 POI 标注之后、其余底图地名之前占位 ——
    # 既绕开图钉，又不会先被远处地名挤掉；符号/文字仍是 draw_osm 那套绿色营地样式。
    _camp_svg = _place_base_labels(XY, (ROUTE_W + HALO_W) / 2 + 2,
                                   POIS, pin_xy, only="camp")
    _rest_lbl_svg = _place_base_labels(XY, (ROUTE_W + HALO_W) / 2 + 2,
                                       POIS, pin_xy,
                                       only=("rest" if _peak_first else None))
    # 图层顺序：底图地名（含先排的峰名）压在最下面，POI 标注盖在上面。
    o.insert(_LBL_AT, _peak_lbl_svg + _camp_svg + _rest_lbl_svg)
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
    furn = {"图例": _lg_rect, "指北针": _cp_rect, "比例尺": _sb_rect, "署名": _bd_rect}
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
    # 20000 px 级底图（18048×15941 ≈ 288 MP）会撞 PIL 的 DecompressionBomb 上限
    # （178 MP）："Image size ... exceeds limit ... could be decompression bomb"。
    # 底图是自己渲的，关掉这道保险是安全的；不关的话 build_guide 直接在 embed 阶段崩。
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(GC.OUT / "base_map.jpg").convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    _q = int(GC.cfg("embed_quality", GC.cfg("jpg_quality", GC.JPG_QUALITY) or GC.JPG_QUALITY) or GC.JPG_QUALITY)
    im.save(b, "JPEG", quality=_q, optimize=True, progressive=True, subsampling=0)
    return base64.b64encode(b.getvalue()).decode()


TITLE_H = 132               # 名称带高度（逻辑像素），仅独立高清图使用
# 图例塞进标题带时带子要加高：132 px 只够把 3 行图例顶到数据行鼻子上
# （2026-10-23 用户实测"融在一起了"）。加高 16 px 只影响用 legend_place="title" 的线，
# 其他模式的画布高一个字都不变。地图本身大小不变，只是它在画布里的 y 起点下移。
TITLE_H_LEGEND = 158
# ⚠ 上面两个值是「内容自然高度」，**不再是最终带高**：2026-11-08 起带高受
#   `CFG["band_max_ratio"]`（默认 0.10）硬约束 —— 矮画幅（IH 小）时带子会等比缩字号，
#   保证 `带高/(地图高+带高) ≤ r`。见 `_title_scale()`。

# ---- 标题带 / 图例带配色 ----
# 原来用暖纸色 #F6F0E3，和底图（冷调植被带，采样中位 (125,139,109)）放一起发闷、发"脏"。
# 现在取**同色相、高明度、低饱和**的一档（G 略高于 R、B 最低），等于把地形色提亮压灰，
# 带上带下是一套色。文字也跟着换：小字从 #8A7A66（对旧底只有 3.66:1，本来就不达标）
# 换成 #5F6B60（对 #E9EEE7 是 4.75:1）。
TITLE_BG = "#E9EEE7"
TITLE_LINE = "#C6D0C4"
TITLE_INK_MUTED = "#5F6B60"


def _use_band(with_title):
    """独立高清图下要不要加通栏图例带。

    ⚠ 2026-11-02 修：`out_size()` 与 `render_svg()` 原先只认旧的 `legend_outside`，
    于是显式写 `CFG["legend_place"]="band"`（SKILL.md 三种放法之一）时
    **图面内图例被关掉、图下方又不画带** → 整幅图一个图例都没有，且不报错。
    现在统一走 `_legend_place()`：显式 "band" 与旧 `legend_outside=True` 等价。
    """
    return bool(with_title) and _legend_place() == "band"


def out_size(with_title=False):
    """输出逻辑尺寸。带名称带时高度加 TITLE_H —— render_map_hi 要按它算画布高。

    `legend_place="band"`（或旧的 `legend_outside`）打开时，独立高清图下方还要再加
    一条图例带（高度由 `_legend_band_geom()` 算）。⚠ 画布高与 viewBox 高必须同源，
    否则会被 letterbox。
    """
    h = IH + (_title_h() if with_title else 0)
    if _use_band(with_title):
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


def _title_legend(x_right, y_bottom, outer_k=1.0):
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

    # ---- 分日格子：附加行（雪线 / 步道）自成一行，不参与列宽 ----
    # ⚠ 早先把附加行也塞进同一网格：它们的描述特别长（"OSM 测绘的既有步道 / 牧道 /
    #   机耕路（非本次轨迹）"），会把整列撑宽 —— 同列的 D1/D4 就跟里程数字拉开一大截空。
    days = list(day_rows)
    ext_w = 0.0
    for i, r in enumerate(extra_rows):
        ext_w += entry_w(r) + (GAP_COL * 2 if i else 0)

    # ---- 可用宽 / 可用高（列数枚举要用，先算） ----
    x_left = 40
    t = _CFG.get("title") or _CFG.get("file_stem") or ""
    sub = _CFG.get("title_lite") or ""
    # 标题块宽度要按**缩后**的字号量（outer_k 是带高被 band_max_ratio 压出来的系数）。
    # ⚠ badge 不参与让宽：它基线在 y≈0.18×带高（≤28），图例顶边最低也在
    #   y_bottom - 4×26 ≈ 46，纵向天然错开。曾把它算进来（27 字×15 px）把 avail
    #   从 420 压到 309、k 从 0.66 掉到 0.58，纯属自伤（2026-11-18 实测回退）。
    _right = x_left + max(GC.text_w(t, 44 * outer_k) + 2, GC.text_w(sub, 16 * outer_k))
    avail = x_right - (_right + 26)
    # ⚠ 标题带图例模式下**不画数据行**（_title_band 里明确跳过），旧写法却仍扣
    #   `68 * outer_k`（数据行基线 52 + 下缘 16），白扔掉六成带高 → k 被压到下限、
    #   图钉糊成小点（2026-11-18 缙磐四尖实测：k=0.45、块高只剩 23 px）。
    #   现在只留 3 px 给带下沿横线（脚注在左下，与右下角图例横向不重叠）。
    # ⚠ 顶边硬下限：badge 那行（"全线地图 · …"）基线在 y≈0.18×带高、字号 15×outer_k，
    #   图例顶边必须降到它下面，否则符号会压在"顺时针环线"上（2026-11-18 实测：
    #   去掉 badge 让宽后枚举选了"符号 1 列 8 行"，顶边 y=3 正好撞 badge）。
    #   注意这是**顶边**约束、不是把 badge 算进宽度：后者把 avail 从 432 压到 309。
    _top_floor = 0.18 * _title_h() + 15 * outer_k + 4 * outer_k
    avail_h = max(1.0, y_bottom - _top_floor)

    # ---- 符号槽位：按图钉实际直径算（与 mk_pin(k=_vk()) 同尺寸） ----
    _glyph = _pin_d()                     # = PIN_D × _vk()
    _txt_dx = _glyph / 2.0 + 6.0
    sym_cell = max([_txt_dx + GC.text_w(nm, FS) for _k, nm in syms] or [0]) + 14

    # ---- 动态枚举列数：联合优化「分日列数 × 符号列数」，取缩放系数 k 最大的 ----
    # 旧写法只按"空格少/行少"选分日列数，符号列数再跟着行数反推，完全不看宽度：
    # 近方画幅（IW=846）下符号 4 列吃 404 px、分日 2 列吃 767 px，total=1204
    # 对 avail≈530 → k 触底 0.45，图钉出图只剩 11 px（图面是 25 px）。
    # 现在让"少列换高度"参与竞争，同时每块顶边必须 ≥ _top_floor（避开 badge）。
    # 两轮：第一轮带顶边约束；极端窄带无解时第二轮放宽（总比不画图例强）。
    best = None
    for _floor in (_top_floor, 0.0):
        _ah = max(1.0, y_bottom - _floor)
        for _nc in range(1, 5):
            if days and _nc > len(days):
                break
            _cols = [[] for _ in range(_nc)]
            for i, r in enumerate(days):
                _cols[i % _nc].append(r)                   # 列优先：横读 D1 → D2 → D3
            _cw = [max([entry_w(r) for r in c] or [0]) for c in _cols]
            _gw = max(sum(_cw) + GAP_COL * (_nc - 1), ext_w)
            _dr = -(-len(days) // _nc) if days else 0
            for _sc in range(1, 5):
                if syms and _sc > len(syms):
                    break
                _nr = max(_dr + (1 if extra_rows else 0),
                          -(-len(syms) // _sc) if syms else 0)
                _tt = sym_cell * _sc + GAP_BLOCK + _gw
                _hh = _nr * PITCH
                _kk = min(1.0, avail / _tt, _ah / _hh) if _tt and _hh else 1.0
                if y_bottom - _hh * _kk < _floor:
                    continue
                _score = (_kk, -_hh)                        # ①字号尽量大 ②同号取更矮
                if best is None or _score > best[0]:
                    best = (_score, dict(ncol=_nc, scol=_sc, cols=_cols, col_w=_cw,
                                         nrow=_dr, nrow_all=_nr, sym_w=sym_cell * _sc,
                                         grid_w=_gw, total=_tt, h=_hh, k=_kk))
        if best is not None:
            break
    _L = best[1]
    ncol, sym_cols = _L["ncol"], _L["scol"]
    cols, col_w = _L["cols"], _L["col_w"]
    nrow, nrow_all = _L["nrow"], _L["nrow_all"]
    sym_w, grid_w, total, h = _L["sym_w"], _L["grid_w"], _L["total"], _L["h"]
    sym_rows = -(-len(syms) // sym_cols) if syms else 0
    # 下限 0.45（2026-11-08）：极端长标题 / 超多天时仍要塞进带子，不能溢出。
    k = max(0.45, _L["k"])
    # 符号对外层 k 反向补偿：外层 scale(k) 之后，图例图钉直径回到 _pin_d()，
    # 与图面逐像素同尺寸（2026-11-18 用户定案）——"放不下"缩的是整块版式，
    # 不是让符号单独缩成点。槽位仍按 _glyph（=图面尺寸）算，二者在缩放后一致。
    _sym_k = _vk() / k

    # 本地坐标原点 = 块左上角；整块 translate 到右下角落点再 scale
    ox = x_right - total * k
    oy = y_bottom - h * k
    o = [f'<g transform="translate({ox:.1f},{oy:.1f}) scale({k:.3f})">']

    for i, (kind, nm) in enumerate(syms):
        cx = (i % sym_cols) * sym_cell + _glyph / 2.0 + 4
        cy = (nrow_all - sym_rows + i // sym_cols) * PITCH + 15
        # 2026-11-18 用户：图例符号必须**和图上符号同尺寸、同样式**。
        # 旧写法 `scale(0.52)` + `k=1.0`：符号被缩到 0.52 倍、且 k 不随画幅走，
        # 于是大画幅上地图里是 76 px 的图钉、图例里只剩一个 27 px 的色点（用户截图）。
        # 现在 `k=_sym_k = _vk()/外层k`：外层 scale(k) 之后正好回到 _vk()，
        # 图例符号与图面符号逐像素同尺寸（外层缩的是整块版式，不单独缩符号）。
        o.append(f'<g transform="translate({cx:.1f},{cy:.1f})">{mk_pin(0, 0, kind, k=_sym_k)}</g>')
        o.append(f'<text x="{cx + _txt_dx:.1f}" y="{cy + 4:.1f}" font-size="{FS:.1f}" '
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


def _title_h_base():
    """标题带的**内容自然高度**：图例搬进带子里时要更高一点（见 TITLE_H_LEGEND）。"""
    return TITLE_H_LEGEND if _legend_place() == "title" else TITLE_H


def _band_max_ratio():
    """标题带占高上限 r：`带高 / (地图高 + 带高) ≤ r`。

    `CFG["band_max_ratio"]`，默认 **0.10**（2026-11-08 用户定：海报上标题带不许吃超过
    一成面积）。设 0 或负数 = 关掉约束、回到固定的 132/158 px。
    """
    try:
        r = float(GC.cfg("band_max_ratio", 0.10) or 0.0)
    except (TypeError, ValueError):
        r = 0.10
    return min(max(r, 0.0), 0.45)


def _title_scale():
    """标题带的等比缩放系数 k（字号 × 纵向节奏 × k）。

    为什么要缩：带高是按**内容**定的（三行字 15/44/16 + 右侧统计），一旦画幅偏矮
    （IH 小，比如竖幅 900 px），132 px 的带子就占 12.8% —— 与「≤10%」冲突。
    两条路可选：①压字号 ②改布局（把统计挪进带内其他地方）。三行字已经是最紧凑布局，
    没有可挪的，所以走 ①：整块等比缩（字**不发生形变**，是缩字号，不是纵向压扁）。
    判据：`_title_h()/(IH + _title_h()) ≤ r + 1e-6`。
    """
    r = _band_max_ratio()
    base = _title_h_base()
    if r <= 0 or IH <= 0 or base <= 0:
        return 1.0
    cap = IH * r / (1.0 - r)          # 带高上限（逻辑 px）
    if base <= cap:
        return 1.0
    k = cap / base
    if k < 0.55:
        print(f"   ⚠ 标题带被压到 {k:.2f}×（画幅 IH={IH}，上限 {r:.0%}）"
              f"—— 字会偏小，考虑加高画幅或改用 legend_place=\"band\"")
    return k


def _title_h():
    """名称带**最终**高度（逻辑 px）：已按 `CFG["band_max_ratio"]` 压过。

    ⚠ `out_size()` 与 `render_map_hi()` 都取自这里 —— 只要这一处一致，
    画布高与 viewBox 高就不会分叉（分叉会被 letterbox）。
    """
    return max(1, int(round(_title_h_base() * _title_scale())))


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
    _k = _title_scale()          # 带高被 band_max_ratio 压过时 <1：字号与纵向节奏一起缩
    # 三行文字的纵向节奏（2026-10-23 用户要求"最美观 + 动态随带高"）：
    #   badge(15px) → title(44px) → subtitle(16px)
    #   视觉净空比 = badge→title : title→subtitle ≈ 1.55 : 1（疏→密，标题组更紧凑）
    #   基线按 H 的 0.18 / 0.58 / 0.84 锚定，字号不缩放、间隙等比。
    #   H=158 → y = 28/92/133（净空 27/18）；H=132 → y = 24/77/111（净空 16/11）
    _y_badge = round(_th * 0.18)
    _y_title = round(_th * 0.58)
    _y_sub   = round(_th * 0.84)
    o = [f'<rect x="0" y="0" width="{IW}" height="{_th}" fill="{TITLE_BG}"/>',
         f'<text x="{x0}" y="{_y_badge}" font-size="{15 * _k:.1f}" font-weight="700" fill="{TITLE_INK_MUTED}" '
         f'letter-spacing="{2.5 * _k:.2f}">全线地图 · {tag}</text>',
         f'<text x="{x0 - 2}" y="{_y_title}" font-size="{44 * _k:.1f}" font-weight="900" fill="#1F2430" '
         f'letter-spacing="{1 * _k:.2f}">{_esc(t)}</text>']
    if sub:
        o.append(f'<text x="{x0}" y="{_y_sub}" font-size="{16 * _k:.1f}" font-weight="600" fill="#55605A">'
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
    # ⚠ 图例搬进标题带时**不再画数据行**（2026-11-16 麦理浩径实测）：带高被
    #   band_max_ratio=0.10 压到 56 逻辑 px 时，图例右下角贴边排布，右对齐的数据行
    #   正好压在图例 D4/D5 两行上（"105.9 km · +5027…" 和「昂平 → 狮子山」摞在一起）。
    #   数字改由 title_lite 承担（作者在 route_def 里写进副标题），版面只留图例。
    if _stats and _legend_place() != "title":
        o.append(f'<text x="{IW - 40}" y="{52 * _k:.1f}" text-anchor="end" font-size="{17 * _k:.1f}" '
                 f'font-weight="700" fill="#4A5261">{_esc(_stats)}</text>')
    # 图例放标题带时整块搬进来（右缘贴数据行、底缘贴带子下沿）；否则只留分日色条
    if _legend_place() == "title":
        # 底缘留 8 px（带子下沿之上）—— 用户口径：默认 8 px，再少就顶到边了
        o.append(_title_legend(IW - 40, _th - 8, _k))
        # 2026-11-10：把图例从独立图例带搬进标题带后，`legend_foot` 里的
        #   © OpenStreetMap contributors (ODbL) 与作者署名就没有落点了。
        #   标题带是唯一的「图外」区域，在左下角补一行小字；随 _title_scale()
        #   一起缩，缩略图里不抢眼、1:1 下仍可读。其它两种放法脚注仍在
        #   图例卡 / 图例带里，这里不重复画。
        _foot = GC.author_legend(_CFG.get("legend_foot", "仅供参考 · 非导航用途"))
        if _foot:
            o.append(f'<text x="{x0}" y="{_th - 4 * _k:.1f}" '
                     f'font-size="{9 * _k:.1f}" font-weight="600" '
                     f'fill="{TITLE_INK_MUTED}">{_esc(_foot)}</text>')
    # 分日色条：与图例同色，先给读者一个"这是几天线"的暗示
    days = _CFG.get("days", [])
    if days and _legend_place() != "title":
        bx = IW - 40 - len(days) * 34
        for i, d in enumerate(days):
            o.append(f'<rect x="{bx + i * 34:.0f}" y="{96 * _k:.1f}" width="28" height="{6 * _k:.1f}" rx="{3 * _k:.1f}" '
                     f'fill="{d.get("color", "#888")}"/>'
                     f'<text x="{bx + i * 34 + 14:.0f}" y="{116 * _k:.1f}" text-anchor="middle" '
                     f'font-size="{10 * _k:.1f}" font-weight="700" fill="{TITLE_INK_MUTED}">{d.get("tag", "")}</text>')
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
    if not _CFG.get("days"):
        return [], rows          # 无分日线路：只有符号 / 附加行
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
         f'{_esc(_CFG.get("legend_foot", "仅供参考 · 非导航用途"))}</text>']

    for i, (kind, col, tag, text, km, glyph) in enumerate(items):
        cx = x0 + (i % cols) * cw
        ry = BD_PADT + BD_ROW0 + BD_PITCH * (i // cols)
        if kind == "sym":
            # 符号与图面**同尺寸**（2026-11-18）：走 `k=_vk()`，不再固定 k=1.0。
            # 说明：旧注释说 0.42 会让符号只剩小点，现在统一以图面尺寸为准，
            #       `pin_d` 小于 8 时 mk_pin 本就退化成「白环 + 实心点」，图面与图例一致。
            _glyph_b = _pin_d()
            o.append(f'<g transform="translate({cx + _glyph_b / 2 + 2:.1f},{ry - 5:.1f})">'
                     f'{mk_pin(0, 0, glyph, k=_vk())}</g>')
            o.append(f'<text x="{cx + _glyph_b + 10:.0f}" y="{ry}" font-size="13.5" font-weight="700" '
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



def build_svg(mobile=False, embed_image=True, embed_max=EMBED_MAX, with_title=False,
              visual=None):
    """返回 (svg 字符串, 自检信息)。embed_max=0 表示不降采样（高清地图用）。

    with_title=True：在整幅图上方加一条名称带（独立高清图用，HTML 内嵌版不用）。
    visual：高清图专用的视觉倍率 dict {ss, k, halo_out_px}；不传 = 旧口径（k=1）。
    """
    global _VIS
    # 每轮重置：同一进程里先出高清图、再出 HTML 内嵌图时，不能把 k 带过去。
    _VIS = {"ss": 1.0, "k": 1.0, "halo_out_px": None}
    if visual:
        _VIS.update({kk: vv for kk, vv in visual.items() if vv is not None})
    overlay, placed = build_map_overlay(mobile)
    img = ""
    if embed_image:
        img = (f'<image x="0" y="0" width="{IW}" height="{IH}" '
               f'href="data:image/jpeg;base64,{_embed_jpeg(embed_max)}"/>')
    cls = "mapsvg map-p" if mobile else "mapsvg map-d"
    band = _title_h() if with_title else 0
    lb = _legend_band_geom()["h"] if _use_band(with_title) else 0
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

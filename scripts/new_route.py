# -*- coding: utf-8 -*-
"""从 out/ 的轨迹产物自动生成 route_def.py 骨架 —— 省掉「手工抄数字」这一步。

为什么要有它：`route_def.py` 的**机械部分**（总里程 / 爬升 / 海拔范围 / 分段建议 /
POI 候选 / 剖面标注 / 时间表）本来就能从 `prep_track.py` 的输出直接算出来，
让人手抄既慢又容易抄错，还违背"自助生成"的初衷。本脚本把这一层自动化，
只把**真正需要人判断的东西**（地名核实、文案、住宿水源、装备提示）留成待办标记。

输入（都要先跑 `python prep_track.py <你的.kml|.gpx>` 产生）：
    out/track_real.json   简化轨迹 + 里程/爬升/海拔范围
    out/track_full.json   全量原始点（算 POI 里程与时间表用）
    out/kml_pois.json     KML/GPX 里的具名标注（POI 候选）

输出：`scripts/route_def.py`（若已存在，先备份成 `route_def.py.bak`）

用法：
    python new_route.py                          # 用目录名当线路名
    python new_route.py --name 峨眉山全景大环线 --days 2
    python new_route.py --kml 峨眉山.kml          # 把 KML 文件名登记进 CFG
    python new_route.py --stdout                 # 只打印，不落盘（先看看再决定）
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TODO = "★ TODO"          # 待办标记：生成的文件里靠它计数

# 分日配色（够用到 4 天；更多天会在末尾循环）。第二项是浅底上的深色文字色。
DAY_PALETTE = [("#E4572E", "#B23A1C"), ("#1E9E76", "#0F6E5C"),
               ("#6C4FD8", "#4A33A8"), ("#2D7FF9", "#1B5FB8")]

# POI 类型推断：按名字里的关键词猜 pin 图形（只影响图标，猜错也不致命）
_KIND_RULES = [
    (("垭口", "达坂", "隘口", "山垭", "丫口"), "warn"),
    (("峰", "顶", "尖", "雪山", "神山", "主峰"), "peak"),
    (("寺", "庙", "庵", "观", "殿", "宫", "教堂", "佛"), "temple"),
    (("营地", "扎营", "宿营", "牧场", "牛棚", "窝", "棚"), "camp"),
    (("海子", "湖", "错", "泊", "泉", "井", "溪", "河", "水", "桥"), "water"),
]
_MAX_POIS = 16            # 地图上超过这个数就开始糊；超了就均匀抽样
_JUNK_NAME = re.compile(r"^[\d\s.·、,#-]+$")     # 「3550」「12」这类随手标注
# KML 注记里混着**导航说明**（「继续沿步道原路下山」「注意左转」），它们是给走路的人看的，
# 不是地名，画到图上就是噪声 —— 按动词/方位词剔掉。
_NARRATIVE = re.compile(r"(沿|继续|原路|下山|上山|前往|注意|左转|右转|方向|步行|到达|经过|"
                        r"此处|建议|然后|之后|往回|返回|行走|走完|可以|不要|请|需|往上|往下)")
_NAME_PAREN = re.compile(r"[（(][^）)]*[）)]")
_MAX_NAME = 11            # 中文地名基本 ≤ 8 字；再长多半是句子

# 标注偏移的「扇形」预设：**必须交替使用**，全给同一个 (dx, dy, anchor)
# 会让一列标注叠在同一条线上 —— 实测 21 个点位在手机版地图上撞出 4 处重叠。
# 四个方位轮着来，撞车概率立刻降一个量级。
_LABEL_FAN = [(28, -10, "start"), (-28, -10, "end"),
              (28, 24, "start"), (-28, 24, "end")]


def _q(s):
    """把字符串安全嵌进生成的源码（中文全角引号无所谓，ASCII 双引号会截断字符串）。"""
    return str(s).replace("\\", "／").replace('"', "＂").replace("\n", " ").strip()


# ------------------------------------------------------------------ 读输入
def _load(name):
    f = OUT / name
    if not f.exists():
        sys.exit(f"缺 {f}\n  → 先跑：python prep_track.py <你的.kml|.gpx>")
    return json.loads(f.read_text(encoding="utf-8"))


def _hav_km(lon1, lat1, lon2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def km_of(full_pts, cum_m, lon, lat):
    """某个坐标在轨迹上最近的**累计里程**（km）与偏离距离（km）。"""
    best_i, best_d = 0, 1e18
    for i, p in enumerate(full_pts):
        d = _hav_km(lon, lat, p[0], p[1])
        if d < best_d:
            best_i, best_d = i, d
    return cum_m[min(best_i, len(cum_m) - 1)] / 1000.0, best_d


def ele_at(real_pts, km):
    """按里程取简化轨迹的平滑海拔。"""
    for p in real_pts:
        if p["km"] >= km:
            return p["ele"]
    return real_pts[-1]["ele"] if real_pts else 0.0


def time_at(full_pts, cum_m, km):
    """按里程取轨迹时间戳的 HH:MM（没有时间戳就返回 '—'）。"""
    tgt = km * 1000.0
    for i, c in enumerate(cum_m):
        if c >= tgt:
            s = ((full_pts[min(i, len(full_pts) - 1)][3] or "") if full_pts else "").strip()
            return s[:5] if s else "—"
    s = ((full_pts[-1][3] or "") if full_pts else "").strip()
    return s[:5] if s else "—"


def guess_kind(name, idx, n, ele, max_ele):
    if idx == 0:
        return "start"
    if idx == n - 1:
        return "end"
    for kws, k in _KIND_RULES:
        if any(w in name for w in kws):
            return "peak_hi" if (k == "peak" and ele is not None
                                 and ele >= max_ele - 1) else k
    return "water"       # 兜底图形：至少看得见（map_svg 对未知 kind 不画任何东西）


def pick_pois(kml_pois, full_pts, cum_m, real_pts):
    """把 KML 标注清洗成 POI 候选：去括注 → 剔导航说明 → 去重 → 挂里程 → 抽样。"""
    rows, seen = [], set()
    for p in kml_pois:
        name = re.sub(r"\s+", " ", (p.get("name") or "")).strip()
        # 「雷洞坪（观光车终点）」→「雷洞坪」：括注是说明，不是名字的一部分
        name = _NAME_PAREN.sub("", name).strip(" ·-—")
        if (not name or len(name) < 2 or len(name) > _MAX_NAME
                or _JUNK_NAME.match(name) or _NARRATIVE.search(name) or name in seen):
            continue
        seen.add(name)
        km, off = km_of(full_pts, cum_m, p["lon"], p["lat"])
        if off > 1.5:                    # 离轨迹 1.5 km 以上的多半不是本线点位
            continue
        ele = p.get("ele")
        if ele is None:
            ele = ele_at(real_pts, km)
        rows.append({"name": name, "km": round(km, 2), "lon": p["lon"], "lat": p["lat"],
                     "ele": int(round(ele)) if ele is not None else None,
                     "desc": (p.get("desc") or "").strip()})
    rows.sort(key=lambda r: r["km"])
    if len(rows) > _MAX_POIS:            # 抽样，但必保 首 / 尾 / 最高
        keep = {0, len(rows) - 1,
                max(range(len(rows)), key=lambda i: rows[i]["ele"] or -1)}
        step = len(rows) / _MAX_POIS
        keep |= {int(i * step) for i in range(_MAX_POIS)}
        rows = [rows[i] for i in sorted(keep)]
    max_ele = max([r["ele"] for r in rows if r["ele"] is not None] or [0])
    for i, r in enumerate(rows):
        r["kind"] = guess_kind(r["name"], i, len(rows), r["ele"], max_ele)
    return rows


def day_stats(real_pts, total_km, bounds, thr=3.0):
    """按分段界算每一天的 +升/−降/最高/首末时间。口径与 prep 一致：3 m 阈值。"""
    edges = [0.0] + list(bounds) + [total_km + 1e-6]
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = [p for p in real_pts if a <= p["km"] < b]
        if len(seg) < 2:
            seg = [p for p in real_pts if abs(p["km"] - a) < 1e-6] or seg
        up = dn = 0.0
        for p, q in zip(seg, seg[1:]):
            d = q["ele"] - p["ele"]
            if d >= thr:
                up += d
            elif d <= -thr:
                dn += -d
        out.append({"km": round(b - a, 2), "up": int(round(up)), "dn": int(round(dn)),
                    "top": int(round(max((p["ele"] for p in seg), default=0))),
                    "t0": ((seg[0]["t"] or "").strip()[:5] or "—") if seg else "—",
                    "t1": ((seg[-1]["t"] or "").strip()[:5] or "—") if seg else "—"})
    return out


def suggest_split(total_km, pois, n_days):
    """分段建议：取等分点里程最近的 POI（通常就是垭口/营地/村），偏离太远就按里程等分。"""
    if n_days <= 1:
        return []
    out = []
    for k in range(1, n_days):
        want = total_km / n_days * k
        best = min(pois, key=lambda r: abs(r["km"] - want)) if pois else None
        if best and abs(best["km"] - want) <= max(2.0, total_km * 0.12):
            out.append((round(best["km"], 2), f"最近的点位是「{_q(best['name'])}」"))
        else:
            out.append((round(want, 2), "按里程等分"))
    return out


def build_src(kml_name, stem, full_n, real_n, split_notes):
    """自动写「数据说明」—— 这几段里全是能算出来的事实，不该让人抄。

    注意：这里双写花括号（`{{TOTAL_KM}}`）是为了在**生成的文件**里留下
    `{TOTAL_KM}` —— 那是 route_def 自己的 f-string，在生成时不该被展开。
    """
    split_txt = "、".join(f"{km} km（{why}）" for km, why in split_notes) or "无"
    return f'''    "src": [
        ("数据说明", [
            f"<span class='k'>轨迹</span>：{_q(kml_name)}，原始 {full_n} 点 → 简化 {real_n} 点，WGS84。"
            f"里程为原始点串 haversine 累计 <b>{{TOTAL_KM}} km</b>；海拔经 11 点滑动平均，"
            f"累计升降按 3 m 阈值滤噪，得 <b>+{{ASC}} / −{{DESC}} m</b>。",
            "<span class='k'>海拔口径</span>：曲线用轨迹 GPS 实测（系统性偏低 20–50 m）；"
            "印出来的海拔用 DEM 高程，与公开资料一致。",
            f"<span class='k'>分日</span>：{split_txt}。",
        ]),
        ("底图与轨迹来源", [
            "<span class='k'>底图</span>：本地自渲染 —— 地形 DEM（Terrarium / SRTM-Copernicus，"
            "AWS Open Data，z15）晕渲 + 等高线，叠加 OpenStreetMap 矢量"
            "（© OpenStreetMap contributors，ODbL）。非在线瓦片服务，无速率限制与版权风险。",
            "<span class='k'>轨迹</span>：用户提供实走记录，非拟合走向。",
            "<span class='k'>声明</span>：本图<b>不可作为导航使用</b>。导航请用本目录下的 "
            f"<b>{_q(stem)}.gpx</b>。所有数值以现场为准。",
        ]),
    ],'''


# 生成的骨架。@@TOKEN@@ 由 gen() 用 .replace 填 —— 比 f-string 模板可读，
# 且不用为生成代码里的每个花括号写 {{ }} 转义。
TEMPLATE = '''# -*- coding: utf-8 -*-
"""@@NAME@@ —— 由 `new_route.py` 自动生成的骨架（生成于 @@TS@@）。

**文件里所有 ★ 标记处都要人工按线路改写**：那些是真正需要人判断的东西
（地名核实、住宿与水源、装备提示、文案）。机械数字——里程 / 爬升 / 海拔范围 /
分段建议 / POI 候选 / 剖面标注 / 时间表——已经算好，不用抄。

改完按这个顺序重跑（改一处要问"下游还有谁用这个值"）：
    python fetch_osm.py --soft → make_terrain.py → build_guide.py
                               → shoot_guide.py → render_map_hi.py → make_gpx.py → qa_guide.py
或者一条命令：python make_all.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find(name):
    for p in (HERE / "out" / name, HERE / name, HERE / "kml" / name):
        if p.exists():
            return p
    raise FileNotFoundError(f"找不到 {name} —— 先跑 parse_track_kml.py / prep_kml_track.py")


# ============================================================ 一、数字区（自动填好）
# 宽容加载：全新项目里 out/track_real.json 还不存在时，本文件也必须能被 import ——
# 否则 prep_track.py 连 CFG["kml"] 都读不到，会卡在"要先有轨迹才能导入配置"的死循环里。
TRACK_READY = True
try:
    _tr = json.loads(_find("track_real.json").read_text(encoding="utf-8"))
except FileNotFoundError:
    _tr = {"pts": [], "total_km": 0, "asc": 0, "desc": 0, "ele_min": 0, "ele_max": 0}
    TRACK_READY = False
_pts = _tr["pts"]

TRACK = [(p["lon"], p["lat"]) for p in _pts]
TRACK_KM = [p["km"] for p in _pts]
TRACK_ELE = [p["ele"] for p in _pts]
TRACK_T = [p["t"] for p in _pts]
TOTAL_KM = _tr["total_km"]              # 原始点串 haversine 累计
ASC, DESC = _tr["asc"], _tr["desc"]     # 11 点平滑 + 3 m 阈值
ELE_MIN, ELE_MAX = _tr["ele_min"], _tr["ele_max"]

@@SPLIT_BLOCK@@


def _idx_at(km):
    for i, k in enumerate(TRACK_KM):
        if k >= km:
            return i
    return len(TRACK_KM) - 1


SPLIT1_I = _idx_at(SPLIT1_KM)


def fmt_ele(e):
    """海拔显示：整数不带小数，非整数保留一位。"""
    f = float(e)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


def at(km):
    """按里程取轨迹上的经纬度与平滑海拔（POI 一律钉在真实轨迹上，别手填坐标）。"""
    if not TRACK:
        return 0.0, 0.0, 0.0
    i = _idx_at(km)
    return TRACK[i][0], TRACK[i][1], TRACK_ELE[i]


def build_route():
    return {"pts": TRACK, "km": TRACK_KM, "ele": TRACK_ELE, "t": TRACK_T,
            "split1": SPLIT1_I, "total_km": TOTAL_KM}


def _p(km, name, ele, kind, dx, dy, anc):
    lon, lat, _ = at(km)
    return (name, round(lon, 6), round(lat, 6), ele, kind, dx, dy, anc)


# ============================================================ 二、点位与标注
# ele 一律填 **DEM 高程**（与公开资料一致），不要填轨迹 GPS 值 ——
# 两套口径混用，页面上同一座山会出现两个海拔数字。
# POI 也支持字典写法，例如：
#     {"name": "金顶", "lon": 103.336, "lat": 29.519, "ele": 3079, "kind": "peak_hi"}
# 两种写法由 guide_common.poi() 统一归一化，下游一视同仁。
POIS = [
@@POIS@@
]

# 手机版短名：手机下图被缩到 ~0.25 倍，带后缀的长标签放不下也读不清
SHORT = {
@@SHORT@@
}

# 剖面标注：(km, 海拔, 文案, 是否高亮, 标签纵向偏移（负=曲线上方）, 手机版是否保留)
MARKS = [
@@MARKS@@
]

BOT_LBL = [
@@BOT_LBL@@
]

# 关键点时间表：(名称, km, 海拔, 时刻, 说明)。时刻从轨迹时间戳取，别按里程估。
SCHEDULE = [
@@SCHEDULE@@
]

# 剖面右题用；放在 POIS 之后计算，别提前引用
_TOP = max(POIS, key=lambda p: p[3] or -1) if POIS else None
TOP_NAME = _TOP[0].split(" · ")[0] if _TOP else "最高点"


# ============================================================ 三、CFG（文案与配色）
# 规则：这里的每一项都会直接进页面。带 ★ 的必须改写 ——
#      留空**不报错**，只是页面上出现占位文字，交付前务必扫一眼。
CFG = {
    "file_stem": "@@STEM@@",                # ★ 交付物文件名前缀（HTML/长图/地图/GPX 共用）
    "kml": "@@KML@@",                       # 轨迹文件名（相对 project 根）
    # "bbox": [lon0, lat0, lon1, lat1],     # 可选：显式覆盖地图窗口。
    #                                        # 默认留空 → resolve_bbox() 从轨迹包围盒自动推（+8% 留白）
    "page_title": "@@NAME@@ · 徒步攻略",     # ★
    "title": "@@NAME@@",                    # ★
    "title_lite": "@@TODO@@ 一句话副题（几天的线 · 以什么为底）",
    "badge": "@@TODO@@ 省市 · 线路方向（如「XX 上 · YY 下」）",
    "meta": f"WGS84 · 实测轨迹口径 · {TOTAL_KM} km",
    "sub": "@@TODO@@ 一句话把沿途要点串起来（用 <em>…</em> 标出最亮的那段）",

    # ---- 分日：颜色 / 图例 / 距离标签（颜色会被地图、剖面、逐日卡、图例共用）----
    "days": [
@@DAYS@@
    ],
    "day_bounds": [@@BOUNDS@@],             # 剖面上的分日竖虚线位置（多日就多写几个）
    "legend_title": "分日路段",
    "legend_foot": "全线为实测轨迹 · 非导航用图",
    "extra_legend_tag": "步道",
    "extra_legend_desc": "OSM 测绘的既有步道 / 公路（非本次轨迹）",
    "extra_legend_color": "#8C8A86",

    # ---- 剖面 ----
    "ele_range": (@@ELE_LO@@, @@ELE_HI@@),  # ★ 上下界：给得比实测范围略宽才不顶格
    "aria_prof": "@@NAME@@全程海拔剖面",
    "aria_map": "@@NAME@@全线地图",
    "prof_right": f"最高点：{TOP_NAME} {fmt_ele(ELE_MAX)} m",
    "prof_note": ("剖面按轨迹等距重采样（每 100 m 一点）。曲线为<b>轨迹 GPS 实测</b>海拔"
                  "（11 点滑动平均，系统性偏低约 20–50 m）；曲线上的标注取 DEM（SRTM / "
                  "Copernicus 30 m）高程，与景区公认海拔一致。"),

    # ---- 地图面板 ----
    "map_tag": "路线为实测轨迹 · 非导航用图",
    "map_right": "北为上 · 比例尺见图中",
    "map_note": ("地图内图例在窄屏会自动隐藏，改用上方文字图例。想看地形细节请放大，"
                 "或另存本目录下的《@@STEM@@-全线地图.jpg》高清图。"),

    # ---- 关键点时间表 ----
    "sched_tag": "时刻取自轨迹时间戳（北京时间）",

    # ---- 逐日卡（10 元组，对应 build_guide.day_card 的签名）----
    # (序号, 日标, 路线串, 里程, 升降, 实测用时, 最高点, 水源, 住宿, 体力提示)
    "day_cards": [
@@DAY_CARDS@@
    ],

    # ---- 关键提示三栏 ----
    "notes": [
        ("进山与规矩", [
            "@@TODO@@ 门票 / 许可 / 保护区规定（写清价格与适用时段）。",
            "@@TODO@@ 是否野线、是否经过景区大门、有无明火与露营限制。",
        ]),
        ("住宿与补给", [
            "@@TODO@@ 沿线可宿营 / 住宿的位置，是否唯一。",
            "@@TODO@@ 补给点，以及必须自带的水量与食物量。",
        ]),
        ("体力、天气与装备", [
            f"累计爬升 <b>+{ASC} / −{DESC} m</b>，@@TODO@@ 折算成「相当于几个标准山」。",
            "@@TODO@@ 本线特有的天气风险（雨雾 / 暴晒 / 失温）与对应装备。",
            "@@TODO@@ 信号与下撤预案；导航请用随本攻略导出的 GPX。",
        ]),
    ],

@@SRC@@

    "foot": ("<b>安全提示：</b>@@TODO@@ 本线最要紧的风险提示。"
             "无痕山野，垃圾自行背下山。本攻略由实测轨迹与公开资料整理，不构成任何安全承诺。"),
}


def _todo_count():
    """数一下本文件里还剩几处待办标记 —— 交付前应该清零。"""
    return Path(__file__).read_text(encoding="utf-8").count("@@MARK@@")


if __name__ == "__main__":
    if not TRACK_READY:
        sys.exit("还没跑轨迹准备：python prep_track.py <你的.kml|.gpx>")
    r = build_route()
    print(f"轨迹点 {len(r['pts'])}  总里程 {r['total_km']} km")
    print(f"分界 @ {r['km'][r['split1']]:.2f} km")
    print(f"海拔 {ELE_MIN} ~ {ELE_MAX} m   爬升 {ASC} / 下降 {DESC}")
    print(f"POIS {len(POIS)} · MARKS {len(MARKS)} · SCHEDULE {len(SCHEDULE)}")
    miss = [k for k in ("file_stem", "title", "sub", "days", "day_cards", "notes", "src")
            if not CFG.get(k)]
    print("CFG 必填项缺失：", miss or "无 ✓")
    print(f"待办还剩 {_todo_count()} 处 —— 交付前请清零")
'''


def gen(args):
    real = _load("track_real.json")
    full = _load("track_full.json")
    pois_raw = _load("kml_pois.json")
    real_pts, full_pts, cum_m = real["pts"], full["pts"], full["cum_m"]
    total_km = real["total_km"]
    if not real_pts:
        sys.exit("track_real.json 里没有轨迹点 —— 先跑 prep_track.py")

    name = args.name or HERE.parent.name or "未命名线路"
    stem = args.stem or name
    kml_name = args.kml or f"{name}.kml"

    # ⚠ --kml 不给就按线路名猜文件名，猜错不会在这里炸 —— 要等到 prep_track.py
    #   报 "KML 不存在"，而那个报错看不出是"名字猜错了"。所以这里就地核对一次，
    #   并把正确的重跑命令原样打出来（省一轮来回）。
    kml_found = next((p for p in (HERE.parent / kml_name, HERE / kml_name,
                                  Path(kml_name)) if p.exists()), None)
    if kml_found is None and args.kml is None:
        print(f"  ⚠ CFG[\"kml\"] 猜的是「{kml_name}」，但项目里没找到这个文件。\n"
              f"    prep_track.py 会因此失败。真实文件名不同就重跑一次：\n"
              f"      python new_route.py --name {name} --kml <真实的.kml|.gpx>\n"
              f"    （若轨迹已在 out/ 里，也可以只改 route_def.py 里 \"kml\" 那一行）")
    elif kml_found is None:
        print(f"  ⚠ --kml 指定的「{kml_name}」在项目里找不到 —— 确认路径是否写错。")

    pois = pick_pois(pois_raw, full_pts, cum_m, real_pts)
    if not pois:
        sys.exit("kml_pois.json 里没有可用的具名标注。\n"
                 "  · 该 KML/GPX 确实没标注 → POIS 请手工补（坐标可用 at(km) 反算）\n"
                 "  · 或者解析异常 → 确认导入的是本线路的轨迹文件")

    splits = suggest_split(total_km, pois, args.days)
    bounds = [km for km, _ in splits]
    dstats = day_stats(real_pts, total_km, bounds)

    # ---- 分日里程变量 ----
    if bounds:
        edges = [0.0] + bounds
        vs = "\n".join(f"D{i + 1}_KM = round({(bounds[i] if i < len(bounds) else total_km) - edges[i]}, 2)"
                       for i in range(len(edges)))
        split_block = (f"SPLIT1_KM = {bounds[0]}          # 建议值：{splits[0][1]}\n{vs}")
    else:                                # 只分一天：不切
        split_block = (f"SPLIT1_KM = {round(total_km, 2)}      "
                       f"# 单日线，不切分（想分段就改这里）\n"
                       f"D1_KM = round(TOTAL_KM, 2)")

    # ---- 分日 CFG ----
    days_lines = []
    for i, st in enumerate(dstats):
        c, tc = DAY_PALETTE[i % len(DAY_PALETTE)]
        a = 0.0 if i == 0 else bounds[i - 1]
        b = bounds[i] if i < len(bounds) else total_km
        nm_a = next((r["name"] for r in reversed(pois) if r["km"] <= a + 0.3), "起点")
        nm_b = next((r["name"] for r in pois if r["km"] >= b - 0.3), "终点")
        days_lines.append(
            f'        {{"tag": "D{i + 1}", "color": "{c}", "text_color": "{tc}",\n'
            f'         "legend": "{TODO} {_q(nm_a)} → {_q(nm_b)}", '
            f'"dist_label": f"{{D{i + 1}_KM}} km"}},')

    # ---- 逐日卡：数字是真的，文案是待办 ----
    cards = []
    for i, st in enumerate(dstats):
        a = 0.0 if i == 0 else bounds[i - 1]
        b = bounds[i] if i < len(bounds) else total_km
        seg = [r["name"] for r in pois if a - 0.3 <= r["km"] <= b + 0.3]
        route = " → ".join(_q(s) for s in seg[:4]) if seg else "当日经过的地名串"
        if len(seg) > 4:
            route += " → …"
        cards.append(
            f'        ({i + 1}, "D{i + 1}", "{TODO} {route}",\n'
            f'         f"{{D{i + 1}_KM}} km", "+{st["up"]} / −{st["dn"]} m",\n'
            f'         "实测用时待填", "最高 {st["top"]} m",\n'
            f'         "{TODO} 水源情况", "{TODO} 住宿 / 营地",\n'
            f'         "{TODO} 当日体力提示（哪段最耗、要不要护膝）。"),')

    # ---- POIS 字面量 ----
    # 首尾固定「上方居中 / 下方居中」，中间按 _LABEL_FAN 轮流换方位
    poi_lines, fan_i = [], 0
    for r in pois:
        if r["kind"] == "start":
            dx, dy, anc = 0, -36, "middle"
        elif r["kind"] == "end":
            dx, dy, anc = 0, 34, "middle"
        else:
            dx, dy, anc = _LABEL_FAN[fan_i % len(_LABEL_FAN)]
            fan_i += 1
        ele = r["ele"] if r["ele"] is not None else "None"
        poi_lines.append(f'    _p({r["km"]:.2f}, "{_q(r["name"])}", {ele}, '
                         f'"{r["kind"]}", {dx}, {dy}, "{anc}"),')

    # ---- 手机短名：把「· 后缀」去掉就是最好的短名，别让用户手填 ----
    short_lines = []
    for r in pois:
        base = r["name"].split("·")[0].strip()
        if base and base != r["name"]:
            short_lines.append(f'    "{_q(r["name"])}": "{_q(base)}",')

    # ---- 剖面标注：除首尾外均匀挑 8 个，最高的高亮 ----
    mid = [r for r in pois if r["kind"] not in ("start", "end")]
    if len(mid) > 8:
        step = len(mid) / 8
        mid = [mid[int(i * step)] for i in range(8)]
    top = max(pois, key=lambda r: r["ele"] or -1)
    marks = []
    for j, r in enumerate(mid):
        hi = r is top
        dy = -17 if hi else (-13 if j % 2 == 0 else 21)
        marks.append(f'    ({r["km"]:.2f}, {r["ele"]}, "{_q(r["name"])} {r["ele"]}", '
                     f'{"True" if hi else "False"}, {dy}, {"True" if hi else "False"}),')

    # ---- 底部三行：首 / 分界 / 尾 ----
    first, last = pois[0], pois[-1]

    def _nearest(km):
        w = min(pois, key=lambda r: abs(r["km"] - km))
        return w if abs(w["km"] - km) <= max(1.5, total_km * 0.08) else None

    bot = [f'    ({first["km"]:.2f}, {first["ele"]}, '
           f'"{_q(first["name"])} {first["ele"]}", "start"),']
    for km in bounds:
        w = _nearest(km)
        lbl = f'{_q(w["name"])} {w["ele"]}' if w else f'km {km}'
        bot.append(f'    ({km:.2f}, {ele_at(real_pts, km):.0f}, "{lbl}", "middle"),')
    bot.append(f'    ({last["km"]:.2f}, {last["ele"]}, '
               f'"{_q(last["name"])} {last["ele"]}", "end"),')

    # ---- 时间表 ----
    has_time = any((p[3] or "").strip() for p in full_pts)
    sched = []
    for r in pois:
        t = time_at(full_pts, cum_m, r["km"]) if has_time else "—"
        sched.append(f'    ("{_q(r["name"])}", {r["km"]:.2f}, {r["ele"]}, "{t}", '
                     f'"{_q(r["desc"][:18] or "—")}"),')

    # ---- 海拔上下界：取实测与 POI 的并集，向外取整到 500 ----
    eles = [e for e in ([real["ele_min"], real["ele_max"]]
                        + [r["ele"] for r in pois]) if e is not None]
    lo, hi = (min(eles), max(eles)) if eles else (0, 1000)

    txt = (TEMPLATE
           .replace("@@MARK@@", TODO)
           .replace("@@TODO@@", TODO)
           .replace("@@TS@@", datetime.now().strftime("%Y-%m-%d %H:%M"))
           .replace("@@NAME@@", _q(name))
           .replace("@@STEM@@", _q(stem))
           .replace("@@KML@@", _q(kml_name))
           .replace("@@SPLIT_BLOCK@@", split_block)
           .replace("@@DAYS@@", "\n".join(days_lines))
           .replace("@@BOUNDS@@", ", ".join(str(b) for b in bounds))
           .replace("@@ELE_LO@@", str(int(lo // 500 * 500)))
           .replace("@@ELE_HI@@", str(int(-(-hi // 500 * 500))))
           .replace("@@DAY_CARDS@@", "\n".join(cards))
           .replace("@@SRC@@", build_src(kml_name, stem, len(full_pts), len(real_pts), splits))
           .replace("@@POIS@@", "\n".join(poi_lines))
           .replace("@@SHORT@@", "\n".join(short_lines))
           .replace("@@MARKS@@", "\n".join(marks))
           .replace("@@BOT_LBL@@", "\n".join(bot))
           .replace("@@SCHEDULE@@", "\n".join(sched)))
    txt = txt.replace("@@MARK@@", TODO)          # _todo_count 里的那个标记
    return txt, pois, splits, dstats, has_time, len(pois_raw)


def main():
    ap = argparse.ArgumentParser(description="从 out/ 产物生成 route_def.py 骨架")
    ap.add_argument("--name", help="线路名（默认取项目目录名）")
    ap.add_argument("--stem", help="交付物文件名前缀（默认同 --name）")
    ap.add_argument("--kml", help="轨迹文件名（写进 CFG['kml']，便于一条命令重跑）")
    ap.add_argument("--days", type=int, default=2, help="分几天（默认 2，只影响分段建议）")
    ap.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    ap.add_argument("--out", default=None, help="输出路径（默认 scripts/route_def.py）")
    a = ap.parse_args()

    txt, pois, splits, dstats, has_time, n_raw = gen(a)

    if a.stdout:
        print(txt)
        return
    dst = Path(a.out) if a.out else (HERE / "route_def.py")
    if dst.exists():
        bak = dst.with_suffix(".py.bak")
        bak.write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"已备份原文件 → {bak.name}")
    dst.write_text(txt, encoding="utf-8")

    print(f"→ {dst}")
    print(f"   POI {len(pois)} 个（从 {n_raw} 条原始标注清洗而来）")
    print("   分段建议：" + "、".join(f"{km} km（{why}）" for km, why in splits))
    for i, st in enumerate(dstats):
        print(f"   D{i + 1}: {st['km']} km  +{st['up']}/−{st['dn']} m  "
              f"最高 {st['top']} m  ({st['t0']}–{st['t1']})")
    if not has_time:
        print("   ⚠ 轨迹没有 <when> 时间戳 → SCHEDULE 里时刻写的是「—」，"
              "请手工标「约 N h」并注明是估算")
    print(f"   待办 {txt.count(TODO)} 处 —— 交付前清零（页面上会显示占位文字）")


if __name__ == "__main__":
    main()

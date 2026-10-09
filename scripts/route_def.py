# -*- coding: utf-8 -*-
"""★★★ 每做一条新线路，**只需要改这一个文件** ★★★

其余脚本（build_guide / shoot_guide / qa_guide / render_map_hi / make_gpx）都是通用的，
不认具体线路名，全部从这里读。所以本文件里的任何改动都会自动流到四件交付物。

数据来源（自下而上，别跳步）：
    1. parse_track_kml.py <你的.kml>   → out/track_full.json + out/kml_pois.json
    2. prep_kml_track.py               → out/track_real.json + out/profile_real.json
    3. **本文件**：把第 2 步打印的「原始里程 / 爬升 / 下降 / 海拔范围」填进下面的数字区，
       再把 POIS / SCHEDULE / CFG 的文案按线路写实
    4. fetch_osm.py → make_terrain.py → build_guide.py → shoot_guide.py
                                      → render_map_hi.py → make_gpx.py → qa_guide.py

改完任何一处，记得按 SKILL.md「改一处要问下游还有谁用这个值」的顺序重跑。
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


# ============================================================ 一、数字区（从工具输出抄）
# 宽容加载：**全新项目**里 out/track_real.json 还不存在时，本文件也必须能被 import ——
# 否则 prep_track.py 连 CFG["kml"] 都读不到，会卡在"要先有轨迹才能导入配置"的死循环里。
# 真正依赖轨迹的脚本（build_guide / map_svg / make_gpx）会检查 TRACK_READY 后硬报错。
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

SPLIT1_KM = 15.28                       # ★ 分日界：写"为什么切在这"，别等分
D1_KM, D2_KM = SPLIT1_KM, round(TOTAL_KM - SPLIT1_KM, 2)


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
    """按里程取轨迹上的经纬度与平滑海拔（POI 一律钉在真实轨迹上，别手填坐标）。

    轨迹未就绪时返回占位坐标，好让本模块仍能被 import（见 TRACK_READY 的说明）；
    真正出图的脚本会先检查 TRACK_READY 再硬报错。
    """
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


# ============================================================ 二、CFG（文案与配色）
# 规则：这里的每一项都会直接进页面。带 ★ 的必须按线路重写，
#      空着会渲染成空串（不报错），所以交付前扫一眼页面别留空。
CFG = {
    "file_stem": "九华山南北穿越",          # ★ 交付物文件名前缀（HTML/长图/地图/GPX 共用）
    "kml": "",                              # ★ 轨迹文件名（相对 project 根）；fill 后可一条命令重跑全流程
    # "bbox": [lon0, lat0, lon1, lat1],     # 可选：显式覆盖地图窗口。**默认留空**，
    #                                        # 让 guide_common.resolve_bbox() 从轨迹包围盒自动推（+8% 留白）
    "page_title": "九华山南北穿越 · 2 天徒步攻略",
    "title": "九华山南北穿越",
    "title_lite": "（2 天 · 八峰连穿实录为底）",
    "badge": "安徽 · 九华山 &nbsp;|&nbsp; 南北穿越 · 清泉村上 · 平坦寺下",
    "meta": f"WGS84 · 实测轨迹口径 · {TOTAL_KM} km",
    "sub": ("2 天 1 晚 &nbsp;|&nbsp; <em>清泉村古道口进 · 平坦寺出</em> &nbsp;|&nbsp; "
            "一线天 → 十王峰 → 天台寺 → 打鼓岭 → 沙弥庵 → 花台正顶 → 天华峰 → 平坦寺"),

    # ---- 分日：颜色 / 图例 / 距离标签（颜色会被地图、剖面、逐日卡、图例共用）----
    "days": [
        {"tag": "D1", "color": "#E4572E", "text_color": "#B23A1C",
         "legend": "清泉村 → 打鼓岭", "dist_label": f"{D1_KM} km"},
        {"tag": "D2", "color": "#1E9E76", "text_color": "#0F6E5C",
         "legend": "打鼓岭 → 十王峰 → 平坦寺", "dist_label": f"{D2_KM} km"},
    ],
    "day_bounds": [SPLIT1_KM],              # 剖面上的分日竖虚线位置（多日就多写几个）
    "legend_title": "分日路段",
    "legend_foot": "全线为实测轨迹 · 非导航用图",
    "extra_legend_tag": "步道",
    "extra_legend_desc": "OSM 测绘的既有步道 / 公路（非本次轨迹）",
    "extra_legend_color": "#8C8A86",

    # ---- 剖面 ----
    "ele_range": (0, 1500),                 # 剖面 y 轴上下界；留空则按剖面数据自动取整
    "aria_prof": "全程海拔剖面",
    "aria_map": "九华山南北穿越全线地图",
    "prof_right": f"最高点：十王峰 {fmt_ele(ELE_MAX)} m",
    "prof_note": ("剖面按轨迹等距重采样（每 100 m 一点）。曲线为<b>轨迹 GPS 实测</b>海拔"
                  "（11 点滑动平均，系统性偏低约 20–50 m）；曲线上的标注取 DEM（SRTM / "
                  "Copernicus 30 m）高程，与景区公认海拔一致。"),

    # ---- 地图面板 ----
    "map_tag": "路线为实测轨迹 · 非导航用图",
    "map_right": "北为上 · 比例尺 2 km",
    "map_note": ("地图内图例在手机上会自动隐藏，改用上方文字图例。想看地形细节请放大，"
                 "或另存本目录下的《九华山南北穿越-全线地图.jpg》高清图。"),

    # ---- 关键点时间表 ----
    "sched_tag": "时刻取自轨迹时间戳（北京时间）· 绿底为 D2 段",

    # ---- 逐日卡（10 元组，对应 build_guide.day_card 的签名）----
    # (序号, 日标, 路线串, 里程, 升降, 实测用时, 最高点, 水源, 住宿, 体力提示)
    "day_cards": [
        (1, "D1", "清泉村 → 一线天 → 楼台山 → 七贤峰 → 十王峰 → 天台寺 → 花台正顶 → 打鼓岭",
         f"{D1_KM} km", "+1420 / −1090 m", "实测 8 h", "最高 1345.5 m",
         "沿线无稳定水源；沙弥庵有活水（D2 才经过），D1 请自带 2 L",
         "打鼓岭（两日分界，可下撤住宿）",
         "楼台山至七贤峰之间是全段最耗体力的连续爬升，<b>十王峰</b>为全程最高点，"
         "过了天台寺之后一路下到打鼓岭，注意膝盖。"),
        (2, "D2", "打鼓岭 → 团箕寨大石海 → 沙弥庵 → 千佛寺 → 天华峰 → 莲花寺 → 平坦寺",
         f"{D2_KM} km", "+980 / −2050 m", "实测 9 h 22 min", "最高 1123 m",
         "沙弥庵有活水、千佛寺有自来水；其余路段无水，自备 1.5 L",
         "打鼓岭（唯一营地）",
         "团箕寨大石海是全程最难段，乱石堆里没有明显路迹，务必跟紧轨迹；"
         "后半段连续陡降近 1400 m，建议戴护膝、用双杖。"),
    ],

    # ---- 关键提示三栏 ----
    "notes": [
        ("进山与规矩", [
            "门票 <b>190 元</b>（旺季 1/16–11/14）；淡季 140 元。",
            "本线为野线穿越，<b>不经过景区大门</b>，但仍需遵守保护区规定。",
            "核心区禁止明火与露营，垃圾全部背下山。",
        ]),
        ("住宿与补给", [
            "全线仅打鼓岭可宿营，无农家、无小卖部。",
            "所有食物与水必须一次背够 2 天用量。",
        ]),
        ("体力、天气与装备", [
            f"累计爬升 <b>+{ASC} / −{DESC} m</b>，单日强度相当于 1.5 个标准山。",
            "山区多雨雾，防滑鞋 + 冲锋衣 + 登山杖必带。",
            "全程信号时断时续，提前下载离线轨迹；导航请用随本攻略导出的 GPX。",
        ]),
    ],

    # ---- 溯源两块（用户说"不要来源"时整块删掉这两个元素即可）----
    "src": [
        ("数据说明", [
            f"<span class='k'>轨迹</span>：两步路导出 KML（gx:Track），{len(TRACK)} 个简化点，"
            f"WGS84。里程为原始点串 haversine 累计 <b>{TOTAL_KM} km</b>；海拔经 11 点滑动平均，"
            f"累计升降按 3 m 阈值滤噪，得 <b>+{ASC} / −{DESC} m</b>。",
            "<span class='k'>海拔口径</span>：曲线用轨迹 GPS 实测（系统性偏低 20–50 m）；"
            "印出来的海拔用 DEM 高程，与公开资料一致。",
            f"<span class='k'>分日</span>：以 <b>打鼓岭</b>（km {SPLIT1_KM}）为界，"
            "该点可下撤住宿，是天然的两日分割点，非等里程硬切。",
        ]),
        ("底图与轨迹来源", [
            "<span class='k'>底图</span>：本地自渲染 —— 地形 DEM（Terrarium / SRTM-Copernicus，"
            "AWS Open Data，z15）晕渲 + 等高线，叠加 OpenStreetMap 矢量"
            "（© OpenStreetMap contributors，ODbL）。非在线瓦片服务，无速率限制与版权风险。",
            "<span class='k'>轨迹</span>：用户提供实走记录，非拟合走向。",
            "<span class='k'>声明</span>：本图<b>不可作为导航使用</b>。导航请用本目录下的 "
            "<b>九华山南北穿越.gpx</b>。所有数值以现场为准。",
        ]),
    ],

    "foot": ("<b>安全提示：</b>本线部分路段无路迹、无水、无手机信号，请结伴而行并留出下撤预案。"
             "无痕山野，垃圾自行背下山。本攻略由实测轨迹与公开资料整理，不构成任何安全承诺。"),
}

# ============================================================ 三、点位与标注
# 说明：ele 一律填 **DEM 高程**（与公开资料一致），不要填轨迹 GPS 值 —— 两套口径混用，
#       页面上同一座山会出现两个海拔数字。
POIS = [
    _p(0.00,  "起点 · 清泉村古道口", 559, "start",     0,  -36, "middle"),
    _p(4.30,  "楼台山",            1048, "peak",    -26,  -10, "end"),
    _p(3.10,  "一线天",             894, "warn",     40,    8, "start"),
    _p(7.90,  "七贤峰",            1305, "peak",    -26,   -8, "end"),
    _p(9.90,  "十王峰 · 全程最高",   1346, "peak_hi", -28,   -6, "end"),
    _p(10.40, "天台寺 · 补给",      1287, "temple",   26,   10, "start"),
    _p(13.10, "花台正顶 · 补给",     1288, "temple",   26,   -8, "start"),
    _p(15.28, "打鼓岭 · 两日分界",    879, "camp",      0,   34, "middle"),
    _p(17.30, "团箕寨大石海",       1035, "warn",     26,    2, "start"),
    _p(17.70, "沙弥庵 · 活水",      1123, "water",   -28,   -6, "end"),
    _p(22.70, "天华峰",            1072, "peak",     26,   -8, "start"),
    _p(21.70, "千佛寺 · 自来水",      967, "temple",   26,   16, "start"),
    _p(27.10, "药师殿",             922, "water",   -30,   -8, "end"),
    _p(30.20, "终点 · 平坦寺",       254, "end",      30,   -6, "start"),
]

# 手机版短名：手机下图被缩到 ~0.25 倍，带后缀的长标签放不下也读不清
SHORT = {
    "起点 · 清泉村古道口": "清泉村",
    "十王峰 · 全程最高": "十王峰",
    "天台寺 · 补给": "天台寺",
    "花台正顶 · 补给": "花台正顶",
    "打鼓岭 · 两日分界": "打鼓岭",
    "沙弥庵 · 活水": "沙弥庵",
    "千佛寺 · 自来水": "千佛寺",
    "终点 · 平坦寺": "平坦寺",
}

# 剖面标注：(km, 海拔, 文案, 是否高亮, 标签纵向偏移，负=在曲线上方, 手机版是否保留)
MARKS = [
    (3.10,  894,    "一线天 894",     False, -12, False),
    (7.90,  1305,   "七贤峰 1305",    False, -13, False),
    (9.90,  1346,   "十王峰 1346",    True,  -17, True),
    (10.40, 1287,   "天台寺 1287",    False,  21, False),
    (13.10, 1288,   "花台正顶 1288",  False, -13, False),
    (17.70, 1123,   "沙弥庵 1123",    False, -13, False),
    (21.70, 967,    "千佛寺 967",     False,  21, False),
    (22.70, 1072,   "天华峰 1072",    False, -15, False),
    (27.10, 922,    "药师殿 922",     False, -13, False),
]

BOT_LBL = [
    (0.00, 559, "清泉村 559", "start"),
    (SPLIT1_KM, 879, "打鼓岭 879（可下撤）", "middle"),
    (TOTAL_KM, 254, "平坦寺 254", "end"),
]

# 关键点时间表：(名称, km, 海拔, 时刻, 说明)。时刻从轨迹时间戳取，别按里程估。
SCHEDULE = [
    ("起点 · 清泉村", 0.00, 559, "05:41", "古道口 · 起点"),
    ("一线天", 3.10, 894, "06:21", "窄段栈道"),
    ("楼台山", 4.30, 1048, "06:43", "—"),
    ("七贤峰", 7.90, 1305, "07:56", "—"),
    ("十王峰", 9.90, 1346, "08:38", "全程最高点"),
    ("天台寺", 10.40, 1287, "08:56", "可补水"),
    ("花台正顶", 13.10, 1288, "09:53", "可补水"),
    ("打鼓岭", 15.28, 879, "10:46", "两日分界 · 宿营"),
    ("沙弥庵", 17.70, 1123, "12:08", "全线唯一活水"),
    ("千佛寺", 21.70, 967, "13:48", "有自来水"),
    ("天华峰", 22.70, 1072, "14:09", "—"),
    ("药师殿", 27.10, 922, "15:56", "—"),
    ("终点 · 平坦寺", 30.20, 254, "16:37", "终点"),
]


if __name__ == "__main__":
    if not TRACK_READY:
        sys.exit("还没跑轨迹准备：python prep_track.py <你的.kml>")
    r = build_route()
    print(f"轨迹点 {len(r['pts'])}  总里程 {r['total_km']} km")
    print(f"分界 @ {r['km'][r['split1']]:.2f} km")
    print(f"D1 {D1_KM} / D2 {D2_KM} km")
    print(f"海拔 {ELE_MIN} ~ {ELE_MAX} m   爬升 {ASC} / 下降 {DESC}")
    print(f"POIS {len(POIS)} · MARKS {len(MARKS)} · SCHEDULE {len(SCHEDULE)}")
    miss = [k for k in ("file_stem", "title", "sub", "days", "day_cards", "notes", "src")
            if not CFG.get(k)]
    print("CFG 必填项缺失：", miss or "无 ✓")

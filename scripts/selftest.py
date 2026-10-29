# -*- coding: utf-8 -*-
"""离线自检：**不联网、不下载 DEM、不启浏览器**，几十秒跑完。

为什么要有这个脚本
------------------
技能里 25 个脚本，此前能独立验证的只有 `_stress_layout.py`（标注排布）和
`tile_tint --selftest`（B 路线配色）—— 而 SKILL.md 末节那套"改完技能怎么验"
是 6 步手工程序，要真跑一遍完整流水线（几十分钟 + 联网下几万块 DEM 瓦片）。
结果是：**改了共享代码，没人能在提交前快速确认没碰坏别的线路**。

本脚本专挑那种"错了不会报错、只会画错"的项目来断言：

  ① 投影一致性 —— 6 处墨卡托抄写收敛后，各入口输出必须**逐位相等**；
     谁再抄一份且抄歪了，这里立刻报（SKILL.md 把"投影错位"列为最惨翻车，
     而它不抛异常，只让轨迹整体偏移）
  ② 投影往返 —— px → ll → px 回到原点
  ③ 里程守恒 —— haversine 累计不随点串切分方式变化
  ④ 轨迹简化纬度自适应 —— 同一份容差在不同纬度应给出**一致的**保留比例
  ⑤ 配置读取统一 —— 4 套入口读同一份 CFG，且 map_svg 那份已改为惰性
  ⑥ 字体统一 —— 各脚本拿到同一个字体文件
  ⑦ TODO 守卫 —— `</b>★ TODO …` 这种不以引号开头的占位必须数得到

用法
----
    python scripts/selftest.py            # 跑全部
    python scripts/selftest.py -v         # 逐条列出判据

退出码 0 = 全部通过；1 = 有失败。
"""
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    print(f"  {mark} {name}" + (f"\n         {detail}" if detail and (VERBOSE or not ok) else ""))
    return ok


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ⚠ 包在 import 外面的 except 必须能抓住 **SystemExit**：make_terrain / map_svg /
#   prep_track 这些脚本在**导入阶段**就会 `sys.exit("轨迹数据未就绪…")`，
#   而 SystemExit 继承自 BaseException，**不属于** Exception。
#   写成 `except Exception` 的后果是自检直接死在第一处，后面几项一个都跑不到 ——
#   还不如明确列出"哪些失败是可以跳过的"，其余照常报错。
_IMPORT_ERR = (ImportError, SystemExit, Exception)


# ---------------------------------------------------------------- ① 投影一致性
def t_projection_consistency():
    print("① 投影一致性（6 处抄写收敛后必须逐位相等）")
    import guide_common as GC

    # 覆盖真实线路会遇到的纬度带：热带 / 中纬 / 高纬，以及负经度
    cases = [(86.84, 27.83), (117.0, 30.5), (100.0, 43.6), (-71.0, -33.0),
             (121.0, 55.0), (0.0, 0.0), (179.9, 84.0)]
    zs = (10, 12, 14)

    import make_terrain as MT
    try:
        import tiles as TL
        has_tiles = True
    except _IMPORT_ERR as e:                                        # noqa
        has_tiles, TL = False, None
        print(f"         （跳过 tiles：{e}）")
    try:
        import draw_osm as DO
        has_do = True
    except _IMPORT_ERR as e:                                        # noqa
        has_do, DO = False, None
        print(f"         （跳过 draw_osm：{e}）")

    worst = 0.0
    for z in zs:
        for lon, lat in cases:
            base = GC.mercator_px(lon, lat, z)
            worst = max(worst, max(abs(a - b) for a, b in zip(base, MT.lonlat_to_px(lon, lat, z))))
            if has_tiles:
                worst = max(worst, max(abs(a - b) for a, b in zip(base, TL.lonlat_to_px(lon, lat, z))))
            # 局部投影：同一偏移参数下，三个入口必须给出同一个图内坐标
            prm = (z, 3000, 1500, (0, 0), 2.0)
            p_gc = GC.make_projector(*prm)(lon, lat)
            if has_do:
                p_do = DO.make_projector(z, 3000, 1500, (0, 0), 2.0)(lon, lat)
                worst = max(worst, max(abs(a - b) for a, b in zip(p_gc, p_do)))
            p_meta = GC.projector_of({"z": z, "tx0": 3000, "ty0": 1500,
                                      "box": (0, 0), "scale": 2.0})(lon, lat)
            worst = max(worst, max(abs(a - b) for a, b in zip(p_gc, p_meta)))

    check("各入口墨卡托输出逐位一致", worst == 0.0,
          f"最大偏差 {worst:.3e} px（要求 0 —— 它们本该是同一个公式）")

    # 与"另一种写法"（tan + 1/cos，改版前 make_terrain 内联的那套）比对，
    # 两者数学等价，用它确认收敛没把公式改错方向。
    def legacy_px(lon, lat, z, tile=256):
        n = tile * 2 ** z
        r = math.radians(lat)
        return ((lon + 180.0) / 360.0 * n,
                (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)

    d = max(max(abs(a - b) for a, b in zip(GC.mercator_px(lon, lat, z), legacy_px(lon, lat, z)))
            for z in zs for lon, lat in cases)
    check("与改版前的 tan/cos 写法数值等价（收敛没改错方向）", d < 1e-6, f"最大偏差 {d:.3e} px")


# ---------------------------------------------------------------- ② 往返
def t_roundtrip():
    print("② 投影往返（px → ll → px 回到原点）")
    import guide_common as GC
    worst = 0.0
    for z in (10, 12, 14):
        for lon, lat in ((86.84, 27.83), (117.0, 30.5), (121.0, 55.0), (-71.0, -33.0)):
            x, y = GC.mercator_px(lon, lat, z)
            lon2, lat2 = GC.mercator_ll(x, y, z)
            worst = max(worst, abs(lon2 - lon), abs(lat2 - lat))
    check("往返误差可忽略", worst < 1e-9, f"最大 {worst:.3e} 度")


# ---------------------------------------------------------------- ③ 里程守恒
def t_distance():
    print("③ 里程守恒（切分方式不影响 haversine 累计）")
    import prep_kml_track as PK

    pts = [(110.0 + 0.001 * i, 30.0 + 0.0006 * math.sin(i / 7)) for i in range(400)]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + PK.hav(pts[i - 1], pts[i]))
    total = cum[-1]
    # 分段求和 vs 一次性求和，误差只应来自浮点结合律
    seg = sum(cum[k] - cum[k - 1] for k in range(1, len(pts)))
    check("分段累计 == 全量累计", approx(total, seg, 1e-6), f"{total:.6f} vs {seg:.6f} m")
    # 400 点 × 0.001° 经度 @30°N ≈ 96 m/步 → 约 38 km。这个区间只是用来兜住
    # "地球半径或单位写错一个数量级"这类错误，不是精确断言。
    check("累计值落在合理量级", 35_000 < total < 45_000, f"{total/1000:.2f} km（期望 35~45 km）")


# ---------------------------------------------------------------- ④ 简化纬度自适应
def t_simplify_lat():
    print("④ 轨迹简化的纬度自适应（同一容差跨纬度应给出一致保留率）")
    import prep_kml_track as PK

    def synth(lat0, n=600, step_m=90.0):
        """把**同一条以米为单位的轨迹**放到不同纬度：横向步长固定 90 m、
        纵向摆幅固定 ±220 m。

        ⚠ 别按"度"步进 —— 那会让横向步长从 19°N 的 95 m 缩到 60°N 的 50 m，
        等于拿 5 条**不同形状**的轨迹互比，测出来的漂移是测试自己的问题，
        不是代码的。第一版就踩了这个坑，误报极差 0.057。
        """
        out, lon = [], 110.0
        r = math.radians(lat0)
        kx = math.cos(r) * 111320.0
        ky = 111132.92 - 559.82 * math.cos(2.0 * r)
        for k in range(n):
            out.append((lon, lat0 + (220.0 * math.sin(k / 9) + 33.0 * math.sin(k / 3.1)) / ky))
            lon += step_m / kx
        return out

    ratios = []
    for lat in (19.0, 30.5, 43.6, 55.0, 60.0):
        p = synth(lat)
        ratios.append(len(PK.dp_simplify(p, 4.0)) / len(p))

    spread = max(ratios) - min(ratios)
    check("同一条轨迹放到 19°~60°，保留率不再漂移", spread < 0.02,
          f"保留率 {min(ratios):.3f}~{max(ratios):.3f}，极差 {spread:.4f}"
          f"（收敛前：经度写死 30.5°，60°N 会多留 10.7% 冗余点）")


# ---------------------------------------------------------------- ⑤ 配置入口
def t_cfg():
    print("⑤ 配置读取统一（4 套入口 → 1 个实现）")
    import guide_common as GC
    try:
        import route_def as RD
    except _IMPORT_ERR as e:                                        # noqa
        print(f"         （跳过：route_def 不可加载：{e}）")
        return
    if not getattr(RD, "CFG", None):
        print("         （跳过：route_def 无 CFG）")
        return

    try:
        import make_terrain as MT
        import draw_osm as DO
    except _IMPORT_ERR as e:                                        # noqa
        print(f"         （跳过：{e}）")
        return

    ok = True
    for k in ("file_stem", "title", "days"):
        v = RD.CFG.get(k)
        if v is None:
            continue
        ok &= (GC.cfg(k) == MT._route_cfg(k, None) == v)
    check("make_terrain / GC 读同一份配置", ok)

    RD.CFG["_probe"] = 1
    same = DO._cfg_flag("_probe", False) is True      # 非严格布尔仍按旧语义成立
    RD.CFG.pop("_probe", None)
    check("布尔开关沿用旧语义（写 1 仍判 True，未顺手改判定）", same)

    # 惰性：改完立刻生效（改版前 map_svg 是 import 期快照，这里会失败）
    try:
        import map_svg as MS
        RD.CFG["_probe"] = "x"
        lazy = MS._CFG.get("_probe") == "x"
        RD.CFG.pop("_probe", None)
        check("map_svg 配置已改为惰性（不再是 import 期快照）", lazy)
    except _IMPORT_ERR as e:                                        # noqa
        print(f"         （跳过 map_svg：{type(e).__name__} —— 它需要完整数据链才能 import）")


# ---------------------------------------------------------------- ⑥ 字体
def t_font():
    print("⑥ 字体统一（各脚本拿到同一个字体文件）")
    import guide_common as GC
    f = GC.label_font(12)
    path = getattr(f, "path", None)
    check("能解析到中文字体（而非 PIL 默认位图）", path is not None, f"→ {path}")
    if path:
        check("同一 size 命中缓存（大栅格上会被调上万次）", GC.label_font(12) is f)
    try:
        import make_terrain as MT
        check("make_terrain 与公共模块用同一字体", getattr(MT._label_font(12), "path", None) == path)
    except _IMPORT_ERR as e:                                        # noqa
        print(f"         （跳过 make_terrain：{e}）")


# ---------------------------------------------------------------- ⑦ TODO 守卫
def t_todo_guard():
    print("⑦ TODO 守卫（`</b>★ TODO …` 这类占位必须数得到）")
    import tempfile
    import qa_guide as QG

    def probe(txt):
        d = Path(tempfile.mkdtemp())
        (d / "route_def.py").write_text(txt, encoding="utf-8")
        old, QG.HERE = QG.HERE, d
        try:
            return QG.todo_guard()
        finally:
            QG.HERE = old

    # 三处占位：两处是引号开头（旧判据也能数），一处是 </b> 开头（旧判据漏掉）
    txt = ('CFG = {\n'
           '    "title_lite": "★ TODO 副题",\n'
           '    "foot": ("<b>安全提示：</b>★ TODO 风险提示。"\n'
           '             "无痕山野…"),\n'
           '}\n')
    n = probe(txt)
    check("安全提示类占位被计入（改版前这里会漏）", n == 2, f"数到 {n} 处，期望 2")

    clean = 'CFG = {"title_lite": "两天穿越", "foot": ("<b>安全提示：</b>注意落石。")}'
    check("已填完的配置不误报", probe(clean) == 0)


# ---------------------------------------------------------------- ⑧ 清理白名单
def t_clean_guard():
    """`clean.py` 的白名单**不许命中任何交付物**。

    这是唯一一类"错了不报错、直接把用户成品删掉"的风险：
    哪天有人为了省事把 `*.png` / `*.jpg` 写进 TEMP_SPECS，长图与全线地图会在
    下次清理时静默消失（而且是不可恢复的 —— 要重跑 5–8 min 出图）。
    所以用 fnmatch 对着交付物名字逐个试，一条命中就算失败。
    """
    print("⑧ 清理白名单（不许命中交付物：长图/地图/攻略/GPX/KML）")
    import fnmatch
    import clean as CL

    deliverables = [
        "珠峰东坡7天-攻略.html",
        "珠峰东坡7天-攻略长图.png",
        "珠峰东坡7天-全线地图.jpg",
        "珠峰东坡7天.gpx",
        "珠峰东坡7天.kml",
    ]
    specs = list(CL.TEMP_SPECS) + list(CL.DEEP_SPECS)
    bad = [(pat, name) for _, pat, _ in specs
           for name in deliverables if fnmatch.fnmatch(name, pat)]
    check("白名单不匹配交付物", not bad,
          "；".join(f"{p} 会命中 {n}" for p, n in bad))

    # 反例：必须是**真的**能匹配到中间产物，否则白名单失效（静默不清 = 用户报的老问题）
    temps = ["_qa_desk.png", "_qa_phone.png", "line.bak", "base_terrain.jpg"]
    miss = [n for n in temps
            if not any(fnmatch.fnmatch(n, pat) for _, pat, _ in specs)]
    check("白名单确实能命中中间产物", not miss, f"漏了 {miss}")

    # `_deliverables()` 用「下划线开头不算交付物」区分审计截图与成品
    import tempfile
    d = Path(tempfile.mkdtemp())
    for n in deliverables + ["_qa_desk.png"]:
        (d / n).write_text("x", encoding="utf-8")
    got = {p.name for p in CL._deliverables(d)}
    check("成品进保留集、`_qa_*` 不进", got == set(deliverables),
          f"保留集 = {sorted(got)}")


# ---------------------------------------------------------------- ⑨ 底图取名
def t_pick_name():
    """只有 `name:en` 的要素不能被当成"没名字"。

    博格达实测：91 个峰点里 3 个是 `Nangfeng/Xifeng/Hongfeng Peak`（中文名进了英文栏，
    `name` 为空）。旧实现只看 `name`，这三个有名字的山头在图上成了无名峰，
    还让"本区只有 7 个有名字的峰"变成对外说法 —— 属于"错了不报错、只少标"的典型。
    """
    print("⑨ 底图取名（name / name:zh / name_map 回退链）")
    import guide_common as GC
    try:
        import draw_osm as DO
    except _IMPORT_ERR as e:                                        # noqa
        check("draw_osm 可导入", False, f"{e}")
        return

    old_cfg, old_flag = GC.cfg, GC.cfg_flag

    def with_cfg(mp, foreign=False):
        GC.cfg = lambda k, d=None: (mp if k == "name_map" else d)
        GC.cfg_flag = lambda k, d=False: (foreign if k == "label_foreign_names" else d)

    try:
        with_cfg({}, False)
        check("取 name", DO._pick_name({"name": "博格达峰", "name:zh": "X"}) == "博格达峰")
        check("没有 name 时退回 name:zh", DO._pick_name({"name:zh": "南峰"}) == "南峰")
        check("只有 name:en 且没配映射 → 不标（不自动音译）",
              DO._pick_name({"name:en": "Nangfeng Peak"}) == "")

        with_cfg({"Nangfeng Peak": "南峰"})
        check("name_map 显式映射生效",
              DO._pick_name({"name:en": "Nangfeng Peak"}) == "南峰")
        check("映射不分大小写与首尾空格",
              DO._pick_name({"name:en": " nangfeng peak "}) == "南峰")

        with_cfg({}, True)
        check("label_foreign_names=True 时显示原文",
              DO._pick_name({"name:en": "Haigou Peak"}) == "Haigou Peak")
    finally:
        GC.cfg, GC.cfg_flag = old_cfg, old_flag


# ---------------------------------------------------------------- ⑩ 峰点校正
def t_peak_snap():
    """OSM 峰点该不该被吸到 DEM 山顶，判据要能被离线验证。

    这段逻辑的错误方式很隐蔽：**要么不动（看不出问题），要么把三角挪到邻峰头上**
    （图上立刻变成"山名标在别的山上"）。所以这里用合成 DEM 逐条钉住四个判据。
    """
    print("⑩ 峰点校正（漂在山腰才吸、吸到别的高峰上要拒绝）")
    import tempfile
    import numpy as np
    try:
        import make_terrain as MT
    except _IMPORT_ERR as e:                                        # noqa
        check("make_terrain 可导入", False, f"{e}")
        return

    z = 14
    dem = np.full((220, 220), 2000.0, np.float32)
    dem[90:111, 90:111] = 3000.0            # 一个 21×21 的方顶"山"

    def mk(tmp, ele, xi, yi):
        lo, la = MT.px_to_lonlat(xi, yi, z)
        p = Path(tmp) / "osm.json"
        t = {"natural": "peak", "name": "测试峰"}
        if ele is not None:
            t["ele"] = str(ele)
        p.write_text(json.dumps({"elements": [
            {"type": "node", "lat": la, "lon": lo, "tags": t}]}, ensure_ascii=False),
            encoding="utf-8")
        return p

    meta = {"z": z, "tx0": 0, "ty0": 0, "box": [0, 0, 220, 220]}
    d = tempfile.mkdtemp()

    # ① 点在山腰（离山顶 ~35 px≈240 m），标称 3000 与山顶相符 → 应吸附
    got = MT.snap_peaks_to_dem(mk(d, 3000, 135, 100), dem, meta, 43.0, verbose=False)
    check("漂在山腰 → 吸附", len(got) == 1,
          f"返回 {len(got)} 条")
    if got:
        lo, la = list(got.values())[0]
        key = list(got)[0]
        check("吸附后落在山头上（DEM ≥ 2900）",
              float(dem[int(round(MT.lonlat_to_px(lo, la, z)[1])) ,
                          int(round(MT.lonlat_to_px(lo, la, z)[0]))]) >= 2900)
        check("key 是 7 位小数坐标（draw_osm 按同一格式查表）",
              key.count(",") == 1 and all(len(p.split(".")[1]) == 7 for p in key.split(",")))

    # ② 点本来就在山顶 → min_gain 拦住，不动
    got = MT.snap_peaks_to_dem(mk(d, 3000, 100, 100), dem, meta, 43.0, verbose=False)
    check("本来就在山顶 → 不动", got == {}, f"却动了 {len(got)} 个")

    # ③ 标称高程远低于周围山顶 → 说明是"别的更高的峰"，不能吸（tol / max_gap）
    got = MT.snap_peaks_to_dem(mk(d, 2100, 135, 100), dem, meta, 43.0, verbose=False)
    check("标称 2100 而山顶 3000 → 拒绝吸附", got == {}, f"却动了 {len(got)} 个")

    # ④ 没有 ele → 没有判据，一律不动
    got = MT.snap_peaks_to_dem(mk(d, None, 135, 100), dem, meta, 43.0, verbose=False)
    check("没有 ele 的裸峰点 → 不动", got == {}, f"却动了 {len(got)} 个")

    # ⑤ 真山顶就在本点 300 m 内 → 不能因为"离自己太近"被挡
    #    bug 现场（2026-10-25 冈仁波齐）：自身排除写成
    #        if math.hypot(nx - xi, ny - yi) > 1e-6
    #    nx,ny 是未取整浮点、xi,yi 是 int(round(...))，恒差 0–0.5 px（远大于 1e-6），
    #    于是**自己永远不算自己**。候选山顶落在本点 300 m 内时被判"会盖住邻峰"，
    #    半径一路减半到 <60 px 后静默放弃 → peak_snap.json 写 `{}`；日志只说
    #    "本窗口无需校正"，图上三角漂在山肩（主峰只需移 175 m 也救不回来）。
    #    这里 z14/lat43 下 1 px ≈ 7 m，节点在 (120,100)、方顶在 x≤110，相距约 70 m。
    got = MT.snap_peaks_to_dem(mk(d, 3000, 120, 100), dem, meta, 43.0, verbose=False)
    check("山顶距本点 <300 m → 仍要吸附", len(got) == 1, f"返回 {len(got)} 条")

    # ⑥ 但"吸附目标 300 m 内另有具名峰"这条护栏仍须生效（张冠李戴）
    lo1, la1 = MT.px_to_lonlat(120, 100, z)
    lo2, la2 = MT.px_to_lonlat(88, 100, z)
    p2 = Path(d) / "osm2.json"
    p2.write_text(json.dumps({"elements": [
        {"type": "node", "lat": la1, "lon": lo1,
         "tags": {"natural": "peak", "name": "甲峰", "ele": "3000"}},
        {"type": "node", "lat": la2, "lon": lo2,
         "tags": {"natural": "peak", "name": "乙峰", "ele": "3000"}},
    ]}, ensure_ascii=False), encoding="utf-8")
    got = MT.snap_peaks_to_dem(p2, dem, meta, 43.0, verbose=False)
    check("吸附目标 300 m 内有别的具名峰 → 放弃", got == {}, f"却动了 {len(got)} 个")

    # ⑦ 位移护栏：几百米外的"最高点"通常已是另一座山，宁可不动。
    #    2026-10-25 全库实测：min_gain 降到 50 后各线冒出 800–1100 m 的"吸附"
    #    （800 m 方窗的对角线才 1131 m），且几乎全是无名峰点 —— 那是邻峰顶。
    #    z14/lat43 下 1 px ≈ 7 m：把 min_gain 放到 50，点放在 100 px（≈700 m）外仍要拒绝。
    got = MT.snap_peaks_to_dem(mk(d, 3000, 200, 100), dem, meta, 43.0,
                               min_gain=50.0, max_shift_m=400.0, verbose=False)
    check("位移超 max_shift（400 m）→ 不动", got == {}, f"却动了 {len(got)} 个")
    got = MT.snap_peaks_to_dem(mk(d, 3000, 200, 100), dem, meta, 43.0,
                               min_gain=50.0, max_shift_m=None, verbose=False)
    check("max_shift_m=None → 护栏可关（单线覆盖）", len(got) == 1,
          f"返回 {len(got)} 条")

    # ⑧ 逐峰例外 CFG["peak_snap_allow"]（2026-10-28 金银山）：
    #   全局 400 m 是为了防"吸到邻峰"（三连山中央峰 501 m 就会落到西北峰顶上），
    #   但个别峰的 OSM 坐标就是偏在几百米外的**同一座山**上（大雪山 440 m）——
    #   这种只能点名放行。只放宽位移上限，其它四条判据一条都不改。
    got = MT.snap_peaks_to_dem(mk(d, 3000, 200, 100), dem, meta, 43.0,
                               min_gain=50.0, max_shift_m=400.0,
                               allow_shift={"测试峰": 800.0}, verbose=False)
    check("逐峰例外：点名的峰放宽到 800 m → 吸附", len(got) == 1, f"返回 {len(got)} 条")
    got = MT.snap_peaks_to_dem(mk(d, 3000, 200, 100), dem, meta, 43.0,
                               min_gain=50.0, max_shift_m=400.0,
                               allow_shift={"别的峰": 800.0}, verbose=False)
    check("逐峰例外没点到这个峰 → 仍按 400 m 拒绝", got == {}, f"却动了 {len(got)} 个")
    got = MT.snap_peaks_to_dem(mk(d, 3000, 200, 100), dem, meta, 43.0,
                               min_gain=50.0, max_shift_m=400.0,
                               allow_shift={"测试峰": 500.0}, verbose=False)
    check("逐峰例外给的上限仍不够（500 < 实际位移）→ 拒绝", got == {},
          f"却动了 {len(got)} 个")


# ---------------------------------------------------------------- ⑪ 等高距下限兜底
def t_contour_cov_min():
    print("⑪ 等高距下限默认兜底（未配 contour_cov_min = 0.15，不是 None）")
    try:
        import numpy as np
        import make_terrain as MT
        import route_def as RD
    except BaseException as e:                                      # noqa
        print(f"         （跳过：{type(e).__name__}: {e}）")
        return
    if not hasattr(MT, "pick_contour_interval"):
        print("         （跳过：make_terrain 无 pick_contour_interval）")
        return

    # 三角波 DEM（沿 x 方向、行方向恒定）：每档覆盖率 ≈ 2A/(iv·P)，
    # 调振幅 A 就能造出「旧单阈值与区间判据答案不同」的样本。
    # bug 现场（2026-10-25 首发版）：pick_contour_interval 里写成
    #   lo = _route_cfg("contour_cov_min", None)
    # 未配键的 18 条线路静默走回单阈值，"下限 0.15" 形同未配。
    saved = {k: RD.CFG.pop(k, None) for k in
             ("contour_cov_min", "contour_target_cov", "contour_minor")}
    try:
        def _sawtooth(A, P=24.0, n=512, rows=64):
            x = np.arange(n, dtype=np.float32)
            tri = np.abs((x / P) % 2.0 - 1.0) * A
            return np.repeat(tri[None, :], rows, axis=0).astype(np.float32)

        def _policies(dem):
            cands = [(iv, MT.contour_coverage(dem, iv)) for iv in MT.CONTOUR_NICE]
            tgt = MT.CONTOUR_TARGET_COV
            single = next((iv for iv, cv in cands if cv <= tgt), MT.CONTOUR_NICE[-1])
            lo, K = MT.CONTOUR_COV_MIN, MT.COV_EST_TO_ACTUAL
            act = lambda cv: cv * K                                  # noqa: E731
            hi = tgt + MT.COV_OVER_TOL                               # 上限容忍带（2026-10-25）
            inside = [(iv, cv) for iv, cv in cands if lo <= act(cv) <= hi]
            if inside:
                mid = (lo + tgt) * 0.5
                dyn = min(inside, key=lambda x: abs(act(x[1]) - mid))[0]
            else:
                below = [(iv, cv) for iv, cv in cands if act(cv) < lo]
                dyn = (max(below, key=lambda x: act(x[1]))[0] if below
                       else min(cands, key=lambda x: act(x[1]))[0])
            return single, dyn

        found = None
        for A in range(60, 321, 10):
            dem = _sawtooth(float(A))
            single, dyn = _policies(dem)
            if single != dyn:
                got = MT.pick_contour_interval(dem, verbose=False)[0]
                found = (A, got, single, dyn)
                break
        if found is None:
            check("扫描到两种判据分叉的样本", False, "振幅 60–320 m 全部一致，测试失去区分度")
            return
        A, got, single, dyn = found
        check(f"未配下限时走区间判据（A={A} m：区间选 {dyn:g} m ≠ 旧单阈值 {single:g} m）",
              got == dyn, f"实选 {got:g} m")
        # 反向：显式配一个很宽的下限（0 < lo < target 仍成立）不应抛异常
        RD.CFG["contour_cov_min"] = 0.10
        MT.pick_contour_interval(_sawtooth(100.0), verbose=False)
        check("显式配下限也能跑（不抛异常）", True)
    finally:
        for k, v in saved.items():
            if v is not None:
                RD.CFG[k] = v


def t_contour_actual():
    print("⑮ 选档判据改为实测覆盖（含陡坡省略）+ 上限容忍带 3 pp")
    try:
        import numpy as np
        import make_terrain as MT
        import route_def as RD
    except BaseException as e:                                      # noqa
        print(f"         （跳过：{type(e).__name__}: {e}）")
        return
    if not hasattr(MT, "contour_actual_coverage") or not hasattr(MT, "COV_OVER_TOL"):
        check("make_terrain 有 contour_actual_coverage / COV_OVER_TOL", False, "缺失")
        return

    # ① 陡坡省略必须真的减线：同一 DEM 传 m_per_px（tan 远超 1.0）后覆盖率应明显下降
    x = np.arange(256, dtype=np.float32)
    tri = np.abs((x / 24.0) % 2.0 - 1.0) * 200.0        # 振幅 200 m、周期 24 px
    dem = np.repeat(tri[None, :], 32, axis=0).astype(np.float32)
    plain = MT.contour_coverage(dem, 20)
    skipped = MT.contour_actual_coverage(dem, 20, m_per_px=0.5)
    check("陡坡省略后实测覆盖 < band 边缘估计（差值 > 20%）",
          skipped < plain * 0.8, f"估计 {plain * 100:.1f}% / 实测 {skipped * 100:.1f}%")
    check("不传 m_per_px 时退回 band 估计（逐位相等）",
          abs(MT.contour_actual_coverage(dem, 20) - plain) < 1e-12,
          f"{MT.contour_actual_coverage(dem, 20)} vs {plain}")

    # ② 上限容忍带：等坡度斜面，覆盖率 = 坡度/等高距（精确可算）
    #    m_per_px 取极大 → tan≈0，不触发陡坡省略，只考察"选档策略"本身。
    saved = {k: RD.CFG.pop(k, None) for k in
             ("contour_cov_min", "contour_target_cov", "contour_minor")}
    try:
        def _ramp(s):
            return np.repeat((x * s)[None, :], 16, axis=0).astype(np.float32)

        # s=5.25：25 m → 21.0%（超上限 1.0 pp，在容忍带内，应被选中）
        got = MT.pick_contour_interval(_ramp(5.25), verbose=False, m_per_px=1e9)[0]
        check("超上限 1.0 pp 的档仍在容忍带内 → 选 25 m", got == 25, f"实选 {got:g} m")
        # s=6.0：25 m → 24.0%（超上限 4.0 pp，超出 3 pp 容忍带 → 退到 50 m / 12%）
        got = MT.pick_contour_interval(_ramp(6.0), verbose=False, m_per_px=1e9)[0]
        check("超上限 4.0 pp 超出容忍带 → 退到 50 m", got == 50, f"实选 {got:g} m")
    finally:
        for k, v in saved.items():
            if v is not None:
                RD.CFG[k] = v


def t_snowline_legend():
    print("⑫ 图例雪线 desc 必须含出图值（meta['snowline']）—— 贡嘎 fork 图文打架守卫")
    try:
        import contextlib
        import io
        import guide_common as GC
    except BaseException as e:                                      # noqa
        print(f"         （跳过：{type(e).__name__}: {e}）")
        return
    if not hasattr(GC, "check_snowline_legend"):
        print("         （跳过：guide_common 无 check_snowline_legend）")
        return

    # monkeypatch load_meta：不碰磁盘，也不依赖当前目录是哪个项目
    saved = GC.load_meta
    GC.load_meta = lambda: {"snowline": 4950.0}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            GC.check_snowline_legend("≈4950 m · 7 月档（ELA 5100 −150 m）以上为积雪白")
        check("desc 含出图值 4950 时不告警", "不一致" not in buf.getvalue(), buf.getvalue())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            GC.check_snowline_legend("西坡雪线 ≈5100 m（ELA 5000–5200 m）")
        out = buf.getvalue()
        check("desc 只有旧值 5100 时告警且点出 4950",
              "不一致" in out and "4950" in out, out)
    finally:
        GC.load_meta = saved


def t_day_palette_and_trail_legend():
    print("⑬ 分日配色回退 + 图例步道色守卫（2026-10-25 用户实测两项）")
    try:
        import contextlib
        import io
        import guide_common as GC
    except BaseException as e:                                      # noqa
        print(f"         （跳过：{type(e).__name__}: {e}）")
        return
    if not (hasattr(GC, "day_colors") and hasattr(GC, "check_trail_legend")):
        print("         （跳过：guide_common 无 day_colors / check_trail_legend）")
        return

    # --- 分日配色：days 没写 color 时必须按色板取，且相邻两天不同色 ---
    # bug 现场：17 条线路的 days 把每天显式写成同一个 #E4572E（数据问题），
    # 而 map_svg 的 d.get("color", D1_C) 让没写色的天也全落回第一天橙。
    days_no_color = [{"tag": f"D{i+1}"} for i in range(7)]
    cols, txts = GC.day_colors(days_no_color)
    check("缺 color 的 7 天取到 7 个不同色", len(set(cols)) == 7, str(cols))
    check("相邻两天永不撞色（含 >7 天循环）",
          all(cols[i] != cols[i + 1] for i in range(len(cols) - 1)) and
          all(a != b for a, b in zip(GC.day_colors(
              [{"tag": f"D{i+1}"} for i in range(13)])[0],
              GC.day_colors([{"tag": f"D{i+1}"} for i in range(13)])[0][1:])))
    check("文字色与线段色成对（非同色）",
          all(c != t for c, t in zip(cols, txts)))
    # 显式写了 color 的仍按配置来（不强行覆盖用户选择）
    cols2, _ = GC.day_colors([{"color": "#123456"}, {"tag": "D2"}])
    check("显式 color 优先，其余按色板", cols2[0] == "#123456" and cols2[1] != "#123456", str(cols2))

    # --- 图例步道色：必须与图上 TRAIL_STYLE 芯色一致 ---
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        GC.check_trail_legend("#563A8A", "（自检）")
    check("图例步道色 = #563A8A 时不告警", "不一致" not in buf.getvalue(), buf.getvalue())

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        GC.check_trail_legend("#8C8A86", "（自检）")
    out = buf.getvalue()
    check("图例步道色写成灰色时告警且点出 #563A8A",
          "不一致" in out and "563A8A" in out, out)

    # --- 步道行的判据：不能只认 tag == "步道"（实测 7 条叫 村道 / 牧道 / 既有道路） ---
    if hasattr(GC, "is_trail_legend"):
        cases = [("步道", "", True), ("村道", "", True), ("牧道", "", True),
                 ("既有道路", "OSM 测绘的公路 / 村道（非本次轨迹）", True),
                 ("雪线", "≈5750 m · 7 月档以上为积雪白", False),
                 ("冰川", "冰裂缝区，勿独行", False)]
        wrong = [t for t, d, want in cases if GC.is_trail_legend(t, d) != want]
        check("步道行判据覆盖 村道/牧道/既有道路（且不误伤雪线）", not wrong, str(wrong))
    else:
        check("guide_common.is_trail_legend 存在", False, "缺失")

    # --- 旧单行键回补：legend_extra 只有雪线行时，步道行必须还能从旧键补回来 ---
    if hasattr(GC, "legacy_trail_legend"):
        cfg = {"legend_extra": [{"tag": "雪线", "desc": "≈4810 m"}],
               "extra_legend_tag": "步道",
               "extra_legend_desc": "OSM 测绘的既有步道 / 公路（非本次轨迹）",
               "extra_legend_color": "#8C8A86"}
        leg = GC.legacy_trail_legend(cfg)
        check("旧键回补出行、且色块取图上芯色",
              leg is not None and leg["color"] == GC.C_TRAIL_LEGEND
              and leg["tag"] == "步道", str(leg))
        check("无旧键时不回补（不凭空多一行）",
              GC.legacy_trail_legend({"legend_extra": []}) is None)
    else:
        check("guide_common.legacy_trail_legend 存在", False, "缺失")


def t_keep_raw_batch():
    """⑭ 批量出图「不删临时文件」约定（2026-10-25：30 条串行重渲，最后 3 条图已渲完、
    却在 finally 清临时文件时被环境层 SAFE_DELETE 钩子按「单轮累计删除量」直接杀进程
    → 成品没落盘。约定：SHOOT_KEEP_RAW=1 时一律不删；此时截图按 mtime 判新旧，
    绝不能靠"尺寸稳定"（相邻两带尺寸可能完全相同，会把上一张当成本次结果）。"""
    print("⑭ 批量出图 keep-raw 约定（不删临时文件 + 截图按 mtime 判新）")
    try:
        import os
        import tempfile
        import time as _t
        from pathlib import Path

        import guide_common as GC
        from PIL import Image
    except BaseException as e:                                      # noqa
        print(f"         （跳过：{type(e).__name__}: {e}）")
        return

    saved_env = os.environ.get("SHOOT_KEEP_RAW")
    saved_find, saved_run = GC.find_browser, GC.subprocess.run
    saved_modes = GC._BROWSER_MODES
    try:
        GC._BROWSER_MODES = saved_modes[:1]      # 只试一种模式，别为用例空等 2×90 s
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "shot.png"

            def _png(path):
                Image.new("RGB", (4, 4), "#ffffff").save(path)

            # ① keep-raw：旧图不被覆盖时**不能**当成结果返回（否则截白带重试会读到上一带）
            _png(out)
            old = _t.time() - 3600
            os.utime(out, (old, old))
            os.environ["SHOOT_KEEP_RAW"] = "1"
            GC.find_browser = lambda: "fake-browser"
            GC.subprocess.run = lambda *a, **k: None        # 假浏览器：不写文件
            try:
                GC.browser_shot(td / "p.html", out, 100, 100, scale=1, timeout=5)
                stale_ok = True
            except RuntimeError:
                stale_ok = False
            check("keep-raw 下旧截图不被误用（报错而不是返回旧图）", not stale_ok)
            check("keep-raw 下不删旧文件（钩子计数不被消耗）", out.exists())

            # ② keep-raw：新图（mtime 现在）正常返回
            def _write(*a, **k):
                _png(out)
            GC.subprocess.run = _write
            im = GC.browser_shot(td / "p.html", out, 100, 100, scale=1, timeout=5)
            check("keep-raw 下新截图正常返回", im.size == (4, 4), str(im.size))

            # ③ render_map_hi._rm_quiet：keep-raw 保留 / 否则删除
            try:
                import render_map_hi as RM
            except BaseException:                        # noqa —— 无 route_def 时导入即 exit
                RM = None
            if RM is None or not hasattr(RM, "_rm_quiet"):
                print("         （render_map_hi 无 _rm_quiet，跳过清理语义用例）")
            else:
                f = td / "tmp.png"
                os.environ.pop("SHOOT_KEEP_RAW", None)
                _png(f)
                RM._rm_quiet(f)
                check("无 SHOOT_KEEP_RAW 时临时文件被清理", not f.exists())
                _png(f)
                os.environ["SHOOT_KEEP_RAW"] = "1"
                RM._rm_quiet(f)
                check("SHOOT_KEEP_RAW=1 时临时文件保留", f.exists())
    finally:
        GC.find_browser, GC.subprocess.run = saved_find, saved_run
        GC._BROWSER_MODES = saved_modes
        if saved_env is None:
            os.environ.pop("SHOOT_KEEP_RAW", None)
        else:
            os.environ["SHOOT_KEEP_RAW"] = saved_env


def t_osm_reuse():
    """⑯ OSM 邻项目复用：差集几何 + 三道闸门（2026-10-28）。

    为什么它必须进自检：这条路径**错了不会报错**——
      · 覆盖判据写成"元素范围"→ 以为邻居有数据、实际窗口边缘从没抓过（静默缺一层矢量）；
      · 差额补抓拿到"半截数据"（镜像超时返回 200 + 少量要素）→ 写出残缺 osm.json，
        而 `_cache_covers()` 之后照样判"覆盖"，再也不会自动重抓（`osm_fetch_budget` 的前科）。
    所以这里全部用**断网替身**（monkeypatch `_fetch_rect` / 假 donor 目录）来守行为。
    """
    print("⑯ OSM 邻项目复用（差集 / 覆盖容差 / 残缺与半截数据必须拒绝落盘）")
    import shutil
    import tempfile

    def mkproj(root, name, bbox, n):
        """造一个"线路项目"：stub route_def（只给 CFG['bbox']）+ osm.json。"""
        p = root / name
        (p / "scripts" / "out").mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / "guide_common.py", p / "scripts" / "guide_common.py")
        (p / "scripts" / "route_def.py").write_text(
            "# -*- coding: utf-8 -*-\nCFG = {'bbox': %r}\n" % (list(bbox),), encoding="utf-8")
        x0, y0, x1, y1 = bbox
        # 节点要**铺满到边界**（65×33 网格）：`_cache_covers()` 判的是"元素四边够不够到
        # 窗口"，稀疏点阵会在边缘留下半格空隙，害得测试失败于几何判定、而不是要测的行为。
        if n >= 300:
            cols, rows = 65, 33
            n = cols * rows
            uv = [((i % cols) / (cols - 1), (i // cols) / (rows - 1)) for i in range(n)]
        else:
            uv = [((i % 20) / 19.0, ((i // 20) % 17) / 16.0) for i in range(n)]
        els = [{"type": "node", "id": i + 1,
                "lon": x0 + u * (x1 - x0), "lat": y0 + v * (y1 - y0),
                "tags": {"highway": "path"}} for i, (u, v) in enumerate(uv)]
        (p / "scripts" / "out" / "osm.json").write_text(
            json.dumps({"elements": els}, ensure_ascii=False), encoding="utf-8")
        # resolve_bbox 会先卡"必须已有真实轨迹"（防几内亚湾占位窗口），所以 stub 项目
        # 也得给一份两点轨迹；窗口本身仍由 CFG['bbox'] 决定。
        (p / "scripts" / "out" / "track_real.json").write_text(json.dumps(
            {"pts": [{"lon": x0, "lat": y0, "km": 0.0, "ele": 3000.0},
                     {"lon": x1, "lat": y1, "km": 1.0, "ele": 3000.0}],
             "total_km": 1.0, "asc": 0, "desc": 0,
             "ele_min": 3000, "ele_max": 3000}, ensure_ascii=False), encoding="utf-8")
        return p

    td = Path(tempfile.mkdtemp())
    F, _saved_fetch = None, None
    saved_mods = {k: sys.modules.pop(k) for k in ("route_def", "fetch_osm", "guide_common")
                  if k in sys.modules}
    try:
        mine = (100.0, 30.0, 100.2, 30.2)
        me = mkproj(td, "本线", mine, 0)
        mkproj(td, "邻居线", (100.0, 30.0, 100.4, 30.4), 300)
        shutil.copy(HERE / "fetch_osm.py", me / "scripts" / "fetch_osm.py")
        shutil.copy(HERE / "guide_common.py", me / "scripts" / "guide_common.py")

        sys.path.insert(0, str(me / "scripts"))
        import fetch_osm as F                            # noqa: 走 fixture 里的副本

        # ---- ① 纯几何：矩形差集 ----
        a, s = (0.0, 0.0, 10.0, 10.0), (2.0, 2.0, 8.0, 8.0)
        parts = F._subtract(a, s)
        tot = sum(F._area(r) for r in parts)
        check("差集面积守恒（a − s 各块之和 = a − 交集）",
              approx(tot, F._area(a) - F._area(s), 1e-9), f"{tot:.3f} vs {F._area(a) - F._area(s):.3f}")
        overlap = sum(F._area(F._intersect(p, s)) or 0.0 for p in parts)
        check("差集各块与 s 无重叠", approx(overlap, 0.0, 1e-12))
        check("完全不相交时差集 = 原矩形", F._subtract(a, (20, 20, 30, 30)) == [a])

        # ---- ② 覆盖判据的容差（2%，与 _cache_covers 同口径）----
        check("外框差 1.5% 仍算覆盖（容差内）",
              F._covers((-0.003, 29.997, 100.203, 30.203), mine))
        check("外框差 5% 不算覆盖",
              not F._covers((100.0, 30.01, 100.2, 30.2), mine))

        # ---- ③ 完整覆盖 → 零请求 ----
        def _boom(*_a, **_k):
            raise AssertionError("完整覆盖时不该发网络请求")
        F._fetch_rect, _saved_fetch = _boom, F._fetch_rect
        d = F.reuse("auto", force=True)
        check("完整覆盖时 0 个差额矩形", d.get("_rects") == 0, str(d.get("_rects")))
        check("完整覆盖时来自邻居线", d.get("_reused_from") == "邻居线")
        check("结果落盘且要素数 ≥ 300", F.CACHE.exists() and len(d.get("elements") or []) >= 300,
              str(len(d.get("elements") or [])))
        check("结果通过 _cache_covers()", F._cache_covers(d))

        # ---- ④ 差额矩形返回 0 要素 = 半截数据 → 必须拒绝，且不覆盖旧缓存 ----
        F._fetch_rect = lambda bb, **k: []
        before = F.CACHE.read_bytes()
        old_bbox = list(F._donors("auto", 300))
        shutil.rmtree(td / "邻居线")
        mkproj(td, "邻居线", (100.0, 30.0, 100.2, 30.10), 300)     # 只盖住南半截
        try:
            F.reuse("auto", force=True, cover_hard=0.0)
            ok, why = False, "没有抛错"
        except SystemExit as e:
            ok, why = True, str(e)[:60]
        check("差额矩形 0 要素 → 拒绝落盘（SystemExit）", ok, why)
        check("拒绝时旧缓存字节未变", F.CACHE.read_bytes() == before)

        # ---- ⑤ 差额抓取直接失败 → 同样拒绝 ----
        def _fail(bb, **k):
            raise RuntimeError("全部镜像 504")
        F._fetch_rect = _fail
        try:
            F.reuse("auto", force=True, cover_hard=0.0)
            ok2 = False
        except SystemExit:
            ok2 = True
        check("差额抓取失败 → 拒绝落盘", ok2 and F.CACHE.read_bytes() == before)

        # ---- ⑥ donor 要素过少（疑似残缺）→ 直接拒绝复用 ----
        shutil.rmtree(td / "邻居线")
        mkproj(td, "邻居线", (99.9, 29.9, 100.3, 30.3), 120)
        try:
            F.reuse("auto", force=True, min_elems=300, cover_hard=0.0)
            ok3 = False
        except SystemExit:
            ok3 = True
        check("donor 只有 120 个要素 → 不当 donor（拒绝复用）", ok3)
        check("整个过程旧缓存字节未变", F.CACHE.read_bytes() == before)

        # ⑦ 覆盖率分流（2026-10-28 用户规则）：<80% 不绕复用，直接整盘抓。
        #   实测定标：差额 21.6% 时两块矩形 2m31s ≈ 整盘 3m09s，复用等于白忙。门槛 85%。
        shutil.rmtree(td / "邻居线")
        mkproj(td, "邻居线", (100.0, 30.0, 100.2, 30.10), 300)   # 只覆盖 50%
        calls = {"n": 0}
        _saved_fetch_impl = F.fetch

        def _fake_fetch(force=False, soft=False, rounds=2):
            calls["n"] += 1
            return {"elements": [{"type": "node", "id": 9, "lat": 30.05, "lon": 100.1}]}

        F.fetch = _fake_fetch
        F._fetch_rect = _boom2 = lambda bb, **k: (_ for _ in ()).throw(
            AssertionError("覆盖率不足时不该再去补抓差额"))
        try:
            out = F.reuse("auto", force=True)
            check("覆盖 50% <85% → 改走整盘抓取", calls["n"] == 1 and len(out["elements"]) == 1,
                  f"fetch 调用 {calls['n']} 次")
        finally:
            F.fetch = _saved_fetch_impl
        # 演练模式（--reuse-dry）不能偷偷联网抓
        calls["n"] = 0
        F.fetch = _fake_fetch
        try:
            out = F.reuse("auto", force=True, dry=True)
            check("--reuse-dry 下覆盖率不足 → 只报告不抓", calls["n"] == 0 and out.get("_reuse_skipped"))
        finally:
            F.fetch = _saved_fetch_impl
        _ = old_bbox
    finally:
        if F is not None and _saved_fetch is not None:
            F._fetch_rect = _saved_fetch
        try:
            sys.path.remove(str(me / "scripts"))
        except ValueError:
            pass
        for k in ("route_def", "fetch_osm", "guide_common"):
            sys.modules.pop(k, None)
        sys.modules.update(saved_mods)
        shutil.rmtree(td, ignore_errors=True)


def t_labels_vector():
    """LABELS_VECTOR 坑 + 山谷名渲染 + 补抓脚本（2026-10-29 贡嘎总览回写）。

    这三条都不是"功能测试"，而是**防止同类事故再犯**的静态断言：
    · 不走 map_svg 的出图必须显式 labels_vector=False，否则 OSM 文字全丢；
    · draw_osm 必须有 valley/ridge 地形名渲染分支（荒野段只剩三角的老坑）；
    · _fetch_extra.py 必须存在且查了水源/营地/补给三类 node。
    """
    me = Path(__file__).resolve().parent
    do = (me / "draw_osm.py").read_text(encoding="utf-8")
    pt = (me.parent / "references" / "pitfalls-terrain.md")
    ptext = pt.read_text(encoding="utf-8") if pt.exists() else ""

    check("draw_osm 有 LABELS_VECTOR 开关", "LABELS_VECTOR" in do)
    check("draw_pixels 由 labels_vector 配置决定", 'label_no_crowd' in do and 'draw_pixels' in do)
    if ptext:
        check("坑文档写明 labels_vector 坑", "labels_vector" in ptext and "LABELS_VECTOR" in ptext)
        check("坑文档写明『日志成功数不可信、要裁 1:1 原图核对』",
              "records" in ptext and "裁 1:1" in ptext)
    else:
        # 项目副本里没有 references/（只有技能目录有）→ 文档项跳过，不算失败
        check("坑文档检查（项目副本无 references/ → 跳过）", True, "跳过")

    check("draw_osm 渲染山谷/山脊名", '("valley", "ridge")' in do)
    check("draw_osm 渲染水源/营地/补给（_kind）", "_kind" in do and "spring" in do)

    fe = me / "_fetch_extra.py"
    check("_fetch_extra.py 存在", fe.exists())
    if fe.exists():
        src = fe.read_text(encoding="utf-8")
        check("补抓脚本查了泉水", 'natural"="spring"' in src)
        check("补抓脚本查了营地", "camp_site" in src)
        check("补抓脚本查了补给", 'node["shop"]' in src)
        check("补抓脚本不把 osm.ch 放首位", "overpass.osm.ch" not in src.split("MIRRORS")[1][:400])


def t_label_knobs():
    """标注层旋钮的静态断言（2026-10-29 贡嘎九线总览回写）。

    这四个键只影响"地名/峰名的字号与落位"，默认全关、不影响既有线路；
    断言它们还在，防止后续重构时被顺手删掉（删了不会报错，只会静默回到分级字号/左挪）。
    """
    me = Path(__file__).resolve().parent
    do = (me / "draw_osm.py").read_text(encoding="utf-8")
    check("draw_osm 支持 osm_label_size（统一字号）", "osm_label_size" in do)
    check("draw_osm 支持 peak_label_all", "peak_label_all" in do)
    check("draw_osm 支持 peak_label_tight", "peak_label_tight" in do)
    check("draw_osm 支持 peak_label_right_only", "peak_label_right_only" in do)
    check("place() 有 right_only 参数", "right_only=False" in do)
    # right_only 分支里绝不能出现左侧候选（2026-10-29 用户要求峰名一律在三角右侧）
    seg = do.split("if right_only:", 1)[-1].split("else:", 1)[0]
    check("right_only 分支不含左侧候选", "(-r" not in seg)


def main():
    print("离线自检（不联网 / 不下 DEM / 不启浏览器）\n")
    for fn in (t_projection_consistency, t_roundtrip, t_distance,
               t_simplify_lat, t_cfg, t_font, t_todo_guard, t_clean_guard,
               t_pick_name, t_peak_snap, t_contour_cov_min, t_snowline_legend,
               t_day_palette_and_trail_legend, t_keep_raw_batch, t_contour_actual,
               t_osm_reuse, t_labels_vector, t_label_knobs):
        try:
            fn()
        except Exception as e:                                      # noqa
            check(f"{fn.__name__} 执行异常", False, f"{type(e).__name__}: {e}")
        print()

    print(f"通过 {len(_PASS)} 项，失败 {len(_FAIL)} 项")
    if _FAIL:
        print("失败项：")
        for n in _FAIL:
            print(f"  · {n}")
    return 0 if not _FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

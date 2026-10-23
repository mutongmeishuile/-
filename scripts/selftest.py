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


def main():
    print("离线自检（不联网 / 不下 DEM / 不启浏览器）\n")
    for fn in (t_projection_consistency, t_roundtrip, t_distance,
               t_simplify_lat, t_cfg, t_font, t_todo_guard, t_clean_guard,
               t_pick_name):
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

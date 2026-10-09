# -*- coding: utf-8 -*-
"""开工预检：30 秒内把所有"环境约定"一次性确认完。

为什么要先跑这个
----------------
实测一次完整攻略 45 min，其中 5 项是纯环境/低级错误，全部在跑到一半时才暴露：
  · 自带 Python 没 numpy/Pillow → 跑到第 5 步才 ModuleNotFoundError，换解释器重来
  · 没装 Chrome 只有 Edge → 渲染脚本里的浏览器路径要改
  · out/ 与 scripts/out/ 两套路径约定 → 文件找不到
  · KML 格式判断错 → 解析出 0 个点，白跑一遍
这些都不是"设计错了"，是"约定没先确认"。花 30 秒跑完本脚本，能省下后面十几分钟。

用法
----
    python scripts/preflight.py [可选：KML 路径]

不传轨迹就只查环境；传了就顺带判断格式（两步路 KML / GPX / 分段 LineString）该用哪个解析脚本。
"""
import importlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ok(msg):
    print(f"  [OK]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")
    return 1


def main():
    fail = 0
    print("① Python 与依赖")
    print(f"  解释器 {sys.executable}")
    for m, need in (("numpy", "地形栅格运算"), ("PIL", "图像读写")):
        try:
            importlib.import_module(m)
            ok(f"{m} 可用（{need}）")
        except ImportError:
            fail += bad(f"缺 {m} —— {need} 会崩。"
                        f"换有 numpy/Pillow 的那个解释器，或装：pip install {m if m=='numpy' else 'Pillow'}")

    print("② 无头浏览器（渲染长图必需）")
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    found = [p for p in cands if Path(p).exists()]
    if found:
        ok(f"找到 {found[0]}")
        if "Edge" in found[0]:
            print("         （Edge 需 --headless=new，Chrome 用 --headless）")
    else:
        fail += bad("没找到 Chrome/Edge —— 长图渲染无从谈起")

    print("③ 路径约定（全部中间产物统一在 scripts/out/）")
    out = HERE / "out"
    if out.is_dir():
        ok(f"scripts/out/ 存在（{len(list(out.glob('*.json')))} 个 json）")
    else:
        print("  [..]   scripts/out/ 不存在（首次运行正常，各脚本会建）")
    # ⚠ 这份清单不是"有几个脚本"的清单，而是"默认链跑通所必需"的清单 ——
    #   别只列 make_all 的 8 个步骤名：步骤名不等于文件名，且有几步是**一个脚本
    #   调另一个**（prep_track → parse_track_kml/prep_kml_track）或**import 另一个**
    #   （build_guide → map_svg；make_terrain → draw_osm）。漏掉被调用的那个，
    #   preflight 全绿、跑到一半才炸，正是最费时间的返工项。
    REQUIRED = (
        # 共享库与唯一配置
        "guide_common.py", "route_def.py",
        # prep：入口 + 它按顺序 subprocess 调用的两个解析器
        "prep_track.py", "parse_track_kml.py", "prep_kml_track.py",
        # osm / dem：fetch_osm 抓底图，make_terrain 出地形并 import draw_osm 叠注记
        "fetch_osm.py", "make_terrain.py", "draw_osm.py",
        # html / long / map：build_guide 与 render_map_hi 共用 map_svg 画图
        "build_guide.py", "map_svg.py", "shoot_guide.py", "render_map_hi.py",
        # gpx / qa
        "make_gpx.py", "qa_guide.py",
    )
    # 按需脚本：只在特定线路/场合用到，缺了不算错，但值得提醒一句
    OPTIONAL = (
        ("parse_kml_ls.py",  "KML 是分段 <LineString> 时才用它替代 parse_track_kml.py"),
        ("tiles.py",         "B 路线：在线瓦片下载器（build_base_map / tile_tint 的前置）"),
        ("build_base_map.py", "B 路线：拼在线瓦片当底图"),
        ("tile_tint.py",     "B 路线：抹掉瓦片自带等高线 + 线画分离 + DEM 自绘"),
        ("build_route_guide.py", "没有轨迹、纯手绘的兜底方案"),
        ("new_route.py",     "开工准备：从 out/ 产物生成 route_def.py 骨架"),
        ("make_all.py",      "一条命令跑全流程（调度器）"),
    )
    miss_req = [f for f in REQUIRED if not (HERE / f).exists()]
    if miss_req:
        for f in miss_req:
            fail += bad(f"缺 {f}（默认链缺一不可，别从旧项目拷贝残缺副本）")
    else:
        ok(f"默认链 {len(REQUIRED)} 个脚本全部就位")
    miss_opt = [f for f, _ in OPTIONAL if not (HERE / f).exists()]
    if miss_opt:
        for f, why in OPTIONAL:
            if f in miss_opt:
                print(f"  [warn] 缺 {f} —— 不影响默认链；{why}")
    # ⚠ 两套 out/ 是真实返工项：有的脚本写 HERE/"out"，有的写上一级
    if (HERE.parent / "out").is_dir():
        print("  [warn] 上一级也有 out/ —— 确认所有脚本统一用 HERE/'out'，别两套并存")

    print("④ route_def.py（唯一需要按线路改的文件）")
    track_ready = any((p / "track_real.json").exists()
                      for p in (out, HERE, HERE / "kml"))
    # ⚠ 首次运行（还没 prep）时，数字区与线路文案**本来就填不了** ——
    #   里程/爬升要等 prep_kml_track.py 算完才有。把它们判成 FAIL 会把人带错方向
    #   （让人先去补数字，而正确顺序是先 prep）。所以按 track_ready 分流。
    try:
        sys.path.insert(0, str(HERE))
        import route_def as RD
        c = getattr(RD, "CFG", {})
        need = ("file_stem", "title", "sub", "days", "day_cards", "notes", "src")
        miss = [k for k in need if not c.get(k)]
        if not track_ready:
            print("  [..]   route_def 尚未填（首次运行正常）—— 正确顺序："
                  "先 prep_track.py 拿到里程/爬升，再回来填数字区与文案")
            if miss:
                print(f"         待填 CFG：{miss}")
            print(f"         待填 POIS {len(getattr(RD, 'POIS', []))} 个 · "
                  f"分日 {len(c.get('days', []))} 天")
        else:
            if miss:
                fail += bad(f"CFG 缺必填项：{miss}")
            else:
                ok("CFG 必填项齐全")
            nv = len(getattr(RD, "POIS", []))
            print(f"        POIS {nv} · MARKS {len(getattr(RD, 'MARKS', []))} · "
                  f"SCHEDULE {len(getattr(RD, 'SCHEDULE', []))} · 分日 {len(c.get('days', []))}")
            if nv < 4:
                print("  [warn] POI 少于 4 个 —— 地图上几乎没有标注，确认不是漏改")
            for k in ("TOTAL_KM", "ASC", "DESC"):
                v = getattr(RD, k, None)
                if v in (None, 0):
                    fail += bad(f"{k} 未填（数据区要从 prep_kml_track.py 的输出抄）")
            print(f"        里程 {getattr(RD,'TOTAL_KM',None)} km · "
                  f"爬升 {getattr(RD,'ASC',None)} / 下降 {getattr(RD,'DESC',None)} m")
        if not c.get("kml"):
            print("  [..]   CFG['kml'] 未填 —— prep_track.py 需手工传 KML 路径")
        else:
            ok(f"KML 已登记：{c['kml']}")
    except FileNotFoundError as e:
        # 全新项目、还没跑过轨迹准备时，这是**正常状态**，不是错误
        print(f"  [..]   route_def 还读不到轨迹数据（{e}）")
        print("         → 首次运行请先：python prep_track.py <你的.kml>")
    except Exception as e:                                          # noqa
        fail += bad(f"route_def.py 导入失败：{e!r}")
    if track_ready:
        ok("out/track_real.json 已就位")

    print("⑤ 轨迹文件格式（决定用哪个解析脚本，选错会得到 0 个点）")
    kml = sys.argv[1] if len(sys.argv) > 1 else None
    if not kml:
        # 没传就退回 route_def.CFG["kml"]（与 prep_track.py 同一套解析）
        try:
            sys.path.insert(0, str(HERE))
            from route_def import CFG as _C
            k = (_C or {}).get("kml")
            if k:
                kml = next((str(p) for p in (Path(k), HERE.parent / k, HERE / k) if p.exists()),
                           str(Path(k)))
                print(f"        （用 route_def.CFG['kml']：{kml}）")
        except Exception:                                           # noqa
            pass
    if not kml:
        print("  [..]   没传轨迹文件，跳过（用法：python preflight.py 路径.kml|.gpx，"
              "或在 route_def.CFG 里登记 'kml'）")
    else:
        p = Path(kml)
        if not p.exists():
            fail += bad(f"轨迹文件不存在：{p}")
        else:
            s = p.read_text(encoding="utf-8", errors="ignore")
            n_coord = len(re.findall(r"<gx:coord>", s))
            n_when = len(re.findall(r"<when>", s))
            n_ls = len(re.findall(r"<LineString>", s))
            n_trkpt = len(re.findall(r"<trkpt\b", s))
            n_rtept = len(re.findall(r"<rtept\b", s))
            n_wpt = len(re.findall(r"<wpt\b", s))
            print(f"        gx:coord {n_coord} | when {n_when} | LineString {n_ls} "
                  f"| trkpt {n_trkpt} | rtept {n_rtept} | wpt {n_wpt}")
            if n_trkpt or n_rtept:
                ok(f"→ GPX 格式（trkpt {n_trkpt} / rtept {n_rtept} / wpt {n_wpt}），"
                   f"用 parse_track_kml.py（它按内容自动识别 GPX）")
            elif n_coord:
                ok("→ gx:Track 格式，用 parse_track_kml.py")
            elif n_ls:
                ok(f"→ 分段 LineString 格式，用 parse_kml_ls.py（{n_ls} 段）")
            else:
                fail += bad("三种都不是 —— 先人工看一眼文件结构")
            if n_coord and n_when > n_coord:
                print(f"        ⚠ when({n_when}) 多于 coord({n_coord})：多出的属于 POI 标注，"
                      f"解析时只取 <gx:Track> 块内的，别全文 findall")

    print("\n" + ("预检通过，可以开工 ✓" if not fail else f"有 {fail} 项没过，先修再往下走 ↑"))
    return fail


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

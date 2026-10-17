# -*- coding: utf-8 -*-
"""一步完成轨迹准备：KML/GPX → out/track_real.json + out/profile_real.json。

做两件事（按顺序调 parse_track_kml.py 与 prep_kml_track.py）：
  ① 解析轨迹文件（两步路 KML / 标准 GPX，自动识别）→ out/track_full.json（全量原始点，导航 GPX 用）
  ② 简化 + 平滑 + 等距剖面 → out/track_real.json / out/profile_real.json（画图用）

KML 路径来源（按优先级）：
  1. 命令行：python prep_track.py "C:/path/线路.kml"
  2. route_def.CFG["kml"]（写在配置文件里，之后就能一条命令重跑全流程）

⚠⚠ **段选择必须写进 route_def.CFG["kml_keep"]，否则每次全流程重跑都会被撤销**
（2026-10-17 北天山中线实测踩坑）：两步路 KML 常带作者手绘的**平行备选段**
（首尾都落在主线上）。手工跑 `parse_kml_ls.py --longest` 选好主线后，
`make_all.py` 的 wave0 会再跑一次本脚本 —— 而它**不带 --longest**，
于是备选段又被合回来，里程从 143.60 km 悄悄涨回 311.56 km，剖面图出现两道空白悬崖。
→ 把选择固化到配置里：CFG["kml_keep"] = "longest"（只留最长段）或 "0,2"（按段号挑）。

**为什么要有这一步**：旧流程里 ① 写到 `out/`、② 却读 `scripts/` 同目录，
中间夹着一次"手工把文件搬过去"，既容易忘也破坏了可重复性。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _cfg_get(key):
    try:
        sys.path.insert(0, str(HERE))
        import route_def as RD
        return getattr(RD, "CFG", {}).get(key)
    except Exception:                                              # noqa
        return None


def _kml_from_cfg():
    return _cfg_get("kml")


def _pick_parser(kml):
    """按**文件内容**选解析脚本，别按扩展名猜。

    两步路有两条导出路径，schema 完全不同：
      · 新版 `<gx:Track>` + `<gx:coord>`（带 `<when>` 时间戳）→ parse_track_kml.py
      · 旧版/分段导出 `<Placemark>/<LineString>/<coordinates>`（无时间戳）→ parse_kml_ls.py
    选错的结果是"解析出 0 个点"，很容易被误判成"文件坏了"。
    ⚠ 2026-10-10 狼塔 C+V 实测：整条流水线在 wave0 直接失败，只因为 prep 写死了
      parse_track_kml.py —— 而该线是分段式 KML，必须走 parse_kml_ls.py。
    """
    txt = kml.read_text(encoding="utf-8", errors="ignore")[:4_000_000]
    if "<gx:coord" in txt or "<trkpt" in txt or "<rtept" in txt:
        return "parse_track_kml.py"
    if "<LineString>" in txt:
        return "parse_kml_ls.py"
    return "parse_track_kml.py"


def main():
    kml = sys.argv[1] if len(sys.argv) > 1 else _kml_from_cfg()
    if not kml:
        sys.exit("没给轨迹文件路径。用法：python prep_track.py <线路.kml|.gpx>，"
                 "或在 route_def.CFG 里加 \"kml\": \"...\"")
    # CFG 里的 kml 允许写相对路径（相对项目根 = scripts 的上一级）
    cands = [Path(kml), HERE.parent / kml, HERE / kml]
    kml = next((p for p in cands if p.exists()), None)
    if kml is None:
        sys.exit(f"KML 不存在。找过：{[str(c) for c in cands]}")
    # ⚠ 必须 resolve()：下面 subprocess 是以 cwd=HERE(scripts/) 跑的，
    #   相对路径（哪怕在当前目录里确实存在）到了子进程就指向 scripts/ 了，
    #   parse_track_kml.py 会报 FileNotFoundError 而看起来像"文件没了"。
    kml = kml.resolve()

    parser = _pick_parser(kml)
    print(f"轨迹格式识别 → {parser}", flush=True)
    # 段选择：命令行优先，其次 route_def.CFG["kml_keep"]（见模块 docstring 的坑）
    keep = None
    for a in sys.argv[2:]:
        if a in ("--longest",) or a.startswith("--keep"):
            keep = a
    if keep is None:
        ck = _cfg_get("kml_keep")
        if ck:
            keep = "--longest" if str(ck).strip().lower() == "longest" else f"--keep={ck}"
    if keep and parser == "parse_kml_ls.py":
        print(f"段选择（{keep}，来自 {'命令行' if keep in sys.argv[2:] else 'route_def.CFG[kml_keep]'}）",
              flush=True)
    for script in (parser, "prep_kml_track.py"):
        if "parse" in script:
            args = [sys.executable, str(HERE / script), str(kml)]
            if keep and script == parser:
                args.append(keep)
        else:
            args = [sys.executable, str(HERE / script)]
        print(f"=== {script} ===", flush=True)
        r = subprocess.run(args, cwd=str(HERE))
        if r.returncode:
            sys.exit(f"{script} 失败（退出码 {r.returncode}）")
    print("\n轨迹准备完成。下一步：按打印出的数字更新 route_def.py，再跑 make_terrain.py")


if __name__ == "__main__":
    main()

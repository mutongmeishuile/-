# -*- coding: utf-8 -*-
"""晕渲光照方向自检 —— 一条命令判定「主光是不是真的在西北」。

用法:
    python scripts/check_hillshade.py            # 读 out/base_meta.json 的窗口
    python scripts/check_hillshade.py --json     # 机器可读

为什么需要这个：
    2026-10-20 贡嘎大环发现 `aspect = arctan2(dzdy, -dzdx)` 在图像坐标系（y 向下）里把坡向
    沿 45° 轴**镜像**了：实测「面向西北的坡亮度 0.293、面向东南的坡 0.787」，主光跑到了东南，
    于是整条谷读成一道亮脊（用户原话"谷底看起来更像山脊"）。
    这种 bug **不会报错、等高线完全对得上、数值自检也全绿** —— 只能靠"坡向 ↔ 亮度"的
    统计关系抓出来。判据只有一句：

        面向西北的坡，必须显著亮于面向东南的坡（≥ +0.25）

退出码：0 = 通过；1 = 光照方向反了 / 太弱。
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_terrain as MT                                        # noqa: E402

MIN_GAP = 0.25          # NW 与 SE 的最小亮度差
SLOPE_LO, SLOPE_HI = 8.0, 60.0


def main():
    as_json = "--json" in sys.argv
    meta_p = HERE / "out" / "base_meta.json"
    if not meta_p.exists():
        print("✗ 没有 out/base_meta.json —— 先跑 make_terrain.py")
        return 1
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    lon0, lat0, lon1, lat1 = meta["bbox"]
    z = meta["z"]
    lat_c = (lat0 + lat1) / 2

    dem, _ = MT.load_dem(z, lon0, lat0, lon1, lat1)
    dem = np.asarray(dem, np.float32)
    res = 156543.03392 * math.cos(math.radians(lat_c)) / (2 ** z)
    p = np.pad(dem, 1, mode="edge")
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * res)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * res)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))

    hs = MT.hillshade(dem, z, lat_c)
    mid = (slope > SLOPE_LO) & (slope < SLOPE_HI)
    nw = mid & (dzdx > 0) & (dzdy > 0)          # 面向西北
    se = mid & (dzdx < 0) & (dzdy < 0)          # 面向东南
    if nw.sum() < 1000 or se.sum() < 1000:
        print("✗ 样本太少，判不了（窗口可能过小）")
        return 1
    v_nw, v_se = float(hs[nw].mean()), float(hs[se].mean())
    gap = v_nw - v_se
    ok = gap >= MIN_GAP

    if as_json:
        print(json.dumps({"nw": round(v_nw, 3), "se": round(v_se, 3),
                          "gap": round(gap, 3), "ok": ok}, ensure_ascii=False))
    else:
        print(f"西北坡亮度 {v_nw:.3f} · 东南坡 {v_se:.3f} · 差 {gap:+.3f}（要求 ≥ +{MIN_GAP}）")
        if ok:
            print("✓ 光照方向正确：主光在西北 315°，山体阴坡阳坡关系正常")
        else:
            print("✗ 光照方向反了 —— 检查 make_terrain.hillshade 里的 aspect：")
            print("     aspect = np.arctan2(-dzdx, dzdy)     # 北0° 东90° 南180° 西270°")
            print("   （写成 arctan2(dzdy, -dzdx) 会把坡向沿 45° 轴镜像，谷底会像山脊）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

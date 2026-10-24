# -*- coding: utf-8 -*-
"""DEM 瓦片预取：只把 Terrarium 瓦片下进全局缓存，**不做**合成、不写产物。

为什么要有这一步
----------------
首次建线（或换窗口）时，两段网络等待原本是**串行**的：

    fetch_osm.py        1–2 min   （Overpass，窗口 = 轨迹包围盒）
    make_terrain.py     1–2 min   （Terrarium 瓦片，窗口 = 同一个 bbox）

两者互不依赖 —— 都只要 bbox。make_all 把本脚本排在 wave1（与 osm ∥ gpx 同层），
于是首次建线的墙钟从「2 + 2 min」变成「max(2, 2) min」，直接省掉一半。
这也是整条流水线里**唯一一段分钟级**的等待（其余步骤都是秒级）。

缓存命中时它几乎是空转（24 线程逐个查 `demcache/`），耗时 <1 s，
所以可以无脑常开，不需要判断"要不要预取"。

⚠ 它和 make_terrain 用的是**同一个** z 选择逻辑（`pick_dem_z` / `CFG["dem_z"]`）与
同一个缓存目录（`MT.CACHE`）。若两者口径不一致，这里下载的瓦片 make_terrain 用不上，
白下 —— 改 z 逻辑时只改 make_terrain，本脚本一律复用，不要复制判断。

用法
----
    python scripts/prefetch_dem.py
"""
import sys


def main():
    import guide_common as GC
    import make_terrain as MT

    lon0, lat0, lon1, lat1, src = GC.resolve_bbox()
    req = MT._route_cfg("dem_z", None)                        # 线路强制档优先
    z = min(int(req), MT.DEM_Z) if req else MT.pick_dem_z(lon0, lat0, lon1, lat1)
    print(f"[prefetch] DEM 瓦片预取 z{z}（{src}）…", flush=True)
    dem, meta = MT.load_dem(z, lon0, lat0, lon1, lat1)
    print(f"[prefetch] 完成：{dem.shape[1]}×{dem.shape[0]} 瓦片像素，"
          f"缓存目录 {MT.CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

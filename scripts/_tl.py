# -*- coding: utf-8 -*-
"""引入逐线林线 CFG['treeline_m']：植被带到林线为止，高山带 = 林线→雪线。"""
import pathlib

p = pathlib.Path("make_terrain.py")
s = p.read_text(encoding="utf-8")

old = """    # 植被带用**雪线相对高程**定位"""
new = """    # 植被带顶端：优先线路级 **林线**（CFG['treeline_m']），否则退回雪线相对值。
    # 为什么要有林线：固定 s−700 对"山体只比雪线高一点点"的线路（年宝玉则 3800–5369、
    # 雪线 4650）会把整片 4000–4650 的草甸划进高山带 → 82% 画面是灰的（2026-10-21 实测）。
    # 林线是独立于雪线的第二条界线（天山 ≈2700、喜马拉雅 ≈4000、横断山 ≈4100–4300），
    # 按它切才符合垂直带谱：林线以下绿、林线以上草甸→砾石→雪。
    _tl = _route_cfg("treeline_m", None)
    if _tl:
        top = min(float(_tl), s - 150.0)
    # 植被带用**雪线相对高程**定位"""
assert old in s
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")
print("ok")

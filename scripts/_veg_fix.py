# -*- coding: utf-8 -*-
"""植被段改按"雪线相对高程"定位。

病根：旧实现把整张植被色表压进 [dmin, s−700]。亚丁窗口从 3573 m 起，
于是 600 m 里塞完"深谷→森林→灌丛→草甸"，谷底直接取到 (58,96,72) 近黑绿，
而 4110–4810 m 的大片高山带是中性灰 → 整图偏灰、河谷不绿。
"""
import pathlib

p = pathlib.Path("make_terrain.py")
s = p.read_text(encoding="utf-8")
n = 0


def rep(a, b):
    global s, n
    assert a in s, "MISS " + a[:60]
    s = s.replace(a, b, 1)
    n += 1


rep("""    top = s - (700.0 if season == "summer" else 1300.0)""",
    """    top = s - (700.0 if season == "summer" else 1300.0)""")

rep("""    lo = max(dmin, top - 4200.0)
    fr = (0.0, 0.22, 0.45, 0.65, 0.85, 1.0)
    veg = [(lo + f * (top - lo), veg_cols[i]) for i, f in enumerate(fr)]""",
    """    # 植被带用**雪线相对高程**定位（每档 ~600–800 m），不再把整张色表压进 [dmin, top]：
    #   · 高雪线线路（亚丁 4810）窗口底就是 3573 m，压缩后谷底直接取到"深谷近黑绿"，
    #     而 4100–4800 m 的大片高山带是中性灰 → 整图偏灰、七月河谷不绿（用户 2026-10-21 反馈）。
    #   · 相对高程定位后，3600 m 落在"中绿草甸"档，4200 m 才转黄绿，与实景一致。
    offs = (-3800.0, -3000.0, -2400.0, -1800.0, -1000.0, -700.0)
    veg = [(s + o, veg_cols[i]) for i, o in enumerate(offs)]
    veg = [(e, c) for e, c in veg if e >= dmin]          # 窗口底以上的锚点才有效
    if not veg or veg[0][0] > dmin:                       # 最低锚点高于窗口底 → 补平到窗口底
        c0 = veg[0][1] if veg else veg_cols[-1]
        veg = [(dmin, c0)] + veg
    veg = [(min(e, top), c) for e, c in veg if e <= top]  # 顶端以上的锚点丢掉
    if not veg:
        veg = [(top, veg_cols[-1])]""")

p.write_text(s, encoding="utf-8")
print("patched", n)

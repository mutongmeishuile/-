# -*- coding: utf-8 -*-
"""把两步路 KML 轨迹预处理成绘图数据（读 out/track_full.json）：

   out/track_real.json    —— 简化后的轨迹（经纬度 + 累计里程 + 平滑海拔 + 时刻）
   out/profile_real.json  —— 等距采样的海拔剖面（每 100 m 一点）

⚠ 路径统一在 scripts/out/ 下 —— 旧版这里读的是 scripts/track_full.json，
  而上一步 parse_track_kml.py 写的是 out/，中间要人工搬一次文件；
  那条"手工搬运"就是"依赖已生成文件"的根源，已去掉。
"""
import json, math
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def _find(name):
    """按 out/ → scripts/ → scripts/kml/ 顺序找输入（兼容历史目录布局）。"""
    for p in (OUT / name, HERE / name, HERE / "kml" / name):
        if p.exists():
            return p
    raise FileNotFoundError(
        f"找不到 {name}。先跑：python parse_track_kml.py <你的.kml|.gpx>")


SRC = _find("track_full.json")

R = 6371008.8


def hav(a, b):
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dl, dp = lon2 - lon1, lat2 - lat1
    h = math.sin(dp / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def dp_simplify(pts, tol):
    """Douglas-Peucker：按平面近似后的**横向偏差**裁点，tol 单位 m。

    2026-10-22 修：经度缩放改用**本条轨迹自己的中纬**，不再写死 30.5°。
    写死的后果是同一份 tol 在不同纬度效果不一样，参数不可移植：经度被按
    cos(30.5°)/cos(φ) 倍缩放，等效容差 = tol × cos(30.5°)/cos(φ) ——
    60°N 上 4 m 变成 6.89 m（多留 10.7% 冗余点），19°N 上变成 3.65 m（少留 0.8%）。
    写成 30.5° 是因为最早的样例线路（九华山）正好在那个纬度，于是这个偏差
    长期没发作 —— 它不致命，但让「tol=4.0」这个数字失去跨线路的可比性。
    （`cum` 形参一直没被函数体用到，一并去掉。）
    """
    if len(pts) < 2:
        return list(range(len(pts)))
    # 用**本条轨迹的中纬**算该处 1° 的真实弧长（WGS84 椭球，取前两项：误差 < 0.002%）
    #   · 平行圈（东西向）：111320·cos φ —— 收敛前这里**写死 30.5°**，是主要误差源；
    #   · 子午线（南北向）：111132.92 − 559.82·cos 2φ —— 收敛前写死 111320（±0.7%）。
    # 两个方向都用真实弧长，tol 才是**名副其实的米**，跨线路可比。
    r = math.radians((min(p[1] for p in pts) + max(p[1] for p in pts)) / 2.0)
    kx = math.cos(r) * 111320.0
    ky = 111132.92 - 559.82 * math.cos(2.0 * r)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i][0], pts[i][1]
        bx, by = pts[j][0], pts[j][1]
        ax, ay, bx, by = ax * kx, ay * ky, bx * kx, by * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for t in range(i + 1, j):
            px, py = pts[t][0] * kx, pts[t][1] * ky
            if L2 <= 1e-9:
                d = math.hypot(px - ax, py - ay)
            else:
                u = ((px - ax) * dx + (py - ay) * dy) / L2
                u = max(0.0, min(1.0, u))
                d = math.hypot(px - (ax + u * dx), py - (ay + u * dy))
            if d > best:
                best, bi = d, t
        if best > tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [i for i, k in enumerate(keep) if k]


def main():
    d = json.loads(SRC.read_text(encoding="utf-8"))
    raw = d["pts"]                      # [lon, lat, ele, hh:mm:ss]
    pts = [(p[0], p[1]) for p in raw]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + hav(pts[i - 1], pts[i]))

    # 海拔**先修无效值**再做低通滤波（11 点滑动平均），去 GPS 高程抖动。
    # ⚠ 2026-10-27 梅里外转实测：两步路整理的 KML 里有成片的 `ele=0`（本线 161 个点，
    #   集中在 64.5 / 68 / 104.5 / 106.5 km 的垭口与营地），以及 -32768 这类哨兵值。
    #   不修的话 11 点滑动平均只把它们摊薄成"掉到 1000 m 以下"的几个点 →
    #   ① 剖面图直接扎到 0 m（用户看到一道深渊）；② 该日累计爬升虚增上千（D01 算出 +5279 m）。
    #   这里按"落在 [500, 9000] 之外即无效"判定，用前后最近的**有效点线性插值**补上；
    #   整条轨迹都无效时退回原值（不改变其它线路的行为）。
    raw_ele = [p[2] for p in raw]
    ok = [i for i, v in enumerate(raw_ele) if 500 <= v <= 9000]
    if ok and len(ok) < len(raw_ele):
        ele = list(raw_ele)
        n_fix = 0
        for i in range(len(ele)):
            if 500 <= ele[i] <= 9000:
                continue
            lo = max([j for j in ok if j < i], default=None)
            hi = min([j for j in ok if j > i], default=None)
            if lo is None:
                ele[i] = raw_ele[hi]
            elif hi is None:
                ele[i] = raw_ele[lo]
            else:
                t = (i - lo) / (hi - lo)
                ele[i] = raw_ele[lo] + (raw_ele[hi] - raw_ele[lo]) * t
            n_fix += 1
        print(f"⚠ 修正无效高程 {n_fix} 点（0 / -32768 / 越界值 → 前后有效点插值）")
    else:
        ele = raw_ele
    sm = []
    for i in range(len(ele)):
        a, b = max(0, i - 5), min(len(ele), i + 6)
        sm.append(sum(ele[a:b]) / (b - a))

    # 累计升降（3 m 阈值，滤掉噪声抖动）
    def gains(arr, thr=3.0):
        up = dn = 0.0
        prev = arr[0]
        for v in arr[1:]:
            dd = v - prev
            if abs(dd) >= thr:
                if dd > 0:
                    up += dd
                else:
                    dn -= dd
                prev = v
        return up, dn

    up, dn = gains(sm)
    up_raw, dn_raw = gains(ele, 3.0)
    print(f"原始里程 {cum[-1]/1000:.2f} km | 滤波后爬升 {up:.0f} m 下降 {dn:.0f} m "
          f"| 未滤波 {up_raw:.0f}/{dn_raw:.0f}")

    idx = dp_simplify(pts, 4.0)          # 4 m 横向容差（按本轨迹中纬换算，不再写死 30.5°）
    print("简化后点数", len(idx), "/", len(pts))
    simp = [{"lon": round(pts[i][0], 6), "lat": round(pts[i][1], 6),
             "km": round(cum[i] / 1000, 4), "ele": round(sm[i], 1),
             "t": raw[i][3]} for i in idx]

    # 等距剖面：每 100 m 取一点
    prof = []
    j = 0
    tgt = 0.0
    while tgt <= cum[-1] + 1e-6:
        while j + 1 < len(cum) and cum[j + 1] < tgt:
            j += 1
        prof.append([round(tgt / 1000, 3), round(sm[j], 1)])
        tgt += 100.0

    OUT.mkdir(exist_ok=True)
    (OUT / "track_real.json").write_text(
        json.dumps({"pts": simp, "total_km": round(cum[-1] / 1000, 3),
                    "asc": round(up), "desc": round(dn),
                    "ele_min": round(min(sm)), "ele_max": round(max(sm))},
                   ensure_ascii=False), encoding="utf-8")
    (OUT / "profile_real.json").write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
    print(f"written out/track_real.json + out/profile_real.json  剖面点 {len(prof)}")
    print(f"  原始里程 {cum[-1]/1000:.2f} km | 滤波后爬升 {up:.0f} m 下降 {dn:.0f} m")
    print("下一步：按上面的数字改 route_def.py，然后 python make_terrain.py")


if __name__ == "__main__":
    main()

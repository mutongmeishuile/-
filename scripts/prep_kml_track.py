# -*- coding: utf-8 -*-
"""把两步路 KML 轨迹预处理成绘图数据：
   track_real.json  —— 简化后的轨迹（经纬度 + 累计里程 + 里程分段归属）
   profile_real.json —— 等距采样的海拔剖面
"""
import json, math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "track_full.json"

R = 6371008.8


def hav(a, b):
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dl, dp = lon2 - lon1, lat2 - lat1
    h = math.sin(dp / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def dp_simplify(pts, cum, tol):
    """Douglas-Peucker，按「里程-位置」空间里的横向偏差裁点。tol 单位 m。"""
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i][0], pts[i][1]
        bx, by = pts[j][0], pts[j][1]
        # 平面近似（纬度 30.5°，经度缩放 cos）
        kx = math.cos(math.radians(30.5)) * 111320.0
        ky = 111320.0
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

    # 海拔低通滤波（11 点滑动平均），去 GPS 高程抖动
    ele = [p[2] for p in raw]
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

    idx = dp_simplify(pts, cum, 4.0)     # 4 m 横向容差
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

    (HERE / "track_real.json").write_text(
        json.dumps({"pts": simp, "total_km": round(cum[-1] / 1000, 3),
                    "asc": round(up), "desc": round(dn),
                    "ele_min": round(min(sm)), "ele_max": round(max(sm))},
                   ensure_ascii=False), encoding="utf-8")
    (HERE / "profile_real.json").write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
    print("written track_real.json / profile_real.json  剖面点", len(prof))


if __name__ == "__main__":
    main()

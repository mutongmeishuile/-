# -*- coding: utf-8 -*-
"""用 OSM 步道网络做图论最短路，拟合九华山南北穿越轨迹"""
import math, heapq, json
from pathlib import Path
from route_data import load_ways, D1_POIS, D2_POIS

M_PER_DEG_LAT = 111320.0
LON0 = 117.82


def dist(a, b):
    """近似平面距离（米）"""
    dy = (b[1] - a[1]) * M_PER_DEG_LAT
    dx = (b[0] - a[0]) * M_PER_DEG_LAT * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dx, dy)


def key(p):
    return (round(p[0], 6), round(p[1], 6))


class Graph:
    def __init__(self, ways):
        self.adj = {}
        self.coord = {}
        for w in ways:
            pts = w["pts"]
            for i in range(len(pts) - 1):
                a, b = key(pts[i]), key(pts[i + 1])
                self.coord[a] = pts[i]
                self.coord[b] = pts[i + 1]
                d = dist(pts[i], pts[i + 1])
                self.adj.setdefault(a, []).append((b, d))
                self.adj.setdefault(b, []).append((a, d))
        # 合并近距离端点（OSM 断线接续）
        nodes = list(self.coord)
        grid = {}
        CELL = 0.0005            # ~50m
        for n in nodes:
            grid.setdefault((int(n[0] / CELL), int(n[1] / CELL)), []).append(n)
        merges = 0
        for n in nodes:
            cx, cy = int(n[0] / CELL), int(n[1] / CELL)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for m in grid.get((gx, gy), []):
                        if m >= n:
                            continue
                        d = dist(self.coord[n], self.coord[m])
                        if d <= 45:
                            self.adj.setdefault(n, []).append((m, d))
                            self.adj.setdefault(m, []).append((n, d))
                            merges += 1
        print(f"  graph nodes={len(self.adj)} merges={merges}")

    def nearest(self, pt):
        best, bd = None, 1e18
        for n, c in self.coord.items():
            d = dist(pt, c)
            if d < bd:
                best, bd = n, d
        return best, bd

    def shortest(self, s, t):
        pq = [(0.0, s, None)]
        prev = {}
        seen = {}
        while pq:
            d, u, p = heapq.heappop(pq)
            if u in seen:
                continue
            seen[u] = d
            prev[u] = p
            if u == t:
                break
            for v, w in self.adj.get(u, []):
                if v not in seen:
                    heapq.heappush(pq, (d + w, v, u))
        if t not in seen:
            return None, None
        path = []
        cur = t
        while cur is not None:
            path.append(self.coord[cur])
            cur = prev[cur]
        path.reverse()
        return path, seen[t]


# 只用「步道紧贴山峰（snap 小）」的锚点，保证拟合段真实可靠
ANCHORS = [
    ("十王峰",     117.8181740, 30.4634260),
    ("天台寺",     117.8197497, 30.4678033),
    ("大花台峰",   117.8277717, 30.4833577),
    ("打鼓岭垭口",  117.8332090, 30.4959380),
    ("展旗峰",     117.8389972, 30.5006138),
]
SNAP_MAX = 120.0        # 超过此距离视为「步道不经过」，不接受该段

if __name__ == "__main__":
    ways = load_ways()
    g = Graph(ways)
    total, full = 0.0, []
    for i in range(len(ANCHORS) - 1):
        n1 = ANCHORS[i]
        n2 = ANCHORS[i + 1]
        a, b = (n1[1], n1[2]), (n2[1], n2[2])
        s, ds = g.nearest(a)
        t, dt = g.nearest(b)
        path, L = g.shortest(s, t)
        if path is None or ds > SNAP_MAX or dt > SNAP_MAX:
            print(f"[X] {n1[0]} -> {n2[0]}: 弃用（snap {ds:.0f}/{dt:.0f} m）")
            continue
        total += L
        print(f"[OK] {n1[0]} -> {n2[0]}: {L/1000:.2f} km  (snap {ds:.0f}/{dt:.0f} m, {len(path)} pts)")
        full.extend(path if not full else path[1:])
    print(f"合计 OSM 实测段里程 ≈ {total/1000:.2f} km")
    Path("route_osm.json").write_text(
        json.dumps({"total_m": total, "pts": full}, ensure_ascii=False), encoding="utf-8")

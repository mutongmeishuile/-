# -*- coding: utf-8 -*-
"""多源 XYZ 瓦片下载与拼接（Web Mercator）。

默认走 318318 专线；服务挂掉或需换源时，改 SOURCE 即可。

关键坑（按源不同）：
  * hiking.318318.xyz —— URL **不能带扩展名**，且 **不能带浏览器 UA**（EdgeOne WAF 回 567）；
  * tile.openstreetmap.org —— 官方 Tile Usage Policy **禁止批量下载**，不要用于本项目；
  * OpenTopoMap —— CC-BY-SA，允许用，但要求**不要 Massendownload**、需署名；
  * Esri ArcGIS Online —— 免费公开服务，需署名；注意部分图层在中国区域只到 z13
    （z14+ 返回灰底 "Map data not yet available"，本模块用"图像近似纯色"自动识别）；
  * 天地图 —— 需注册免费 tk，无 key 回 418；
  * Carto / Stadia / Thunderforest —— 无 key 回 "API KEY REQUIRED" 占位图。

占位图检测：解码后若像素标准差 < 2（近似纯色）即判为无效瓦片，与源无关。
"""
import http.client, io, math, threading, queue, time, urllib.parse
from pathlib import Path
from PIL import Image

TILE = 256
CACHE = Path(__file__).resolve().parent / "tilecache"
CACHE.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 瓦片源
UA_DEFAULT = "hiking-route-guide/1.0 (personal hiking guide; contact: local user)"

SOURCES = {
    # 原专线（地形渲染，等高线 20 m）。URL 无扩展名、不发 UA。
    "318318": dict(
        template="https://hiking.318318.xyz/t05/{z}/{x}/{y}",
        ua=None, min_z=1, max_z=17, suffix_ext=False,
        attribution="© OpenStreetMap contributors / 地形渲染（318318 瓦片服务）"),
    # OSM 生态里最正统的"地形图"：OSM 数据 + SRTM 高程。CC-BY-SA。z≤17。
    "opentopomap": dict(
        template="https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        ua=UA_DEFAULT, min_z=1, max_z=17,
        attribution="Kartendaten: © OpenStreetMap-Mitwirkende, SRTM | "
                    "Kartendarstellung: © OpenTopoMap (CC-BY-SA)"),
    # Esri 地形底图：注记 + 晕渲 + 高程点，观感最接近纸质地形图。**中国区域仅到 z13**。
    "esri_topo": dict(
        template="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        ua=UA_DEFAULT, min_z=1, max_z=13, swap_xy=True,
        attribution="Esri, HERE, Garmin, USGS, Intermap, NASA, NGA — World Topographic Map"),
    # Esri 纯晕渲（灰度），z 到 16+，中国覆盖完整。适合当底衬再叠注记。
    "esri_hillshade": dict(
        template="https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        ua=UA_DEFAULT, min_z=1, max_z=16, swap_xy=True,
        attribution="Esri, USGS — World Hillshade"),
    # 标准 OSM 风格（非地形），做备胎。
    "osmde": dict(template="https://tile.openstreetmap.de/{z}/{x}/{y}.png",
                  ua=UA_DEFAULT, min_z=1, max_z=19,
                  attribution="© OpenStreetMap contributors (openstreetmap.de)"),
    "osmfr": dict(template="https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
                  ua=UA_DEFAULT, min_z=1, max_z=20,
                  attribution="© OpenStreetMap contributors (openstreetmap.fr)"),
}

SOURCE = "318318"          # 在这里切换源


def src():
    return SOURCES[SOURCE]


# ---------------------------------------------------------------- 投影
def lonlat_to_px(lon, lat, z):
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def px_to_lonlat(x, y, z):
    n = TILE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


# ---------------------------------------------------------------- 校验
def _is_blank(data):
    """占位图/空白图检测：解码后近似纯色即视为无效。"""
    try:
        im = Image.open(io.BytesIO(data)).convert("L")
    except Exception:
        return True
    px = im.resize((32, 32)).tobytes()      # L 模式：每像素 1 字节
    n = len(px)
    mean = sum(px) / n
    var = sum((v - mean) ** 2 for v in px) / n
    return var ** 0.5 < 2.0


# ---------------------------------------------------------------- 下载
def fetch_tile(z, x, y, timeout=25, retries=3, use_cache=True, source=None):
    """下载单块瓦片，返回 bytes 或 None。"""
    s = SOURCES[source or SOURCE]
    if not (s["min_z"] <= z <= s["max_z"]):
        return None
    if s.get("swap_xy"):
        url = s["template"].format(z=z, x=x, y=y)
    else:
        url = s["template"].format(z=z, x=x, y=y)
    u = urllib.parse.urlsplit(url)
    path = u.path + (("?" + u.query) if u.query else "")

    fp = CACHE / f"{source or SOURCE}_{z}_{x}_{y}"
    if use_cache and fp.exists() and fp.stat().st_size > 3000:
        return fp.read_bytes()

    last = None
    for a in range(retries):
        try:
            c = http.client.HTTPSConnection(u.netloc, timeout=timeout)
            headers = {}
            if s["ua"]:
                headers["User-Agent"] = s["ua"]
                headers["Referer"] = f"https://{u.netloc}/"
            c.request("GET", path, headers=headers)   # 318318: 不带任何头
            r = c.getresponse()
            data = r.read()
            c.close()
            ok = (r.status == 200 and len(data) > 1200
                  and (data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG")
                  and not _is_blank(data))
            if ok:
                fp.write_bytes(data)
                return data
            last = f"status={r.status} len={len(data)} blank={_is_blank(data) if len(data)>1200 else '-'}"
        except Exception as e:            # noqa
            last = repr(e)
        time.sleep(0.4 * (a + 1))
    if last:
        print(f"    tile {z}/{x}/{y} FAIL {last}")
    return None


def download_area(z, lon_min, lat_min, lon_max, lat_max,
                  workers=12, progress=True, source=None):
    """下载覆盖给定经纬度矩形的全部瓦片。"""
    x0f, y0f = lonlat_to_px(lon_min, lat_max, z)   # 左上
    x1f, y1f = lonlat_to_px(lon_max, lat_min, z)   # 右下
    tx0, ty0 = int(math.floor(x0f / TILE)), int(math.floor(y0f / TILE))
    tx1, ty1 = int(math.floor(x1f / TILE)), int(math.floor(y1f / TILE))
    jobs = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]

    s_use = SOURCES[source or SOURCE]
    if z > s_use["max_z"]:
        raise SystemExit(
            f"源 '{source or SOURCE}' 最高只到 z{s_use['max_z']}，请求 z{z}。"
            f"（Esri World Topo 在中国区域 z14+ 无数据，请改用 esri_hillshade 或 opentopomap）")

    ok, q, lock, done = {}, queue.Queue(), threading.Lock(), [0]
    for j in jobs:
        q.put(j)

    def work():
        while True:
            try:
                x, y = q.get_nowait()
            except queue.Empty:
                return
            d = fetch_tile(z, x, y, source=source)
            with lock:
                ok[(x, y)] = d
                done[0] += 1
                if progress and done[0] % 50 == 0:
                    print(f"  tiles {done[0]}/{len(jobs)}")

    ths = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    bad = [k for k, v in ok.items() if not v]
    if bad:
        print(f"  WARN 失败 {len(bad)}/{len(jobs)} 块: {bad[:6]}")
    return {"z": z, "tx0": tx0, "ty0": ty0, "tx1": tx1, "ty1": ty1,
            "tiles": ok, "lon_min": lon_min, "lat_min": lat_min,
            "lon_max": lon_max, "lat_max": lat_max, "source": source or SOURCE}


def stitch(area, out_path, quality=88):
    """把已下载瓦片拼成一张大图。"""
    tx0, ty0, tx1, ty1 = area["tx0"], area["ty0"], area["tx1"], area["ty1"]
    W, H = (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE
    canvas = Image.new("RGB", (W, H), "#EDEAE3")
    for (x, y), d in area["tiles"].items():
        if not d:
            continue
        im = Image.open(io.BytesIO(d)).convert("RGB")
        if im.size != (TILE, TILE):
            im = im.resize((TILE, TILE), Image.LANCZOS)
        canvas.paste(im, ((x - tx0) * TILE, (y - ty0) * TILE))
    canvas.save(out_path, quality=quality)
    print(f"  stitched {canvas.size} -> {out_path}")
    return canvas


if __name__ == "__main__":
    # 探针：逐个源检查可用性与最高级别
    import sys
    lon, lat = 117.818174, 30.463426        # 十王峰
    targets = sys.argv[1:] or list(SOURCES)
    for name in targets:
        SOURCE = name
        s = SOURCES[name]
        line = []
        for z in (12, 13, 14, 15, 16):
            x, y = lonlat_to_px(lon, lat, z)
            d = fetch_tile(z, int(x // TILE), int(y // TILE), source=name)
            line.append(f"z{z}:{'OK(%d)' % len(d) if d else '—'}")
        print(f"{name:<16} " + "  ".join(line))

# -*- coding: utf-8 -*-
"""九华山南北穿越 · 底图构建
下载 XYZ 瓦片 → 拼接 → 裁剪到目标经纬度窗口 → 输出底图 + 投影函数

换底图源：改下面的 TILE_SOURCE（见 tiles.py 的 SOURCES 表）。
  * "318318"        原专线地形渲染（等高线 20 m）—— 2026-09-29 起整站 567，暂不可用
  * "opentopomap"   OSM 生态正统地形图（OSM + SRTM），CC-BY-SA，z≤17
  * "esri_hillshade" Esri 纯晕渲，中国覆盖完整、z 到 16
  * "esri_topo"     Esri 地形注记图，**中国区域仅到 z13**（z14+ 返回空白占位图，已自动识别）
  * "osmde"/"osmfr" 标准 OSM 风格（非地形）
注意：tile.openstreetmap.org 官方站的 Tile Usage Policy **禁止批量下载**，本项目不采用。
"""
import base64, io, json, math
from pathlib import Path
from PIL import Image

from tiles import download_area, lonlat_to_px, TILE, SOURCES

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

# 目标经纬度窗口（含线路全线 + 必要的地形上下文）
# 注：2026-09-29 瓦片服务（hiking.318318.xyz）开始对整站返回 567，无法重下；
#     此窗口即先前已成功下载的底图窗口，实测 KML 轨迹（南起 30.4149）落在其内。
LON0, LAT0, LON1, LAT1 = 117.7765, 30.4120, 117.8625, 30.5980

TILE_SOURCE = "318318"
Z = 15
OUT_W = 1504          # 输出底图像素宽（页面 2x 设备像素下清晰）


def build_base():
    area = download_area(Z, LON0, LAT0, LON1, LAT1, workers=14, source=TILE_SOURCE)
    stitched = Image.new("RGB", ((area["tx1"] - area["tx0"] + 1) * TILE,
                                 (area["ty1"] - area["ty0"] + 1) * TILE), "#EDEAE3")
    for (x, y), d in area["tiles"].items():
        if d:
            im = Image.open(io.BytesIO(d)).convert("RGB")
            if im.size != (TILE, TILE):
                im = im.resize((TILE, TILE), Image.LANCZOS)
            stitched.paste(im, ((x - area["tx0"]) * TILE, (y - area["ty0"]) * TILE))

    # 精确裁剪到经纬度窗口
    ax0, ay0 = lonlat_to_px(LON0, LAT1, Z)
    ax1, ay1 = lonlat_to_px(LON1, LAT0, Z)
    box = (round(ax0 - area["tx0"] * TILE), round(ay0 - area["ty0"] * TILE),
           round(ax1 - area["tx0"] * TILE), round(ay1 - area["ty0"] * TILE))
    crop = stitched.crop(box)
    scale = OUT_W / crop.width
    out = crop.resize((OUT_W, round(crop.height * scale)), Image.LANCZOS)
    return out, scale, box, area


def projector(img_w, img_h, scale, box, area):
    """经纬度 -> 输出底图像素坐标"""
    def proj(lon, lat):
        x, y = lonlat_to_px(lon, lat, Z)
        px = (x - area["tx0"] * TILE - box[0]) * scale
        py = (y - area["ty0"] * TILE - box[1]) * scale
        return px, py
    return proj


def smooth(pts, n=12):
    """Catmull-Rom 平滑插值"""
    if len(pts) < 3:
        return pts
    P = [pts[0]] + list(pts) + [pts[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for j in range(n):
            t = j / n
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(tuple(pts[-1]))
    return out


if __name__ == "__main__":
    img, scale, box, area = build_base()
    img.save(OUT / "base_map.jpg", quality=84, optimize=True, progressive=True)
    meta = {"size": img.size, "scale": scale, "box": list(box),
            "bbox": [LON0, LAT0, LON1, LAT1], "z": Z,
            "tx0": area["tx0"], "ty0": area["ty0"],
            "source": TILE_SOURCE,
            "attribution": SOURCES[TILE_SOURCE]["attribution"]}
    (OUT / "base_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    print("base map:", img.size, "scale=%.4f" % scale)
    print("source:", TILE_SOURCE, "|", SOURCES[TILE_SOURCE]["attribution"])
    print("jpg KB:", (OUT / "base_map.jpg").stat().st_size // 1024)

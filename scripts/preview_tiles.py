# -*- coding: utf-8 -*-
"""按 bbox/z 渲染底图 + OSM 步道 + POI，用于人工核对路线走向
用法: python preview.py LON0 LAT0 LON1 LAT1 Z OUT [--half]
"""
import io, sys
from PIL import Image, ImageDraw
from tiles import lonlat_to_px, download_area, TILE
from route_data import load_ways, D1_POIS, D2_POIS


def render(lon0, lat0, lon1, lat1, z, out, split=False, waywidth=2):
    area = download_area(z, lon0, lat0, lon1, lat1, workers=14)
    tx0, ty0 = area["tx0"], area["ty0"]
    canvas = Image.new("RGB", ((area["tx1"] - tx0 + 1) * TILE,
                               (area["ty1"] - ty0 + 1) * TILE), "#EDEAE3")
    for (x, y), d in area["tiles"].items():
        if d:
            im = Image.open(io.BytesIO(d)).convert("RGB")
            if im.size != (TILE, TILE):
                im = im.resize((TILE, TILE), Image.LANCZOS)
            canvas.paste(im, ((x - tx0) * TILE, (y - ty0) * TILE))

    def to_px(lon, lat):
        x, y = lonlat_to_px(lon, lat, z)
        return x - tx0 * TILE, y - ty0 * TILE

    dr = ImageDraw.Draw(canvas)
    COL = {"path": (210, 30, 30), "footway": (20, 100, 230), "steps": (150, 50, 210),
           "track": (150, 105, 40), "bridleway": (20, 160, 120)}
    for w in load_ways():
        col = COL.get(w["tags"].get("highway", "path"), (130, 130, 130))
        dr.line([to_px(lo, la) for lo, la in w["pts"]], fill=col, width=waywidth)
    for name, lon, lat, ele, kind in (D1_POIS + D2_POIS):
        x, y = to_px(lon, lat)
        dr.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        dr.text((x + 7, y - 6), name, fill=(0, 0, 0))
    canvas.save(out)
    print("saved", out, canvas.size)
    if split:
        h = canvas.height
        canvas.crop((0, 0, canvas.width, h // 2)).save(out.replace(".png", "_a.png"))
        canvas.crop((0, h // 2, canvas.width, h)).save(out.replace(".png", "_b.png"))
    return canvas


if __name__ == "__main__":
    a = sys.argv[1:]
    render(float(a[0]), float(a[1]), float(a[2]), float(a[3]), int(a[4]), a[5],
           split=("--half" in a))

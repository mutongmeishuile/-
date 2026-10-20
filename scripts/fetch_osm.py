# -*- coding: utf-8 -*-
"""抓取 OSM 矢量要素（Overpass API）→ 本地缓存，供地形底图叠加。

为什么走 Overpass 而不是 tile.openstreetmap.org：
  官方瓦片服务的 Tile Usage Policy **明文禁止批量下载/爬取**，
  但 OSM **原始数据**（ODbL 许可）经 Overpass 导出是受人欢迎的正当用法，
  自己渲染还能完全控制配色、线宽、注记密度。

用法：
    python fetch_osm.py                 # 用轨迹自动推的窗口抓取并缓存
    python fetch_osm.py --force         # 忽略缓存重新抓
    python fetch_osm.py --soft          # 全镜像失败也算成功（流水线不中断，只丢矢量层）

**这一步是可选的**：抓不到只意味着底图上少一层道路/水系/地名，
地形晕渲与等高线照常出图。所以流水线默认以 `--soft` 调用它，
Overpass 集体抽风不该让整条流水线挂掉。
"""
import argparse, json, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
CACHE = OUT / "osm.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import guide_common as GC                                           # noqa: E402

# 窗口与 make_terrain.py 完全同源（同一个 resolve_bbox()）——
# 旧版这里写死了一份常量，改线路时极易只改一处，导致矢量与地形整体错位。
LON0, LAT0, LON1, LAT1, _src = GC.resolve_bbox()
BBOX = f"{LAT0},{LON0},{LAT1},{LON1}"

UA = "hiking-route-guide/1.0 (offline guide map; contact: local user)"

# 抓什么：路网 + 步道 + 水系 + 水体 + 林地 + 地名 + 山峰 + 冰川 + 哑口
# 冰川（natural=glacier）单独要——希夏邦马这一带冰川是地图主角，
# 且冰川面配「雪线 + 退缩速率」注释，数据来自联网检索（见 route_def.py 头部）。
# natural=saddle（哑口/垭口）2026-10-17 念青东补：非登山区（念青东、横断山腹地）
# OSM 的 natural=peak 常常**一个都没有**，saddle 是那一带唯一稳的山口标注来源，
# 也是"图上一个山名都没有"时少数还能挖出东西的标签。
# place=locality 同时补上（原 place 正则里没有，小地名会被整类漏掉）。
# 水体（natural=water / landuse=reservoir）**way 与 relation 都要**：
# OSM 上许多大湖/水库是 multipolygon relation（如年宝玉则仙女湖 relation 4491548），
# 只抓 way 会整片漏掉 —— 2026-10-17 年宝玉则实测踩坑（地图把湖画成平地）。
QUERY = f"""
[out:json][timeout:180];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|path|footway|steps|bridleway)$"]({BBOX});
  way["waterway"~"^(river|stream|canal|drain)$"]({BBOX});
  way["natural"="water"]({BBOX});
  way["landuse"="reservoir"]({BBOX});
  relation["natural"="water"]({BBOX});
  relation["landuse"="reservoir"]({BBOX});
  way["natural"="wood"]({BBOX});
  way["landuse"="forest"]({BBOX});
  way["landuse"="meadow"]({BBOX});
  way["natural"="glacier"]({BBOX});
  relation["natural"="glacier"]({BBOX});
  node["place"~"^(city|town|village|hamlet|suburb|neighbourhood|locality)$"]({BBOX});
  node["natural"="peak"]({BBOX});
  node["natural"="saddle"]({BBOX});
  node["natural"="valley"]({BBOX});
  node["natural"="ridge"]({BBOX});
  node["mountain_pass"="yes"]({BBOX});
  way["natural"="valley"]({BBOX});
  way["natural"="ridge"]({BBOX});
  node["tourism"="viewpoint"]({BBOX});
  node["historic"]({BBOX});
  way["natural"="water"]({BBOX});
  node["natural"="saddle"]({BBOX});
  node["tourism"~"^(attraction|viewpoint|alpine_hut)$"]({BBOX});
  node["amenity"="place_of_worship"]({BBOX});
);
out geom;
"""

# Overpass 镜像顺序 —— **先探活再定序**。
# 实测教训：某一轮 overpass-api.de 持续 504、kumi.systems 500、
# osm.jp 直接 SSL 证书域名不匹配（是硬失败，连重试都没意义），三个全灭。
# 换成 overpass.osm.ch 一次就通。→ 把稳定性好的瑞士站放首位，
# 并把 osm.jp 从列表里去掉（证书不匹配，不值得占一个轮次）。
MIRRORS = [
    "https://overpass.osm.ch/api/interpreter",         # 实测最稳
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",         # 主站，繁忙时 429/504
    "https://overpass.kumi.systems/api/interpreter",
]


def fetch(force=False, soft=False, rounds=2):
    """抓取并缓存。soft=True 时，全镜像失败返回空要素集而不是抛错。

    失败不是异常而是**可降级状态**：底图没矢量层也能看，噪点比断链好。
    """
    if CACHE.exists() and not force:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"命中缓存 {CACHE.name}：{len(d['elements'])} 个要素")
        return d

    print(f"窗口 {LON0}, {LAT0} → {LON1}, {LAT1}")
    last = None
    for rnd in range(rounds):                      # 多轮：每轮把镜像列表走一遍
        for host in MIRRORS:
            try:
                print(f"→ {host}（第 {rnd + 1} 轮）", flush=True)
                req = urllib.request.Request(
                    host,
                    data=urllib.parse.urlencode({"data": QUERY}).encode(),
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=200) as r:
                    d = json.loads(r.read().decode("utf-8"))
                n = len(d.get("elements", []))
                if n < 50:
                    raise RuntimeError(f"只返回 {n} 个要素，疑似被限流")
                CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                print(f"OK {n} 个要素，已缓存 {CACHE.name} "
                      f"({CACHE.stat().st_size // 1024} KB)")
                return d
            except Exception as e:                                   # noqa
                last = e
                print("  失败:", repr(e)[:120], flush=True)
                time.sleep(2 + 3 * rnd)

    # 全挂：写一份空缓存，让下游"有文件可读"，只是没有矢量。
    empty = {"elements": [], "_failed": repr(last)[:200]}
    CACHE.write_text(json.dumps(empty, ensure_ascii=False), encoding="utf-8")
    msg = f"全部镜像失败（{len(MIRRORS)} 个 × {rounds} 轮）：{last}"
    if soft:
        print("!! " + msg + "\n   —— 已按 --soft 降级：本轮不叠加 OSM 矢量，底图照常出。")
        return empty
    raise SystemExit(msg)


def summarize(d):
    from collections import Counter
    kind, names = Counter(), []
    for e in d["elements"]:
        t = e.get("tags", {})
        if "highway" in t:
            kind["路:" + t["highway"]] += 1
        elif "waterway" in t:
            kind["水:" + t["waterway"]] += 1
        elif t.get("natural") == "water" or t.get("landuse") == "reservoir":
            kind["水体"] += 1
            names.append(("水体", t.get("name", "(无名)"), ""))
        elif t.get("natural") == "wood" or t.get("landuse") == "forest":
            kind["林地"] += 1
        elif t.get("landuse") == "meadow":
            kind["草地"] += 1
        elif t.get("natural") == "glacier":
            kind["冰川"] += 1
            names.append(("冰川", t.get("name", "(无名)"), ""))
        elif "place" in t:
            kind["地名:" + t["place"]] += 1
            names.append(("地名", t.get("name", "?"), t.get("place")))
        elif t.get("natural") == "peak":
            kind["山峰"] += 1
            names.append(("山峰", t.get("name", "(无名)"), t.get("ele", "")))
        elif "tourism" in t:
            kind["景点:" + t["tourism"]] += 1
            names.append(("景点", t.get("name", "?"), t.get("tourism")))
        elif "amenity" in t:
            kind["寺庙/宗教"] += 1
            names.append(("寺", t.get("name", "?"), t.get("amenity")))
    print(f"\n共 {len(d['elements'])} 个要素：")
    for k, v in sorted(kind.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v}")
    return kind, names


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    ap.add_argument("--soft", action="store_true",
                    help="全镜像失败也算成功（流水线不中断，只是底图少一层矢量）")
    a = ap.parse_args()
    data = fetch(a.force, a.soft)
    if not data.get("elements"):
        print("\n（无矢量要素，跳过汇总）")
        sys.exit(0)
    _, names = summarize(data)
    print("\n具名要素（前 40）：")
    seen = set()
    for k, n, extra in names:
        if n in seen:
            continue
        seen.add(n)
        print(f"  {k:<4} {n}  {extra}")
        if len(seen) >= 40:
            break

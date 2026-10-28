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
    python fetch_osm.py --reuse         # 复用同级项目的 osm.json（差额补抓），同区域新线首选
    python fetch_osm.py --reuse 梅里外转攻略   # 指定 donor；--reuse-dry 只演练不写盘

**这一步是可选的**：抓不到只意味着底图上少一层道路/水系/地名，
地形晕渲与等高线照常出图。所以流水线默认以 `--soft` 调用它，
Overpass 集体抽风不该让整条流水线挂掉。
"""
import argparse, json, subprocess, sys, time, urllib.parse, urllib.request
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
def _query(bbox):
    """生成整条查询语句（bbox 形如 "lat0,lon0,lat1,lon1"）。

    ⚠ 2026-10-26 起是**函数**而不是模块常量：`--shard` 分片抓取要逐块换 bbox。
      老调用点不受影响 —— 下面仍有 `QUERY = _query(BBOX)`。
    """
    return f"""
[out:json][timeout:180];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|path|footway|steps|bridleway)$"]({bbox});
  way["waterway"~"^(river|stream|canal|drain)$"]({bbox});
  way["natural"="water"]({bbox});
  way["landuse"="reservoir"]({bbox});
  relation["natural"="water"]({bbox});
  relation["landuse"="reservoir"]({bbox});
  way["natural"="wood"]({bbox});
  way["landuse"="forest"]({bbox});
  way["landuse"="meadow"]({bbox});
  way["natural"="glacier"]({bbox});
  relation["natural"="glacier"]({bbox});
  node["place"~"^(city|town|village|hamlet|suburb|neighbourhood|locality)$"]({bbox});
  node["natural"="peak"]({bbox});
  node["natural"="saddle"]({bbox});
  node["natural"="valley"]({bbox});
  node["natural"="ridge"]({bbox});
  node["mountain_pass"="yes"]({bbox});
  way["natural"="valley"]({bbox});
  way["natural"="ridge"]({bbox});
  node["tourism"="viewpoint"]({bbox});
  node["historic"]({bbox});
  way["natural"="water"]({bbox});
  node["natural"="saddle"]({bbox});
  node["tourism"~"^(attraction|viewpoint|alpine_hut)$"]({bbox});
  node["amenity"="place_of_worship"]({bbox});
  way["boundary"="administrative"]["admin_level"="2"]({bbox});
  relation["boundary"="administrative"]["admin_level"="2"]({bbox});
);
out geom;
"""


QUERY = _query(BBOX)
# 国界（admin_level=2）2026-10-25 补：用户看珠峰东坡地图问「缺少国界」。
# 珠峰/洛子/马卡鲁全骑在中尼边界上，窗口里国界是所有地理要素里**信息量最大的那条线**
# （哪座山在境外、徒步终点离边界多远），但原 QUERY 只抓 highway/waterway/自然要素，
# boundary 一条都没抓 → 图上完全看不出这是边境。
# 只在 admin_level=2 抓，**不抓 4（省界）/6（地市界）**：省界在山区多沿山脊/河谷走，
# 画上去与等高线、步道混在一起，看图的人分不清哪条是山脊哪条是界线；
# 而国界是徒步攻略里唯一"必须知道"的行政界。
# way 与 relation 都要（国界两端常被切成 many way，整条也常组织成 relation）。

# 单独抓国界的查询（供 `--border` 用，见 fetch_borders()）。
# 不带 natural/highway 那些标签 —— 查询越小越快，Overpass 越不容易超时。
BORDER_QUERY = f"""
[out:json][timeout:90];
(
  way["boundary"="administrative"]["admin_level"="2"]({BBOX});
  relation["boundary"="administrative"]["admin_level"="2"]({BBOX});
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


def _el_extent(e):
    """单个要素的经纬度包围盒（node 用 lat/lon，way/relation 用 bounds 或 geometry）。"""
    if "lat" in e and "lon" in e:
        return e["lon"], e["lat"], e["lon"], e["lat"]
    b = e.get("bounds")
    if b:
        return b["minlon"], b["minlat"], b["maxlon"], b["maxlat"]
    c = e.get("center")
    if c:
        return c["lon"], c["lat"], c["lon"], c["lat"]
    g = e.get("geometry") or []
    if g:
        los = [p["lon"] for p in g]
        las = [p["lat"] for p in g]
        return min(los), min(las), max(los), max(las)
    return None


def _cache_covers(d):
    """缓存是否仍然覆盖当前窗口。

    为什么需要：缓存以"文件存在"为命中条件，但窗口是**从轨迹推的** ——
    改了 bbox_pad 或轨迹后，`make_all` 仍会命中旧缓存，于是底图扩了、
    矢量层却还停在旧范围（新扩出来的边缘没有道路/水系/峰名，且**全程不报错**）。
    判据：缓存的四边都要够到当前窗口（容差 2%，OSM 边缘本来就可能没要素）。
    空缓存（上次全部镜像失败的产物）一律判为"不覆盖"→ 自动重抓。
    """
    if d.get("_sharded") and (d.get("elements") or []):
        return True          # 分片抓取是按网格全覆盖做的，不必再靠元素范围判边
    els = d.get("elements") or []
    if not els:
        return False
    lons, lats = [], []
    for e in els:
        ex = _el_extent(e)
        if ex:
            lons += [ex[0], ex[2]]
            lats += [ex[1], ex[3]]
    if not lons:
        return False
    tx = (LON1 - LON0) * 0.02
    ty = (LAT1 - LAT0) * 0.02
    return (min(lons) <= LON0 + tx and max(lons) >= LON1 - tx and
            min(lats) <= LAT0 + ty and max(lats) >= LAT1 - ty)



def _route_cfg_osm(key, default=None):
    """从 route_def.CFG 读一个键（失败就用默认值）—— fetch_osm 不整体 import route_def。"""
    try:
        import importlib
        import sys as _sys
        _sys.path.insert(0, str(HERE))
        _rd = importlib.import_module("route_def")
        _v = (getattr(_rd, "CFG", None) or {}).get(key)
        return default if _v is None else _v
    except Exception:
        return default


def fetch(force=False, soft=False, rounds=2):
    """抓取并缓存。soft=True 时，全镜像失败返回空要素集而不是抛错。

    失败不是异常而是**可降级状态**：底图没矢量层也能看，噪点比断链好。
    """
    if CACHE.exists() and not force:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if _cache_covers(d):
            print(f"命中缓存 {CACHE.name}：{len(d['elements'])} 个要素")
            return d
        print(f"缓存未覆盖当前窗口（{len(d.get('elements') or [])} 个要素）→ 重抓 "
              f"{LON0:.4f}, {LAT0:.4f} → {LON1:.4f}, {LAT1:.4f}")

    print(f"窗口 {LON0}, {LAT0} → {LON1}, {LAT1}")
    last = None
    import time as _time
    _t0 = _time.time()
    _budget = float(_route_cfg_osm("osm_fetch_budget", 420))   # 本条线路的总抓取时限（秒）
    for rnd in range(rounds):                      # 多轮：每轮把镜像列表走一遍
        for host in MIRRORS:
            if _time.time() - _t0 > _budget:
                print(f"   ⚠ 抓取总时限 {_budget:.0f}s 已到，停止重试")
                last = last or RuntimeError("抓取总时限已到")
                break
            try:
                print(f"→ {host}（第 {rnd + 1} 轮）", flush=True)
                req = urllib.request.Request(
                    host,
                    data=urllib.parse.urlencode({"data": QUERY}).encode(),
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=90) as r:
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
        else:
            continue                      # 本轮镜像全部试完 → 下一轮
        break                             # 因总时限而跳出 → 结束重试

    # ⚠ 全挂时**绝不能覆盖已有缓存**（2026-10-24 批量重出实测踩到）：
    #   旧实现写一份空 osm.json，把原来好端端的矢量（几百 KB）冲掉，
    #   后续所有出图都变成"没矢量层的裸底图"，而且**全程不报错**。
    #   现在：缓存里还有非空要素 → 原样保留，失败信息另写 osm.json.failed；
    #   缓存本来就不存在（首建线路）→ 才写空缓存，让下游"有文件可读"。
    empty = {"elements": [], "_failed": repr(last)[:200]}
    keep = False
    if CACHE.exists():
        try:
            _old = json.loads(CACHE.read_text(encoding="utf-8"))
            keep = len(_old.get("elements") or []) > 0
        except Exception:
            keep = False
    if keep:
        try:
            (CACHE.parent / (CACHE.stem + ".failed")).write_text(
                json.dumps(empty, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        print(f"   ⚠ 抓取失败，**保留原缓存** {CACHE.name}"
              f"（{len(_old.get('elements') or [])} 个要素）；失败详情见 "
              f"{CACHE.stem}.failed")
    else:
        CACHE.write_text(json.dumps(empty, ensure_ascii=False), encoding="utf-8")
    msg = f"全部镜像失败（{len(MIRRORS)} 个 × {rounds} 轮）：{last}"
    if soft:
        print("!! " + msg + "\n   —— 已按 --soft 降级：本轮不叠加 OSM 矢量，底图照常出。")
        return empty
    raise SystemExit(msg)


def _trim_admin_members(el, pad=0.03):
    """把国家 relation 的 members 裁到窗口内（只留几何与窗口相交的）。

    为什么必须裁（2026-10-25 珠峰东坡实测）：
      国界在 OSM 上是「一个国家一个 relation」—— 中国 relation 270056 有 1663 个
      member（全国陆上边界 2.2 万 km），尼泊尔 184633 有 657 个。`out geom` 会把
      **整条边界所有成员**的几何全抓回来，即使窗口只有 50×20 km。
      实测 osm.json 从 2.96 MB 涨到 **10.01 MB**，而其中真正落在窗口里的只有 5+5 段。
      缓存是**按窗口**服务的（换窗口就重抓），留着全国边界的点毫无用处。
    """
    ms = []
    for m in el.get("members") or []:
        g = m.get("geometry") or []
        if not g:
            continue
        los = [p["lon"] for p in g if p.get("lon") is not None]
        las = [p["lat"] for p in g if p.get("lat") is not None]
        if not los:
            continue
        if (max(los) < LON0 - pad or min(los) > LON1 + pad or
                max(las) < LAT0 - pad or min(las) > LAT1 + pad):
            continue
        ms.append(m)
    out = dict(el)
    out["members"] = ms
    return out


def fetch_borders(soft=True):
    """只抓**国界**（admin_level=2）并合并进现有缓存，其余要素原样保留。

    为什么要单独一个入口（2026-10-25）：
      ① 老线路的 `osm.json` 里一条 boundary 都没有（QUERY 现在才加上），
         但整盘 `--force` 重抓代价极高 —— 竞态实测会撞 `osm_fetch_budget` 拿到残缺数据
         （珠峰东坡那次只回 500/1212 个要素，而 `_cache_covers()` 还判"覆盖"）。
      ② 国界是小查询（十几到几十条 way），几十秒就能回来，失败也不该动主缓存。

    ⚠ **不走 `MIRRORS` 的原顺序**：首位 overpass.osm.ch 实测**没有亚洲数据**，
       返回的是一个语法合法的空集。对整盘查询这无妨（`n < 50` 会判限流换下一个），
       但国界本来就可能只有十几条 —— 空集在这条查询上**判不出真假**，
       会把"镜像没数据"误当成"这里没有国界"。所以 osm.ch 排到最后。
    """
    order = [h for h in MIRRORS if "osm.ch" not in h] + \
            [h for h in MIRRORS if "osm.ch" in h]
    last = None
    for rnd in range(2):
        for host in order:
            try:
                print(f"→ 国界 {host}（第 {rnd + 1} 轮）", flush=True)
                req = urllib.request.Request(
                    host,
                    data=urllib.parse.urlencode({"data": BORDER_QUERY}).encode(),
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    bd = json.loads(r.read().decode("utf-8"))
                els = bd.get("elements") or []
                if not els:
                    raise RuntimeError("返回 0 条国界要素（可能该镜像无亚洲数据）")
                # 合并：按 (type, id) 去重，新抓的覆盖旧的同名要素
                cur = {"elements": []}
                if CACHE.exists():
                    try:
                        cur = json.loads(CACHE.read_text(encoding="utf-8"))
                    except Exception:
                        cur = {"elements": []}
                keep = {}
                for e in cur.get("elements") or []:
                    keep[(e.get("type"), e.get("id"))] = e
                for e in els:
                    if e.get("type") == "relation" and                             (e.get("tags") or {}).get("admin_level") == "2":
                        e = _trim_admin_members(e)
                    keep[(e.get("type"), e.get("id"))] = e
                cur["elements"] = list(keep.values())
                CACHE.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
                print(f"OK 国界 {len(els)} 个要素，已合并进 {CACHE.name}"
                      f"（共 {len(cur['elements'])} 个）")
                return els
            except Exception as e:                                   # noqa
                last = e
                print("  失败:", repr(e)[:110], flush=True)
                time.sleep(2 + 3 * rnd)
    msg = f"国界要素全部镜像失败：{last}"
    if soft:
        print("!! " + msg + "\n   —— 已降级：底图不画国界，其余不变。")
        return []
    raise SystemExit(msg)


# ============================================================ 分片抓取（2026-10-26）
# 为什么要有：公共 Overpass 实例对大 bbox 的"一次问全类目"查询，**要么 504，
# 要么更坏——超时后返回半截数据**。2026-10-24 EBC 实测：窗口 41×69 km 一次查回来
# 只有 542 个要素、`highway=path` 一条都没有、连珠峰节点都缺（`osm.json` 里
# `Everest` 0 命中），而**没有 remark、不报错**，`_cache_covers()` 照样判覆盖。
# 分片后单块 ≤0.08°（约 7×9 km）就能稳定返回；小块失败再二分，最小 0.02°。
# 每块落一个文件 → 断点续跑（一小时的抓取中断了不用从头来）。
SHARD_DIR = OUT / "osm_shards"
SHARD_TILE = 0.08        # 默认块边长（度）
SHARD_MIN = 0.02         # 最小块边长（度），到这一步仍失败就放弃该块


def _mirrors_for_window():
    """按窗口挑镜像 + 排序：**先能用的**。

    · `overpass.osm.ch` 只有欧洲数据，查亚洲会返回空/400，白耗一轮重试
      （`pitfalls-terrain §14`）→ 非欧洲窗口直接去掉；
    · ⚠ 2026-10-26 实测：kumi / private.coffee / osm.jp / openstreetmap.ru **全部连不上**
      （Bash 出口对这几个域名直接超时，75 s 白等）。所以欧洲窗口外把**主站排第一**，
      其余只当兜底 —— 原来 os.ch 排首位的顺序在亚洲会先白等一个必然失败的镜像。
    """
    eu = (LON0 >= -12 and LON1 <= 45 and LAT0 >= 34 and LAT1 <= 72)
    if eu:
        return MIRRORS
    others = [m for m in MIRRORS if "overpass.osm.ch" not in m]
    main = [m for m in others if "overpass-api.de" in m]
    rest = [m for m in others if m not in main]
    return main + rest


def _shard_file(b):
    return SHARD_DIR / ("shard_%.5f_%.5f_%.5f_%.5f.json" % (b[0], b[1], b[2], b[3]))


def _fetch_shard(bbox, attempts=3, timeout=120):
    """抓单块。0 要素是**合法结果**（纯雪山/无人区），不像大查询那样当失败。"""
    q = _query("%.5f,%.5f,%.5f,%.5f" % (bbox[1], bbox[0], bbox[3], bbox[2]))
    last = None
    for att in range(attempts):
        for host in _mirrors_for_window():
            try:
                req = urllib.request.Request(
                    host,
                    data=urllib.parse.urlencode({"data": q}).encode(),
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                rm = str(d.get("remark") or "")
                if "error" in rm.lower():
                    raise RuntimeError("remark: " + rm[:120])
                return d.get("elements") or []
            except Exception as e:                                # noqa
                last = e
                time.sleep(1.5 + 2 * att)
    raise RuntimeError(repr(last)[:160])


def _walk_shard(bbox, depth=0):
    """递归抓一块：成功就落盘；失败沿长边二分；到 SHARD_MIN 仍失败则放弃该块。"""
    p = _shard_file(bbox)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("_ok"):
                return d.get("elements") or []
        except Exception:                                         # noqa
            pass
    lon0, lat0, lon1, lat1 = bbox
    ind = "  " * (depth + 1)
    try:
        els = _fetch_shard(bbox)
    except Exception as e:                                        # noqa
        w, h = lon1 - lon0, lat1 - lat0
        if max(w, h) <= SHARD_MIN + 1e-9:
            print(f"{ind}✗ {lon0:.3f},{lat0:.3f} 块放弃：{e}")
            return []
        if w >= h:
            mid = (lon0 + lon1) / 2
            subs = [(lon0, lat0, mid, lat1), (mid, lat0, lon1, lat1)]
        else:
            mid = (lat0 + lat1) / 2
            subs = [(lon0, lat0, lon1, mid), (lon0, mid, lon1, lat1)]
        print(f"{ind}↘ {lon0:.3f},{lat0:.3f} 块失败（{e}）→ 二分")
        out = []
        for sb in subs:
            out += _walk_shard(sb, depth + 1)
        return out
    SHARD_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_ok": True, "bbox": list(bbox), "n": len(els),
                             "elements": els}, ensure_ascii=False), encoding="utf-8")
    print(f"{ind}✓ {lon0:.3f},{lat0:.3f} {len(els)} 要素")
    return els


def fetch_sharded(force=False, tile=None, soft=False, jobs=3):
    """按网格分片抓取并合并写 `osm.json`（带 `_sharded` 标记，供 `_cache_covers` 直接放行）。"""
    tile = float(tile or _route_cfg_osm("osm_shard_tile", SHARD_TILE))
    if CACHE.exists() and not force:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if d.get("_sharded") and d.get("elements"):
            print(f"命中分片缓存 {CACHE.name}：{len(d['elements'])} 个要素")
            return d
    print(f"分片抓取窗口 {LON0}, {LAT0} → {LON1}, {LAT1}（块 ≤ {tile}°）")
    grid, y = [], LAT0
    while y < LAT1 - 1e-9:
        y2 = min(y + tile, LAT1)
        x = LON0
        while x < LON1 - 1e-9:
            x2 = min(x + tile, LON1)
            grid.append((x, y, x2, y2))
            x = x2
        y = y2
    done = sum(1 for b in grid if _shard_file(b).exists())
    print(f"  共 {len(grid)} 块，已完成 {done} 块（断点续跑，重跑会跳过）")
    els, seen = [], set()
    t0 = time.time()
    # 并发：公共实例对同一 IP 的并发不做硬限制，但 3 路以上会开始 429，
    # 所以默认 3，失败靠 _fetch_shard 内部重试兜。结果顺序无关，合并时按 (type,id) 去重。
    from concurrent.futures import ThreadPoolExecutor
    jobs = max(1, min(int(jobs or 1), 4))

    def _one(i_b):
        i, b = i_b
        return b, _walk_shard(b)

    if jobs == 1:
        results = [_one((i, b)) for i, b in enumerate(grid, 1)]
    else:
        with ThreadPoolExecutor(jobs) as ex:
            results = list(ex.map(_one, list(enumerate(grid, 1))))
    for b, part in results:
        for e in part:
            k = (e.get("type"), e.get("id"))
            if k in seen:
                continue
            seen.add(k)
            els.append(e)
    print(f"  合并 {len(grid)} 块（{jobs} 路并发）")
    dt = time.time() - t0
    print(f"  合并完成：{len(els)} 个要素（本轮 {dt:.0f}s）")
    out = {"elements": els, "_sharded": True,
           "_bbox": [LON0, LAT0, LON1, LAT1], "_tile": tile}
    CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"已写 {CACHE.name}（{CACHE.stat().st_size // 1024} KB）")
    return out


# ============================================================ 邻项目复用（2026-10-28）
# 为什么要有：`osm.json` 是**按窗口**服务的项目级缓存，没有 DEM 那种瓦片层，
# 所以每开一条新线路都要整盘问一次 Overpass —— 实测同区域 34×44 km 窗口 6 分钟，
# 而其中绝大多数要素在隔壁项目里**早就抓过了**（贡嘎小环线 894 个要素 100% 命中大环线）。
#
# 做法：找同级项目的 osm.json，把它的要素裁到本窗口；盖不住的差额矩形再补抓。
#
# ⚠ 判"盖得住"不能用元素范围（`_extent_of`）：
#   横跨窗口的 way / 国界 relation 会带出**窗口外几百公里**的几何，元素范围远大于当初
#   查询的窗口。拿它当覆盖证明，就会以为邻居有数据、其实本窗口边缘从没被抓过 ——
#   静默缺一层矢量，且 `_cache_covers()` 也看不出来（它判的正是元素范围）。
#   → 权威口径是邻居项目**当前**的 `resolve_bbox()`（子进程 import 它自己的 route_def），
#     再与元素范围取交集（交集只会更小，方向安全）。
REUSE_MIN_ELEMS = 300      # **donor 门槛**：低于此判"残缺缓存"，不拿它当数据源
# **结果门槛**：只防"塌缩"（合并后几乎没东西），不用来判区域稀疏。
# 2026-10-28 金银山实测：窗口 16×31 km 真抓只有 170 个要素（贡嘎南麓无人区，
# OSM 本来就稀），而复用能给出 182 —— 拿 300 去卡结果会把更全的数据拒掉。
REUSE_MIN_MERGED = 100
REUSE_TOL = 0.02           # 与 _cache_covers 同口径的容差（相对窗口尺寸）
# ---- 覆盖率门槛（2026-10-28 用户定规则，实测定标）----
# ≥95%：差额 ≤5%、通常 0–1 个小矩形 → 复用 5 s~1 min，比整盘快一个数量级；
# <85%：差额 >15%，两块矩形在 Overpass 上是**两次固定开销**（实测 2m31s vs 整盘 3m09s），
#       绕这一圈没意义 → 直接整盘抓；
# 中间带（85–95%）：差额 5–15%，通常 1 个矩形，仍比整盘省 0.5–1.5 min，照常复用。
REUSE_COVER_MIN = 0.95
REUSE_COVER_HARD = 0.85


def _extent_of(d):
    """要素集合的经纬度包围盒（lon0, lat0, lon1, lat1）；空集返回 None。"""
    lo, la = [], []
    for e in (d.get("elements") or []):
        x = _el_extent(e)
        if x:
            lo += [x[0], x[2]]
            la += [x[1], x[3]]
    if not lo:
        return None
    return (min(lo), min(la), max(lo), max(la))


def _intersect(a, b):
    """两个 bbox 的交集；不相交返回 None。"""
    if not a or not b:
        return None
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if (r[0] < r[2] and r[1] < r[3]) else None


def _area(b):
    return 0.0 if not b else max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _subtract(a, s):
    """a − s（矩形差集）→ 最多 4 个矩形（南 / 北 / 西 / 东）。"""
    if not _intersect(a, s):
        return [a]
    alon0, alat0, alon1, alat1 = a
    slon0, slat0, slon1, slat1 = s
    out = []
    if slat0 > alat0:
        out.append((alon0, alat0, alon1, min(slat0, alat1)))
    if slat1 < alat1:
        out.append((alon0, max(slat1, alat0), alon1, alat1))
    m0, m1 = max(alat0, slat0), min(alat1, slat1)
    if m1 > m0:
        if slon0 > alon0:
            out.append((alon0, m0, min(slon0, alon1), m1))
        if slon1 < alon1:
            out.append((max(slon1, alon0), m0, alon1, m1))
    return [r for r in out if r[2] - r[0] > 1e-6 and r[3] - r[1] > 1e-6]


def _covers(outer, inner, tol=REUSE_TOL):
    """outer 是否包住 inner（tol = 相对 inner 尺寸的容差，与 _cache_covers 同口径）。"""
    if not outer or not inner:
        return False
    tx = (inner[2] - inner[0]) * tol
    ty = (inner[3] - inner[1]) * tol
    return (outer[0] <= inner[0] + tx and outer[1] <= inner[1] + ty and
            outer[2] >= inner[2] - tx and outer[3] >= inner[3] - ty)


def _claim_of(proj):
    """邻居项目**当前**的窗口（子进程 import 它自己的 route_def → resolve_bbox）。

    ⚠ 绝不 import 到本进程：两个项目的 route_def 同名，一旦进 sys.modules 就串味。
    """
    code = ("import sys;sys.path.insert(0,'scripts');import guide_common as GC;"
            "b=GC.resolve_bbox();print('%.6f,%.6f,%.6f,%.6f'%tuple(float(x) for x in b[:4]))")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(proj),
                           capture_output=True, text=True, timeout=180)
        line = (r.stdout or "").strip().splitlines()[-1]
        v = tuple(float(x) for x in line.split(","))
        return v if len(v) == 4 else None
    except Exception:                                             # noqa
        return None


def _donors(spec, min_elems):
    """列出可作 donor 的 osm.json：[(项目名, 项目目录, dict, 要素数)]。

    spec = "auto" 扫同级项目；也可给项目目录名或 osm.json 路径（想指定就写死）。
    """
    root = HERE.parent                        # <项目>/scripts → <项目>
    ws = root.parent                          # 工作区根（同级都是别的线路项目）
    if spec and str(spec).lower() not in ("auto", "1", "yes", "true"):
        p = Path(spec)
        if p.is_file():
            paths = [p]
        elif p.is_dir():
            paths = [p / "scripts" / "out" / "osm.json", p / "out" / "osm.json"]
        else:
            paths = [ws / str(spec) / "scripts" / "out" / "osm.json"]
    else:
        paths = sorted(ws.glob("*/scripts/out/osm.json"))
    out = []
    for p in paths:
        try:
            if not p.exists() or p.resolve() == CACHE.resolve():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                         # noqa
            continue
        n = len(d.get("elements") or [])
        if n < min_elems:
            print(f"   跳过 {p.parent.parent.parent.name}：只有 {n} 个要素"
                  f"（< {min_elems}，疑似残缺缓存）")
            continue
        out.append((p.parent.parent.parent.name, p.parent.parent.parent, d, n))
    out.sort(key=lambda t: -t[3])
    return out


def _clip(els, box, pad=REUSE_TOL):
    """只留与 box 相交（含 pad 余量）的要素 —— 邻居窗口比本窗口大，留着没用还拖慢渲染。"""
    if not box:
        return els
    bx0 = box[0] - (box[2] - box[0]) * pad
    bx1 = box[2] + (box[2] - box[0]) * pad
    by0 = box[1] - (box[3] - box[1]) * pad
    by1 = box[3] + (box[3] - box[1]) * pad
    keep = []
    for e in els:
        x = _el_extent(e)
        if not x:
            continue
        if x[2] < bx0 or x[0] > bx1 or x[3] < by0 or x[1] > by1:
            continue
        if e.get("type") == "relation" and (e.get("tags") or {}).get("admin_level") == "2":
            e = _trim_admin_members(e)
        keep.append(e)
    return keep


def _fetch_rect(bb, attempts=2, timeout=180):
    """补抓一个矩形差额。**0 个要素是合法结果**（纯雪山/无人区），不能当失败。

    bb = (lon0, lat0, lon1, lat1)
    """
    q = _query("%.5f,%.5f,%.5f,%.5f" % (bb[1], bb[0], bb[3], bb[2]))
    last = None
    for att in range(attempts):
        for host in _mirrors_for_window():
            try:
                req = urllib.request.Request(
                    host,
                    data=urllib.parse.urlencode({"data": q}).encode(),
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                rm = str(d.get("remark") or "")
                if "error" in rm.lower():
                    raise RuntimeError("remark: " + rm[:120])
                return d.get("elements") or []
            except Exception as e:                                # noqa
                last = e
                time.sleep(1.5 + 2 * att)
    raise RuntimeError(repr(last)[:160])


def _keep_old(soft, msg):
    """复用失败时的统一出口：保留原缓存（这是**唯一允许的降级**，绝不写半截数据）。"""
    print("   ✗ " + msg)
    if soft:
        print("   —— 已按 --soft 降级：沿用原缓存/无矢量层，底图照常出。")
        if CACHE.exists():
            try:
                return json.loads(CACHE.read_text(encoding="utf-8"))
            except Exception:                                     # noqa
                pass
        return {"elements": []}
    raise SystemExit(msg)


def reuse(spec="auto", force=False, dry=False, soft=False, min_elems=REUSE_MIN_ELEMS,
          min_cover=REUSE_COVER_MIN, cover_hard=REUSE_COVER_HARD):
    """复用邻项目 osm.json + 补抓差额，写完做**覆盖率硬校验**。

    三道闸门（任一不过 → 整体失败，绝不写半截数据）：
      ① 每个差额矩形都抓成功（返回 0 要素合法，但抓失败不合法）；
      ② 面积 ≥2% 窗口的差额矩形，返回要素数不能是 0 —— 那是"镜像超时给半截数据"的特征；
      ③ 合并结果必须通过 `_cache_covers()`（与正常抓取同一把尺子）。
    """
    mine = (LON0, LAT0, LON1, LAT1)
    if dry:
        force = True
    if CACHE.exists() and not force:
        try:
            d0 = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:                                         # noqa
            d0 = {"elements": []}
        if _cache_covers(d0):
            print(f"本线路缓存已覆盖当前窗口：{len(d0['elements'])} 个要素 → 无需复用")
            return d0

    print(f"邻项目复用：窗口 {LON0:.4f}, {LAT0:.4f} → {LON1:.4f}, {LAT1:.4f}")
    _t0 = time.time()
    best = None
    for name, proj, d, n in _donors(spec, min_elems):
        ext = _extent_of(d)
        # ⚠ 先做**零成本的矩形相交预筛**，再起子进程取邻居窗口：
        #   子进程要 import 它自己的 route_def + guide_common，单个 0.5 s；
        #   54 个项目全跑一遍就是 20+ 秒，而其中真正相交的通常只有 2–4 个。
        if not _intersect(ext, mine):
            print(f"   跳过 {name}：窗口不相交")
            continue
        claim = _claim_of(proj)
        covered = _intersect(claim, ext) if claim else None
        if not covered:
            print(f"   跳过 {name}：拿不到它自己的窗口，或要素范围为空")
            continue
        inter = _intersect(covered, mine)
        if not inter:
            print(f"   跳过 {name}：窗口不相交")
            continue
        full = _covers(covered, mine)
        gain = _area(inter) / max(1e-12, _area(mine))
        print(f"   {name}：{n} 个要素 · 覆盖本窗口 {gain * 100:.0f}%"
              + ("（完整覆盖 → 零请求）" if full else ""))
        if best is None or _area(inter) > best[3]:
            best = (name, proj, d, _area(inter), covered, full)
        if full:
            best = (name, proj, d, _area(inter), covered, full)
            break

    if not best:
        return _keep_old(soft, "没有可复用的邻项目缓存（同级目录里没有要素数达标的 osm.json）")

    print(f"   扫描 {time.time() - _t0:.1f}s")
    name, proj, d, _best_area, covered, full = best
    _cov = _area(_intersect(covered, mine)) / max(1e-12, _area(mine))
    if not full and _cov < float(cover_hard):
        msg = (f"最佳 donor「{name}」只覆盖本窗口 {_cov * 100:.0f}%"
               f"（< {float(cover_hard) * 100:.0f}%）→ 差额太大，复用不省时间，"
               f"改用整盘抓取（等效一次 Overpass 查询）")
        print("   " + msg)
        if dry:
            print("   （--reuse-dry：只演练，未真的抓取）")
            return {"elements": [], "_reuse_skipped": msg}
        return fetch(force=True, soft=soft)
    if not full and _cov < float(min_cover):
        print(f"   覆盖率 {_cov * 100:.0f}%（{float(min_cover) * 100:.0f}% 门槛以下）"
              f"→ 仍复用，但差额要补抓，预计比整盘省 0.5–2 min")
    els = _clip(d.get("elements") or [], mine)
    print(f"   取 {name} 的 {len(els)} 个要素（已裁到本窗口）")
    if not els:
        return _keep_old(soft, f"{name} 裁到本窗口后一个要素都不剩 → 不复用")

    todo = [] if full else [r for r in _subtract(mine, covered) if r]
    _need = sum(_area(r) for r in todo) / max(1e-12, _area(mine))
    if _need > 0:
        # 差额占比直接决定复用值不值：Overpass 的耗时主要由**每次查询的开销**决定
        # （排队 + 建连），不是按面积算。实测金银山（差额 21.6%、2 个矩形）2m31s，
        # 与整盘抓取 3m09s 几乎同量级 —— 差额一过 ~20% 就基本白忙。
        print(f"   差额合计占窗口 {_need * 100:.1f}%（{len(todo)} 个矩形）"
              + ("，预计与整盘抓取同量级" if _need > 0.2 else "，很快"))
    _t1 = time.time()
    got = []
    for r in todo:
        ra = _area(r) / max(1e-12, _area(mine))
        print(f"   补抓差额 {r[0]:.4f},{r[1]:.4f} → {r[2]:.4f},{r[3]:.4f}"
              f"（占窗口 {ra * 100:.1f}%）…", flush=True)
        try:
            part = _fetch_rect(r)
        except Exception as e:                                    # noqa
            return _keep_old(soft, f"差额矩形补抓失败：{e} → 放弃复用")
        print(f"     ✓ {len(part)} 个要素")
        if ra >= 0.02 and not part:
            return _keep_old(soft, f"差额矩形占 {ra * 100:.0f}% 窗口却返回 0 个要素 —— "
                                   f"镜像超时给半截数据的典型症状，拒绝落盘")
        got += part

    if todo:
        print(f"   差额补抓 {time.time() - _t1:.1f}s")
    keep = {}
    for e in els + got:                    # 补抓的在后 → 同 (type,id) 以新抓为准
        keep[(e.get("type"), e.get("id"))] = e
    merged = list(keep.values())
    out = {"elements": merged, "_reused_from": name, "_bbox": list(mine),
           "_rects": len(todo), "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    if len(merged) < REUSE_MIN_MERGED:
        return _keep_old(soft, f"合并后只有 {len(merged)} 个要素（< {REUSE_MIN_MERGED}），"
                               f"疑似塌缩/残缺")
    if len(merged) < min_elems:
        print(f"   （注：合并后 {len(merged)} 个要素，低于 donor 门槛 {min_elems} —— "
              f"多半是本窗口 OSM 本来就稀（高山区无路网），不是残缺；"
              f"不放心可 python fetch_osm.py --force 对照一次）")
    if not _cache_covers(out):
        return _keep_old(soft, "合并结果未通过 _cache_covers()（四边够不到本窗口）→ "
                               "请改用 python fetch_osm.py --shard")

    print(f"   ✓ 复用通过：{len(merged)} 个要素（{name} + {len(todo)} 个差额矩形）")
    if dry:
        print("   （--reuse-dry：只演练，未写缓存）")
        return out
    CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"已写 {CACHE.name}（{CACHE.stat().st_size // 1024} KB）")
    print("⚠ 缓存已改动 → 必须重跑 make_terrain.py（矢量叠加层烘焙在底图里）")
    return out


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
    ap.add_argument("--border", action="store_true",
                    help="只补抓国界（admin_level=2）并合并进现有缓存；"
                         "老线路加国界线用这个，不必整盘 --force")
    ap.add_argument("--shard", action="store_true",
                    help="按网格分片抓取：绕开公共实例大查询 504 / 半截数据（残缺 osm.json 的正解）")
    ap.add_argument("--tile", type=float, default=SHARD_TILE,
                    help=f"分片边长（度），默认 {SHARD_TILE}；越小越稳越慢")
    ap.add_argument("--jobs", type=int, default=3,
                    help="分片并发数（默认 3；公共实例 >3 开始 429）")
    ap.add_argument("--reuse", nargs="?", const="auto", default=None, metavar="[auto|项目名|路径]",
                    help="复用同级项目的 osm.json（auto = 自动找覆盖最大的邻居），"
                         "盖不住的差额矩形再补抓；三道闸门任一不过就保留原缓存不落盘")
    ap.add_argument("--reuse-dry", action="store_true",
                    help="只演练复用（打印会复用谁、补抓几块），不写缓存")
    ap.add_argument("--reuse-min-elems", type=int, default=REUSE_MIN_ELEMS,
                    help=f"donor 要素数下限，低于此判残缺不参与复用（默认 {REUSE_MIN_ELEMS}）")
    a = ap.parse_args()
    if a.border:
        els = fetch_borders(soft=True)
        print(f"\n国界要素 {len(els)} 个：")
        for e in els[:20]:
            t = e.get("tags", {})
            print(f"  {e['type']:<8} {e['id']:<12} {t.get('name') or t.get('name:en') or '(未命名)'}"
                  f"  {len(e.get('geometry') or [])} 点 / {len(e.get('members') or [])} 成员")
        print("\n⚠ 缓存已改动 → 必须重跑 make_terrain（底图叠加层在底图里烘焙）。")
        sys.exit(0)
    if a.reuse or a.reuse_dry:
        data = reuse(a.reuse or "auto", a.force, a.reuse_dry, a.soft, a.reuse_min_elems)
    elif a.shard:
        data = fetch_sharded(a.force, a.tile, a.soft, a.jobs)
    else:
        data = fetch(a.force, a.soft)
    if not data.get("elements"):
        print("\n（无矢量要素，跳过汇总）")
        sys.exit(0)
    if data.get("_reused_from"):
        print(f"\n（本篇来自邻项目复用：{data['_reused_from']} + {data.get('_rects', 0)} 个差额矩形）")
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

# hiking-route-guide-poster

把一条徒步/登山线路的资料（路线海报、KML/GPX 轨迹、口述数据，或用户指定的瓦片底图）做成可分享的攻略。

## 产出

- **自包含 HTML 攻略页**：数据条 → 全线地图（真实轨迹、按天分色）→ 海拔剖面图 → 逐日攻略卡 → 关键提示 → 数据说明/来源。
- **分享长图 PNG**：宽屏版（约 2340px 宽，微信/存档/打印通吃）。
  *手机阅读请直接看 HTML —— 它本身响应式（<900px 切单栏），不再另出手机版长图。*
- **高清全线地图 JPG**。
- **可导航 GPX**：全量 `<trkpt>` 轨迹点 + 具名 `<wpt>`，可导入手表/两步路。

## 核心理念

- **有真实轨迹就全程实线**，绝不画"示意虚线"——虚线只用于明确非徒步的搭车段。
- **自渲染地形底图**（本地 DEM 晕渲 + 等高线 + OSM 矢量），不依赖任何在线瓦片，无速率限制、无版权问题。
- **HTML 必须是响应式的**（<900px 切单栏）；手机端排版靠 CSS 断点解决，不靠另出一版窄长图。

## 安装

方式一（推荐，跟随更新）：

```bash
git clone <本仓库地址> ~/.workbuddy/skills/hiking-route-guide-poster
```

方式二（离线分发）：把 `hiking-route-guide-poster.zip` 解压到 `~/.workbuddy/skills/` 下即可。

> 项目级技能则放到 `<仓库>/.workbuddy/skills/`，与协作者共享。

## 快速流程

```bash
python scripts/preflight.py                 # ① ~10s 环境预检（numpy/PIL、浏览器、out/ 约定、KML 格式）
# ② 把 KML 路径登记进 scripts/route_def.py 的 CFG["kml"]（也可命令行直接传）
python scripts/prep_track.py 线路.kml       # ③ 解析 + 简化 + 等距剖面 → scripts/out/*.json
python scripts/new_route.py --name 线路名 --kml 线路.kml   # ④ ★ 自动生成 route_def.py（数字已填好，只留 ★ TODO）
#    --kml 每次都给：不给就按线路名猜 <线路名>.kml，猜错要等 ⑤ 的 prep 才报错
#    然后 grep "★ TODO" scripts/route_def.py —— 逐条把文案/水源/住宿/装备改写成真实内容
python scripts/make_all.py --dry-run        # ⑤ 先看 DAG 认没认对脚本
python scripts/make_all.py                  # ⑥ 一条命令跑全程（prep→osm→dem→html→long∥map→gpx→qa）
python scripts/make_all.py --skip-dem       #   改配色/标注时跳过地形，单轮 ~15s
```

用在线瓦片做底图时（B 路线）多一步 —— 瓦片只留线画，底色与等高线用本地 DEM 重出：

```bash
python scripts/build_base_map.py                       # 拼瓦片
python scripts/tile_tint.py --tile out/base_tile.jpg   # 线画分离 + DEM 自绘
python scripts/tile_tint.py --selftest                 # 合成图自检，不联网
```

没有轨迹、只有一张海报时走兜底的手绘模板：复制 `scripts/build_route_guide.py`，
只改它的 ②③④ 三个分区（线路几何 / 地形示意 / `DATA` 文案块）。

## 环境要求

- Python 3 + `numpy` + `Pillow`
- 一个无头浏览器用于渲染长图：Edge 或 Chrome（`--headless=new`）

> DEM 瓦片缓存在 `~/.workbuddy/cache/hiking-dem/`（**用户级、跨项目共享**，
> 用 `HIKING_DEM_CACHE` 可改到别的盘）。瓦片按 z/x/y 命名，天生就是全局唯一的，
> 所以同一片山区做第二条线路时直接命中缓存，不必重下几万块瓦片。

## 参考文档

`references/` 下按主题拆分，按需加载：地形渲染、配色体系、手机端排版、避坑清单、效能复盘等。

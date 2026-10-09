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
# ④ 按 out/ 里的数据填 scripts/route_def.py（里程/爬升/POIS/日程）—— 唯一需要改的文件
python scripts/make_all.py --dry-run        # ⑤ 先看 DAG 认没认对脚本
python scripts/make_all.py                  # ⑥ 一条命令跑全程（prep→osm→dem→html→long∥map→gpx→qa）
python scripts/make_all.py --skip-dem       #   改配色/标注时跳过地形，单轮 ~15s
```

## 环境要求

- Python 3 + `numpy` + `Pillow`
- 一个无头浏览器用于渲染长图：Edge 或 Chrome（`--headless=new`）

## 参考文档

`references/` 下按主题拆分，按需加载：地形渲染、配色体系、手机端排版、避坑清单、效能复盘等。

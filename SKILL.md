---
name: hiking-route-guide-poster
description: This skill should be used when the user wants a hiking/mountaineering route guide ("攻略"/"路书") built from a route poster, a KML/GPX track, hand-collected data, or a known XYZ tile service — producing a self-contained HTML guide page with a full-route map, elevation profile, and day-by-day breakdown, plus a long-image PNG for WeChat sharing and a navigable GPX export. Trigger on requests like "帮我做一份 XX 线路攻略", "要有全线地图、海拔剖面图、路途简况", "把这张路线图做成攻略长图", "用这个图层/瓦片做地图基础", "虚线部分也要实际轨迹", or "重装/徒步路书". Use it also when refining an existing guide's readability — "小路/步道看不清"、"配色太接近"、"轨迹线太粗"、"图例再丰富一点"、"等高线标注没了". Also use it to accurately extract numbers from a low-resolution route poster image before generating anything, and to parse a 2bulu (两步路) KML into a drawable track.
agent_created: true
---

# 徒步线路攻略长图（全线地图 + 海拔剖面 + 逐日卡）

## 用途

把一条徒步线路的资料（别人发的路线海报、KML/GPX 轨迹、口述数据、或用户指定的瓦片底图）做成三类交付物：

1. **自包含 HTML 攻略页**：顶部数据条 → 全线地图（SVG，真实轨迹 / 按天分色）→ 海拔剖面图（SVG）→ 逐日攻略卡 → 关键提示 →（可选）数据说明 + 底图与轨迹来源。
2. **分享用长图 PNG**：把上述页面渲染成 ~2340px 宽的竖版长图，可直接发微信群/朋友圈。
3. **可导航 GPX**（有真实轨迹时）：`<trkpt>` 全量轨迹点 + 具名 `<wpt>`，供导入手表/两步路。

交付物落盘到工作区一个子目录，例如 `<workspace>/<线路名>攻略/`，含

| 文件 | 规格 | 用途 |
|---|---|---|
| `XX-攻略.html` | 单文件、离线可开、**同时适配桌面与手机** | 电脑上读；手机直接看它 |
| `XX-攻略长图.png` | ~2340 宽 | 电脑看 / 存档 / 微信分享 |
| `XX-全线地图.jpg` | 底图原生宽（如 3008），真 1:1 | 放大看细节 |
| `XX.gpx` | 全量轨迹点 + 具名 wpt | 导航 |

> **只出一种长图（宽屏 ~2340）**。手机阅读直接看 HTML —— 它本身就是响应式的（<900px 切单栏）。
> 本技能**不再生成手机版长图**（用户明确要求），旧版那条 `860 宽` 的产物与 `--split long=desk,phone`
> 用法都已废弃，别再照抄。

**画完之后还有五件事最容易翻车**（都别等用户来提）：步道**色相**要和等高线错开、主线路线宽与点位符号要成比例、**图例样本必须由出图参数生成**、**文字灰阶要过 4.5:1**、**地图「三件套」（图例 / 指北针 / 比例尺）必须缩小并动态避让，不许压住轨迹**。
见 `references/legend-and-color.md` 与 `references/pitfalls-map-svg.md` §9。

## 三条地图路线，先选对

| | **C. 自渲染地形底图（首选）** | A. 手绘示意底图 | B. 在线瓦片底图 |
|---|---|---|---|
| 适用 | **默认走这条** | 只有海报/口述数据，什么都拿不到 | 用户点名要某个瓦片服务 |
| 数据 | 开放 DEM（SRTM/Copernicus）+ OSM 矢量 | 无 | 第三方瓦片站 |
| 坐标 | **必须换成真实经纬度** | 画布像素，按里程等比手摆 | **必须换成真实经纬度** |
| 依赖 | Python + numpy + Pillow，几分钟出图 | 无 | 瓦片站得活着、得给 key、得合规 |
| 风险 | 几乎为零 | 等高线易假 | **会挂站 / 被 WAF 拦 / 禁止批量下载** |
| 做法 | **`references/self-hosted-terrain.md`** | 见「生成流程」 | `references/tile-basemap.md` |

**路由规则**：
- 没有特别要求 → **走 C**。理由：不依赖任何外部服务（B 路线本项目的瓦片站是"先能下、后来整站 567"）、
  无速率限制、无版权问题、可无限次重出。一份攻略底图 ≈ 1300 张 z15 瓦片，本来就属于"批量下载"，
  用官方 OSM 瓦片站是违规的，自渲染才是正解。
- 用户点名"用这个瓦片/图层" → 走 B，并把 `scripts/tiles.py`、`scripts/build_base_map.py` 拷进工作区。
  **先探活**（`python tiles.py`），挂了就直接切 C。瓦片到手后别直接当底图用 ——
  它自带的等高线要么太淡要么太密、底色又是"一色米底"，用 `scripts/tile_tint.py`
  把它改成地形图管线（**瓦片只留线画，底色与等高线用本地 DEM 重出**）。
- 只有海报/口述数据、连经纬度都拿不到 → 走 A。

无论 A/B/C，**只要用了真实经纬度底图，线路与标注就必须换成真实经纬度**——
否则线路会飘到别处甚至压不到任何地形上（A 之外的核心风险都是**投影错位**）。
三处投影公式（DEM 渲染 / OSM 叠加 / SVG 层）**必须完全一致**。

## 轨迹来源：真实轨迹 > 拟合，永远优先要真实轨迹

**底图可以手绘，轨迹不可以。** 用户对线路走向的唯一要求是"能照着走"，所以：

| 优先级 | 来源 | 做法 | 图上线型 |
|---|---|---|---|
| **1（务必争取）** | 用户给的 KML/GPX，或两步路/六只脚等下载量高的公开轨迹 | 直接解析真实点串 | **全线实线**，不分虚实 |
| 2 | OSM 已测绘步道（Overpass 拉 `highway=path/footway/steps`） | 建图 + Dijkstra 串锚点 | 实线，图例注明"OSM 测绘" |
| 3（下策） | 只有海报里程/海拔，无任何轨迹 | Catmull-Rom 过控制点拟合 | 虚线，图例注明"走向示意" |

- **只要拿得到真实轨迹（哪怕要用户手动导出 KML），就不要用拟合虚线。** 判据：能拿到 `gx:Track` 或 GPX `trkpt` 就用它，找不到才退到 2、3。
- 用户明确要求"虚线部分也要实际轨迹"时，唯一的正解是**去要一份 KML/GPX**（两步路 App「轨迹 → 导出 → KML」即可），而不是把拟合线画得更像。
- 只要真实轨迹整段覆盖到，图上就不该再出现任何虚线图例——虚线会让人误以为那段是猜的。
- 两步路 KML 的解析见 `scripts/parse_track_kml.py`；轨迹预处理（简化 + 剖面）见 `scripts/prep_kml_track.py`，详见 `references/track-prep.md`。

## 关键前提：先把数据核准确，再动手画

**永远不要凭缩略图或低分辨率截图直接抄数字。** 路线海报里的海拔、逐日里程、垭口名极易误读（实测踩坑：`4829` 被读成 `4629`、`桃海子` 被读成 `姚海子`）。流程：

1. 用 Pillow 读原图尺寸，按比例裁剪出关键区块（顶部数据条 / 地图标注 / 海拔剖面 / 逐日卡片），**放大 2–4 倍**后逐块看图。
2. 提取：总里程、累计爬升/下降、命名垭口数、最高点及名称，以及每天的 距离/爬升/下降/用时/最高海拔/水源/营地。
3. **做一致性校验**（这是发现原图笔误的唯一手段）：
   - 各日距离之和 ≈ 总里程；各日爬升/下降之和 ≈ 总累计值；
   - 逐日"最高点" ≥ 该日起终点海拔；
   - 剖面图上各日峰值 = 该日"最高点"。
   - 校验中出现的矛盾（如某日最高点与剖面峰值不符）→ 取在图中出现两次的那个数值，并在交付物脚注说明。
4. 无法确定的数值不要编造：改成"以现场轨迹为准"，或在脚注标注来源。

提取清单与常见坑见 `references/data-extraction.md`。

### 行程划分（几天、在哪扎营）：地形拐点 + 网络检索双证

用户要"按 N 天分"或"把某天拆成两天"时，**不要只按等里程硬切，也不要只凭感觉选点**。两步走：

**① 先用本地轨迹找「地形拐点」定候选。** 逐 1 km 打印 `km / ele / 累计±`，找升降结构的自然分界。
判据：一天之内应尽量只有**一个主升降方向**；把「大爬升 + 大下降」与「长缓下」切开，比等分里程合理得多。
例子（南天山北线末段）：
```
72.2 → 94.0   21.80 km  +1023 / −1090   ← 翻托格腊苏达坂后一路下降
94.0 → 111.58 17.58 km   +117 /  −395   ← 骤转平缓出山
```
→ 94.0 km 就是天然拐点。而按等里程切（原为 39.38 km 一天）会把两种强度压在同一天。

**② 必须再去网络检索，验证这个点是不是公认宿营地，并拿到实操细节。** 本地轨迹给不出营地名、
水情、土壤（草地/乱石）这些信息，而公开攻略全都有。检索词模板：
`<线路名> 徒步 攻略 营地 <达坂名>`、`<线路名> 行程 Day6 Day7 营地`、`<线路名> 重装约伴`。
优先看：**8264 出行攻略的 7/9 日行程表**（逐日里程+升降+营地名的标准口径）、
**两步路 community 的实走游记**（有真实水情与突发情况）、**约伴帖**（含备用营地）。

**③ 交叉校验，口径吻合才落笔。** 本线实测（21.8 km / +1023 / −1090）对上公开攻略
（8264 商业队 19.1 km / +1170 / −1115；实走游记 19 km / +1041 / −1123）——
量级一致即确认选点正确；若差得远，说明候选点选错了，回第 ① 步。

**④ 把检索到的「实操细节」全部写进攻略**，这些是纸面轨迹永远没有的价值：
- **正式营地名**（采纳公开攻略的叫法，如「河边草坪营地 / 牧屋营地」），不要用自己编的「94 km 营地」；
- **水源真相**：本线该营地**无可直饮水源** —— 紧贴的夏特苏河是乳白色冰川融水、含沙砾，
  须在距营地 ~20 min 的支流山溪提前打满。这类"到了才发现没水"的坑必须前置告知；
- **过河纪律**：河水「早上小、下午湍急」→ 营地扎在河边正是为了次日一早过河；
- **难度排序**：公开攻略会直说"今天强度最大"「碎石坡易落石」等，比自己从等高线猜准得多。

⚠ 注意**口径差异**：公开攻略常是全程版（如 140 km 到玉湖 / 9 天），而手上是截取段（如 111.58 km 夏塔出山段）。
对不上不代表数据错，是路线范围不同 —— 页面上要明确标注差异，别硬套别人的天数。

## 生成流程

### 0. 真实轨迹流水线（拿到 KML/GPX 时走这条 · 推荐）

**整套流水线只有 8 个通用脚本 + 1 个线路文件**，全在 `scripts/` 里，拷进工作区即可用，
**不依赖任何"已生成的文件"**（不需要先跑过一版，也不需要手工搬中间产物）：

> ⚠ **拷贝脚本时，`route_def.py` 是唯一的线路文件，绝不能覆盖！**
> `scripts/` 里那份 `route_def.py` 是**示例骨架**（内容是"九华山南北穿越"）。
> 开工时用 `cp .../scripts/*.py 工作区/scripts/` 一次性拷齐是可以的，
> **但中途补拷/重拷脚本时只拷通用脚本**，否则会把已经写好的线路配置（标题、8 天分日、
> 20 个点位、全部文案）**静默冲成示例**——`make_all` 仍会跑完、QA 仍会通过，
> 只是交付物变成"九华山"（2026-10-10 狼塔 C+V 实测踩过，产物文件名都变了才被发现）。
> 判据：交付物文件名前缀 == `route_def.CFG["file_stem"]`，对不上就是被覆盖了。

| 步骤 | 脚本 | 产出（一律落 `scripts/out/`） |
|---|---|---|
| prep | `prep_track.py` | `track_full.json`（全量原始点）· `track_real.json`（简化点）· `profile_real.json`（等距剖面）· `kml_pois.json` |
| osm | `fetch_osm.py` | `osm.json`（OSM 矢量；**可选**，全镜像失败自动降级） |
| dem | `make_terrain.py` | `base_terrain.jpg` · `base_map.jpg` · `base_meta.json`（窗口 / 投影） |
| html | `build_guide.py` | `<file_stem>-攻略.html` |
| long | `shoot_guide.py` | `<file_stem>-攻略长图.png` |
| map | `render_map_hi.py` | `<file_stem>-全线地图.jpg` |
| gpx | `make_gpx.py` | `<file_stem>.gpx` |
| qa | `qa_guide.py` | 数值化自检（不产文件，只判成败） |

**线路相关的只有一个文件：`scripts/route_def.py`。** 其余 8 个脚本一句线路名都不认，
全部从 `route_def` 读（`CFG` 文案 / `POIS` 点位 / 里程爬升 / `CFG["kml"]`）。
换一条线路 = 改 `route_def.py` 一个文件，然后重跑一条命令。

`make_all.py` 的 DAG 分层（依赖表就在脚本里，改窗口 / 加步骤看这里）：

```
wave0:  prep                        （唯一的根：后面所有步的**地图窗口**都从它的轨迹推）
wave1:  osm ∥ gpx                   （窗口 = 轨迹包围盒；gpx 只读全量点与 route_def）
wave2:  dem                         （依赖 prep + osm）
wave3:  html                        （依赖 dem + prep）
wave4:  long ∥ map                  （都只吃 HTML，可并存两个浏览器进程）
wave5:  qa                          （依赖前两步产物）
```

**地图窗口（bbox）自动推导 —— 这是"画错山"的唯一防线。**
窗口由 `guide_common.resolve_bbox()` 从**轨迹包围盒 + 8% 留白**自动算（也可在
`route_def.CFG["bbox"]` 里显式覆盖）。`make_terrain.py` 与 `fetch_osm.py` 调**同一个函数**
取同一个窗口，因此不可能再出现"矢量与地形错位"。开工前 `assert_bbox_covers()` 会校验
轨迹是否落在窗口内。**窗口写死是历史最惨的一次翻车**：曾把上一座山的窗口留给新线路，
terrarium 老老实实把千里之外的另一座山渲了出来 —— 页面看起来"有山有水"，只是那不是你要走的山，
而且**全程不报错**。所以：**永远不要手写 bbox 常量**，让它从轨迹推。

**提速要点**（实测数据见 `references/efficiency.md`）：DEM 下载是唯一的分钟级瓶颈
（首次 ~2 min，之后走**用户级共享缓存** `~/.workbuddy/cache/hiking-dem/` 秒级 ——
瓦片按 z/x/y 命名、天生就是全局唯一的，所以缓存不该放在项目里，否则同一片山区
做第二条线路时一块都复用不上），已把 `load_dem(workers=24)` 拉满并发；
长图导出改成「先注入 JS 只量版式（≈0.5 s）→ 恰好开窗截一次 → 像素复量断言」，
不再"给足 7600 px 窗高盲截"；`make_all` 的分层并行只省 ~6–10 s，
**真正的省时来自"一条命令、不用人守着"**，别指望并行救那 2 min 的 DEM。


```bash
# ⓪ 环境预检（~10 s）：numpy/PIL、浏览器路径、out/ 路径约定、KML 格式 + route_def 校验
python scripts/preflight.py

# ⓪' 一条命令跑全程（DAG 分层并行；--dry-run 先看认没认对脚本）
python scripts/make_all.py --dry-run
python scripts/make_all.py

# 单独重跑某几步 / 跳过地形（改配色、改标注时用）
python scripts/make_all.py --only html,long,map
python scripts/make_all.py --skip-dem
```

手动分步（**排错时才用**，正常走 `make_all`；命令一律在 `scripts/` 目录下执行）：

```bash
python scripts/prep_track.py                 # KML/GPX 取 route_def.CFG["kml"]，也可命令行直接传
python scripts/new_route.py --days 2         # ★ 自动生成 route_def.py 骨架（数字全填好，文案留待办）
python scripts/fetch_osm.py --soft           # 抓不到只丢一层矢量，不中断
python scripts/make_terrain.py               # DEM 晕渲 + 等高线 → 叠 OSM → base_map.jpg
python scripts/build_guide.py                # → <file_stem>-攻略.html
python scripts/shoot_guide.py                # → <file_stem>-攻略长图.png（只宽屏一版）
python scripts/render_map_hi.py              # → <file_stem>-全线地图.jpg
python scripts/make_gpx.py                   # → <file_stem>.gpx
python scripts/qa_guide.py                   # 数值化自检
```

`new_route.py` 是「不用手抄数字」的关键一步：它读 `out/` 的三个 json，
把**总里程 / 爬升 / 海拔范围 / 分段建议 / POI 候选 / 剖面标注 / 时间表**直接算出来写进
`route_def.py`（覆盖前自动留 `.bak`），只把真正要人判断的（地名核实、水源住宿、装备提示、文案）
留成 `★ TODO`，并在结尾报"还剩 N 处待办"。所以新线路的流程是：

```bash
python scripts/prep_track.py <你的.kml|.gpx>   # ① 出 out/ 四个 json
python scripts/new_route.py --name 线路名 --kml <你的.kml|.gpx>
                                               # ② 生成 route_def.py（数字已填好）
# ③ 打开 route_def.py，把 ★ TODO 逐条改写（grep "★ TODO" 一眼看全）
python scripts/make_all.py                     # ④ 一条命令出四件交付物
```

> `--kml` 建议**每次都写**：不给时它按线路名猜 `<线路名>.kml`，猜错不会当场报错，
> 要等 ④ 的 `prep` 才炸成"KML 不存在"。写了 `--kml` 就一步到位（它也会顺手核对文件在不在）。

**B 路线（用户点名在线瓦片）多一步**：瓦片自带等高线要么太淡要么太密、底色又是"一色米底"，
用 `tile_tint.py` 把它改成地形图管线 —— **瓦片只留线画（步道/公路/文字/水体），
面积底色换成 DEM 分层设色 + 晕渲，等高线用本地 DEM 重画**：

```bash
python scripts/build_base_map.py               # 拼在线瓦片 → out/base_tile.jpg（或复用其输出）
python scripts/tile_tint.py --tile out/base_tile.jpg
python scripts/tile_tint.py --selftest         # 合成图自检：不联网也能验"抹得干不干净"
```

关键口径（见 `references/track-prep.md` 详述）：

- **里程基线用「原始累计」而非「平滑后」**：平滑只用于**画海拔线**，累计里程一定要用原始点串的 haversine 累计（本例原始 30.20 km 与作者所述 30 km 吻合，平滑后只剩 28.10 km，会把所有 POI 的 km 值带偏）。POI、分段点、时间表一律挂原始累计。
- **海拔用 11 点滑动平均**去 GPS 抖动，但**累计升降用 3 m 阈值**过滤噪声后统计（本例 +3081 / −3371 m）。
- **海拔分两个口径，别混用**（详见 `references/track-prep.md` §6）：剖面**曲线**用轨迹 GPS 高程
  （系统性偏低 20–50 m，价值在相对起伏）；**印出来的数字**（标注 / 关键点表 / 地名卡）用 DEM 高程，
  因为它才对齐景区公认海拔。页面的「数据说明」里要把这个口径写明。
  同理**累计爬升 ≠ 净升高**（净升 2598 m vs 阈值累计 3797 m，差在"上—下—上"的多级台阶），两个数都写清。
- **逐点时间从 `<when>` 取**（UTC→北京时间 +8h），用于生成"关键点时间表"，比按里程估算更快；**KML 里没有 `<when>` 就老实标「约 N h」并注明是估算**，别编钟点。
- **原始坐标保留 6 位小数、里程 4 位**，不要提前取整，取整会在地图上产生明显折角。
- 额外产出 **GPX**（`trkpt` + 具名 `wpt`）方便用户直接导入手表/两步路导航，图脚注要写"导航以 GPX 为准"。
  **航点不要直接搬 KML 注记**——里面混着「3550」「回望某某垭口」这类随手标注，导进手表是噪声；
  要用已按真实地名核验过的规范 POI 列表（`route_def.POIS`）。

**三种轨迹文件格式都要能进，`parse_track_kml.py` 按内容自动识别前两种**：

| 格式 | 特征 | 谁解析 |
|---|---|---|
| 两步路 `<gx:Track>` + `<gx:coord>` | 现代两步路导出，**带 `<when>` 时间戳** | `scripts/parse_track_kml.py`（主用） |
| **标准 GPX** `<trkpt lat lon>` + `<ele>/<time>` | 手表 / 其它 App 导出，可能多 `<trkseg>`、附 `<wpt>` 航点 | **同一个 `parse_track_kml.py`**（无需换脚本） |
| 多个 `<Placemark>/<LineString>/<coordinates>` | 旧版 / 分段导出，**无时间戳** | `parse_kml_ls.py`（见 `references/track-prep.md` §分段式 KML） |

拿到文件先数一遍再选脚本，**别假设是哪种**——选错了解析结果是 0 点，很容易误判成"文件坏了"：

```bash
grep -c "<gx:coord>"  线路.kml     # >0 → 两步路 KML
grep -c "<trkpt"      线路.gpx     # >0 → GPX（手表导出的基本上是这种）
grep -c "<LineString>" 线路.kml     # >0 → 分段式，走 parse_kml_ls.py
```

`preflight.py` 会自动做这个判断。分段式 KML 还有个附带问题：
**段与段之间可能有几百米的无记录断点**（本线 483 m），如实保留、不插值，GPX 里表现为多个 `<trkseg>`。

### 1. 手绘方案（**兜底**：完全拿不到轨迹时用）

⚠ 只有在"只有海报 / 口述数据、连经纬度都拿不到"时才走这条。**能拿到轨迹就走第 0 节**。

以 `scripts/build_route_guide.py` 为模板（一个**自包含、可跑**的骨架，内含地图 SVG、
剖面 SVG、CSS、逐日卡渲染；它**不在** `make_all` 的默认链里）。**先复制本文件**，再改三个分区：

| 分区 | 内容 | 要不要改 |
|---|---|---|
| ① 画布与配色 | `W/H`、分日色、强调色 | 一般不动 |
| ② 线路几何 | `S_PT/C1_PT/P_MAIN/E_PT` 关键点 + `D1_ROUTE…` 每日折线（1000×552 **画布坐标**，手摆） | ★ 必改 |
| ③ 地形示意 | `CHAINS` 山脊链（决定等高线质感与走向 `ROT`）、`RIVERS`、`LAKES` | ★ 必改 |
| ④ 逐日与文案 | `PROF` 剖面折线、`PEAKS_LBL/BOT_LBL` 标注、**`DATA` 一整块**（数据条/图例/逐日卡/提示/落款） | ★ 必改 |
| ⑤ 渲染引擎 | 出图逻辑 | 改版面时才动 |

两个最容易踩的点：

- **`DATA["stats"]` 是三元组** `(数值, 桌面副标题, 窄版副标题)`，窄版副标题**别把数值抄一遍** ——
  否则手机上会出现「12.0 km / 12.0 km」（版式自检查不出来，只能看图发现）。
- `PROF` 的峰值点必须与逐日卡的"最高点"对得上，`DAY_BOUNDS` 里的分日界会换算成剖面虚线位置。

```bash
python scripts/build_route_guide.py     # 输出到 OUT_DIR/XX-攻略.html（复制后先改 OUT_DIR）
```

跑完会打印「★ 待填 N 处」—— **交付前必须清零**，页面上会直接显示 ★ 开头的占位文字。
脚本只依赖标准库，输出单文件 HTML，字体走系统字体栈，**不引用任何 CDN**，离线可渲染。
**A 路线图上必须出现虚线**（走向示意），且图例要注明"走向示意 · 非导航用图" —— 这是它与第 0 节的分水岭。

### 2. 导出长图 PNG（只出宽屏一版）

用本机无头浏览器渲染，再**按像素量出版心边界**裁切。**本技能只出一种尺寸**：

| 用途 | 设计宽 | 缩放 | 成品 |
|---|---|---|---|
| 电脑看 / 存档 / 微信分享 | 1120 CSS | ×2 | 2344×H px |

```bash
python scripts/shoot_guide.py            # 宽屏长图；浏览器自动探测 Chrome → Edge → PATH
```

脚本零线路硬编码：设计宽 / 缩放 / 白名单之外全自动。要点：

- `--headless=new --force-device-scale-factor=2` → 2 倍图，微信里放大看文字仍清晰；
- **先量版式、再截一次**：注入 JS 量出 `.page` 的四边与文档总高（只排版不光栅，≈0.5 s），
  据此**恰好**开窗截一次，然后用像素复量断言量到的宽度 == 设计宽 × 倍率。
  旧做法"按窗宽反推裁剪区间"是错的 —— Windows 无头 Chrome 对窗口宽有**最小钳制**：
  `--window-size=430` 实测拿到 `clientWidth = 500`，`.page`（max-width 430 居中）
  因此落在 x = 35…465，而画布只有 430 → **右侧 35 px 整条被切，每行文字末尾都缺字**，
  且 `body` 与 `.page` 同底色时肉眼看不出是裁，极易误判成"换行不对"。
- **若量到的下边界顶到窗口底，就是窗高给小了**，必须调大重截，别静默截断；
- Chrome 不存在时改用 `msedge.exe`（同样支持 `--headless=new`）；都没有则只交付 HTML 并说明。

> **手机阅读怎么办**：直接看 HTML —— 它本身是响应式的（<900px 切单栏），
> 手机上打开满宽即可读，不需要再出一版窄长图。这也是为什么本技能把手机版长图**从流水线里彻底移除**
> （旧版那套"窄版另做一套排版 + 无头 Chrome 最小窗宽"的坑随之作废）。

### 3. 自检（必做）

⚠ **如果你看不到图（当前模型不支持图片），不要走"出图 → 看 → 改"这条路。**
本次实测：产了 11 张检查图却读不了，每张 5–15 s 加上来回切换全是白费。
直接把下面的每一条写成**数值断言**跑（切边扫描 / 标注越界与重叠 / 注入 JS 取版式 / 量实际字号），
让脚本把"看"变成"量"。详见 `references/efficiency.md` §3.6。

渲染后**务必回看图片**：把长图切成几块缩略图逐块确认，重点查

- **扫一遍左右边缘两列有没有深色像素**（`a[:,W-3:W-1].min() < 120`）——
  有就是被裁了（本例修前右边缘 46 行有字）；
- 地图地名标注是否互相压字（最常见问题，白色描边 halo 只能兜一部分）；
- 剖面端点标签是否与最右/最左的刻度数字撞在一起（「25430」），或被画布边缘裁掉；
- 逐日卡数字与剖面峰值是否一致；
- **把手机版存成 390 px 宽再看一眼**：满宽下正文是否真能读，有没有单字孤行、
  悬空的分隔符 `|`、卡片因换行而高低不齐、表格串列；
- **把所有文字色扫一遍对比度**（脚本见 `references/legend-and-color.md` §8.1），`< 4.5:1` 的报警。

发现问题回改坐标/锚点后再重渲染，不要交付没看过的图。

## 版式约定

- 版心 1120px，白卡片 + 暖灰底（`#FBF8F4` / `#EAE3D9`），浅色主题；深色文字，避免深底。
- 每天一个主题色，全篇统一：D1 橙红 `#E4572E`、D2 绿 `#1E9E76`、D3 紫 `#6C4FD8`、D4 蓝 `#2D7FF9`（多于 4 天按同族色延伸）。
- 地图必须带：指北针、比例尺（写明 km）、分日图例（含每日距离）、来源声明。声明口径随轨迹来源变：**真实轨迹写"路线为实测轨迹 · 非导航用图"**；拟合方案才写"路线走向示意 · 非导航用图"。
  **这三件套（图例 / 指北针 / 比例尺）必须缩小、并动态避让轨迹**：尺寸按 `map_svg.FURN_LG≈0.55`
  / `FURN_CP≈0.58` / `FURN_SB≈0.62` 缩小，落点交给 `guide_common.Placer` 求解
  （硬约束：不压轨迹、互不重叠；软避让：让开标注框与 POI 符号）。固定四角坐标硬摆是错的 ——
  轨迹一变就会压住，而"家具压轨迹"是读图错误，不是审美问题。
  ⚠ **`FURN_SB` 只能缩"字与内衬"，绝不能缩色条长度**（缩了"1 km"就不是 1 km 了，
  而且底衬框会窄于色条、色条戳出来 —— 见 `references/pitfalls-map-svg.md` §12）。
- 剖面图必须带：纵横轴标题（海拔 m / 累计里程 km）、分日竖虚线、命名垭口与营地/端点标注、最高点高亮。
- 底部**默认**写「数据说明」+「底图与轨迹来源」两块：来源（轨迹/海报/自测）、DEM 与 OSM 出处、口径（WGS84 / 累计里程 / 平滑与阈值）、地图不可导航、数值以现场为准。
  **但这两块属于"溯源交代"，是可选内容** —— 用户说「剔除来源/数据说明」「不要写来源」时，要整块删干净（含页头 `meta`、地图 panel 副标题、图例里的 `OSM 路网 / 轨迹实测` 等零散字样），
  只保留「非导航用图」这类**安全提示**（它不是溯源，是免责，别一起删）。删完记得重出长图（高度会明显变矮）。
  是否保留**默认提供**：先按默认做，用户没提就留着（多数人要溯源）；提了就一次删到位，别留半截。

## 常见坑

**不要通读全表** —— 按症状查分诊表，再跳专题文件。

| 症状 / 场景 | 去哪查 |
|---|---|
| **地图画出来是"别的山"、轨迹跑到画布外（bbox 写死）** | `guide_common.resolve_bbox()` + `assert_bbox_covers()`；本文档「生成流程 §0」的窗口段 |
| **解析出 0 个点（选错了入口）** | 本文档「生成流程 §0」的格式判断表；`preflight.py` 会替你数 gx:coord / trkpt / LineString。KML 与 **GPX 都走 `parse_track_kml.py`**，分段式 LineString 才走 `parse_kml_ls.py`。**`prep_track.py` 现已按文件内容自动识别**，正常流程不用手工挑脚本 |
| **长线（>100 km）渲底图特别慢 / 内存爆掉（几十分钟、十几 GB）** | `references/self-hosted-terrain.md` §2.1（`pick_dem_z()` 按窗口像素数自适应降档，z15→z13，25 分钟降到 1 分钟） |
| **`make_all` 跑完但交付物变成"别的线路名"（route_def 被示例覆盖）** | 本文档「生成流程 §0」的开头警示；重拷脚本时**只拷通用脚本，别碰 `route_def.py`** |
| **`prep_track.py` 报 `FileNotFoundError: '线路.kml'`，但文件明明在当前目录** | 已修（`prep_track.py` 末尾 `kml.resolve()`）。子进程以 `cwd=scripts/` 跑，相对路径会指错地方；传绝对路径也能绕开 |
| **指北针/比例尺/图例压住轨迹，或太大挡视线** | `scripts/map_svg.py` 的 `FURN_*` 系数 + `guide_common.Placer`；`references/pitfalls-map-svg.md` §9 |
| 底图取源、合规、瓦片站挂了 | `references/pitfalls-terrain.md` §1 |
| **山顶出现"没有等高线的平板/米色斑"（色带被截平 / 真峰顶被当空洞填掉）** | `references/pitfalls-terrain.md` §8（**判据必须单边偏低**，`abs()` 会把峰顶填平）+ §8b（色带取 p99.7 并把雪色端延到最高点） |
| **底图的地名/山峰名/湖泊名/水系名看不见、或几乎全丢** | `references/pitfalls-terrain.md` §11（字号按"缩图后"定 + Labeller 三级兜底 + 补湖名水系名 + 禁标网格别多乘 ss） |
| **>2 天线路整条被画成一种颜色（图例却是彩色的）** | `references/pitfalls-map-svg.md` §10（按 `day_bounds` + `days` 颜色画 N 段） |
| **图例里找不到图上的符号 / 图例占地过大** | `references/pitfalls-map-svg.md` §11（只列用到的 kind + 两列网格 + 复用 `mk_pin` 出样本） |
| **比例尺/家具显得"贴了张白标签"、色条戳出底衬** | `references/pitfalls-map-svg.md` §12（**色条长度不参与 FURN 缩放**，底衬=色条+内衬） |
| **文字有一圈"白色阴影"/发虚** | `references/pitfalls-map-svg.md` §13（描边宽度控制在字号的 11–15%，别删） |
| **底图"一片单色"、看不出高差（色带写死了）** | `references/pitfalls-terrain.md` §4 首条（**通用 ramp + p2–p98 拉伸**，无雪区不给雪色端） |
| **Overpass 镜像全体 504/500/证书错** | `references/pitfalls-terrain.md` §9（先探活再定序，osm.ch 首选；`fetch_osm.py --soft` 可降级） |
| **底图与攻略 POI 出现两条同地注记** | `references/pitfalls-terrain.md` §10（skip_names 要含「·」前核心地名） |
| **同一座山在页面上有两个海拔数字** | `references/track-prep.md` §6（轨迹 GPS 偏低 20–50 m → **曲线用 GPS、印出来的数用 DEM**） |
| **沙箱完全无外网（DEM/OSM 全超时）** | `references/self-hosted-terrain.md` §10（用 WebFetch 打 Open-Elevation 文本接口取真实 SRTM 样本，本地插值重建 DEM） |
| 放大看不清、字糊 | `references/pitfalls-terrain.md` §2 |
| 等高线糊成"棕色泥" / 密度不对 | `references/pitfalls-terrain.md` §3 |
| **瓦片等高线太密/太淡，或用户要适配等高距** | `references/tile-basemap.md` §7（抹除+DEM 自绘管线）、§11（100/500 m 攻略适配） |
| **等高线与地形对不上（位置错）** | `references/tile-basemap.md` §12（DEM→栅格映射核查清单） |
| **等高线/设色后地图"一个颜色"无地形感** | `references/tile-basemap.md` §11（线画分离设色法） |
| **用户质疑等高线数值对不对** | `references/tile-basemap.md` §14（POI 对比表 + 独立峰锚定 + 口径图注） |
| **高清图/底图细线被抹掉、发糊** | `references/tile-basemap.md` §8（两次重采样坑） |
| **换 zoom 后轨迹漂移/缩到一角** | `references/tile-basemap.md` §9（投影一致性）+ §11.6（比例尺 z 取 meta） |
| **地图画幅浪费、路线被挤小** | `references/tile-basemap.md` §10（bbox 贴线 + 边缘标签翻转） |
| **单轮迭代耗时过长** | `references/tile-basemap.md` §13、`references/efficiency.md`（本技能实测：DEM ~2 min，其余全部秒级） |
| **图像脚本突然崩（PIL/argv/IndexError）** | `references/efficiency.md` §6（本机环境三坑） |
| **Edge 截图不出文件** | `references/pitfalls-layout.md` 末节（--screenshot 需绝对路径） |
| 整张图发闷发脏、平原起云、林地硬边 | `references/pitfalls-terrain.md` §4 |
| **雪线怎么着色、用户要求"配色偏白"** | `references/pitfalls-terrain.md` §5 |
| **用户问"多少米有雪线"** | `references/pitfalls-terrain.md` §6 |
| 改色 / 改窗口后哪里要跟着改 | `references/pitfalls-terrain.md` §7 |
| 地图与右栏不等高、右栏被切一半 | `references/pitfalls-layout.md` §1 |
| 文案折行出孤字、表格溢出 | `references/pitfalls-layout.md` §2 |
| 文字灰阶对比度不够 | `references/pitfalls-layout.md` §3 |
| **某段文字只占左边一小条** | `references/pitfalls-layout.md` §5（类名撞车）|
| **行宽太长/太短** | `references/pitfalls-layout.md` §6 |
| 无头浏览器裁剪、版心量边 | `references/pitfalls-layout.md` §7 |
| 符号与线层级失衡、标注悬空 | `references/pitfalls-layout.md` §8 |
| 图例放哪、图例只有文字没图形 | `references/pitfalls-map-svg.md` §1–2 |
| **三件套（图例/指北针/比例尺）动态避让** | `references/pitfalls-map-svg.md` §9 |
| 剖面标签互压 | `references/pitfalls-map-svg.md` §5 |
| 虚实线用错、步道配色撞色 | `references/pitfalls-map-svg.md` §6–7 |
| 图例样本与出图配色不一致 | `references/pitfalls-map-svg.md` §4 |
| **做得慢、返工多、一小时还没完** | **`references/efficiency.md`**（耗时实测 + 提速做法） |
| **哪些步骤能并行、并行能省多少** | `references/efficiency.md` §3.7（DAG 分层 + 实测 + 别抱幻想） |
| **`route_def.py` 的里程/爬升不知从哪来、只会手抄** | `scripts/new_route.py`（读 `out/` 三个 json 自动生成骨架，文案留 `★ TODO`） |
| **`route_def.POIS` 写成字典就崩（`KeyError: 0`）** | `guide_common.poi()` / `pois_of()`：元组与字典都收；下游一律走它取值 |
| **B 路线：瓦片米底没地形感、自带等高线太密/太淡** | `scripts/tile_tint.py`（瓦片只留线画 + DEM 设色打底 + 自绘等高线）+ `references/tile-basemap.md` §7/§11 |
| **换个项目做同一片山，DEM 又要重下几万块瓦片** | 缓存已改**用户级共享** `~/.workbuddy/cache/hiking-dem/`（`HIKING_DEM_CACHE` 可覆盖）；瓦片按 z/x/y 命名，天生全局唯一 |
| **手机版缩略图标注轻微互压，被判成"失败"** | `map_svg.self_check` 是**两档制**：手机版的「标注重叠 / 压家具」只告警；硬指标是越界 / 压轨迹 / 家具互叠 |

### 最高频的 10 条（先记住这些）

1. **默认自渲染底图**（`fetch_osm.py` + `make_terrain.py`），不要等别人给瓦片：
   无速率限制、无版权风险、可无限重出。用户点名瓦片服务才走 B 路线。
2. **真实轨迹 > 拟合**：能拿到 KML/GPX 就用它，图上不该再出现任何虚线图例。
3. **地图窗口绝不写死**，一律 `resolve_bbox()` 从轨迹推；跑底图前先让
   `assert_bbox_covers()` 过一遍（窗口错了会静默画出另一座山）。
4. **只改 `route_def.py` 一个文件**；其余脚本（`prep_track / fetch_osm / make_terrain /
   build_guide / shoot_guide / render_map_hi / make_gpx / qa_guide`）全是通用的，不认线路名。
   连 `route_def.py` 本身也不用从零手抄 —— 先 `new_route.py` 生成骨架，再改文案。
   改完用 `make_all.py --only ...` 定点重跑。
5. **改任何一处配色/字号/窗口，都要问"下游还有谁用这个值"**：
   底图类改动（配色/窗口）→ 重跑 `make_terrain` → `build_guide` → `shoot_guide` → `render_map_hi`；
   仅 SVG 类改动（地图内字号/图例）→ 后三步。
6. **地图画布宽高比由窗口（bbox）决定**，所以「地图该多高」要在**底图阶段**算好，
   不要用 CSS `stretch` 事后补救（会产生死白，用户会说"上下区域很丑"）。
7. **CSS 类名先 grep 再用**：单文件长页面里 `.src` / `.hl` / `.nm` / `.el` / `.sub` / `.foot`
   极易撞车，症状是"某段文字只占左边一小条"。
8. **地图「三件套」要缩小 + 动态避让**：图例 / 指北针 / 比例尺由 `guide_common.Placer` 求解落点，
   **硬约束是不压轨迹、互不重叠**；尺寸按 `FURN_*` 系数缩小（图例 ≈0.78、指北针 ≈0.72）。
   别用固定四角坐标硬摆，轨迹一变就会压住。
9. **图例放进地图空白区**（先量轨迹分布找 0 点矩形），并做「桌面 `g.il` / 手机 `.rbox-lgm`」
   双份切换 —— 地图内图例在手机上必然不可读。
10. **同一张卡片里的"桌面副标题 / 手机副标题"要各写各的，别把数值抄进副标题**：
   数据条卡片常用 `span.donly` / `span.monly` 按断点二选一。若图省事把 `monly` 填成数值本身，
   桌面版正常、**手机版会出现"46.8 km / 46.8 km"这种数值重复**（版式自检查不出来，只能看图发现）。

11. **"图上一块平板 / 一条注记都没有"这类问题，先查"是不是被程序逻辑吃掉了"，而不是审美**：
   - 山顶平板 = 判据用了 `abs()` 把真峰顶当空洞填了，或色带只拉到 p98 把峰顶 clip 了
     （`references/pitfalls-terrain.md` §8 / §8b）；
   - 底图地名消失 = `Labeller` 撞上等高线标注就静默 `return False`
     （`references/pitfalls-terrain.md` §11）；
   - 分日颜色不对 = 只按 `split1` 画了两段（`references/pitfalls-map-svg.md` §10）。
   **三者都不会报错、自检也全绿**，唯一可靠的判据是"回看渲染结果 + 打印落位率/最高点"。

12. **底图与攻略的注记是两套系统，会互相压**：底图注记（OSM）画在栅格上，
   攻略 POI 标注画在 SVG 上且带白描边 —— 谁后在谁上就谁压谁。
   所以要让底图注记**知道**攻略标注的位置（`make_terrain.poi_label_cells()` → 禁标网格），
   而不是指望"两边各自避让"。
   → 手机副标题要么写短文案（"总里程 · 实测轨迹"），要么就不显示。

### 交付前自检清单

- [ ] **地图窗口盖住轨迹**：`make_terrain.py` 打印的窗口来自 `轨迹包围盒 + 8% 留白（自动）`，
      且 `assert_bbox_covers` 没抛错
- [ ] 长图**边缘深色像素 = 0**（无裁切；`qa_guide.py` 的 `edge_scan` 自动判）
- [ ] 高清地图四边无内容被截（满幅图片的边缘深色占比天然偏高，**要 1:1 目视裁切确认，别只看数字**）
- [ ] 地图与右栏**底边齐平**、右栏所有元素 `right ≤ 内容右界`（探针量，别目测）
- [ ] 地图内标注**桌面版两两冲突 = 0**、无标注压图例（`map_svg.self_check` 的「压轨迹点」应为 0）
- [ ] 手机版缩略图的标注重叠若不为 0，`self_check` 会明确标成"只告警"（拥挤 ≠ 错误）；
      **但桌面版必须为 0** —— 那是出长图的那一版
- [ ] **三件套不压轨迹、彼此不重叠**（`self_check` 的「家具互相重叠」应为 0）
- [ ] 用 `new_route.py` 生成的 route_def：`python route_def.py` 打印的「待办还剩 N 处」**已清零**
- [ ] **交付物文件名前缀 == `route_def.CFG["file_stem"]`**（对不上说明 `route_def.py` 被示例覆盖了）
- [ ] 图例**每一行样本图形都显示**（不只文字）
- [ ] 手机版无横向滚动、`g.il` 已隐藏、文字版图例正常
- [ ] 手机版卡片副标题**没有把数值重复一遍**（`monly` 别填成值本身）
- [ ] **底图高低海拔各裁一块 1:1 对比底色**：确认分层设色真的铺开了，
      而不是被 clip 卡在同一档（"一片单色"是静默失败，只看整图不容易发现）
- [ ] **最高峰没有被"填平"**：`fill_voids` 后 `max(dem)` 与 `load_dem` 打印的原始最高点一致；
      山顶是**有等高线、有渐变的雪帽**，不是一块没有线的平板
- [ ] **底图注记落位率**：`draw_osm` 打印的「山峰名 x/N」≥ 60%（正常 ~80%）；
      湖名/水系名也有（不只是峰名）
- [ ] **图例覆盖图上所有符号**：图上出现的每种 kind 都能在图例里找到样本，
      且图例没有溢出卡片（`_legend_size` 已含符号区行数）
- [ ] **比例尺色条完整落在底衬框内**（框宽 = 色条 + 两侧内衬；色条长度不参与 `FURN_*` 缩放）
- [ ] **文字描边宽度 ≈ 字号的 11–15%**：`< 10%` 在深色晕渲上读不出、`> 20%` 会糊成白阴影
- [ ] `make_terrain.py` 打印的 `抑制重名 N` 不为 0（为 0 = 重名注记还在图上）
- [ ] GPX 回读断言的 `trkpt` 数 == `track_full.json` 点数
- [ ] 所有数值口径统一（海拔取整一致、里程累计口径说明）

### 交付后：把经验回写进本技能

技能**本身就是一个 Git 仓库**，权威副本就在当前生效的目录：

```
C:/Users/<用户名>/.workbuddy/skills/hiking-route-guide-poster/     ← 改这里
├── .git/                    ← remote: github.com/.../hiking-route-guide-poster
├── SKILL.md
├── references/*.md
└── scripts/*.py
```

- **改完即刻生效**，下一次会话读到的就是新版，不需要额外的"安装/同步"步骤。
- 想留痕就**原地提交**（`.gitignore` 已排除 `out/`、`demcache/`、`__pycache__/`）：

  ```bash
  cd ~/.workbuddy/skills/hiking-route-guide-poster
  git status --short
  git add -A && git commit -m "hiking: <这次改了什么、为什么>"
  ```

- **`push` 属于对外动作，先问过用户再推**（别自动 push）。

**新增一条踩坑经验时**：先判断它属于哪类，写进对应文件，而不是都堆到 SKILL.md——

| 经验类型 | 写到哪 |
|---|---|
| 高频到必须记住 / 影响流程 | 本文档（保持精简，只放骨架与最高频项） |
| 底图 · 地形 · 配色 | `references/pitfalls-terrain.md` |
| 版式 · 排版 · CSS | `references/pitfalls-layout.md` |
| 地图 SVG · 图例 | `references/pitfalls-map-svg.md` |
| 轨迹解析 · 数据提取 | `references/track-prep.md` / `data-extraction.md` |
| 图例配色体系全景 | `references/legend-and-color.md` |
| **效能 · 返工 · 提速** | `references/efficiency.md` |

同时**在「常见坑」的分诊表里加一行症状 → 指向新章节**，否则下次找不到。

### 自测：怎么验证技能改完还能跑（`_selftest_*` 目录的做法）

改完脚本一定要做一次**全新目录自测**，别在已有产物上"看起来能跑"就收工：

```bash
# 1) 造一个干净目录：只放脚本 + KML + 一个只有 "kml" 字段的最简 route_def
#    （这一步专门验证"不依赖已生成文件"——route_def 的宽容加载就靠它兜）
# 2) python scripts/preflight.py            → 应为"[..] route_def 尚未填（首次运行正常）"，
#    且 ③ 区打印"[OK] 默认链 14 个脚本全部就位"、零 FAIL
#    （清单分两档：默认链缺一 ⇒ FAIL；按需脚本缺 ⇒ [warn]，如 parse_kml_ls / tile_tint）
# 3) python scripts/make_all.py --only prep → 应产出 out/ 四个 json（鸡生蛋已解）
# 3b) python scripts/new_route.py --name 线路名 --kml <线路.kml>
#     ★ 自动生成 route_def.py（数字已填好，只留 ★ TODO）
#     然后 python route_def.py 应打印「待办还剩 N 处」且 CFG 必填项缺失 = 无
# 4) cp 完整 route_def.py 进去 → python scripts/make_all.py
#    → 期望"全流程完成 ✓"；改过底图相关代码时务必看 make_terrain 的窗口行是否为
#      "轨迹包围盒 + 8% 留白（自动）"，而不是某个写死的经纬度
# 5) 改过解析器时补一条 GPX 通路回归：用 make_gpx 出的 GPX 反喂 parse_track_kml.py，
#    trkpt 数应 == 源 track_full.json 点数（证明 GPX 与 KML 两条入口的 schema 一致）
"$PY" scripts/parse_track_kml.py 峨眉山全景大环线.gpx --out /tmp/rt && \
  python -c "import json;a=json.load(open('out/track_full.json',encoding='utf-8'));\
             b=json.load(open('/tmp/rt/track_full.json',encoding='utf-8'));\
             assert len(a['pts'])==len(b['pts']);print('GPX 通路 OK', len(b['pts']))"
# 6) 改过 B 路线（tile_tint.py）时：python scripts/tile_tint.py --selftest
#    → 合成图自检，断言"等高线抹掉 / 步道保留 / 底色换成设色"三件事，不联网也能跑
```

判据（本次实测全部满足）：

| 检查 | 期望 |
|---|---|
| `preflight.py` ③ 区 | `[OK] 默认链 14 个脚本全部就位`；按需脚本缺失只出 `[warn]`，不 FAIL |
| 全新目录首跑总耗时 | ~150 s（DEM 缓存命中）/ ~280 s（冷缓存） |
| `make_terrain` 打印的窗口来源 | `轨迹包围盒 + 8% 留白（自动）` |
| `map_svg.self_check`（桌面版） | 越界 / 两两重叠 / **压轨迹点** / **家具互相重叠** / 标注压家具 全 0 |
| `map_svg.self_check`（手机版） | 硬指标同上仍为 0；**两两重叠 / 压家具可能 > 0，只告警**（缩略图的拥挤不是错误） |
| `render_map_hi` | 打印「最白行 / 最白列」且都 < 60%（无被裁白带） |
| `new_route.py` 生成的 route_def | `python route_def.py` 打印 POIS/MARKS/SCHEDULE 数量合理、CFG 无缺项 |
| `tile_tint.py --selftest` | 抹除率 > 95% 且步道保留率 > 95%（g>90 判据生效）|
| `qa_guide.py` | 结论：全部通过 ✓ |
| 交付物 | HTML + 长图 + 高清地图 + GPX 四件，字节数与改前一致（可复现） |

自测留下的临时目录（`_selftest_*`、`_chk/`、`out_html/`）确认完就删掉，别留在工作区里。
DEM 缓存现在在 `~/.workbuddy/cache/hiking-dem/`，**不要删** —— 它是跨项目的资产。


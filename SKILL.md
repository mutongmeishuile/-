---
name: hiking-route-guide-poster
description: This skill should be used when the user wants a hiking/mountaineering route guide ("攻略"/"路书") built from a route poster, a KML/GPX track, hand-collected data, or a known XYZ tile service — producing a self-contained HTML guide page with a full-route map, elevation profile, and day-by-day breakdown, plus a long-image PNG for WeChat sharing and a navigable GPX export. Trigger on requests like "帮我做一份 XX 线路攻略", "要有全线地图、海拔剖面图、路途简况", "把这张路线图做成攻略长图", "用这个图层/瓦片做地图基础", "虚线部分也要实际轨迹", or "重装/徒步路书". Use it also when refining an existing guide's readability — "小路/步道看不清"、"配色太接近"、"轨迹线太粗"、"图例再丰富一点"、"等高线标注没了". Also use it to accurately extract numbers from a low-resolution route poster image before generating anything, and to parse a 2bulu (两步路) KML into a drawable track.
agent_created: true
---

# 徒步线路攻略长图（全线地图 + 海拔剖面 + 逐日卡）

## 用途

把一条徒步线路的资料（别人发的路线海报、KML/GPX 轨迹、口述数据、或用户指定的瓦片底图）做成两类交付物：

1. **自包含 HTML 攻略页**：顶部数据条 → 全线地图（SVG，真实轨迹 / 按天分色）→ 海拔剖面图（SVG）→ 逐日攻略卡 → 关键提示 →（可选）数据说明 + 底图与轨迹来源。
2. **分享用长图 PNG**：把上述页面渲染成 2300px 左右宽的竖版长图，可直接发微信群/朋友圈。
3. **可导航 GPX**（有真实轨迹时）：`<trkpt>` 全量轨迹点 + 具名 `<wpt>`，供导入手表/两步路。

交付物落盘到工作区一个子目录，例如 `<workspace>/<线路名>攻略/`，含

| 文件 | 规格 | 用途 |
|---|---|---|
| `XX-攻略.html` | 单文件、离线可开、**同时适配桌面与手机** | 电脑上读 |
| `XX-攻略长图.png` | ~2340 宽 | 电脑看 / 存档 |
| `XX-攻略长图-手机版.png` | ~860 宽（430 CSS × 2） | **微信直发**（满宽即可读） |
| `XX-全线地图.jpg` | 底图原生宽（如 3008），真 1:1 | 放大看细节 |
| `XX.gpx` | 全量轨迹点 + 具名 wpt | 导航 |

**画完之后还有六件事最容易翻车**（都别等用户来提）：步道**色相**要和等高线错开、主线路线宽与点位符号要成比例、**图例样本必须由出图参数生成**、**文字灰阶要过 4.5:1**、**必须另出一版手机长图且必须按像素量版心边界裁**（无头 Chrome 有最小窗宽，按窗宽算必切字）、**窄版要另做一套排版**（孤字 / 悬空分隔符 / 卡片不等高）。
见 `references/legend-and-color.md`。

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
  **先探活**（`python tiles.py`），挂了就直接切 C。
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

四步脚本串起来，全程真实经纬度，产出**无虚线**的全线轨迹图。

⚠ **开工前先跑 `preflight`、并把整条链写成 `make_all.py`（带 `--skip-dem`）**：
本次实测单步最慢的只有 DEM 下载（~8 min，一次性）和 `make_terrain`（~2 min），
下游出 HTML <1 s、长图 7 s、高清地图 5 s —— **慢的不是计算，是返工轮次**。
没有预检和一键编排时，45 min 里有大半耗在"手敲命令 + 环境踩坑 + 等底图重跑"上。
详见 `references/efficiency.md`。

```bash
# ⓪ 环境预检（30 s）：numpy/PIL、浏览器路径、out/ 路径约定、KML 格式探测
python scripts/preflight.py

# ① 解析两步路 KML（或 GPX）→ track_full.json + kml_pois.json
python scripts/parse_track_kml.py "D:/路径/线路.kml"

# ② 简化 + 生成等距剖面 → track_real.json + profile_real.json
#    （把上一步的 track_full.json 放到脚本同目录再运行）
python scripts/prep_kml_track.py

# ③ 按 ② 的数据改 route_def.py（TRACK/TRACK_KM/TOTAL_KM/ASC/DESC + POIS/MARKS/SCHEDULE）
#    改 map_svg.py 只用一条实线画 TRACK，不再有 D1/D2 虚实之分

# ④ 自渲染底图（推荐，三条命令，全部可重复执行）
python scripts/fetch_osm.py --force     # Overpass 抓 OSM 矢量 → out/osm.json（改了 bbox 才需 --force）
python scripts/make_terrain.py          # DEM 晕渲+等高线 → out/base_terrain.jpg → 叠 OSM → out/base_map.jpg
python scripts/build_base_map.py        # 仅当要走在线瓦片（B 路线）时才用，会覆盖 base_map.jpg

# ⑤ 出 HTML（改 build_jiuhua.py 顶部数据区）→ <线路>攻略/<线路>-攻略.html
python build_jiuhua.py

# ⑥ 渲染分享长图 → <线路>攻略/<线路>-攻略长图.png
python render_jiuhua.py
```

关键口径（见 `references/track-prep.md` 详述）：

- **里程基线用「原始累计」而非「平滑后」**：平滑只用于**画海拔线**，累计里程一定要用原始点串的 haversine 累计（本例原始 30.20 km 与作者所述 30 km 吻合，平滑后只剩 28.10 km，会把所有 POI 的 km 值带偏）。POI、分段点、时间表一律挂原始累计。
- **海拔用 11 点滑动平均**去 GPS 抖动，但**累计升降用 3 m 阈值**过滤噪声后统计（本例 +3081 / −3371 m）。
- **逐点时间从 `<when>` 取**（UTC→北京时间 +8h），用于生成"关键点时间表"，比按里程估算更快；**KML 里没有 `<when>` 就老实标「约 N h」并注明是估算**，别编钟点。
- **原始坐标保留 6 位小数、里程 4 位**，不要提前取整，取整会在地图上产生明显折角。
- 额外产出 **GPX**（`trkpt` + 具名 `wpt`）方便用户直接导入手表/两步路导航，图脚注要写"导航以 GPX 为准"。
  **航点不要直接搬 KML 注记**——里面混着「3550」「回望某某垭口」这类随手标注，导进手表是噪声；
  要用已按真实地名核验过的规范 POI 列表（`route_def.POIS`）。

**两种 KML 格式都要能解析（`parse_track_kml.py` 只认其中一种）**：

| 格式 | 特征 | 解析脚本 |
|---|---|---|
| `<gx:Track>` + `<gx:coord>` | 现代两步路导出，**带 `<when>` 时间戳** | `scripts/parse_track_kml.py` |
| 多个 `<Placemark>/<LineString>/<coordinates>` | 旧版 / 分段导出，**无时间戳** | `parse_kml_ls.py`（见 `references/track-prep.md` §分段式 KML） |

拿到文件先 `grep -c "gx:coord"` 与 `grep -c "<LineString>"` 各数一遍，**别假设是哪种**——
选错了解析结果是 0 点，很容易误判成"文件坏了"。分段式 KML 还有个附带问题：
**段与段之间可能有几百米的无记录断点**（本线 483 m），如实保留、不插值，GPX 里表现为多个 `<trkseg>`。

### 1. 生成 HTML（手绘方案，仅在完全拿不到轨迹时用）

以 `scripts/build_route_guide.py` 为模板（已跑通党岭拉东线一版的完整脚本，内含地图 SVG、剖面 SVG、CSS、逐日卡渲染）。按线路替换文件顶部的数据区：

- `S_PT / C1_PT … / E_PT`：地图画布（1000×552）上的关键点坐标，按"实走方向在图上怎么走"手摆，不必是真实经纬度。
- `D1_ROUTE … D4_ROUTE`：每天路段折线点，插入起伏让线自然（原图有大量之字弯，照抄走向）。
- `CHAINS`：等高线背景的山脊链（沿 NE-SW 走向），只影响质感。
- `RIVERS / LAKES`：水系与海子位置。
- `PROF / PEAKS_LBL / BOT_LBL`：剖面折线点与标注点，`x` 用累计公里、`y` 用海拔米；**峰值点必须来自第 2 步的逐日数据**。
- `DAY_CARDS`：逐日卡片文案。
- 顶部 `stats`、`notes`、`plan`、`foot` 按线路改写。

```bash
python scripts/build_route_guide.py     # 输出 <输出目录>/XX-攻略.html
```

脚本只依赖标准库，输出单文件 HTML，字体走系统字体栈，**不引用任何 CDN**，离线可渲染。

### 2. 导出长图 PNG（两条流水线共用）

用本机 Chrome 无头模式渲染，再**按像素量出版心边界**裁切。**同一份 HTML 出两个尺寸**：

| 用途 | 窗宽 | 成品 | 触发 |
|---|---|---|---|
| 宽屏（电脑看 / 存档） | `1172,H` | 2344×H px | 桌面 CSS，保留版心外的纸面外框 |
| **手机（微信直发）** | `600,H` | 860×H px | `@media(max-width:900px)` 单栏 CSS，裁到版心、全出血 |

```bash
python scripts/shoot_long_png.py           # 两版一起出
```

需要改的只有三处：`ROOT`（输出目录）、`HTML`（目标 html）、`CHROME`（本机 Chrome 路径）。要点：

- `--headless=new --force-device-scale-factor=2` → 2 倍图，微信里放大看文字仍清晰；
- **别用窗宽反推裁剪区间**：Windows 无头 Chrome 对窗口宽有**最小钳制** ——
  `--window-size=430` 实测拿到的是 `clientWidth = 500`，`.page`（max-width 430，居中）
  因此落在 x = 35…465，而画布只有 430 → **右侧 35 px 整条被切，每行文字末尾都缺字**，
  且 `body` 与 `.page` 同底色时肉眼看不出是裁，极易误判成"换行不对"。
  脚本的做法是：临时给 `<body>` 注入一个与版心不同的底色 → 量出版心的左右/上下边再裁，
  并断言量到的宽度 == 设计宽 × 倍率。原理与完整代码见 `references/legend-and-color.md` §8.2；
- 窗高先给足（宽屏 4800、手机 7600）；**若量到的下边界顶到窗口底，就是窗高给小了**，
  必须调大重截，别静默截断；
- Chrome 不存在时改用 `msedge.exe`（同样支持 `--headless=new`）；都没有则只交付 HTML 并说明。

**为什么宽屏长图必须再配一版手机长图**：2344 px 宽的长图在 390 px 手机上缩放比只有 0.166，
12.8 px 的正文落到屏幕上 2 px，整块右栏等于灰噪。详见 `references/legend-and-color.md` §8.2。

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
- 剖面图必须带：纵横轴标题（海拔 m / 累计里程 km）、分日竖虚线、命名垭口与营地/端点标注、最高点高亮。
- 底部**默认**写「数据说明」+「底图与轨迹来源」两块：来源（轨迹/海报/自测）、DEM 与 OSM 出处、口径（WGS84 / 累计里程 / 平滑与阈值）、地图不可导航、数值以现场为准。
  **但这两块属于"溯源交代"，是可选内容** —— 用户说「剔除来源/数据说明」「不要写来源」时，要整块删干净（含页头 `meta`、地图 panel 副标题、图例里的 `OSM 路网 / 轨迹实测` 等零散字样），
  只保留「非导航用图」这类**安全提示**（它不是溯源，是免责，别一起删）。删完记得重出长图（高度会明显变矮）。
  是否保留**默认提供**：先按默认做，用户没提就留着（多数人要溯源）；提了就一次删到位，别留半截。

## 常见坑

**不要通读全表** —— 按症状查分诊表，再跳专题文件。

| 症状 / 场景 | 去哪查 |
|---|---|
| 底图取源、合规、瓦片站挂了 | `references/pitfalls-terrain.md` §1 |
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
| **单轮迭代耗时过长** | `references/tile-basemap.md` §13（性能复盘：冒烟测试先行 / 向量化 / 探针复用） |
| **图像脚本突然崩（PIL/argv/IndexError）** | `references/efficiency.md` §6（本机环境三坑） |
| **Edge 截图不出文件** | `references/pitfalls-layout.md` 末节（--screenshot 需绝对路径） |
| 整张图发闷发脏、平原起云、林地硬边 | `references/pitfalls-terrain.md` §4 |
| **雪线怎么着色、用户要求"配色偏白"** | `references/pitfalls-terrain.md` §5 |
| **用户问"多少米有雪线"** | `references/pitfalls-terrain.md` §6 |
| 改色 / 改 bbox 后哪里要跟着改 | `references/pitfalls-terrain.md` §7 |
| 地图与右栏不等高、右栏被切一半 | `references/pitfalls-layout.md` §1 |
| 文案折行出孤字、表格溢出 | `references/pitfalls-layout.md` §2 |
| 文字灰阶对比度不够 | `references/pitfalls-layout.md` §3 |
| **某段文字只占左边一小条** | `references/pitfalls-layout.md` §5（类名撞车）|
| **行宽太长/太短** | `references/pitfalls-layout.md` §6 |
| 手机长图、窄版排版、无头 Chrome 裁剪 | `references/pitfalls-layout.md` §7 |
| 符号与线层级失衡、标注悬空 | `references/pitfalls-layout.md` §8 |
| 图例放哪、图例只有文字没图形 | `references/pitfalls-map-svg.md` §1–2 |
| 图例手机版看不清 | `references/pitfalls-map-svg.md` §3 |
| 剖面标签互压 | `references/pitfalls-map-svg.md` §5 |
| 虚实线用错、步道配色撞色 | `references/pitfalls-map-svg.md` §6–7 |
| 图例样本与出图配色不一致 | `references/pitfalls-map-svg.md` §4 |
| **做得慢、返工多、一小时还没完** | **`references/efficiency.md`**（耗时实测 + 七条提速做法） |

### 最高频的 8 条（先记住这些）

1. **默认自渲染底图**（`fetch_osm.py` + `make_terrain.py`），不要等别人给瓦片：
   无速率限制、无版权风险、可无限重出。用户点名瓦片服务才走 B 路线。
2. **真实轨迹 > 拟合**：能拿到 KML/GPX 就用它，图上不该再出现任何虚线图例。
3. **改任何一处配色/字号/bbox，都要问"下游还有谁用这个值"**：
   底图类改动（配色/bbox）→ 重跑 `make_terrain` → `build_*` → `shoot_*` → `render_map_hi`；
   仅 SVG 类改动（地图内字号/图例）→ 后三步。
4. **地图画布宽高比由 bbox 决定**，所以「地图该多高」要在**底图阶段**算好，
   不要用 CSS `stretch` 事后补救（会产生死白，用户会说"上下区域很丑"）。
5. **CSS 类名先 grep 再用**：单文件长页面里 `.src` / `.hl` / `.nm` / `.el` / `.sub` / `.foot`
   极易撞车，症状是"某段文字只占左边一小条"。
6. **行宽用 px 不用 `ch`**（中文一字 ≈ 2ch）；带底色的段落要 `max-width:none`。
7. **手机版是另一套排版**：必须另出 860 宽长图，且
   **无头 Chrome 有最小窗宽 500、绝不能按窗宽反推裁剪区间**，要按像素量版心边界。
8. **图例放进地图空白区**（先量轨迹分布找 0 点矩形），并做「桌面 `g.il` / 手机 `.rbox-lgm`」
   双份切换 —— 地图内图例在手机上必然不可读。

### 交付前自检清单

- [ ] 三张图（桌面长图 / 手机长图 / 高清地图）**边缘深色像素 = 0**（无裁切）
- [ ] 地图与右栏**底边齐平**、右栏所有元素 `right ≤ 内容右界`（探针量，别目测）
- [ ] 地图内标注**两两冲突 = 0**、无标注压图例
- [ ] 图例**每一行样本图形都显示**（不只文字）
- [ ] 手机版无横向滚动、`g.il` 已隐藏、文字版图例正常
- [ ] 所有数值口径统一（海拔取整一致、里程累计口径说明）
- [ ] 改过底图的话：确认 `build_map.py` 与 `make_terrain.py` 的 bbox **一致**

### 交付后：把经验回写进本技能

本技能纳入版本库集中管理（见 `README.md` 的同步说明）：

```
仓库：~/WorkBuddy/workbuddy-skills         ← 改这里，不是改 ~/.workbuddy/skills/
├── skills/hiking-route-guide-poster/      ← 本技能在仓库里的副本
└── install.sh / sync.sh
```

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

写完同步：

```bash
cd ~/WorkBuddy/workbuddy-skills
bash sync.sh push "hiking: <这次学到了什么>"
```


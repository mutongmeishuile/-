# 常见坑分诊表（按症状查，不要通读）

从 `SKILL.md`「常见坑」下沉而来（2026-10-22）。**不要通读全表** ——
用 `Ctrl+F` 搜症状关键词，命中了再跳专题文件。

主文件 `SKILL.md` 的「常见坑」一节只保留了触发频率最高的 10 条入口，
其余完整内容在下面。

---

| 症状 / 场景 | 去哪查 |
|---|---|
| **地图画出来是"别的山"、轨迹跑到画布外（bbox 写死）** | `guide_common.resolve_bbox()` + `assert_bbox_covers()`；本文档「生成流程 §0」的窗口段 |
| **解析出 0 个点（选错了入口）** | 本文档「生成流程 §0」的格式判断表；`preflight.py` 会替你数 gx:coord / trkpt / LineString。KML 与 **GPX 都走 `parse_track_kml.py`**，分段式 LineString 才走 `parse_kml_ls.py`。**`prep_track.py` 现已按文件内容自动识别**，正常流程不用手工挑脚本 |
| **长线（>100 km）渲底图特别慢 / 内存爆掉（几十分钟、十几 GB）** | `references/self-hosted-terrain.md` §2.1（`pick_dem_z()` 按窗口像素数自适应降档，z15→z13，25 分钟降到 1 分钟） |
| **`make_all` 跑完但交付物变成"别的线路名"（route_def 被示例覆盖）** | 本文档「生成流程 §0」的开头警示；重拷脚本时**只拷通用脚本，别碰 `route_def.py`** |
| **`prep_track.py` 报 `FileNotFoundError: '线路.kml'`，但文件明明在当前目录** | 已修（`prep_track.py` 末尾 `kml.resolve()`）。子进程以 `cwd=scripts/` 跑，相对路径会指错地方；传绝对路径也能绕开 |
| **指北针/比例尺/图例压住轨迹，或太大挡视线** | `scripts/map_svg.py` 的 `FURN_*` 系数 + `guide_common.Placer`；`references/pitfalls-map-svg.md` §9 |
| **图例压住了冰川/山体（用户："遮住想看的山峰了"）、或"下半幅图例占比太大、喧宾夺主"** | `references/pitfalls-map-svg.md` **§21**（满幅地形没有空白角 → `CFG["legend_outside"]=True` 走「标题带 + 地图 + 图例带」，图例带通栏密排；口径 地图≥75% / 标题≈11% / 图例≤15%） |
| **用户说"字体统一 / 精细 / 线太粗 / 符号太大 / 地名太密太抢眼 / 要动态避让"** | `references/pitfalls-map-svg.md` **§22**（`<svg>` 根节点声明一次字体；POI 字号固定 14.4 **不乘 PS**；线宽 3.0/4.9、符号 0.66；底图地名最后统一排位 + 按"垭口>村>湖>峰"取舍 + 与 POI 同名去重） |
| **高清地图"没有标题" / 要求扩大窗口、把周边知名山峰包进来** | `references/pitfalls-terrain.md` **§21**（`bbox` 显式给、按轨迹居中、只包必要的山；⚠ 换窗口 = `osm.json` 作废，**重抓前必须先备份**，否则全镜像失败会写空缓存覆盖） |
| 底图取源、合规、瓦片站挂了 | `references/pitfalls-terrain.md` §1 |
| **图上一整块区域完全没有起伏、只剩等高线（不是水体却是一个色）** | `references/pitfalls-terrain.md` **§8a / §8a-2**（先量 `fill_voids` 前后 `diff>0.5` 的像素占比，>0.5% 就要警惕：§8a = 「低于参考面 900 m」在几百 km 窗口的误判；§8a-2 = 「`dem < med−2500` 坏瓦片兜底」把高海拔窗口的低谷整片抹平） |
| **多线路总览图上，自己的注记盖住了底图的 OSM 峰名/地名（或峰名之间叠成一团）** | `references/pitfalls-terrain.md` **§15**（把 `Labeller._mark` 的占位框落盘给上层当禁标区；峰名按 12 km 间距贪心去重，别用高程阈值） |
| **区域/多线总览图（≥1°，几百 km）整片谷地变成没有起伏的平板、山体只剩一条"海岸线"** | `references/pitfalls-terrain.md` **§8a**（`VOID_MAD=900 m` 只适用于小窗口；大窗口 25% 真地形被当空洞填掉 → 只填 NaN + 负值夹 0） |
| **山顶出现"没有等高线的平板/米色斑"（色带被截平 / 真峰顶被当空洞填掉）** | `references/pitfalls-terrain.md` §8（**判据必须单边偏低**，`abs()` 会把峰顶填平）+ §8b（色带取 p99.7 并把雪色端延到最高点） |
| **底图的地名/山峰名/湖泊名/水系名看不见、或几乎全丢** | `references/pitfalls-terrain.md` §11（字号按"缩图后"定 + Labeller 三级兜底 + 补湖名水系名 + 禁标网格别多乘 ss） |
| **村名/峰名后面跟一排黑框□□□（藏文等字体画不出）** | `references/pitfalls-terrain.md` §11 ⑤（`_clean_name` 收集阶段过滤，只留 CJK/拉丁/数字） |
| **两条线的图"地名/山峰名差很多"，先怀疑技能没同步** | `references/pitfalls-terrain.md` §14。**先 `out count` 比 bbox + 比 `osm.json` 命名元素数**，多半是 OSM 数据源稀疏（非登山区 peak 常为 0），不是脚本；诊断查询**别用 overpass.osm.ch**（它没有亚洲数据，会返回假的 0） |
| **某个峰/湖明显有名却显示成无名（只标高程数字）** | 先查它是不是**只有 `name:en`、没有 `name`**：`grep '"name:en"' out/osm.json`。OSM 里中文名被录进英文栏很常见（博格达 `Nangfeng/Xifeng/Hongfeng Peak`）。配 `CFG["name_map"] = {"Nangfeng Peak": "南峰"}` 显式补名 —— **脚本不自动音译**（会在别的语种上造错名）；`selftest.py` ⑨ 守这条 |
| **峰名的三角符号不在山顶上、漂在山腰（和等高线对不上）** | 这是 OSM `natural=peak` **人工点位漂移**，不是投影错。`make_terrain.snap_peaks_to_dem()` 已自动校正。⚠ **无 `ele` 标签的峰现在也会吸**（保守半径 ≤350 m + 纯 DEM 判据，不再跳过 → 亚丁 14/18 无 ele 峰此前全漏）；有 `ele` 的峰走"标注高程可信再吸"原逻辑。阈值 `CFG["peak_snap_min_gain"]`（默认 200，亚丁收 50 → 吸到 4 峰真顶）。日志看 `峰点校正：N 个 …`；某峰没动多半是周围无高程相符山头，脚本宁可不动。详细 `pitfalls-terrain.md` §28。关掉用 `CFG["peak_snap"]=False` |
| **冰川看不出范围 / 冰舌和雪原糊成一片** | `references/pitfalls-terrain.md` §12 ①（边界**绝不能画近白**，要冰蓝；填充透明度 ≤150 保住冰面晕渲） |
| **峰名太多糊死山体，或"主峰没名、无名矮峰有名"** | `references/pitfalls-terrain.md` §12 ②（按 ele 降序 + 分三级；阈值用**本窗口分位数** `PEAK_TIER_PCT=(88,62)`，低档走 `fallback=False`） |
| **换到新线路后主峰不突出 / 所有峰名都是小字** | `references/pitfalls-terrain.md` §12 ②（分级阈值写死过绝对米数就会这样，**必须相对**；样本 <8 自动免分级） |
| **时令河被画成常年河（旱季断流却给了水源错觉）** | `references/pitfalls-terrain.md` §12 ③（`intermittent=yes` 走虚线 + 图例单列一行） |
| **>2 天线路整条被画成一种颜色（图例却是彩色的）** | `references/pitfalls-map-svg.md` §10（按 `day_bounds` + `days` 颜色画 N 段） |
| **图例里找不到图上的符号 / 图例占地过大** | `references/pitfalls-map-svg.md` §11（只列用到的 kind + 两列网格 + 复用 `mk_pin` 出样本） |
| **比例尺/家具显得"贴了张白标签"、色条戳出底衬** | `references/pitfalls-map-svg.md` §12（**色条长度不参与 FURN 缩放**，底衬=色条+内衬） |
| **文字有一圈"白色阴影"/发虚** | `references/pitfalls-map-svg.md` §13（描边宽度控制在字号的 11–15%，别删） |
| **底图"一片单色"、看不出高差（色带写死了）** | `references/pitfalls-terrain.md` §4 首条（**通用 ramp + p2–p98 拉伸**，无雪区不给雪色端） |
| **Overpass 镜像全体 504/500/证书错** | `references/pitfalls-terrain.md` §9（先探活再定序，osm.ch 首选；`fetch_osm.py --soft` 可降级） |
| **底图与攻略 POI 出现两条同地注记** | `references/pitfalls-terrain.md` §10（skip_names 要含「·」前核心地名） |
| **同一座山在页面上有两个海拔数字** | `references/track-prep.md` §6（轨迹 GPS 偏低 20–50 m → **曲线用 GPS、印出来的数用 DEM**） |
| **总里程比作者口述多一倍、剖面出现两道空白悬崖（`seg_id` 多个值）** | `references/track-prep.md` §8（两步路 KML 的**平行备选段**被全并：`--longest` 只留主线，**并把 `CFG["kml_keep"]` 写进 route_def**，否则 make_all wave0 会悄悄撤销） |
| **用户说"标注字体动态缩小 / 位置动态合理 / 图例动态缩小"** | `references/pitfalls-map-svg.md` §15（字号阶梯 FS_LADDER + 8 方向×4 档半径位置搜索 + 图例 `_LG_S` 单系数动态缩放；⚠ `_layout` 试探必须纯函数，`_LABELS` 漏登记会让自检假通过） |
| **沙箱完全无外网（DEM/OSM 全超时）** | `references/self-hosted-terrain.md` §10（用 WebFetch 打 Open-Elevation 文本接口取真实 SRTM 样本，本地插值重建 DEM） |
| 放大看不清、字糊 | `references/pitfalls-terrain.md` §2 |
| 等高线糊成"棕色泥" / 密度不对 | `references/pitfalls-terrain.md` §3（**间隔按实测线覆盖率自适应**；顺带看那里的**配色**：别用暖橙棕） |
| **用户说"等高线太粗 / 再细一些"** | `references/pitfalls-terrain.md` §3（两个旋钮：① 线宽 `CONTOUR_DILATE=(0,0)` 首/计都 1 px，分级交给明度；② **`CFG["contour_target_cov"]` 收窄**（希夏邦马 0.30→0.26 → 20 m 退到 25 m）。⚠ **别去加密等高距**：本区 20 m 已 28.6%、10 m 到 46% 必糊；也**别改全局默认阈值**（狼塔 50 m=29.8% 会被误伤） |
| **首曲线密到能跟计曲线搞混 / 计曲线间隔不是整数** | `references/pitfalls-terrain.md` §3（`nice_major()`：25 m 首曲线配 **100 m** 计曲线而不是 125 m） |
| **用户说"标注字太大 / 字与字重叠"** | `references/pitfalls-terrain.md` §13 ①②（字号 ×0.85 落在 13–15.5 px 区间；**留白 `LBL_PAD` 2.0→2.8 才是治重叠的主手段**）；自检看 `· 挤放 N`（§13 ③） |
| **用户说"去掉冰川层 / 冰川面把地形压平了"** | `references/pitfalls-terrain.md` §12 ①（**技能默认就不画冰川**；判据＝数有多少冰川末端伸到雪线以下（与雪线白重复就关）。要开按线路写 `CFG["glacier_layer"]=True`，⚠ 连带改图例与图注三处，否则自相矛盾） |
| **切边扫描报警，但图其实没被裁** | `references/pitfalls-terrain.md` §13 ④（满幅底图边缘天然是地形暗部 → 阈值放宽到 0.40；先确认不是真裁再放宽） |
| **用户说"等高线颜色要和这片地接近"** | `references/pitfalls-terrain.md` §3（**派生色** `_contour_tint()`：同色相 + 降饱和 0.30 + 明度分档；暗处反转成提亮；强制最小明度差） |
| **用户说"等高线还嫌深、再淡一点"** | `references/pitfalls-terrain.md` §3（**只降 `contour_alpha`，别动 `contour_min_delta`**；亚丁 (0.24,0.55)→(0.19,0.47)，1 px 线 alpha<0.15 会消失，0.19 是可读下沿） |
| **"比例尺越大等高线越密、地图难看"**（长线/大窗口） | `references/pitfalls-terrain.md` §3（`pick_contour_interval`：覆盖率 ≤30%，狼塔自动 20→50 m） |
| **"如何更真实地反映地貌"**（背光侧一片黑、地形发平） | `references/pitfalls-terrain.md` §4（**MDOW 多向晕渲** + 阴阳分色）、`references/self-hosted-terrain.md` §3 |
| **"谷底看起来像山脊" / 山体阴坡阳坡是反的** | `references/pitfalls-terrain.md` §16（坡向公式在图像坐标系被镜像；**`python scripts/check_hillshade.py` 一把判定**，退出码非 0 = 光反了） |
| **"雪线以上一片白、主峰不立体"** | `references/pitfalls-terrain.md` §17（`snow_w` 别用"回抬"，改成雪面强塑形；验收：雪坡低频明暗 std ≥ 35） |
| **"色阶又黄又绿又黄 / 过渡发脏"** | `references/pitfalls-terrain.md` §18（带位必须单调；混用绝对米数与雪线偏移会折返；`_ramp_check()` 自检） |
| **"图例位置不合适 / 压在比例尺上"** | `references/pitfalls-map-svg.md` §16（位置不能写死；占位表**必须含指北针与比例尺**） |
| **"字体大小不统一"** | `references/pitfalls-map-svg.md` §17（只留两级：地名 13 一档、POI 19/17 一档） |
| **"轨迹/POI 标注遮住底图地名"** | `references/pitfalls-map-svg.md` §19（底图导出 `label_boxes.json` 给 SVG 避让 + `poi_label_cells` 反向预留；⚠ 字号单位要乘 `ss`） |
| **"峰名挤成一坨 / 三连山三条名叠在一起"** | `references/pitfalls-map-svg.md` §20（邻域密度 → 字号阶梯 13/10.5/9，逐条试、绝不原地硬叠） |
| **"标注重复 / 一山两名"** | `references/pitfalls-map-svg.md` §18（自绘地名 `route_def.PLACES` 必须进 OSM 的 skip_names，再加 `seen_names` 同名去重） |
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
| **雪线以上「又是白的又是绿的」（白绿相间断层）** | `references/pitfalls-terrain.md` §5b（`belt_stops` 的植被锚点是**绝对高程**，雪线一低就窜到雪线以上；判据＝任一锚点 > 雪线就按比例收编。**改前先跑逐米新旧色差对比确认零回归** —— 希夏邦马 6000 / 念青东 5000 本来就没这问题，别顺手改） |
| **切边扫描对满幅地图报警，但轨迹明明没被裁** | `references/pitfalls-terrain.md` §5c（深绿林区 + 晕渲会把最低档森林深绿压到亮度 120 以下，top 冲到 44%。**别抬阈值**，走 `qa_guide` 的 `track_inside()` 轨迹完整性判据；改完必须做反向验证——裁一半的图要能被判失败） |
| **用户问"多少米有雪线"（天山以外的山脉）** | `references/pitfalls-terrain.md` §6 + **`references/china-terrain.md` §4**（全国山脉雪线/林线表） |
| **图上写的雪线米数 ≠ `CFG["snowline_m"]`** | `references/pitfalls-terrain.md` §6（四个键要一起配：`snowline_m` / `_label` / `_note` / `_src`；只改高度不改文案是**静默错误**） |
| **两张图色系不同 / 配色突然变成暖绿** | **`references/china-terrain.md` §4**（配了 `snowline_m` 没配 `snow_month` → summer 档兜底 +350 m 抬高雪线 → `belt_stops` 回退 `ramp_stops` 暖调。同山同月直接补 `snow_month`） |
| **中国区线路底图配色（沙漠/戈壁/草原/森林/盐湖怎么上色）** | **`references/china-terrain.md`** §2–3 |
| **黄土沟壑 / 喀斯特 / 雅丹 / 滩涂怎么表现** | **`references/china-terrain.md`** §6–7 |
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
| **不知道立面/地图上哪两条标注会撞（不想靠看图试）** | 不用查 —— `build_guide` 现在会打印**标注预检**（桌面/手机各一遍）、`map_svg` 会打印**点位密度预检**（分"符号会叠"与"标注偏挤"两档）。命中就按提示改 `MARKS` 的 off 或删点位，别再出图试 |
| 虚实线用错、步道配色撞色 | `references/pitfalls-map-svg.md` §6–7 |
| 图例样本与出图配色不一致 | `references/pitfalls-map-svg.md` §4 |
| **做得慢、返工多、一小时还没完** | **`references/efficiency.md`**（耗时实测 + 提速做法） |
| **哪些步骤能并行、并行能省多少** | `references/efficiency.md` §3.7（DAG 分层 + 实测 + 别抱幻想） |
| **`route_def.py` 的里程/爬升不知从哪来、只会手抄** | `scripts/new_route.py`（读 `out/` 三个 json 自动生成骨架，文案留 `★ TODO`） |
| **`route_def.POIS` 写成字典就崩（`KeyError: 0`）** | `guide_common.poi()` / `pois_of()`：元组与字典都收；下游一律走它取值 |
| **B 路线：瓦片米底没地形感、自带等高线太密/太淡** | `scripts/tile_tint.py`（瓦片只留线画 + DEM 设色打底 + 自绘等高线）+ `references/tile-basemap.md` §7/§11 |
| **换个项目做同一片山，DEM 又要重下几万块瓦片** | 缓存已改**用户级共享** `~/.workbuddy/cache/hiking-dem/`（`HIKING_DEM_CACHE` 可覆盖）；瓦片按 z/x/y 命名，天生全局唯一 |
| **手机版缩略图标注轻微互压，被判成"失败"** | `map_svg.self_check` 是**两档制**：手机版的「标注重叠 / 压家具」只告警；硬指标是越界 / 压轨迹 / 家具互叠 |

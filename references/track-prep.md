# 真实轨迹预处理（KML / GPX → 绘图数据）

有真实轨迹时，全线走实线、无虚线，坐标全真。本文档记录从"拿到 KML"到"能画图"的完整口径，
对应脚本：`scripts/parse_track_kml.py` → `scripts/prep_kml_track.py`。

---

## 1. 拿到轨迹：优先让用户从 App 导出

两步路网页端（`2bulu.com`）前置 SafeLine（雷池）WASM 挑战，直接抓包/构造指纹既不稳定，
运行求解脚本还可能触发敏感操作审批拦截。**最稳的做法是让用户在两步路 App 里
「轨迹 → 右上角 ⋯ → 导出 → KML」导出一份文件发过来**，一个文件解决所有问题。

支持的输入：
- **两步路 KML**：`<Document id="TbuluKmlVersion2">`，轨迹在 `<gx:Track>`（`<gx:coord>` lon lat ele + 并列 `<when>` UTC 时间戳）。
- **通用 GPX**：轨迹在 `<trkpt lat lon><ele><time>`，航点在 `<wpt>`。GPX 解析需另写一段（结构与 KML 不同，见下）。

---

## 2. 解析 KML（`parse_track_kml.py`）

```bash
python scripts/parse_track_kml.py "D:/路径/线路.kml"          # 默认写到 KML 同目录
python scripts/parse_track_kml.py "D:/路径/线路.kml" --out ./kml
```

产出：
- `track_full.json` = `{"pts": [[lon, lat, ele, "hh:mm:ss"], ...], "cum_m": [...]}`
- `kml_pois.json` = 具名标注点 `[{"name","lon","lat","ele","desc"}, ...]`

解析要点（踩坑）：
- `<gx:coord>` 顺序是 **lon lat ele**（不是 lat lon），空格分隔；
- `<gx:when>` 是 **UTC**（`2026-05-31T08:36:01Z`），按出现顺序与 coord 一一配对，转北京时间 +8h 只取 `hh:mm:ss`；
- 标注点藏在 `<Placemark>`，`name` 是地名、`description` 可能带 `<br>` 等 HTML，要 strip 标签；**轨迹自身的 Placemark 也含 `<coordinates>`**，但它是一大串多行数据（含换行），用"是否含换行"过滤掉；
- 用 ElementTree 命名空间通配 `{*}`，不要硬编 `gx:` 前缀（导出器不同前缀会变）。
- 部分轨迹 `<gx:coord>` 只有两个数（无高程），`ele` 置 `None`，后续用 SRTM 或跳过高程。

GPX 补充：能用 `gpxpy` 最好；纯标准库时解析 `<trk>/<trkseg>/<trkpt>` 的 `lat/lon` 属性与子节点 `ele`/`time`，`wpt` 取 `lat/lon`+`<name>`+`<desc>`。

### 2.1 先判断 KML 是哪种格式（**必做，选错脚本会得到 0 点**）

两步路有**两种**导出格式，`parse_track_kml.py` 只认其中一种：

```bash
grep -c "gx:coord"     线路.kml     # >0 → <gx:Track> 式，用 parse_track_kml.py
grep -c "<LineString>" 线路.kml     # >0 → 分段式，用 parse_kml_ls.py
```

| 格式 | 特征 | 时间戳 | 脚本 |
|---|---|---|---|
| `<gx:Track>` + `<gx:coord>` | 现代导出，一点一 coord | **有 `<when>`** | `parse_track_kml.py` |
| 多个 `<Placemark>/<LineString>/<coordinates>` | 旧版 / 分段导出 | **无** | `parse_kml_ls.py` |

选错的症状是**解析出 0 个点**，第一反应容易误判成"文件坏了"或"用户导错了"——
所以拿到文件先数一遍，别假设。

```bash
python scripts/parse_kml_ls.py "D:/路径/线路.kml" --out ./kml
```

`parse_kml_ls.py` 解析要点：
- 注记点在 `<Folder id="TbuluHisPointFolder">` 下的 `<Placemark>/<Point>`，
  `name` 是作者随手写的地名（**89 条里只有 21 条具名**），其余是「3550」「回望某某垭口」这类纯数值/随手标注；
- **段间可能有几百米的无记录断点**（实测南天山北线 段 1→2 间隔 **483 m**）：按 `--gap`（默认 500 m）识别，
  **只记不补点、如实保留**。插值会凭空造出一段没走过的直线，反而误导；下游 GPX 里表现为多个 `<trkseg>`，
  这恰好是正确表达。
- **`prep_kml_track.py` 的 DP 简化里纬度是硬编码的**（`kx = cos(30.5°) × 111320`），
  换纬度带必须改（南天山 42.5°：`cos(42.5°)`）。改错会让简化容差在经度方向偏 ~15%。

---

## 3. 简化 + 剖面（`prep_kml_track.py`）

```bash
# 把 track_full.json 放到脚本同目录（SRC = HERE / "track_full.json"）
python scripts/prep_kml_track.py
```

产出：
- `track_real.json` = `{"pts":[{"lon","lat","km","ele","t"}], "total_km", "asc", "desc", "ele_min", "ele_max"}`
- `profile_real.json` = 等距剖面 `[[km, ele], ...]`（每 100 m 一点）

核心口径（**这几条决定图准不准**）：

| 项目 | 口径 | 原因 |
|---|---|---|
| 简化 | Douglas-Peucker，横向容差 **4 m**（在"经纬度×近似米"空间里做） | 11345 点 → 约 1025 点，肉眼看不出差异，SVG 体积可控 |
| **里程基线** | **原始点串的 haversine 累计** | 用平滑后里程会把所有 POI 的 km 带偏（本例平滑后 28.10 km ≠ 原始 30.20 km） |
| 海拔平滑 | 11 点滑动平均，**只用于画剖面** | 去 GPS 高程抖动 |
| 累计升降 | 平滑后海拔 + **3 m 阈值**逐点累计（|Δ|<3 m 不计） | 滤掉噪声，本例 +3081 / −3371 m |
| 剖面采样 | 沿原始累计里程每 **100 m** 取一点 | 剖面折线均匀，不被密集点挤爆 |

> `total_km` 取原始累计（本例 30.198，与作者所述 30 km 吻合），不是平滑后值。
> `asc/desc` 用平滑后 + 3 m 阈值；`ele_min/ele_max` 同。

---

## 4. 接进绘图（`route_def.py` / `map_svg.py`）

- `route_def.py`：脚本开头 `json.load(track_real.json)` 得到 `TRACK / TRACK_KM / TRACK_ELE / TRACK_T`，
  `TOTAL_KM / ASC / DESC` 直接取文件里的值；`POIS` 的 km 值**按原始累计重算**（不要沿用任何早期用平滑里程算的旧值）。
- `map_svg.py`：**只用一条实线**画整条 `TRACK`（先画白色 halo 描边再画彩色主线），不再有 D1/D2 之分；
  分天/分段如需区分，只用颜色分段，不要用虚线。5 km 一档打里程点，标 km 数字。
- 坐标投影：底图是真 Web Mercator，`projector(lon,lat)` 投到**底图像素坐标系**，`<image>` 与矢量标注共用 `viewBox="0 0 图宽 图高"`，绝不手动摆像素。

---

## 5. 顺带产出 GPX（给用户导航用）

从 `track_real.json` 或原始 `track_full.json` 生成 `.gpx`：
- `<trk>/<trkseg>` 每点一个 `<trkpt lat lon><ele/><time/>`（用**原始全量点**最完整，
  不要用简化后的 —— 简化只为绘图服务，导航要的是密度）；
- `<wpt lat lon>` 放 `kml_pois.json` 里的具名点，`<name>` 写地名；
- 图脚注写"**导航以离线 GPX 为准**，长图仅供预览"。

⚠ 四个真实返工点：

1. **`<time>` 必须取 KML 原始 `<when>`（UTC，`Z` 后缀）**，不能拿预处理后的本地
   `HH:MM:SS` 字符串反推 —— 反推出来会在手表/两步路里整体偏 8 小时。
2. **`gx:Track` 的 `<when>` 与 `<gx:coord>` 是"先全部 coord 再全部 when"的排列**，
   按**索引配对**。而且文档开头往往还另有 N 个 `<when>` 属于 POI 标注
   （本例 13 个），全文 `findall` 会多出这 13 个导致错位。
   → **先 `re.search(r"<gx:Track>.*?</gx:Track>")` 取出块，再在块内 findall。**
3. **分段时不要把段界点写两遍**：`seg(0,i1+1) + seg(i1,i2+1) + seg(i2,n)` 会多出 2 个点
   （本例 2870 ≠ 2868）。让营地/段界点归前一天：`seg(0,i1+1) + seg(i1+1,i2+1) + seg(i2+1,n)`。
   导出后断言 `trkpt 数 == 原始点数`。
4. **用 `xml.etree` 回读校验**再交付（trkpt 数 / wpt 数 / trkseg 数），
   肉眼在 300 KB 文本里数不出来。

---

## 6. 自检

- 里程两项对得上：脚本打印的"原始里程"应≈作者口述里程（本例 30.2 ≈ 30）；
- 点数合理：简化后应是原始的 5%–15%（本例 1025/11345 ≈ 9%）；
- 剖面最高点与关键点表里的最高点名一致；
- 地图上轨迹应**贴着底图的步道/山脊线走**（若明显飘到别处，说明投影或坐标系不对）；
- 图上不再出现任何虚线图例。

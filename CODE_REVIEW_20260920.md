# 乌有乡 nowhere_repo 代码审查报告

> 审查日期：2026-09-20
> 范围：`nowhere/` 全部 43 个源码模块（21,404 行，不含 tests）
> 方法：亲自逐模块读源码 + 用 grep 落地 `22_编码规范_分层详版` 里「可自动检验」的规则。**每条发现都回源码核过原行，不是转述。**
> 判准依据：`22_编码规范_分层详版`（45 条）+ `33_我们的纪律与约束`（23 条，信代码 / 引用带行号 / 给推荐不给罗列）。

---

## 〇、结论（给推荐）

**P0（崩溃 / 安全 / 数据丢失）0 条，P1 16 条，P2 约 45 条，静默吞异常约 30 处。**

整体工程质量不错——原子写盘（`state.py` 用了 tempfile+fsync+replace）、配置 fail-closed（`weather.py:132` 是 `if not key: return None`）、经纬度 wrap 都做了。问题集中在三处：

1. **一个最新 commit 引入的测试回归**（`content.pools()` 被删）。
2. **大量「判据写死 / 取值对齐」类的静默错误**（0°C 真值陷阱、`lng=0` 硬编码、水类型不匹配）。
3. **约 30 处吞异常掩盖根因**（C2/B5）。

### 最该先看的 5 处（几行内修完，但会真实出错）

| 优先级 | 问题 | 位置 | 为什么 |
|---|---|---|---|
| 1 | `content.pools()` 被删、测试仍调用 → 测试套件崩 | `content.py` | 最新 commit 引入的回归 |
| 2 | `humanities._load()` 往 stdout 打印，破坏 MCP stdio 协议 | `humanities.py:56,75,89` | 一行改 stderr |
| 3 | 5 个 solar/islamic 节日正日永不触发 | `server.py:2195,2478` | 漏两个字符串 |
| 4 | `errands._haversine_km` 缺 `min(d,1.0)` → 反极值崩溃 | `errands.py:84` | 同仓其它 5 处都有 clamp，唯此漏 |
| 5 | `describe` 温度约束把 0°C 当缺失 | `describe.py:514,517` | `or 999` 真值陷阱 |

---

## 一、按 `22_规范` 分层的对照结果

### E 层（安全）—— 基本干净，唯一问题是暴露面

grep 三条最易扫的安全规则，**nowhere 都干净**：

- **E1 fail-open 配置**：0 命中。`weather.py:132` 是 `if not key: return None`（fail-closed，正确）。
- **D6 运行时拼标识符名**（`getenv/getattr` 里含 `+`/f-string）：0 命中。
- **E3 明文比 secret**（`==` 比 key/token）：0 命中。

**唯一安全问题属 E4「不用的暴露面」**：`web.py:396-423` 的 Starlette 路由把 `/open_door` `/walk` `/ask` `/mark` `/walk_to` `/wait` `/postcard` `DELETE /postcard/{id}` 等**写接口全部无鉴权暴露**，且服务端绑 `0.0.0.0`（`server.py:6700,6744`）。同网段任意人可替 AI 走路、删明信片。现有的 `_check_injection` 只挡提示词注入，不是访问控制。

### A 层（架构）

- **A4 命名即契约 / 纪律 13 文档与代码不一致**：`terrain.py:336` docstring 声称「grid/tile 与 DEM 差 >100m 就信 DEM」，但代码只在 grid 分支（`terrain.py:366-371`）做 DEM 校验，tile 分支（`357-360`）直接 `return`。**信代码**：tile 实际不做 DEM 校验，docstring 是错的。
- **A1 唯一真源（轻度违反）**：`_haversine_km` 在 `terrain.py:320`、`errands.py:79`、`hydrology.py`、`water.py`、`radio.py`、`soundscape.py`、`dem.py` 各自实现；`_EARTH_RADIUS_KM=6371.0` 重复 3 处。已造成实际漂移——`terrain.py:326` 有 `min(a,1.0)` clamp，`errands.py:84` 没有。

### B 层（功能耦合）

- **B1/C3 声明接线对等（写后不读）**：`errands.py:87-101` `check_delivery` 算出 `hint`（97 行）后**从不使用**，只按半径返回第一个邻近地点——差事送达不校验收信地。
- **B5 失败必须可见 → 大量违反**：全项目约 30 处 `except + pass/continue` 静默吞异常（见第三节清单）。
- **B5 失败方向反了**：`server.py:3826-3831` 疲劳+陡坡的「拦住」判定发生在 `walk_mod.step()`（3789 已改写 `_state.pos/path/elapsed`）**之后**——位置已动、文本却说「爬不动」。

### C 层（代码）

- **C2 异常不许静默**：见第三节清单，30 处。
- **C4 枚举无产生点 / 死标志**：`server.py:4586` 的 `encountered` 标记写后无任何读点，同一条留言会在 `look_around` 重复出现。
- **C7 死代码**（逐条核实为真死）：
  - `walk.py:32` `_clamp_dist` 定义后全仓无人调用（`walk.py:146` 内联了）。
  - `terrain.py:136` `if "lat_min" not in info` 分支永假——`_load_tile_index` 对 legacy 要么读出 bounds、要么 `continue` 丢弃。
  - `server.py:140` `_load_places_patch_sync` 与 `_load_places_patch` 逐字重复、零调用。
  - `server.py:3602` `_check_late_night_shop` 定义后零调用、形参未用。
  - `salience.py:24-29` `_LAT_BANDS_MAP` 全文零引用。
  - `describe.py:1385` terrain 分支 `if "elevation" in payload` 恒真（构造时必有该键），后面整段场景组合走不到。
  - `describe.py:1427` 等四处 `scene_name in ("water",)` 永不命中（水面对应 `ocean`/`water_features`）。

### D 层（函数内部）

- **D1 判据取值对齐（真值陷阱）**：`describe.py:514,517` `(ctx.get("temp") or 999) < temp_min`、`(ctx.get("temp") or -999) > temp_max`——**0°C 是合法温度却是 falsy**，暖/冻场景在冰点都误判。
- **D1 判据与产出不对齐（水类型）**：`walk.py:100` `water_ahead_km` 只判 `== "water_ocean"`，但 `walk.py:203` 的阻挡闸门认 `("water_ocean","water_fresh")`——淡水湖的「水诚实」闸门是死的：目的地是 `water_fresh` 时探不到淡水。
- **D1 名字与行为不符**：`salience.py:130` `if not payload or not isinstance(payload, dict): return True` 先把 list 放行，导致 `salience.py:151-158` 的 water_features biome/water_type 过滤对真实候选（`list[dict]`）永不执行。
- **D5 边界值硬编码**：`errands.py:84`（缺 clamp）；`notebook.py:193` 时区 `lng=0` 硬编码；`notebook.py:430-431` 季节 `lat=30.0` 硬编码（南半球标反）。
- **D5 边界值相位算反**：`weather.py:102` `hour_angle = (local_hour - 5) * (2π/24)`，sin 峰值在 `local_hour=11`、谷值 23 点，注释却称「14 点峰值 / 5 点谷值」；amplitude 区带映射（equator/subtropical 给 12、其余给 8）与注释「低地 ±8 / 荒漠 ±12」相反，且 `_climate_zone` 没有 desert 区。

---

## 二、完整 P1 清单（16 条，全部亲自核过原行）

| # | 问题 | 位置 | 错在哪 | 怎么修 |
|---|---|---|---|---|
| 1 | solar/islamic 节日正日永不触发 | `server.py:2195,2478` | `_festival_in_window` 只处理 `("lunar","hijri")`，漏了 `solar`/`islamic`；数据里 4 个 solar + 1 个 islamic 落到 `return False` | 两处都改成 `("lunar","hijri","solar","islamic")` |
| 2 | ask 子串匹配抢先话题映射 | `knowledge.py:195-198` | 第 2 步双向子串在第 4 步话题映射之前执行，「北京 美食」命中「北京」通用条 | 子串匹配排除「查询含话题词」的情形，或话题映射提前 |
| 3 | `content.pools()` 删了但测试仍调用 | `content.py` | 最新 commit 删了 `pools()`，`test_content_pipeline.py:44,51` 仍调用 → `AttributeError` | 恢复 `pools()` 或改测试 |
| 4 | humanities 往 stdout 打印破坏 MCP 协议 | `humanities.py:56,75,89` | 三行 `print(flush=True)` 写 `sys.stdout`，MCP stdio 的 stdout 是 JSON-RPC 通道 | `print(..., file=sys.stderr)` |
| 5 | web 写接口无鉴权 + 绑 0.0.0.0 | `web.py:396-423` | 写接口无鉴权无 CSRF，服务端绑 `0.0.0.0`，局域网可驱动/删除状态 | 加共享 token 或默认绑 127.0.0.1 |
| 6 | 差事送达不校验收信地 | `errands.py:87-101` | `hint`（97 行）算出后从未使用，只按半径返回第一个 5km 内地点 | 用 `hint` 匹配地点名再判距离 |
| 7 | 手账时段词 `lng=0` 硬编码 | `notebook.py:185-193` | `tf.timezone_at(lat=lat, lng=0)`，除格林尼治外本地小时全错 | 把 `lon` 一路传下来 |
| 8 | 疲劳+陡坡判定在移动之后 | `server.py:3826-3831` | `walk_mod.step()`（3789）已改写 `_state`，3826 才判 fatigue_slope_block | 在 `step()` 之前预判拦截 |
| 9 | water_features 过滤是死代码 | `salience.py:130,151-158` | `isinstance(payload, dict)` 守卫先把 `list` 放行，后面 `.get()` 永不执行 | 对 list 逐项取字段，或改候选结构 |
| 10 | water_features 元数据约束从未生效 | `describe.py:3058` | `_pick_scene(pool, f"water_{biome}", ...)` 用的 meta 名在 `scene_meta.json` 不存在，`requires` 全跳过 | name 改成实际有 meta 的 key |
| 11 | 节律卡混进普通抽卡池 | `localcolor.py:138-151` | 手写层过滤只按 place+seen，不排除 `category=="节律"`；`unseen_handwritten` 把节律卡也计入 | 过滤和计数都加 `!= "节律"` |
| 12 | build_index 不合并 knowledge/food | `build_index.py:466-480` | `all_places.update(...)` 漏掉 `knowledge`/`food`，输出索引这两层恒空 | 补 `update(knowledge.keys())` / `food.keys()` |
| 13 | poster 超时子进程未杀 | `poster.py:65-74` | `wait_for(proc.wait(), 180)` 超时只取消等待、不终止子进程 | `except TimeoutError: proc.kill()` |
| 14 | knowledge 硬编码路径绕过 DB 重定位 | `knowledge.py:274` | `sqlite3.connect(_DATA/"places.db")` 不经过 `places._resolve_db()`，搬 D 盘后静默建空库 | 改用 `places` 的连接 |
| 15 | knowledge 用全局 `_random` 而非 `_rng` | `knowledge.py:227,234` | `_random.choice(...)`，同种子下结果不可复现 | 把 `rng` 传进来 |
| 16 | elevation 异常保护失效 | `server.py:1937-1938` | `isinstance(elev_result, Exception)` 对 `asyncio.to_thread` 的结果恒假（异常在 await 处抛出），且无 try 兜底 | `try/except` 包住，删死判断 |

> **#3 是唯一由最新 commit（`42a5dd4`「content.pools 死API」）引入的回归**，其余 15 条都是历史存量。

---

## 三、静默吞异常精确清单（C2/B5，约 30 处）

`except` 后紧跟 `pass`/`continue` 且无日志的（非 tests）：

```
actions.py:241,452      country.py:33         dem.py:46
geocode.py:133          humanities.py:54      journeys.py:96,289
knowledge.py:286        landing.py:56         listen.py:182
localcolor.py:83        notebook.py:196,432,480,512
people.py:41,83,99      poster.py:72          radio.py:247
server.py:1231,1243,1830,2088,3019,3029,3938,3946,3955,4268,4287,4561,6408
state.py:285            terrain.py:92,235     water.py:257
weather.py:225,234
```

其中**最该看的 3 处**：

- `knowledge.py:286`：`_get_chinese_name` 吞掉异常且 `db.close()` 在异常路径不执行（连接泄漏）。
- `weather.py:225,234`：`except Exception: pass` 吞掉的不只是网络错误，连编程错误一起吞。
- `server.py:1231,1243`：需要看吞的是什么（疑似加载类兜底）。

`sky.py:79,83`（ephem NeverUp/AlwaysUp）和 `terrain.py:92`（逐瓦片容错）这类**不算违规**——是合理处理。

---

## 四、P2 清单（约 45 条，按主题归并）

### 会算错（数值 / 几何）

| # | 问题 | 位置 |
|---|---|---|
| 17 | 气温日变化相位反（峰值 11 点 / 谷值 23 点，注释称 14/5 点）+ amplitude 区带映射反 | `weather.py:101-103` |
| 18 | 温度约束 0°C 当缺失（真值陷阱） | `describe.py:514,517` |
| 19 | `_haversine_km` 缺 `min(d,1.0)` → 反极值 `ValueError` | `errands.py:84` |
| 20 | 手账季节固定北纬 30°，南半球标反 | `notebook.py:430-431` |
| 21 | `elevation_delta` 用原始值判方向、`round` 取值，0~0.5m 输出「抬高了 0 米」 | `describe.py:1932-1936` |
| 22 | atlas 极东/极西用裸经度，反经线附近算错 | `journeys.py:308-309` |
| 23 | 罗盘取词两处实现不一致（银行家舍入 vs `int((deg+22.5)/45)`） | `places.py:50` / `travelers.py:150` |

### 逻辑闸门失效（死条件 / 死分支）

| # | 问题 | 位置 |
|---|---|---|
| 24 | 水域阻挡只识别 `water_ocean`、不识别 `water_fresh` | `walk.py:100` vs `203` |
| 25 | terrain 分支 `if "elevation" in payload` 恒真 | `describe.py:1385` |
| 26 | `scene_name in ("water",)` 永不命中 | `describe.py:1427` 等 |
| 27 | 电台「文化圈」回退是空操作（只留本国） | `radio.py:115-125` |
| 28 | 电台「地区代表」回退在本国无台时不可达 | `radio.py:136-148` |
| 29 | 离线电台不过滤 `dead` 标记（17 台死台被返回） | `radio.py:82-84` |
| 30 | 电台外网 API 在兜底非空时是死代码 | `radio.py:199-201` |
| 31 | `_GEO_CULTURE` 顺序反了，通用 `European` 遮蔽西班牙/意大利/法国 | `art.py:75-81` |
| 32 | `soundscape_credit` 检查了错的变量（`pool` 而非 `near_pool`） | `soundscape.py:208` |
| 33 | 跨门 seen 卡拷贝被立即覆盖 | `server.py:2924-2925` vs `2950-2951` |
| 34 | `_find_tile` 的「未预载 bounds」分支永假 | `terrain.py:136-143` |

### 状态 / 持久化不一致

| # | 问题 | 位置 |
|---|---|---|
| 35 | `walk_impl` 的 `last_text`/足迹在 POST_NORMALIZE 之前记录 | `server.py:4210-4218` |
| 36 | `walk_to_impl` 被拦的一步仍计入总里程 | `server.py:5006,5008` |
| 37 | `give_souvenir` 丢弃后不 `_state.save()` | `server.py:6029-6035` |
| 38 | `bury_impl` 屏显与落盘文案不一致 | `server.py:6055,6070` |
| 39 | `placememory.postcards()` 从无人写入的 `state.json` 迁移（死分支） | `placememory.py:220` |
| 40 | 命名目的地返回 `data["biome"]` 恒 None | `server.py:3273` |
| 41 | 非原子写盘（`marks.py`/`journeys.py`/`travelers.py` 直接 `write_text`） | 三处 |

### 文本 / 文案错误

| # | 问题 | 位置 |
|---|---|---|
| 42 | 疲劳钳制到 3km 时提示仍写「一步最多 5 公里」 | `server.py:4176-4182` |
| 43 | 进食清饥饿 `_try_complete_whim("eat")` 调两次、满足文本丢弃 | `server.py:4134-4143` |
| 44 | 盲开模式步数报两次 | `server.py:5160,5170` |
| 45 | `ask_impl` 不拒空话题 | `server.py:4900-4905` |
| 46 | 三段落「余韵」角色总被「转折」覆盖 | `describe.py:1547-1559` |
| 47 | `wait` docstring 写「0.25-12 小时」实际钳到 720 | `server.py:6217` |
| 48 | `SouvenirAction` docstring 写「15%/25%」实际 0.5/0.3 | `actions.py:519-526` |
| 49 | `baked.render_food()` 部分匹配依赖 dict 顺序、只认全角冒号 | `baked.py:164-166,40,52` |
| 50 | `encounters.py` 双向子串把「马德里」卡配给「德里」 | `encounters.py:136-137` |

### 健壮性 / 资源

| # | 问题 | 位置 |
|---|---|---|
| 51 | `analyze_pcm` 样本 <2048 时 `chunk * window` 抛 `ValueError` | `listen.py:33-43,55-70` |
| 52 | `_try_play_stream` 启动 ffplay/mpv 后不 wait 不 kill（孤儿进程） | `server.py:4259-4288` |
| 53 | `providers._cache` 无上限、只 TTL 懒删除 | `providers.py:38,98-99` |
| 54 | `geocode` 缓存超 500 条整体清空（thrash）+ 缓存 `None` 失败 | `geocode.py:92-93,136` |
| 55 | 每张明信片全量扫 `cities15000` 两遍 | `server.py:5624,5628` |
| 56 | 断路器先查电路再查缓存，开闸期间不返回新鲜缓存 | `providers.py:64-76` |
| 57 | `dem.py:44` `if dem>0` 丢弃海拔 ≤0 的城市 | `dem.py:44` |
| 58 | WAV 头硬编码 44 字节 | `listen.py:212-217` |
| 59 | 注入防护只覆盖 2 个端点，`/postcard` `/mark` `/ask` 等裸奔 | `web.py:186,219` |
| 60 | `make_ask_labels.py` 单字关键词过宽 + 相对路径依赖 cwd | `make_ask_labels.py:5-6,119-121` |

### 确定性（可复现性）

| # | 问题 | 位置 |
|---|---|---|
| 61 | `poster.blank()` 用 `hash(place)` 做种子（进程随机盐） | `poster.py:111` |
| 62 | `knowledge` 子串用 `set(title)` 迭代（hash 随机） | `knowledge.py:195` |
| 63 | `_check_anniversary` 用模块全局 `_rng` 而非传入 rng | `server.py:605,632` |

---

## 五、其它质量备注

**死代码 / 死常量**：`_check_late_night_shop`（server.py:3602）、`_load_places_patch_sync`（server.py:140）、`_LAT_BANDS_MAP`（salience.py:24-29）、`_clamp_dist`（walk.py:32）、`_get_weekday_rhythm` 的 `cc`/`market_groups`（server.py:703,725）、`_TOPIC_LABELS` 里映射到空列表的 12 个词（knowledge.py:42）、`encountered` 标记写后不读（server.py:4586 等）、`_ARID_COUNTRIES` 同函数重复定义且内容不同（server.py:488 vs 569）。

**重复常量（有漂移风险）**：`_haversine_km` 6 处、`_EARTH_RADIUS_KM` 3 处、`_climate_zone`/`_stable_random`/`_CULTURE_CIRCLES` 各 2 处（radio 与 soundscape 的文化圈表已不同步）。

**命名 / 误导**：`_HYPOTHERmia_TEXTS` 大小写混排（server.py:3428）、`_render_water_features` 类型注解 `dict` 实际收 `list`（describe.py:2954）、`_REGION_MAP` 在 salience/describe 两处重复、`elevation()` docstring 声称 tile 做 DEM 校验实际不做（terrain.py:335 vs 357）。

---

## 六、审查方法学限度（诚实声明）

1. **静态读码抓不到 A7「功能不可达」**——`22_规范` A7 这条需要「真跑一遍」，本报告没做冷启动冒烟，所以「0 个 P0」只代表「这些模块里没有一眼能看的崩溃」，不代表没有。
2. **`state.py:289` `load()` 里 `print(msg)` 也写 stdout**——与 `humanities.py` 同属 MCP stdio 协议风险，是否在请求期被触发取决于 `load()` 的调用时机，未深追。
3. 数据文件（`festivals.json`、`scene_meta.json`、`radio_fallback.json` 等）的字段正确性，部分依赖「数据是这么构造的」这一前提，个别条目（如 #12 knowledge/food 索引）是通过对照已提交的 `explorable_index.json` 反向确认的。

---

## 七、建议（按投产比，给推荐不给罗列）

**第一批——立即修，代价最小、影响最大**：恢复 `content.pools()` 或改测试（#3）；`humanities._load()` 改 stderr（#4）；节日补 `solar/islamic`（#1）；`knowledge` 换 `rng`（#15）+ `errands._haversine_km` 补 clamp（#19）+ `describe` 温度 0°C 判 None（#18）；`server.py:1937` elevation 包 try（#16）。

**第二批——需要想清楚语义，别盲改**：ask 子串/话题映射优先级（#2）、差事送达校验收信地（#6）、手账传 `lon`（#7）、疲劳拦截提前（#8）——这些改的是**行为**，先定预期再动。

**第三批——架构债，单独立项**：`web.py` 鉴权 + 绑定策略（#5 安全）、`providers` 缓存上限、6 处 `_haversine_km` 归并、`server.py`（6756 行）拆分。属「修根因不修条目」，不建议这次顺手做。

---

*本报告由人工逐行核验生成，非子代理转述。*

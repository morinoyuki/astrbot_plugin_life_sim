# 转生模拟器 (astrbot_plugin_life_sim)

在 AstrBot 群聊 / 私聊中模拟"转生到任意世界"的一生。由「凯伊」代为叙述,纯叙事驱动。

## 特性

- **三种模式** — 纯叙事 A / 游戏世界 RPG B / DND 5E 跑团 C,自动按世界观关键词识别或显式指定
- **独立上下文** — 叙事历史走文件存储 + 显式 `contexts` 传入 LLM,不污染主对话
- **LLM 智能压缩** — 超长历史调 LLM 提炼成摘要(失败自动回退规则抽取),不是简单丢消息
- **LLM 模式识别** — `/创建` 时调 LLM 分析语境判断 A/B/C(失败回退关键词匹配)
- **完整 RPG/DND 工具链** — 27 个 `rpg_*` 工具(HP/EXP/装备/技能/技能点/物品)+ `roll_dice` 骰子工具(模式 C)
- **持久化 lore** — 角色设定(支持多角色,按 `character` 分组)+ 世界观设定由 LLM 在对话中自动调用工具落库,后续每轮注入 system prompt
- **/undo 完整回滚** — 叙事历史 + lore 快照 + RPG 数值(HP/EXP/装备/会话)按 turn 计数一起回滚
- **每会话互斥锁** — `/创建` `/do` `/undo` `/删除` 各持同 session 的 `asyncio.Lock`,并发命令直接返回"上一条还在处理"
- **灵活模型路由** — 主 provider / 模式专属 / 压缩 / 模式识别各自分配,可把不同任务路由到不同模型
- **引用兼容** — 回复消息时自动提取引用内容作为补充背景传入
- **人生结束标记** — 死亡结局输出 `<LIFE_SIM_END>`,插件自动识别并提示重开

## 指令

| 指令                        | 说明                                                                          |
| --------------------------- | ----------------------------------------------------------------------------- |
| `/创建 [rpg\|dnd] <世界观>` | 创建会话(覆盖已有)。可加 `rpg` / `dnd` 前缀强制模式,否则自动判断;会自动清理旧 RPG 存档 |
| `/do <选项/行动/反馈>`      | 推进剧情。可发选项序号、自定义行动、或对剧情的反馈;引用消息会一并传入         |
| `/进度`                     | 查看进度:世界/轮数/当前位置(## 标题)/最近一段                                 |
| `/undo [N]`                 | 撤销最近 N 轮对话(默认 1)。叙事历史 + 持久化 lore + RPG 数值全部按 turn 回滚  |
| `/redo`                     | 重试上一轮:自动回滚最近一轮,并用相同输入(含图片)重新生成                  |
| `/删除`                     | 删除当前会话,同时清理该群/私聊的 RPG 存档与会话文件                          |

**支持群聊和私聊**(私聊可能无 prefix,插件通过 `text.find(cmd)` 自适应)。

## 三种模式

### 模式 A — 纯叙事(默认)

不引入 RPG 数值,纯叙事驱动。**两种推进方式自动选择**:

- **方式 1 · 年龄时间线**(默认) — 适合现实/历史/校园/恋爱/日常奇幻
  - 时间线 0-5 岁婴幼儿 → 6-12 童年 → 13-17 少年 → 18-35 青年 → 36-55 壮年 → 56+ 老年
  - 整场 4-6 次选择,选项间隔 ≥ 5 年
  - 死亡后输出「人生总结」+ 评价 S/A/B/C/D/E + `<LIFE_SIM_END>` 标记
- **方式 2 · 章节式推进**(剧情向世界观自动触发) — 适合转生到已有作品(欧布奥特曼/刀剑神域/RE:0/海贼王/Fate 等),或用户明确要求"经历 TV 剧情/按原作/章节式"
  - 触发条件:世界观是已有作品 / 含剧情向关键词 / 含结构化事件链 / 用户要求按章节
  - 标题改为 `## 第N章:章节名`(替代 `## N岁:阶段`),章节内部允许时间跳跃
  - 整场 8-15 章,每章至少 1 个关键选择,选项间隔不强制 5 年(按剧情节奏)
  - 死亡/结局更宽松:可战死、原作结局、回到现实、转生下一个世界
  - 优先按原作顺序走,空白处可自创原创剧情(合理且与原作风格一致)

### 模式 B — 游戏世界 RPG

按楼层/章节/冒险节点推进,带数值系统。适合刀剑神域 / GGO / 哥布林杀手 / 无职转生 / RE:0 / 异世界冒险 / 迷宫等。

凯伊会主动调 `rpg_*` 工具管理数值(HP/EXP/装备/技能/技能点),战斗推演完全由凯伊全权负责。

### 模式 C — DND 5E 跑团

凯伊是 DM + 数值管理者。所有不确定判定都用 `roll_dice` 掷骰(D20 检定 + DC),严格按 DND 5E 规则裁决。

- 六维属性 STR/DEX/CON/INT/WIS/CHA(adjustment = (属性-10)//2)
- 熟练加值 ⌈等级/4⌉ + 1(Lv1=+2, Lv5=+3)
- 职业 12 个(战士/法师/牧师/盗贼/野蛮人等)
- HP=0 时进入死亡豁免流程,3 次成功稳定 / 3 次失败死亡

## 用法示例

```
/创建 转生到欧布奥特曼的世界,这是与原版完全不一样的平行世界。O50行星的勇者之巅半山腰上,久久正在努力往上爬,他脚底一滑差一点掉下去,幸好千钧一发之际抓住了凸起的岩石,然后继续往上爬

/创建 转生到刀剑神域,跟着 TV 剧情走,纯叙事不要数值   # 章节式推进(模式 A)

/创建 转生到普通校园世界,我只想谈个恋爱

/创建 rpg 转生到刀剑神域第74层,我用星辰缎带开局

/创建 dnd 我是一名半精灵游侠,被遗忘的国度,灰鹰冒险

/do 1                    # 选第 1 项
/do 2      do              # 选第 2 项
/do 我决定先去跟久久打招呼    # 自定义行动
/do 上一段写得太惨了,重新写  # 反馈修正

/进度                       # 查看进度
/undo 2                     # 撤销最近 2 轮(含 lore 与 RPG 状态回滚)
/删除                       # 删档重来
```

**关键词自动识别:**

- 含 `dnd` / `龙与地下城` / `跑团` / `trpg` / `d20` → 模式 C
- 含 `rpg` / `数值` / `刀剑神域` / `ggo` / `迷宫` / `boss` / `经验值` → 模式 B
- 其他 → 模式 A(LLM 分析语境决定,可能升级为 B/C)

## 配置

WebUI → 插件管理 → 转生模拟器 → 配置,共 12 项:

### 模型路由

| 字段                      | 默认 | 说明                                            |
| ------------------------- | ---- | ----------------------------------------------- |
| `provider_id`             | `""` | 主 LLM 提供商(WebUI 下拉选择)。覆盖会话默认模型 |
| `provider_mode_a`         | `""` | 模式 A 专属 — 推荐便宜快速模型                  |
| `provider_mode_b`         | `""` | 模式 B 专属 — 需要 function calling             |
| `provider_mode_c`         | `""` | 模式 C 专属 — 推荐能力强模型                    |
| `compress_provider_id`    | `""` | 压缩任务专用 — 路由到便宜模型省成本             |
| `mode_detect_provider_id` | `""` | 模式识别专用 — 路由到便宜分类模型               |

优先级(每个字段):`field 专属 > provider_id > 会话默认(provider_id 空时)`

### 调参

| 字段                 | 默认  | 范围   | 说明                                |
| -------------------- | ----- | ------ | ----------------------------------- |
| `tool_max_steps`     | 30    | 5-100  | 模式 B/C 单次 LLM 调用最大工具步数  |
| `tool_call_timeout`  | 60    | 10-300 | 单次工具调用超时(秒)                |
| `max_history_chars`  | 60000 | —      | 叙事历史最大字符数,超过时压缩成摘要 |
| `keep_tail_messages` | 20    | —      | 压缩后保留的最近消息条数            |

### 开关

| 字段                     | 默认    | 说明                                                                       |
| ------------------------ | ------- | -------------------------------------------------------------------------- |
| `use_llm_compress`       | `true`  | 关闭则用纯规则抽取标题与世界观(快但粗)                                     |
| `use_llm_mode_detect`    | `true`  | 关闭则只用关键词匹配(快但简陋)                                             |
| `output_as_image`        | `false` | 开启后 `/创建` `/do` 的叙事输出自动渲染为图片(失败自动回退纯文本)           |
| `output_image_style_path`| `""`     | pillowmd 模板样式目录;样式带 `page>0` 多帧动画背景时输出 GIF(如独角兽gif)  |
| `output_image_auto_page` | `true`  | 渲染时自动分页(黄金分割比),避免超长单图                                   |

**模型路由推荐配置:**

- 模式 A → 便宜快速模型(无工具调用,省成本)
- 模式 B/C → 能力强模型(需要 function calling / 规则理解)
- 历史压缩 → 最便宜的模型(只是抽取要点)
- 模式识别 → 便宜的分类模型(简单 A/B/C 选择)

## 文件结构

```
astrbot_plugin_life_sim/
├── _conf_schema.json    # WebUI 配置 schema(12 项)
├── metadata.yaml         # 插件元数据
├── requirements.txt      # 无第三方依赖
├── README.md             # 本文档
├── main.py               # 主入口 - LifeSimPlugin 类
│                         #   - 4 个指令 + LLM 调度
│                         #   - 文件会话 (SimStore)
│                         #   - 历史压缩 (LLM + 规则)
│                         #   - 模式识别 (LLM + 关键词)
│                         #   - lore 暂存 + 多角色 character_lore
├── prompts.py            # 系统提示词 + 模式检测
│                         #   - COMMON_RULES (三模式共用)
│                         #   - SYSTEM_PROMPT_A / B / C
│                         #   - SUMMARY_SYSTEM_PROMPT (历史压缩)
│                         #   - MODE_DETECT_SYSTEM_PROMPT (模式识别)
│                         #   - 关键词 fallback _keyword_detect_mode
├── dice.py               # 骰子工具 (模式 C)
│                         #   - _roll_dice_expr (NdM, NdMk{h,l}X, +/-)
│                         #   - DiceMixin.roll_dice (LLM 工具)
├── rpg_tools.py          # RPG 工具全套(模式 B/C)
│                         #   - 27 个 rpg_* LLM 工具
│                         #   - DEFAULT_WORLD_RULES
│                         #   - RPGMixin 类(标注 self.data_dir / self.rpg_store)
├── storage_base.py       # JSON 文件存储公共原语
│                         #   - read_json / write_json_atomic(原子写)
│                         #   - safe_remove / ensure_dir
│                         #   - list_json_stems / sanitize_key
├── storage_sim.py        # SimStore:sim 会话存储
│                         #   - <data>/sim_sessions/<key>.json
│                         #   - asyncio.to_thread 包装同步 IO
├── storage_narrative.py  # NarrativeStore:剧情历史存储(独立于会话)
│                         #   - <data>/narrative_history/<scope>/<id>.json
├── storage_branch.py     # BranchStore:剧情分支快照存储(独立于会话)
│                         #   - <data>/sim_branches/<scope>/<name>.json
└── storage_rpg.py        # RpgStore:RPG 角色 + 会话存储
                          #   - <data>/rpg_saves/<uid>.json
                          #   - <data>/sessions/<sid>.json
                          #   - purge_group(group_id, sender_uid) 群/私聊清理
```

## 关键技术点

### 独立上下文 + 文件存储

叙事历史存到 `<data>/sim_sessions/<key>.json`,key 形如 `group_<gid>` / `user_<uid>`。
文件层不依赖 AstrBot 的 KV(原 KV 实现因并发工具调用存在"工具保存被外层覆写"的竞态,见更新日志 v3.0)。
LLM 调用时通过 `contexts=[...]` 显式传入,完全不走主对话的 `conversation_manager`。

### 剧情分支(独立快照存储)

- 分支快照存 `<data>/sim_branches/<scope>/<urlencoded_name>.json`,**不**随 sim 会话文件读写;
  分支名用 `quote_plus` 编码成安全文件名(兼容 `/ \ : *` 等非法字符),真实名存在快照的 `name` 字段。
- 每个分支是自包含的完整快照(messages / lore / RPG 数值 / 剧情历史全量),切换时整体还原,
  并覆盖 `narrative_history` 与 RPG 存档到分支点。
- 生命周期与会话绑定:`/创建`(覆盖重开)与 `/删除` 会同时清理该 scope 的全部分支快照;
  老版本存在 `session["branches"]` 里的数据会在首次使用 `/分支` 时自动迁移到独立存储。

### Markdown → 图片渲染(含 GIF)

- 渲染基于 pillowmd(无浏览器),`md_to_image.py` 统一处理 PNG / GIF 保存:
  - 样式 `elements.json` 带多帧动画背景(`page` > 0)时,渲染结果 `imageType == "gif"`,
    按逐帧保存为 `.gif`(`save_all` + `append_images` + 帧时长 + 无限循环);
    其余保存为 `.png`(多图分页取首张)。
  - 帧时长用 Pillow 正确的 `duration` 参数(pillowmd 库自带 `Save` 错写为 `duratio` 会被忽略)。
- 临时文件:渲染成功交给 `event.track_temporary_local_file`,由框架在事件处理完统一删除;
  渲染/保存失败时 `md_render_to_path` 先删临时文件再抛错,不残留空文件。

### 持久化 lore

- `character_lore`:dict 结构 `{角色名: [{section, content, updated_at}]}`,多角色并行支持;
  旧 list 结构自动迁移到"主角"桶。工具签名:
  ```
  life_sim_save_character_lore(content, section, character="主角")
  ```
- `world_lore`:list 结构 `[{section, content, updated_at}]`,同 section 覆盖。工具签名:
  ```
  life_sim_save_world_lore(content, section)
  ```
- 工具调用写入 `self._pending_lore` 实例暂存,**不立即落库**;`_generate_locked` 末尾与消息一起一次性 `_save_sim`,消除"工具内 save vs 外层 save"的竞态。
- `/undo` 用 turn 计数回滚 lore(每 turn 开始时拍快照),不受消息压缩/增删影响。

### RPG 状态快照与回滚

`_generate_locked` 每个 turn 起始调用 `_rpg_snapshot(event, mode)` 抓 RPG 数据快照:

- 群聊:`{group_id}_*.json` 全部角色 + 同 group_id 的全部 session。
- 私聊:当前 sender 的存档 + 该存档引用的 session(避免误删别人的私聊存档)。

`/undo` 找到对应 turn 的快照,先写回快照中存在的文件,再删"快照里没有但磁盘上有"的(被回滚期间新建的),统计 `restored_*` / `deleted_*`。

### 并发互斥

每个 session 一把 `asyncio.Lock`,挂在 `self._sim_locks[key]`。`/创建` `/do` `/undo` `/删除` 各自在命令入口取锁;`lock.locked()` 时返回"⏳ 上一条还在处理"提示,不等待。`asyncio.Lock` 不可重入,所以 `_generate` 不再取锁,锁完全在命令层互斥。

### 历史压缩

超过 `max_history_chars` 时:

1. 默认调 LLM 把前面消息压缩成一段【叙事历史摘要】(中文/英文/日文自适应)
2. 摘要含:世界观设定 + 主要阶段标题 + 早期用户决策 + 结局标记
3. LLM 失败自动回退规则抽取(快速但粗)
4. 摘要自身超 8000 字符硬截断兜底
5. 下次压缩时旧摘要会纳入 head,重新生成新摘要 — 摘要不会无限增长

### 模式识别

1. 显式 `/创建 rpg/dnd xxx` 前缀 > 自动判断
2. LLM 分析(默认开):传入世界观 + 关键词初判,LLM 输出 A/B/C 字母
3. LLM 失败回退关键词匹配(刀剑神域/GGO/dnd/跑团等)
4. 关闭 `use_llm_mode_detect` 可只用关键词(快)

### 指令前缀兼容

不硬编码 prefix — 通过 `text.find(cmd)` 取命令名首次出现位置之后的内容,自动适配 `/` / `!` / `！` / `~` / 无 prefix(私聊)等各种情况。

## 注意事项

- **每个群(或私聊)同时只有一段人生** — 新 `/创建` 会覆盖旧的,同时清理该群/私聊的 RPG 存档
- **依赖**:无第三方依赖,使用 AstrBot 自带能力
- **最低 AstrBot 版本**: `>=4.5.7`(用到 `llm_generate` 的 `system_prompt` / `contexts` 参数;老版本会自动回退)
- **上下文窗口**:大部分模型支持 32k-128k,设置 `max_history_chars` 在 30k-100k 之间比较合理

# B站动态记忆桥2 · 给 AI 的配置与验证文档

> 这份文档是写给 **AI 助手** 看的：照它把 **astrbot_plugin_bili_bridge2** 配好、验证通过，
> 让用户在 QQ 里直接问「最近刷到什么」「第 3 个讲了什么」「刷视频」就能用。
>
> 用户把插件装好后，会发给你这句话：
>
> **「请帮我配置好 AstrBot 里的B站动态记忆桥2插件，并验证是否可用。」**
>
> 收到这句就开始按本文档执行。

---

## 0. 前置插件（必须先装、先配好）

| 插件 | 作用 | 仓库 |
| --- | --- | --- |
| **bili-cli** | 本机取 B站 视频信息/字幕/搜索（桥2 靠它取素材） | https://github.com/kukemc/bili-cli |
| **B站 AI Bot** | 主动刷片、评价、写看片记录（桥2 的数据来源） | https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot |
| **我会永远陪着你**（astrbot_plugin_private_companion） | QQ 侧的陪伴/记忆插件（**可选**；桥2 不依赖它，但用户通常一起用） | https://github.com/menglimi/astrbot_plugin_private_companion |

安装顺序（不要跳）：

1. 装 **bili-cli**，`bili auth login` 扫码登录，能跑通 `bili video info BV1xx` 和 `bili video subtitle BV1xx`；
2. 在 AstrBot 里装 **B站 AI Bot**，按它自己的施工文档配好（能主动刷片、能往 QQ 推视频）；
3. 装 **astrbot_plugin_bili_bridge2**（本文档的对象）；
4. 用户发那句配置请求，AI 按本文档配置 + 验证。

> 桥2 不复制 B站 AI Bot 的业务逻辑：它读它写出来的 `watch_log.json` / `video_memory.json`，
> 用 bili-cli 补细节。所以 **B站 AI Bot 跑不起来，桥2 就没有数据可读**。

---

## 1. 这个插件能做什么

| 功能 | 用户在 QQ 里怎么说 / 怎么发 | 桥2 做什么 |
| --- | --- | --- |
| 最近刷片清单 | 「最近刷到了什么」「今天刷了啥」 | 直接回一份 5~10 条的清单（标题 + UP + BV），延迟 2~5 秒 |
| 问时间 / 刷没刷 | 「最近刷视频是在什么时候」「刚刚刷视频了吗」 | 只回一句时间和条数，不甩清单 |
| 追问某条细节 | 「第 3 个讲了什么」「BV1xxx 讲了啥」「悬链线那条讲了什么」 | 序号/BV/片名都能认：片名用标题模糊匹配找回 BV；优先用 24 小时内的分析缓存，不够就现拉字幕 |
| 又问一遍清单 | 一分钟内再发一次「最近刷到了什么」 | 把刚才那份清单塞给她当参考，让她用自己的话挑两三条带过，不整块复读 |
| 按关键词找视频 | 「那个讲悬链线的」 | 在标题/摘要/感想里搜，给候选 BV，再读细节 |
| 说一句去刷一轮 | 「刷视频」「去刷点B站」「再刷一次」 | 直接调 B站 AI Bot 的刷片流程，跑完把这一轮结果推回 QQ |
| 分享卡片/短链 | 转发 B站 视频卡片、发 b23.tv 链接 | 解析成 BV，取素材，再回答 |
| 记忆注入 | 问刷片相关问题时 | 把「最近一轮」的时间/清单临时挂到这一轮请求上，避免她现编时间 |
| 记忆里的「B站近况」 | （自动，无需说话） | 启动后和每次发清单时，把最近一轮写进 MemoryCompanion 的 core block `bili-digest-core`；内容没变就不写 |
| 清单存多少 / 发给谁 | 「最近刷到了什么」 | 清单里存最近 20 条（`recent_store_limit`），发出去只有 5~10 条；再问时把**完整 20 条**给她当参考，并告诉她哪几条没发出去 |
| 推送进记忆 | 「你推过什么」「上周发给我哪些」 | 直接从 B站 AI Bot 的推荐账本翻，回一份带日期的清单（不靠记忆库检索） |
| QQ 日记同步 | （自动，无需说话；手动 `/diary_sync`） | AstrBot 每次启动后等 `diary_sync_delay_minutes`（默认 3 分钟）跑一次 `sync_qq_diary.py`，把陪伴插件写好的日记同步进 B站 记忆 |

---

## 2. 安装位置与数据路径

```
<AstrBot>\data\plugins\astrbot_plugin_bili_bridge2\        ← 插件代码
<AstrBot>\data\config\astrbot_plugin_bili_bridge2_config.json   ← 配置
<AstrBot>\data\plugin_data\astrbot_plugin_bilibili_ai_bot\      ← B站 AI Bot 数据（桥2 读取）
    bilibot.sqlite3        ★ 2.0 起的主数据源：
                             activity_log(kind='watch')    每轮看了什么
                             keyed_json(kind='video_memory') 每条视频的完整解析（24h 缓存）
                             keyed_json(state.owner_recommend_log.json) 推送顺序
    watch_log.json         1.x 旧文件；2.0 只在升级时导入一次，桥2 仅作兜底
    video_memory.json      同上，兜底用
    owner_last_push.json   同上，兜底用
```

`ai_bot_data_dir` 留空时桥2 自动找 `data/plugin_data/astrbot_plugin_bilibili_ai_bot`；
数据目录不在默认位置才需要填。

---

## 3. 配置步骤

### 3.1 装插件

二选一：

```powershell
# A. 从 git 装
cd <AstrBot>\data\plugins
git clone https://github.com/zuoyu123654/astrbot_plugin_bili_bridge2.git
```

或直接把插件目录放进 `<AstrBot>\data\plugins\`，确保目录名是 `astrbot_plugin_bili_bridge2`。

### 3.2 填配置

文件：`<AstrBot>\data\config\astrbot_plugin_bili_bridge2_config.json`
（没有就从默认值建一份；json 要 **UTF-8 无 BOM**）

```json
{
  "owner_uids": "<主人的QQ号>",
  "bili_executable": "bili",
  "ai_bot_data_dir": "",
  "recent_limit": 10,
  "recent_limit_min": 5,
  "subtitle_max_chars": 1800,
  "command_timeout": 90,
  "enable_card_share": true,
  "enable_watch_trigger": true,
  "watch_words": "",
  "watch_min_gap": 180,
  "watch_memory_hours": 24,
  "recent_list_max_chars": 12,
  "list_repeat_window": 600
}
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `owner_uids` | 1792559819 | 允许触发的 QQ 号，多个用英文逗号。**必须填对**，否则插件不响应 |
| `bili_executable` | `bili` | bili-cli 可执行文件；PATH 里没有就写绝对路径 |
| `ai_bot_data_dir` | 空 | B站 AI Bot 数据目录，留空自动找 |
| `recent_limit` / `recent_limit_min` | 10 / 5 | 清单条数在 min~max 之间随机 |
| `subtitle_max_chars` | 1800 | 字幕截断长度 |
| `command_timeout` | 90 | 单条 bili 命令超时（秒） |
| `enable_card_share` | true | 是否解析分享卡片/短链 |
| `enable_watch_trigger` | true | 是否允许「刷视频」触发一轮 |
| `watch_words` | 空 | 额外触发词，逗号分隔 |
| `watch_min_gap` | 180 | 两次口令触发最小间隔（秒） |
| `watch_memory_hours` | 24 | 刷片回忆窗口（小时），也是 24h 缓存的判定窗口 |
| `recent_list_max_chars` | 12 | 问「刷到什么」时，句子超过这个长度就不直接发清单，交给模型说 |
| `list_repeat_window` | 600 | 清单重复保护窗口（秒），窗口内再问不重复列 |

### 3.3 重载插件

AstrBot 面板 → 插件 → 「B站动态记忆桥2」→ 重载；或者用面板 API：

```
POST /api/v1/plugins/astrbot_plugin_bili_bridge2/reload
```

日志出现 `Plugin astrbot_plugin_bili_bridge2 (1.2.x) by paopao` 即加载成功。

---

## 4. 验证（必须真的跑一遍，不要只看配置）

### 4.1 工具是否注册

日志里应有：

```
Added llm tool: bili_bridge2_last_round
Added llm tool: bili_bridge2_find
Added llm tool: bili_bridge2_recent
Added llm tool: bili_bridge2_read
```

### 4.2 四项功能测试（在 QQ 主号里发，别在群里）

| # | 发这句 | 期望 | 日志关键字 |
| --- | --- | --- | --- |
| 1 | `最近刷视频是在什么时候？` | 一句话：「最近一轮在 X」 | `只问时间/有没有刷，回一句` |
| 2 | `最近刷到了什么` | 清单 5~10 条，延迟 2~5 秒 | `最近清单 N 条` |
| 3 | 紧接着再发 `最近刷到了什么` | 她用自己话挑两三条（片名来自最近一轮），不复读整份清单 | `刚发过清单…交给她说（清单当参考）` / `已注入清单参考` |
| 4 | `BV1WJY56aEbd 讲了什么`（换成清单里真实的 BV） | 用缓存/字幕讲内容 | `命中近 24 小时缓存` 或 `已注入素材` |
| 5 | `悬链线那条讲了什么`（换成清单里真实的片名） | 认出是哪条并讲内容 | `按片名命中 BV…` |

### 4.3 刷一轮（可选，会真的跑）

发 `刷视频` → 期望：

```
口令直接触发 AI Bot 刷一轮：N 个
```

约 1~2 分钟后 QQ 收到一条合并的推荐（**合并是 B站 AI Bot 做的**，桥2 只负责触发和说明；细节见 B站 AI Bot 的施工文档 12.3）；期间再发会被 3 分钟冷却挡住（`口令太密（还剩 X 秒）`）。

### 4.4 数据链路自检

```powershell
# 2.0 的主数据源：看片记录（activity_log）
$db = "<AstrBot>\data\plugin_data\astrbot_plugin_bilibili_ai_bot\bilibot.sqlite3"
python -c "import sqlite3,json;c=sqlite3.connect(r'$db');[print(json.loads(r[0]).get('time'),json.loads(r[0]).get('bvid')) for r in c.execute(\"select data from activity_log where kind='watch' order by id desc limit 3\")]"
```

看片记录一直在涨、最近一条是今天的时间，就说明链路通；如果时间停在几天前，是 B站 AI Bot 没刷，不是桥2 的事。

### 4.5 清单存了多少、记忆里的「B站近况」是不是新的

问一次「最近刷到了什么」，日志里应出现：

```
[Bridge2] 最近清单：存 20 条，发 6 条（5~10 随机）
```

`存 20` 是 `recent.json` 里留的完整清单，`发 6` 是真正发到 QQ 的条数。
再问一遍时日志是 `已注入清单参考（20 条）`——她拿到的是完整那份，并且知道哪几条没发出去。

记忆那边只剩一条「B站近况」（core block），发句话就会刷：

```
[Bridge2] 已更新记忆里的「B站近况」（8 条，最近 2026-11-09 02:42）
```

确认 core block 的时间是今天：

```powershell
python -c "import sqlite3;c=sqlite3.connect(r'<AstrBot>\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db');print(list(c.execute(\"select updated_at, substr(content,1,60) from memories where id='bili-digest-core'\")))"
```

> 1.3.5 起不再往记忆库写「推送记录」，改用 `recent.json` 的 20 条清单。
> 1.3.3/1.3.4 写进去的那批 `bili-round-*`（20 条）已于 2026-11-09 清空（走 MemoryCompanion 的
> `/page/memory/delete`，FTS 和向量索引一起清）；1 版留下的同类记录没动。

### 4.6 QQ 日记同步（AstrBot 启动后）

桥2 里带着「QQ 日记 → B站 记忆」的同步。它**不是开机定时**，是跟着 AstrBot 的生命周期：

```
AstrBot 启动 → 等 diary_sync_delay_minutes（默认 3 分钟）→ 跑 sync_qq_diary.py → 写日志
```

为什么这样：日记是陪伴插件在 AstrBot 里生成的，AstrBot 没起来就没有新日记；
开机就倒计时的话会跑空，AstrBot 中途打开又不会补跑。

配置（面板 → 桥2 配置）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `diary_sync_enable` | true | 关掉就不排队 |
| `diary_sync_delay_minutes` | 3 | 给陪伴插件补写留的时间 |
| `diary_sync_script` | `D:\app1\tools\sync_qq_diary.py` | 同步脚本 |
| `diary_sync_python` | 空 | 留空用 AstrBot 自己的解释器 |
| `diary_sync_extra_args` | `--no-wait` | 启动这次不空等；每日 00:05 的计划任务才等日记 |
| `diary_sync_log` | `D:\app1\logs\qqdiary_sync.log` | 脚本输出追加到这里 |

日志里应有：

```
[Bridge2] 日记同步已排队：AstrBot 启动后 3 分钟跑一次（D:\app1\tools\sync_qq_diary.py）
[Bridge2] 日记同步已在后台跑（startup）：...python.exe -u D:\app1\tools\sync_qq_diary.py --no-wait
[Bridge2] 同步结果看 D:\app1\logs\qqdiary_sync.log（写到最后一行就是这次的结论）
```

**结论看日志文件，不看 AstrBot 日志**：脚本是后台进程，输出直接追加进
`qqdiary_sync.log`，最后几行长这样：

```
  2026-11-09 已在记忆里，跳过
没有新增
```

或者 `已写入 1 条到 memory.json，并标记待同步（重启 B站插件后并入 SQLite）`。
写完 memory.json 后要**重启 B站 AI Bot / AstrBot** 才会并进 `bilibot.sqlite3`。

> 为什么不让桥2 等它跑完：Windows 上等子进程常常卡在「子进程退出了、管道 EOF 不来」，
> 会把插件的一个协程挂死。改成“点火就走”，脚本自己把结果写进日志。
> 手动补一次发 `/diary_sync`，回你「已启动（后台）」。

> 这个功能 1.4.0 从独立插件 `astrbot_plugin_qq_diary_sync` 并进来；那个插件已删除，
> 面板里不用再装。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 发什么都不回 | `owner_uids` 没填对，或消息不在私聊；桥2 只响应白名单私聊 |
| 清单永远空 | B站 AI Bot 还没刷过，或 `ai_bot_data_dir` 指错；先手动让它刷一轮 |
| 「取素材失败」 | bili-cli 没装/没登录；先 `bili auth login`，再 `bili video subtitle BV1xx` 验证 |
| 说「刷视频」没反应 | AI Bot 没跑起来，或 `enable_watch_trigger=false`，或还在 3 分钟冷却 |
| 和桥1 抢消息 | 两个都别同时开；桥1 是旧版，功能重叠 |
| 问「最近刷了什么」还是报几天前的片名 | 1.3.3 起：清单刚发过又被问，桥2 把刚才那份清单塞给她当参考（日志 `已注入清单参考`），让她用自己的话挑两三条，允许带一点自己的印象。若还报旧片名，看她那句注入有没有生效、`recent.json` 是不是还停在旧日期 |
| 记忆里的「B站近况」停在老日期 | 那条 core block 本来是 1 版（bili_bridge）写的，桥2 接管后没人写就冻住了。1.3.1 起桥2 自己写：启动后 5 分钟内、以及每次发清单时刷新，日志 `已更新记忆里的「B站近况」`。若报 `label_conflict`，说明记忆里已有同名不同 id 的块，删掉旧的再重载 |
| 她记不住推过什么 | 1.3.5 起问「你推过什么」直接从推荐账本翻（日志 `推送历史 N 条`），不靠记忆检索；清单本身也存最近 20 条 |
| 提到片名却说「没认出视频」 | 1.3.1 加了标题模糊匹配；两三个字的短名（如「抓饭」）仍可能认不出，让她说 BV 或用「第几个」 |
| 清单太长/太短 | 调 `recent_limit_min` / `recent_limit` |
| 日记没同步 / `/diary_sync` 报 exit=2 | 陪伴插件那天没生成日记；先按 `QQ日记同步与陪着你日记修复-施工文档.md` 第 11 节补写，再跑一次 |
| 问「什么时候」却给清单 | 把 `recent_list_max_chars` 调小（如 8） |

---

## 6. 回滚

- 配置：删掉 `astrbot_plugin_bili_bridge2_config.json` 里的新键，或整份恢复默认
- 插件：面板里停用/卸载，或把插件目录移走
- 数据：桥2 不写 B站 AI Bot 的数据，回滚不影响它

---

## 7. 给 AI 的汇报模板

配置完成后回给用户：

> 桥2 配好了：`owner_uids` 已设成你的 QQ；bili-cli 在 PATH 里；B站 AI Bot 数据目录找到了，里面有 N 条看片记录。
> 四项验证都过了：问时间只回一句、问「刷到了什么」给清单、再问一遍会原样再念同一份、按 BV 或片名能读细节。
> 现在可以直接在 QQ 里说「最近刷到了什么」或「刷视频」。
>
> （失败时写清楚卡在哪一步：owner_uids 不对 / 找不到 watch_log / bili-cli 没登录 / 插件没加载。）

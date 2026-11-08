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
| 追问某条细节 | 「第 3 个讲了什么」「BV1xxx 讲了啥」 | 优先用 24 小时内的分析缓存；不够就现拉字幕；追问「再具体讲讲」会重新拉一次 |
| 按关键词找视频 | 「那个讲悬链线的」 | 在标题/摘要/感想里搜，给候选 BV，再读细节 |
| 说一句去刷一轮 | 「刷视频」「去刷点B站」「再刷一次」 | 直接调 B站 AI Bot 的刷片流程，跑完把这一轮结果推回 QQ |
| 分享卡片/短链 | 转发 B站 视频卡片、发 b23.tv 链接 | 解析成 BV，取素材，再回答 |
| 记忆注入 | 问刷片相关问题时 | 把「最近一轮」的时间/清单临时挂到这一轮请求上，避免她现编时间 |

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
| 3 | 紧接着再发 `最近刷到了什么` | 不重复列，她用自己的话回 | `刚发过清单…交给模型` / `注入「别重复」说明` |
| 4 | `BV1WJY56aEbd 讲了什么`（换成清单里真实的 BV） | 用缓存/字幕讲内容 | `命中近 24 小时缓存` 或 `已注入素材` |

### 4.3 刷一轮（可选，会真的跑）

发 `刷视频` → 期望：

```
口令直接触发 AI Bot 刷一轮：N 个
```

约 1~2 分钟后 QQ 收到一条合并的推荐；期间再发会被 3 分钟冷却挡住（`口令太密（还剩 X 秒）`）。

### 4.4 数据链路自检

```powershell
# watch_log 有记录
Get-Content <AstrBot>\data\plugin_data\astrbot_plugin_bilibili_ai_bot\watch_log.json -Raw | ConvertFrom-Json | Select -Last 3 time,bvid,title

# video_memory 有解析（24h 缓存来源）
Get-Content <AstrBot>\data\plugin_data\astrbot_plugin_bilibili_ai_bot\video_memory.json -Raw | ConvertFrom-Json | Select -Last 1
```

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 发什么都不回 | `owner_uids` 没填对，或消息不在私聊；桥2 只响应白名单私聊 |
| 清单永远空 | B站 AI Bot 还没刷过，或 `ai_bot_data_dir` 指错；先手动让它刷一轮 |
| 「取素材失败」 | bili-cli 没装/没登录；先 `bili auth login`，再 `bili video subtitle BV1xx` 验证 |
| 说「刷视频」没反应 | AI Bot 没跑起来，或 `enable_watch_trigger=false`，或还在 3 分钟冷却 |
| 和桥1 抢消息 | 两个都别同时开；桥1 是旧版，功能重叠 |
| 重复问清单不给清单 | 正常，`list_repeat_window` 默认 600 秒内不重复 |
| 清单太长/太短 | 调 `recent_limit_min` / `recent_limit` |
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
> 四项验证都过了：问时间只回一句、问「刷到了什么」给清单、重复问不重复列、按 BV 能读细节。
> 现在可以直接在 QQ 里说「最近刷到了什么」或「刷视频」。
>
> （失败时写清楚卡在哪一步：owner_uids 不对 / 找不到 watch_log / bili-cli 没登录 / 插件没加载。）

# astrbot_plugin_bili_bridge2 · B站动态记忆桥2

把 **B站 AI Bot** 刷到的视频接进 QQ：问「最近刷到了什么」直接给清单，问「第 3 个讲了什么」按 BV 读细节，
说一句「刷视频」就真让它跑一轮。

> 桥2 **不自己刷 B站**：它读 B站 AI Bot 2.0 的 `bilibot.sqlite3`（旧版 `watch_log.json` 兼容），
> 再用本机 **bili-cli** 补字幕细节。所以这两个前置必须先在。

## 关于日记同步（1.4.1 起）

- `diary_sync_enable` **默认关闭**：这是会执行外部 Python 脚本的功能，不该默认开着。
- `diary_sync_script` / `diary_sync_log` 留空时用**插件数据目录**里的
  `sync_qq_diary.py` / `qqdiary_sync.log`，不再指向作者本机路径。
- 口令触发不再在「TextPart 不可用」时改写用户消息，只记一条警告。

## 前置插件

| 插件 | 作用 | 仓库 |
| --- | --- | --- |
| **bili-cli** | 取视频信息、字幕、搜索 | https://github.com/kukemc/bili-cli |
| **B站 AI Bot** | 主动刷片、评价、写看片记录 | https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot |
| **我会永远陪着你**（可选） | QQ 侧陪伴/记忆 | https://github.com/menglimi/astrbot_plugin_private_companion |

## 安装后怎么配

装好上面三个、并确认 B站 AI Bot 能正常刷片之后，把插件装进 AstrBot，然后对 AI 说：

> **请帮我配置好 AstrBot 里的B站动态记忆桥2插件，并验证是否可用。**

AI 会照 [SETUP-FOR-AI.md](SETUP-FOR-AI.md) 配好并跑四项验证。手配的话看那份文档第 3 节。

## 功能

- **最近刷片清单**：「最近刷到了什么」「今天刷了啥」→ 5~10 条（标题 + UP + BV），2~5 秒延迟
- **只问时间/刷没刷**：「最近刷视频是在什么时候」→ 一句话，不甩清单
- **按 BV 读细节**：「第 3 个讲了什么」「BV1xxx 讲了啥」→ 24 小时内命中分析缓存，追问再拉字幕
- **按片名读细节**：「悬链线那条讲了什么」→ 标题模糊匹配找回 BV，再讲内容
- **再问一遍清单**：10 分钟内又问一次 → 把完整清单（最近 20 条）交给她当参考，她知道哪几条没发出去，自己挑两三条说
- **记忆里的「B站近况」自动续命**：启动后 / 每次发清单时把最近一轮写进记忆（core block `bili-digest-core`），
  不用再靠 1 版那个已经停更的同步
- **你推过什么**：「上周发给我哪些视频」→ 从 B站 AI Bot 的推荐账本翻，不靠记忆检索
- **QQ 日记同步**：AstrBot 每次启动后等 3 分钟，跑一次 `sync_qq_diary.py`，把陪伴插件写好的日记同步进 B站 记忆；
  手动补一次发 `/diary_sync`
- **关键词找视频**：「那个讲悬链线的」→ 搜标题/摘要/感想，给候选 BV
- **说一句去刷一轮**：「刷视频」「再刷一次」→ 调 B站 AI Bot 的刷片流程，跑完推回 QQ（3 分钟冷却）
- **卡片/短链解析**：转发 B站 卡片或 b23.tv 链接 → 解析 BV、取素材、回答
- **记忆注入**：问刷片相关问题时，把「最近一轮」临时挂到请求上，避免现编时间

## 配置项

见 [SETUP-FOR-AI.md](SETUP-FOR-AI.md) 第 3.2 节（`owner_uids` 必填，其余有默认值）。

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

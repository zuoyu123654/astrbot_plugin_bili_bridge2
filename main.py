"""B站动态记忆桥 2 —— 把「B站 AI Bot」刷到的视频接进 QQ。

和 1 版的区别：数据源从 DSH 刷B站的 digest 文件，换成 AstrBot 的「B站 AI Bot」插件
（读它的 watch_log.json），刷一轮也交给它自己的 bili_watch_videos 工具。

四件事：
1. 分享卡片 → 取 BV → bili-cli 拉信息+字幕 → 注入这一轮，让她用自己的话说
2. 问「最近刷到什么」→ 直接回带序号的清单（来自 AI Bot 的看片记录）
3. 问「第几个/第几条讲了什么」→ 序号换 BV → bili-cli 现拉字幕
4. 说「刷视频 / 刷B站 / 看视频」→ 引导模型调用 B站 AI Bot 的 bili_watch_videos 去跑一轮

说明：这一版**不做链接（b23.tv）解析**，只认卡片和 BV 号。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:  # AstrBot 4.17+
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    TextPart = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_bili_bridge2"
#: 和 metadata.yaml 的 version 必须一致，改版本时两处一起改
PLUGIN_VERSION = "1.6.0"
# QQ 日记 → B站 记忆的同步（AstrBot 启动后跑一次；脚本路径可以改）
# 默认值不再指向作者本机路径：放到本插件自己的 plugin_data 里。
# 真正的路径在运行时用 _diary_default_script() / _diary_default_log() 算。
DEFAULT_DIARY_SCRIPT = "sync_qq_diary.py"
DEFAULT_DIARY_LOG = "qqdiary_sync.log"
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# 老式 av 号链接（bilibili.com/video/av12345）：BV_RE 认不出，单独兜一下
AV_RE = re.compile(r"(?:^|[/=?&])av(\d{3,12})", re.I)
# B站分享卡片里放的是短链（b23.tv / bili2233.cn），不带 BV，得跟一次 302 才拿得到
SHORT_RE = re.compile(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]{4,16})")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# 这些词一出现，基本是在问「上次刷到了什么 / 那条讲了啥」，不是在喊她去刷
WATCH_CMD_RE = re.compile(
    r"(?:随机|随便|再|去|帮我|给我|帮|快|现在|马上|赶紧|立刻|麻烦|来)?\s*"
    r"(?:"
    r"(?:刷|看|逛)\s*(?:一?轮|一?次|一?会儿|点|个|下)?\s*"
    r"(?:视频|b站|B站|哔哩|bilibili|小破站)"
    r"|刷\s*(?:一?轮|一?次|一?会儿)"
    r"|再来\s*(?:一?轮|一?次)"
    r")",
    re.IGNORECASE,
)

WATCH_RECALL_RE = re.compile(
    r"(?:上次|上一次|上回|最近|刚才|刚刚|今天|昨天|昨晚|这几天|这一轮|这轮)"
    r"(?:.{0,10}?)(?:刷|看|刷到|看过)"
    r"|(?:刷|看)(?:了|过)?(?:什么|啥|哪些|哪几个|哪条)"
    r"|(?:刷|看)到(?:什么|啥|哪些)"
    r"|(?:第\s*[0-9一二三四五六七八九十]+\s*个)(?:.{0,6}?)(?:什么|啥|讲|内容)",
    re.IGNORECASE,
)

WATCH_BLOCKERS = (
    "上次", "上一次", "刚才", "刚刚", "什么时候", "几点", "多久",
    "哪一条", "哪条", "哪个", "什么", "啥", "讲了", "讲什么",
    "内容", "详情", "记忆", "看过", "刷过什么", "刷到过",
)

DEFAULT_WATCH_WORDS = ("刷视频", "刷b站", "刷B站", "看视频", "刷会儿b站")
RECENT_Q_RE = re.compile(r"(最近|今天|刚才|刚刚).{0,6}(刷|看).{0,6}(什么|啥|哪些|视频|片子)")
# 「刷的都啥」「看过的有哪些」这种没带时间词的说法，也得算问清单
RECENT_Q_RE2 = re.compile(r"(?:刷|看)(?:的|过|了)(?:都)?(?:是)?(?:什么|啥|哪些|哪几个|哪几条)")
# 问某条细节的说法：「X那条讲了什么」「X是啥意思」
DETAIL_WORD_RE = re.compile(r"(讲|内容|什么|啥|意思|细节|怎么回事)")
# 「你推过什么」「上周发给我哪些」——问推送历史，不是问最近刷片
PUSH_RECALL_RE = re.compile(
    r"(推|发|分享)(给|过|了)?(我|你)?(的)?(那些|什么|啥|哪些|几条|视频)"
    r"|推过什么|推过哪些|推荐过什么|发过什么视频"
)
ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]{1,2})\s*(?:个|条|支|部)?")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 「再具体讲讲 / 展开说说 / 后来呢」这类是在追问上一条的细节
DETAIL_Q_RE = re.compile(r"(再|具体|详细|展开|细说|多说|深入|后来|然后呢|为什么|怎么回事)")


@register(PLUGIN_NAME, "paopao", "把 B站 AI Bot 刷到的视频接进 QQ", PLUGIN_VERSION)
class BiliBridge2(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._pending: dict[str, str] = {}
        self._cache_hit: dict[str, tuple[str, float]] = {}
        self._last_watch = 0.0
        # 老大分享过来的视频：后台真看一遍，看完写进记忆（会镜像进中枢）
        self._share_watching: set[str] = set()
        self._share_watched: dict[str, float] = {}
        self._share_watch_tasks: set["asyncio.Task"] = set()
        self._share_watch_ttl = 6 * 3600
        self._share_done: tuple[float, str, str] | None = None
        # 分享时拉到的字幕/简介，完整分析跑不动时拿它兜底写记忆
        self._last_share_material: dict[str, tuple[float, str]] = {}
        self._digest_fp = ""
        self._digest_checked = 0.0
        self._last_list_shown = 0
        self._diary_task: "asyncio.Task | None" = None
        self._data_dir = self._plugin_data_dir()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._recent_path = self._data_dir / "recent.json"
        legacy_recent = Path(__file__).resolve().parent / "recent.json"
        if legacy_recent.is_file() and not self._recent_path.is_file():
            try:
                shutil.copy2(legacy_recent, self._recent_path)
                logger.info("[Bridge2] 已把旧的 recent.json 迁到插件数据目录")
            except OSError:
                pass

    # ── 生命周期：AstrBot 起来后补写并同步 QQ 日记到 B站 ──────────────────
    async def initialize(self) -> None:
        if not self._diary_bool("diary_sync_enable", False):
            logger.info("[Bridge2] 日记同步已关（diary_sync_enable=false），不排队")
            return
        delay = max(0, self._diary_int("diary_sync_delay_minutes", 3))
        logger.info(
            "[Bridge2] 日记同步已排队：AstrBot 启动后 %s 分钟跑一次（%s）",
            delay,
            self._diary_str("diary_sync_script", self._diary_default_script()),
        )
        self._diary_task = asyncio.create_task(self._diary_sync_later(delay))

    async def terminate(self) -> None:
        if self._diary_task and not self._diary_task.done():
            self._diary_task.cancel()

    async def _diary_sync_later(self, delay_minutes: int) -> None:
        try:
            await asyncio.sleep(delay_minutes * 60)
            await self._run_diary_sync("startup")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 启动日记同步失败：%s", exc)

    def _diary_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return default

    def _diary_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key))
        except (TypeError, ValueError):
            return default

    def _diary_str(self, key: str, default: str) -> str:
        value = self.config.get(key)
        return str(value).strip() if value is not None and str(value).strip() else default

    async def _run_diary_sync(self, reason: str) -> str:
        """起一个后台进程跑 sync_qq_diary.py（不等它，输出直接追加进日志）。

        为什么不等：AstrBot 里等子进程在 Windows 上会碰到「子进程已经退出了、
        管道 EOF 却迟迟不来」的卡死；同步脚本自己就把结果写进日志，
        这边只负责“点火”，不负责“等它烧完”。
        """

        script = Path(self._diary_str("diary_sync_script", self._diary_default_script()))
        if not script.is_file():
            logger.warning("[Bridge2] 日记同步脚本不存在：%s", script)
            return "脚本不存在"
        python = self._diary_str("diary_sync_python", "") or sys.executable
        args = [python, "-u", str(script)]
        extra = self._diary_str("diary_sync_extra_args", "")
        if extra:
            args += shlex.split(extra)
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            log_path = Path(self._diary_str("diary_sync_log", self._diary_default_log()))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("a", encoding="utf-8")
            log_file.write(f"[{started}] start ({reason}): {' '.join(args)}\n")
            log_file.flush()
            subprocess.Popen(
                args,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 日记同步起不来：%s", exc)
            return f"起不来：{exc}"
        logger.info("[Bridge2] 日记同步已在后台跑（%s）：%s", reason, " ".join(args))
        logger.info("[Bridge2] 同步结果看 %s（写到最后一行就是这次的结论）", log_path)
        return "已启动（后台）"

    @filter.command("diary_sync")
    async def diary_sync_command(self, event: AstrMessageEvent):
        """手动补一次 QQ 日记 → B站 同步（等同等 AstrBot 启动后自动跑的那次）。"""

        if not self._is_owner(event):
            logger.warning(
                "[Bridge2] 非主人触发了 /diary_sync，已忽略：%s",
                str(getattr(event, "get_sender_id", lambda: "")() or "?"),
            )
            return
        result = await self._run_diary_sync("manual")
        yield event.plain_result(f"日记同步：{result}")

    # ── 配置/工具 ────────────────────────────────────────────────────────
    def _diary_default_script(self) -> str:
        return str(self._plugin_data_dir() / DEFAULT_DIARY_SCRIPT)

    def _diary_default_log(self) -> str:
        return str(self._plugin_data_dir() / DEFAULT_DIARY_LOG)

    def _owners(self) -> set[str]:
        raw = str(self.config.get("owner_uids") or "").strip()
        if not raw:
            if not getattr(self, "_owner_warned", False):
                self._owner_warned = True
                logger.warning(
                    "[Bridge2] 还没填 owner_uids —— 主人的 QQ 号是必填项，"
                    "不填的话所有私聊指令都不会响应"
                )
            return set()
        return {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) in self._owners()
        except Exception:  # noqa: BLE001
            return False

    def _plugin_data_dir(self) -> Path:
        """插件运行数据统一放 data/plugin_data/<插件名>/，不写插件安装目录。"""

        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001
            return Path(__file__).resolve().parent.parent.parent / "plugin_data" / PLUGIN_NAME

    def _ai_bot_dir(self) -> Path:
        raw = str(self.config.get("ai_bot_data_dir") or "").strip()
        if raw:
            return Path(raw)
        return Path(__file__).resolve().parent.parent.parent / "plugin_data" / "astrbot_plugin_bilibili_ai_bot"

    async def _bili(self, *args: str, timeout: int | None = None) -> str:
        exe = self._bili_exe()
        limit = int(timeout or self.config.get("command_timeout") or 90)

        def run() -> str:
            try:
                env = dict(os.environ)
                # AstrBot 进程的 PATH 未必有 bili / ffmpeg，把它所在目录补进去
                exe_dir = str(Path(exe).resolve().parent) if os.path.sep in exe else ""
                if exe_dir:
                    env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
                p = subprocess.run([exe, *args], capture_output=True, text=True,
                                   encoding="utf-8", errors="ignore", timeout=limit, env=env)
                return (p.stdout or "") + (p.stderr or "")
            except FileNotFoundError:
                return f"[bili-cli 没找到: {exe}]"
            except subprocess.TimeoutExpired:
                return "[bili-cli 超时]"
            except Exception as exc:  # noqa: BLE001
                return f"[bili-cli 出错: {exc}]"

        return await asyncio.to_thread(run)

    def _bili_exe(self) -> str:
        """找一个真能跑的 bili。配置里写 bili 时，按 PATH → venv Scripts → 已知安装位置兜底。"""

        raw = str(self.config.get("bili_executable") or "bili").strip()
        if raw and os.path.sep in raw and Path(raw).is_file():
            return raw
        found = shutil.which(raw) if raw else None
        if found:
            return found
        candidates = [
            Path(sys.executable).resolve().parent / "Scripts" / "bili.exe",
            Path(sys.executable).resolve().parent / "bili.exe",
            Path(r"C:\QQBot\Python312\Scripts\bili.exe"),
            Path.home() / ".local" / "bin" / "bili.exe",
            Path.home() / ".local" / "bin" / "bili",
        ]
        for cand in candidates:
            if cand.is_file():
                logger.info("[Bridge2] bili 不在 PATH，改用 %s", cand)
                return str(cand)
        return raw or "bili"

    # ── 看片记录 ────────────────────────────────────────────────────────
    def _ai_bot_db(self) -> Path:
        return self._ai_bot_dir() / "bilibot.sqlite3"

    def _watch_records_from_db(self) -> list[dict]:
        """B站 AI Bot 2.0+ 把看片记录搬进了 bilibot.sqlite3 的 activity_log 表。"""

        db = self._ai_bot_db()
        if not db.is_file():
            return []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            rows = list(con.execute(
                "select data from activity_log where kind='watch' order by id"))
            con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 读 bilibot.sqlite3 看片记录失败：%s", exc)
            return []
        records = []
        for (raw,) in rows:
            try:
                item = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            try:
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            records.append({
                "bvid": item["bvid"],
                "title": item.get("title") or "",
                "up": item.get("up_name") or "",
                "time": item.get("time") or "",
                "score": score,
                "summary": item.get("content_summary") or "",
                "review": item.get("review") or "",
                "mood": item.get("mood") or "",
                "round": item.get("round") or "",
            })
        records.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
        return records

    def _watch_records(self) -> list[dict]:
        records = self._watch_records_from_db()
        if records:
            return records
        path = self._ai_bot_dir() / "watch_log.json"
        if not path.is_file():
            logger.info("[Bridge2] 没有 watch_log.json：%s", path)
            return []
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 读 watch_log 失败：%s", exc)
            return []
        records = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            try:
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            # 刷到过的都进清单（包括没推给你、评价失败的），顺序由 _recent_rows 排
            records.append({
                "bvid": item["bvid"],
                "title": item.get("title") or "",
                "up": item.get("up_name") or "",
                "time": item.get("time") or "",
                "score": score,
                "summary": item.get("content_summary") or "",
                "review": item.get("review") or "",
                "mood": item.get("mood") or "",
                "round": item.get("round") or "",
            })
        records.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
        return records

    def _save_recent(self, records: list[dict]) -> None:
        try:
            self._recent_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 存 recent.json 失败：%s", exc)

    def _load_recent(self) -> list[dict]:
        if not self._recent_path.is_file():
            return []
        try:
            data = json.loads(self._recent_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for rec in data if isinstance(data, list) else []:
            if not isinstance(rec, dict) or not rec.get("bvid"):
                continue
            bvid = str(rec["bvid"])
            if bvid in seen:
                continue
            seen.add(bvid)
            out.append(rec)
        return out

    def _format_list(self, records: list[dict], head: str = "最近刷到的：") -> str:
        lines = []
        for i, r in enumerate(records, 1):
            title = str(r.get("title") or "(无标题)").strip()
            if len(title) > 35:
                title = title[:35] + "…"
            up = f"（{r['up']}）" if r.get("up") else ""
            lines.append(f"{i}. {title}{up} · {r['bvid']}")
        return head + "\n" + "\n".join(lines) + "\n\n想问哪条直接说「第几个讲了什么」。"

    # ── 给 MemoryCompanion 的「B站近况」续命 ─────────────────────────────
    def _digest_text(self, rows: list[dict]) -> str:
        newest = str(rows[0].get("time") or "")
        day = newest[:10] or datetime.now().strftime("%Y-%m-%d")
        clock = newest[11:16] or "?"
        lines = [
            f"- {r['bvid']} {str(r.get('title') or '')[:30]}"
            for r in rows[:8]
        ]
        hint = (
            "（老大问起某条具体讲了什么时，用你自己的语气讲：两三句内容 + 你的判断，"
            "别用「主旨：/关键三点：」这种提纲；他问「最近刷到什么」时以当场给的那份清单为准，"
            "这段只是底档。）"
        )
        text = "\n".join([f"## B站近况（{day}，最近一轮 {clock}）这一轮刷到的：", *lines, hint])
        return text[:900]

    def _digest_enabled(self) -> bool:
        """「B站近况」核心块要不要继续写。默认关。

        健康记忆中枢接管跨端之后，核心块等于第二份简报，和中枢里她自己的看片记录重复。
        想恢复 1 版「每轮对话都带上最近一轮」的行为，把 ``core_digest_enable`` 打开。
        """
        value = self.config.get("core_digest_enable", False)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    async def _refresh_core_digest(self, records: list[dict] | None = None) -> None:
        """把最近一轮写进「B站近况」，再把推给主人的几批单独记成记忆。

        1 版（bili_bridge）就是靠它让「她记忆里的 B站近况」保持新鲜；
        桥2 接管后不写的话，那条 core block 会永远停在最后一次写入的那天，
        她一被问「最近刷了什么」就照着几天前的旧片名答。
        """

        try:
            owner = next(iter(self._owners()), "")
            if not owner:
                return
            meta = self.context.get_registered_star("astrbot_plugin_memory_companion")
            if meta is None or not getattr(meta, "activated", False):
                logger.info("[Bridge2] MemoryCompanion 没启用，跳过 B站近况同步")
                return
            service = getattr(getattr(meta, "star_cls", None), "service", None)
            if service is None:
                logger.info("[Bridge2] MemoryCompanion 没有暴露 service，跳过")
                return

            rows = records or self._recent_rows(10)
            if rows:
                text = self._digest_text(rows)
                fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
                save_core = getattr(service, "save_core_memory_block", None)
                if save_core is not None and fingerprint != getattr(self, "_digest_fp", ""):
                    result = await save_core(
                        {
                            "id": str(self.config.get("core_block_id") or "bili-digest-core"),
                            "label": str(self.config.get("core_block_label") or "B站近况"),
                            "content": text,
                            "scope": "private",
                            "target_id": owner,
                            "kind": "profile",
                            "priority": 75,
                            "bot_id": "self",
                        }
                    )
                    ok = bool(isinstance(result, dict) and result.get("ok", True))
                    if ok:
                        self._digest_fp = fingerprint
                        logger.info("[Bridge2] 已更新记忆里的「B站近况」（%s 条，最近 %s）",
                                    len(rows[:8]), str(rows[0].get("time") or "")[:16])
                    else:
                        logger.warning("[Bridge2] 更新 B站近况失败：%s", result)

        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 更新 B站近况异常：%s", exc)

    def _push_batches(self, limit: int = 8) -> list[tuple[str, list[str]]]:
        """从推荐账本里取最近几批推送：同一分钟算一批，返回 [(时间, [BV...]), ...]。"""

        ledger: dict = {}
        db = self._ai_bot_db()
        if db.is_file():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                row = con.execute(
                    "select data from keyed_json where kind='state' "
                    "and key='owner_recommend_log.json' order by id desc limit 1").fetchone()
                con.close()
                ledger = json.loads(row[0]) if row and row[0] else {}
            except Exception:  # noqa: BLE001
                ledger = {}
        batches: dict[str, list[str]] = {}
        if isinstance(ledger, dict):
            for bvid, item in ledger.items():
                if not isinstance(item, dict) or not bvid:
                    continue
                when = str(item.get("time") or "")
                batches.setdefault(when, []).append(bvid)
        if not batches:
            pushed = [r["bvid"] for r in self._pushed_records()]
            if pushed:
                batches[""] = pushed
        out = sorted(batches.items(), key=lambda kv: kv[0], reverse=True)
        return out[: max(1, limit)]

    @classmethod
    def _cn_count(cls, n: int) -> str:
        words = "零一二三四五六七八九十"
        if 0 <= n <= 10:
            return words[n]
        return str(n)

    @classmethod
    def _approx_when(cls, value) -> str:
        """把 2026-11-08 02:55 说成「今天凌晨三点左右」这种人话。"""

        try:
            stamp = datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return "刚才"
        hour, minute = stamp.hour, stamp.minute
        if minute >= 40:
            hour = (hour + 1) % 24
        elif minute <= 20:
            pass
        else:
            minute = 30
        day = stamp.date()
        today = datetime.now().date()
        delta = (today - day).days
        if delta == 0:
            day_word = "今天"
        elif delta == 1:
            day_word = "昨天"
        elif delta == 2:
            day_word = "前天"
        else:
            day_word = f"{stamp.month} 月 {stamp.day} 号"
        if hour < 5:
            part = "凌晨"
        elif hour < 8:
            part = "早上"
        elif hour < 11:
            part = "上午"
        elif hour < 13:
            part = "中午"
        elif hour < 17:
            part = "下午"
        elif hour < 19:
            part = "傍晚"
        elif hour < 23:
            part = "晚上"
        else:
            part = "深夜"
        clock = "十二点" if hour % 12 == 0 else f"{cls._cn_count(hour % 12)}点多"
        return f"{day_word}{part}{clock}"

    @classmethod
    def _casual_last_round(cls, records) -> str:
        """一句人话，不报分秒、不像报表。"""

        when = cls._approx_when(records[-1].get("time") if records else "")
        n = cls._cn_count(len(records)) if records else "零"
        templates = (
            f"{when}刷的，也就{n}条。",
            f"{when}吧，没刷多少，{n}条。",
            f"我想想……{when}刷的，{n}条。",
        )
        return random.choice(templates)

    def _cached_detail_from_db(self, bvid: str, hours: int = 24) -> str:
        """2.0+ 的视频解析存在 keyed_json(video_memory)，时间用 activity_log 里那次看片的记录。"""

        db = self._ai_bot_db()
        if not db.is_file():
            return ""
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            row = con.execute(
                "select json_extract(data,'$.time') from activity_log "
                "where kind='watch' and json_extract(data,'$.bvid')=? "
                "order by id desc limit 1", (bvid,)).fetchone()
            when = row[0] if row and row[0] else ""
            mem = con.execute(
                "select data from keyed_json where kind='video_memory' and key=? "
                "order by id desc limit 1", (bvid,)).fetchone()
            con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 读缓存解析失败：%s", exc)
            return ""
        if not mem:
            return ""
        try:
            item = json.loads(mem[0])
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(item, dict):
            return ""
        try:
            ts = datetime.strptime(str(when)[:16], "%Y-%m-%d %H:%M").timestamp()
        except Exception:  # noqa: BLE001
            return ""
        if time.time() - ts > max(1, hours) * 3600:
            return ""
        analysis = str(item.get("analysis") or "").strip()
        if not analysis:
            return ""
        title = item.get("title") or ""
        up = item.get("owner_name") or ""
        return (
            f"【B站视频素材 {bvid}·缓存】\n"
            f"— 标题：{title}（{up}）\n"
            f"— 看过时间：{when}\n"
            f"— 内容（当时的分析，可直接用）：\n{analysis[:1600]}\n"
            "用法：保持人设用你自己的话说，别复述标题。"
        )

    def _cached_detail(self, bvid: str, hours: int = 24) -> str:
        """近 N 小时看过的视频，直接从 video_memory.json 拿当时的完整解析，省一次 bili-cli。"""

        detail = self._cached_detail_from_db(bvid, hours)
        if detail:
            return detail
        path = self._ai_bot_dir() / "video_memory.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            return ""
        item = data.get(bvid) if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return ""
        try:
            ts = datetime.strptime(str(item.get("time")), "%Y-%m-%d %H:%M").timestamp()
        except Exception:  # noqa: BLE001
            return ""
        if time.time() - ts > max(1, hours) * 3600:
            return ""
        analysis = str(item.get("analysis") or "").strip()
        if not analysis:
            return ""
        title = item.get("title") or ""
        up = item.get("owner_name") or ""
        return (
            f"【B站视频素材 {bvid}·缓存】\n"
            f"— 标题：{title}（{up}）\n"
            f"— 看过时间：{item.get('time')}\n"
            f"— 内容（当时的分析，可直接用）：\n{analysis[:1600]}\n"
            "用法：保持人设用你自己的话说，别复述标题。"
        )

    # ── 视频素材 ────────────────────────────────────────────────────────
    async def _fetch_material(self, bvid: str) -> str:
        info = await self._bili("video", "info", bvid)
        subtitle = await self._bili("video", "subtitle", bvid)
        has_subtitle = not ("[bili-cli" in subtitle or len(subtitle.strip()) < 20)
        if not has_subtitle:
            subtitle = await self._bili("video", "summary", bvid)
        cap = int(self.config.get("subtitle_max_chars") or 1800)
        visual = await self._analyze_frames(bvid, has_subtitle=has_subtitle)
        return (
            f"【B站视频素材 {bvid}】\n"
            f"— 基本信息 —\n{info.strip()[:1200]}\n"
            f"— 内容（字幕/官方总结）—\n{subtitle.strip()[:cap]}\n"
            f"{visual}"
            "用法：保持人设用你自己的话说，别复述标题、别写成评测稿。"
        )

    # ── 抽帧看图 ────────────────────────────────────────────────────────
    async def _analyze_frames(self, bvid: str, *, has_subtitle: bool) -> str:
        """抽几帧画面让模型看：有字幕时少抽（字幕已经讲清楚），没字幕多抽。

        走 ``bili video frames``，不自己下视频；失败一律返回空串，不影响主流程。
        """

        if not bool(self.config.get("enable_frame_analysis", True)):
            return ""
        key = (
            "frame_count_with_subtitle"
            if has_subtitle
            else "frame_count_without_subtitle"
        )
        try:
            count = int(self.config.get(key) or (5 if has_subtitle else 12))
        except (TypeError, ValueError):
            count = 5 if has_subtitle else 12
        count = max(1, min(24, count))
        out_dir = self._plugin_data_dir() / "frames" / bvid
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 建帧目录失败：%s", exc)
            return ""
        quality = str(self.config.get("frame_quality") or "preview").strip() or "preview"
        raw = await self._bili(
            "video",
            "frames",
            bvid,
            "--count",
            str(count),
            "--quality",
            quality,
            "--out",
            str(out_dir),
            timeout=180,
        )
        frames = self._parse_frames(raw)
        if not frames:
            logger.info("[Bridge2] %s 没抽到帧，跳过画面分析", bvid)
            return ""
        return await self._vision_describe(bvid, frames)

    @staticmethod
    def _parse_frames(raw: str) -> list[dict]:
        """从 bili-cli 的 JSON 里挑出真正存在的帧图片。"""

        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
        items = ((data or {}).get("data") or {}).get("frames") or []
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path or not Path(path).is_file():
                continue
            out.append({"path": path, "time_str": str(item.get("time_str") or "")})
        return out

    async def _vision_describe(self, bvid: str, frames: list[dict]) -> str:
        """把帧丢给支持看图的模型，拿回一段画面描述。"""

        try:
            pid = str(self.config.get("frame_provider_id") or "").strip()
            provider = None
            if pid:
                provider = await self.context.provider_manager.get_provider_by_id(pid)
            if provider is None:
                provider = await self.context.get_using_provider_async()
            if provider is None:
                logger.info("[Bridge2] 没有可用的看图模型，跳过画面分析")
                return ""
            listing = "\n".join(
                f"- 第 {i + 1} 帧（{f['time_str'] or '时间未知'}）"
                for i, f in enumerate(frames)
            )
            prompt = (
                "这是同一个 B站 视频按时间顺序抽出的几帧画面，请按顺序描述：\n"
                "1. 每个画面里在发生什么、出现了谁或什么东西；\n"
                "2. 场景在哪（室内/室外/手机录屏/动画等）；\n"
                "3. 能看清的文字、标题、字幕就写出来，看不清就说看不清。\n"
                "只描述你真看到的，不要猜剧情，不要评论。\n\n"
                f"帧列表：\n{listing}"
            )
            resp = await provider.text_chat(
                prompt=prompt,
                image_urls=[f["path"] for f in frames],
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 抽帧分析失败：%s", exc)
            return ""
        if not text:
            return ""
        logger.info("[Bridge2] %s 画面分析完成（%d 帧，%d 字）", bvid, len(frames), len(text))
        return f"— 画面（自动抽帧 {len(frames)} 帧）—\n{text[:1500]}\n"

    @staticmethod
    def _walk_strings(obj, out: list[str], depth: int = 0) -> None:
        """把 JSON/字典里所有字符串挖出来（不截断、不丢层）。"""

        if depth > 8:
            return
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                BiliBridge2._walk_strings(v, out, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                BiliBridge2._walk_strings(v, out, depth + 1)

    def _collect_chunks(self, event: AstrMessageEvent) -> list[str]:
        """把消息里所有可能藏链接的东西摊成一堆字符串。"""

        chunks: list[str] = []
        try:
            chunks.append(str(event.message_str or ""))
        except Exception:  # noqa: BLE001
            pass
        try:
            for comp in getattr(event.message_obj, "message", None) or []:
                chunks.append(str(comp))
                if isinstance(comp, dict):
                    BiliBridge2._walk_strings(comp, chunks)
                    chunks.append(json.dumps(comp, ensure_ascii=False, default=str))
                    continue
                for attr in ("data", "meta", "url", "qqdocurl", "jumpUrl", "text", "content"):
                    val = getattr(comp, attr, None)
                    if val:
                        BiliBridge2._walk_strings(val, chunks)
                        if isinstance(val, (dict, list)):
                            chunks.append(json.dumps(val, ensure_ascii=False, default=str))
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is not None:
                chunks.append(str(raw))
                msg = getattr(raw, "message", None)
                if msg:
                    BiliBridge2._walk_strings(msg, chunks)
        except Exception:  # noqa: BLE001
            pass
        # 卡片里的 URL 常被转义/编码过，多给几种变体
        extra = []
        for s in chunks:
            if not s:
                continue
            try:
                un = html.unescape(s).replace("\\/", "/")
                extra.append(un)
                extra.append(urllib.parse.unquote(un))
            except Exception:  # noqa: BLE001
                pass
        return chunks + extra

    @staticmethod
    def _follow_short(code: str) -> str:
        """跟随 b23.tv 短链的 302，拿最终 URL 里的 BV。"""

        url = f"https://b23.tv/{code}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                final = resp.geturl() or ""
                body = resp.read(150_000).decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 短链解析失败 %s：%s", code, exc)
            return ""
        m = BV_RE.search(final) or BV_RE.search(body)
        return m.group(1) if m else ""

    async def _resolve_bvid(self, event: AstrMessageEvent) -> str:
        """从卡片/文本里认出 BV：先直接找 BV，找不到再跟 b23 短链。"""

        blob = "\n".join(self._collect_chunks(event))
        m = BV_RE.search(blob)
        if m:
            return m.group(1)
        for code in dict.fromkeys(SHORT_RE.findall(blob)):
            bv = await asyncio.to_thread(BiliBridge2._follow_short, code)
            if bv:
                logger.info("[Bridge2] 短链 %s → %s", code, bv)
                return bv
        return ""

    async def _resolve_video_ref(self, event: AstrMessageEvent) -> tuple[str, str]:
        """认出视频编号，返回 ``("BV", "BV1xx")`` / ``("AV", "av12345")`` / ``("", "")``。

        比 ``_resolve_bvid`` 多认两种：纯文本链接里的 BV，和老的 av 号。
        卡片、b23 短链、完整链接、纯文本 BV 都走这里。
        """
        blob = "\n".join(self._collect_chunks(event))
        m = BV_RE.search(blob)
        if m:
            return "BV", m.group(1)
        for code in dict.fromkeys(SHORT_RE.findall(blob)):
            bv = await asyncio.to_thread(BiliBridge2._follow_short, code)
            if bv:
                logger.info("[Bridge2] 短链 %s → %s", code, bv)
                return "BV", bv
        m = AV_RE.search(blob)
        if m:
            return "AV", f"av{m.group(1)}"
        return "", ""

    # ── 分享过来就真看一遍 ────────────────────────────────────────────────
    def _start_share_watch(self, video_id: str) -> None:
        """老大分享过来一个视频 → 让她真的去看一遍（后台跑，不挡回复）。

        真看走的是 B站 AI Bot 自己的完整流程：字幕 / ASR、评分、写 video 记忆。
        写记忆那一步会镜像进记忆中枢（``bot_self``），所以在 QQ、B站、DSH 都答得出。
        """
        vid = str(video_id or "").strip()
        if not vid:
            return
        if vid in self._share_watching:
            logger.info("[Bridge2] 已经在看了，不重复：%s", vid)
            return
        done_at = self._share_watched.get(vid)
        if done_at and time.time() - done_at < self._share_watch_ttl:
            logger.info("[Bridge2] 最近看过，跳过：%s", vid)
            return
        self._share_watching.add(vid)
        try:
            task = asyncio.create_task(self._share_watch_task(vid))
        except Exception as exc:  # noqa: BLE001
            self._share_watching.discard(vid)
            logger.warning("[Bridge2] 起看片任务失败：%s", exc)
            return
        self._share_watch_tasks.add(task)
        task.add_done_callback(self._share_watch_tasks.discard)

    async def _share_watch_task(self, video_id: str) -> None:
        try:
            inst = self._ai_bot_instance()
            if inst is None:
                logger.info("[Bridge2] B站 AI Bot 不在，分享的视频没看成：%s", video_id)
                return
            watcher = getattr(inst, "watch_on_request", None)
            fn = getattr(watcher, "_watch_video_and_save_memory", None)
            if not callable(fn):
                logger.info("[Bridge2] B站 AI Bot 没有看片入口，跳过：%s", video_id)
                return
            result = await fn(video_id, "qq_share", force_rewatch=True)
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            # 「以前见过、这次没重看」也会返回 ok，但不能当成刚看完去报喜
            seen_only = bool(result.get("seen_only")) if isinstance(result, dict) else False
            msg = str(result.get("message") or "") if isinstance(result, dict) else ""
            # 缺 yt-dlp / 视觉模型时，分析会退化成「根据标题、标签推测」的 metadata 级：
            # 依然返回 ok，但一条长期记忆都不写。认出来，别当成真看。
            weak = (
                "根据标题" in msg
                or "未取得可靠" in msg
                or "曾经看过" in msg
                or bool(result.get("from_cache")) and "详细内容" in msg
            ) if isinstance(result, dict) else True
            if ok and not seen_only and not weak:
                logger.info("[Bridge2] 分享视频真看完成：%s", video_id)
                self._share_watched[video_id] = time.time()
                self._share_done = (time.time(), video_id, msg[:1200])
            else:
                logger.info(
                    "[Bridge2] 分享视频没进完整分析（%s），改用字幕素材记一条",
                    "仅命中旧记录" if seen_only else ("内容分析降级" if weak else "失败"),
                )
            # 不管上面成没成，都保证中枢里有这条。已经写过就跳过（幂等键同一个）。
            await self._ensure_share_memory(video_id, already=ok and not seen_only and not weak)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 分享视频看失败 %s：%s", video_id, exc)
        finally:
            self._share_watching.discard(video_id)

    async def _ensure_share_memory(self, video_id: str, *, already: bool = False) -> None:
        """保证这条分享在中枢里有一条记忆。

        ``already=True`` 说明 B站 AI Bot 自己写过了，它写的时候会镜像进中枢，
        这里先查一下，有就跳过；没有（分析降级、或镜像没成功）再用字幕素材补一条。
        """
        try:
            meta = self.context.get_registered_star("paopao_life_bridge")
            if meta is None or not getattr(meta, "activated", False):
                logger.info("[Bridge2] 没有生活桥，分享的记忆没处写")
                return
            bridge = getattr(meta, "star_cls", None)
            if bridge is None:
                return
            if already:
                has = getattr(bridge, "has_memory", None)
                mid = f"bili:video:{video_id}" if video_id.upper().startswith("BV") else ""
                if callable(has) and mid and await has(mid):
                    logger.info("[Bridge2] 中枢里已有这条，不重复写：%s", video_id)
                    return
            await self._remember_share_text(video_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 补写分享记忆失败 %s：%s", video_id, exc)

    async def _remember_share_text(self, video_id: str) -> None:
        """分析链跑不动时，用已经拉到的字幕/简介在中枢写一条「我看过」。

        reality 标 ``media_index``：她确实拿到了文字资料，但没看画面。
        不这么标的话，她会跟人说「我看了」，被追问画面细节就露馅。
        """
        try:
            meta = self.context.get_registered_star("paopao_life_bridge")
            if meta is None or not getattr(meta, "activated", False):
                logger.info("[Bridge2] 没有生活桥，字幕素材没处写")
                return
            bridge = getattr(meta, "star_cls", None)
            remember = getattr(bridge, "remember_bili", None)
            if not callable(remember):
                return
            cached = self._last_share_material.get(video_id)
            material = cached[1] if cached else ""
            if not material:
                try:
                    material = await self._fetch_material(video_id)
                except Exception:  # noqa: BLE001
                    material = ""
            if not material:
                logger.info("[Bridge2] 没拿到素材，这条不记：%s", video_id)
                return
            text = "老大分享给我一个 B站 视频，我看了它的简介和字幕：" + str(material)[:600]
            await remember(
                text,
                kind="video",
                visibility="bot_self",
                bvid=video_id if video_id.upper().startswith("BV") else "",
                reality="media_index",
                tags=["B站", "分享", "字幕"],
                important=0.55,
            )
            logger.info("[Bridge2] 已用字幕素材记下这条分享：%s", video_id)
            self._share_watched[video_id] = time.time()
            self._share_done = (time.time(), video_id, text[:900])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 记字幕素材失败 %s：%s", video_id, exc)

    def _dump_card(self, event: AstrMessageEvent) -> None:
        """认不出卡片时把原始结构落盘，方便照真实字段补解析。"""

        try:
            raw = getattr(event.message_obj, "raw_message", None)
            comps = []
            for comp in getattr(event.message_obj, "message", None) or []:
                comps.append({
                    "type": type(comp).__name__,
                    "repr": str(comp)[:4000],
                    "data": json.loads(json.dumps(getattr(comp, "data", None), ensure_ascii=False, default=str))
                    if getattr(comp, "data", None) is not None else None,
                })
            payload = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message_str": str(getattr(event, "message_str", "") or "")[:500],
                "raw": str(raw)[:4000] if raw is not None else "",
                "comps": comps,
                "chunks": [c[:1000] for c in self._collect_chunks(event) if c][:60],
            }
            path = self._data_dir / "card_debug.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 卡片诊断落盘失败：%s", exc)

    def _ai_bot_instance(self):
        """拿到 B站 AI Bot 的插件实例，用来判断在不在跑、直接触发刷一轮。"""

        try:
            meta = self.context.get_registered_star("astrbot_plugin_bilibili_ai_bot")
            inst = getattr(meta, "star_cls", None)
            if inst is not None and (
                hasattr(inst, "_run_proactive") or hasattr(inst, "proactive_browse")
            ):
                return inst
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 拿不到 AI Bot 实例：%s", exc)
        return None

    @staticmethod
    def _ai_bot_busy(inst) -> bool:
        if inst is None:
            return False
        acts = getattr(inst, "activities", None)
        try:
            if acts is not None and hasattr(acts, "_activity_busy"):
                return bool(acts._activity_busy("proactive"))
        except Exception:  # noqa: BLE001
            pass
        task = getattr(inst, "_proactive_task", None)
        return task is not None and not task.done()

    def _attach_watch_note(self, event: AstrMessageEvent, status: str) -> None:
        """把「刷一轮」的后台情况挂到事件上，措辞交给模型（桥1 的做法）。"""

        try:
            event.bridge2_watch_note = status  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 挂刷一轮说明失败：%s", exc)

    @staticmethod
    def _watch_note_text(status: str) -> str:
        """给模型的后台说明：不报具体时间，给几种语气参考。"""

        if status == "started":
            body = (
                "老大让你去刷B站，这件事已经提交出去了，正在后台刷；"
                "跑完 B站 那边会把挑到的推过来（那不是你现在要说的事）。"
                "现在用你自己的话回一句——别报结果、别编标题、别承诺具体几条，"
                "也别用「稍等我去看看」这种客服腔。可以嫌他催、可以说去看看、可以嘴硬一句，看你当时心情。"
                "参考（别照抄）：「行吧，我去看看」「催什么催，等着」「刷完再说」。"
            )
        elif status == "gap":
            body = (
                "老大又让你去刷，但才刚刷过一轮，这一轮**没有**开刷，也没给他排着。"
                "用你自己的话回一句就行——嫌他急、说懒得动、让他等会儿、耍赖都行。"
                "**千万别报具体多少秒/多少分钟**（那是机器话，人不会这么说），"
                "也别装作已经开刷、别报任何视频。"
                "参考（别照抄）：「刚刷完嘛，让我歇会儿」「急什么，等会儿再说」「……不想动」。"
            )
        elif status == "busy":
            body = (
                "老大让你去刷，但上一轮还在跑，这一轮**没有**开刷，也没排着。"
                "用你自己的话回一句，让他等会儿再喊；别报具体时间，也别说「正在跑」「冷却」这种系统话。"
                "参考（别照抄）：「上一轮还没完呢」「等会儿，还没刷完」。"
            )
        else:
            body = "老大让你去刷，但没开成。用你自己的话如实说一句，别编结果。"
        return f"（后台：{body}）"

    @staticmethod
    def _suppress_bili_hook(event: AstrMessageEvent) -> None:
        """给 AI Bot 的 on_llm_request 留个标记：这一轮别再自己开刷。"""

        try:
            event.set_extra("bili_proactive_suppress", True)
        except Exception:  # noqa: BLE001
            pass

    def _last_push_bvids(self) -> list[str]:
        """最近一批推送：2.0+ 看数据库里的推荐账本，老版本看 owner_last_push.json。"""

        db = self._ai_bot_db()
        if db.is_file():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                row = con.execute(
                    "select data from keyed_json where kind='state' "
                    "and key='owner_recommend_log.json' order by id desc limit 1").fetchone()
                con.close()
                ledger = json.loads(row[0]) if row and row[0] else {}
                if isinstance(ledger, dict) and ledger:
                    entries = [
                        (str(v.get("time") or ""), bvid)
                        for bvid, v in ledger.items()
                        if isinstance(v, dict) and bvid
                    ]
                    if entries:
                        entries.sort(reverse=True)
                        newest = entries[0][0]
                        bvids = [bvid for when, bvid in entries if when == newest]
                        if bvids:
                            return bvids
            except Exception:  # noqa: BLE001
                pass

        path = self._ai_bot_dir() / "owner_last_push.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            return []
        return [str(b) for b in (data.get("bvids") or []) if b]

    def _recommend_ledger(self) -> dict:
        """推荐账本：bvid -> {time, title, ...}。2.0+ 在数据库，老版本在 json。"""

        db = self._ai_bot_db()
        if db.is_file():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                row = con.execute(
                    "select data from keyed_json where kind='state' "
                    "and key='owner_recommend_log.json' order by id desc limit 1").fetchone()
                con.close()
                ledger = json.loads(row[0]) if row and row[0] else {}
                if isinstance(ledger, dict):
                    return ledger
            except Exception:  # noqa: BLE001
                pass

        path = self._ai_bot_dir() / "owner_recommend_log.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_ts(text: str) -> float:
        raw = str(text or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).timestamp()
            except Exception:  # noqa: BLE001
                continue
        return 0.0

    def _push_history(self, days: int = 7) -> list[dict]:
        """近 N 天推给主人的视频，按时间倒序，带标题。

        用户问「你推过什么 / 上周发给我哪些」时走这里，不从记忆检索。
        """

        ledger = self._recommend_ledger()
        if not ledger:
            return []
        try:
            span = max(1, int(days))
        except (TypeError, ValueError):
            span = 7
        cutoff = time.time() - span * 86400
        titles = {}
        try:
            titles = {r["bvid"]: r.get("title") or "" for r in self._watch_records() if r.get("bvid")}
        except Exception:  # noqa: BLE001
            pass
        out = []
        for bvid, item in ledger.items():
            if not bvid or not isinstance(item, dict):
                continue
            when = str(item.get("time") or "")
            ts = BiliBridge2._parse_ts(when)
            if ts and ts < cutoff:
                continue
            out.append({
                "bvid": str(bvid),
                "time": when,
                "title": str(item.get("title") or titles.get(bvid) or ""),
                "up": str(item.get("up") or ""),
            })
        out.sort(key=lambda r: r["time"], reverse=True)
        return out

    def _pushed_records(self) -> list[dict]:
        """最近一次推给主人的那一批，按 QQ 消息里的顺序。"""

        bvids = self._last_push_bvids()
        if not bvids:
            return []
        by_bvid = {r["bvid"]: r for r in self._watch_records()}
        out = []
        for bv in bvids:
            out.append(by_bvid.get(bv) or {"bvid": bv, "title": "", "up": "", "time": ""})
        return out

    def _last_activity_ts(self) -> float:
        """最近一次真的开刷的时间：既算本插件触发的，也算工具/自动轮留下的记录。"""

        ts = float(getattr(self, "_last_watch", 0) or 0)
        try:
            recs = self._watch_records()
            if recs:
                ts = max(ts, datetime.strptime(str(recs[0].get("time")), "%Y-%m-%d %H:%M").timestamp())
        except Exception:  # noqa: BLE001
            pass
        return ts

    def _rounds(self, hours: int = 24) -> list[list[dict]]:
        """把看片记录按时间聚成「轮」：相邻两条间隔 > 10 分钟就算新的一轮。"""

        records = sorted(self._watch_records(), key=lambda r: str(r.get("time") or ""))
        rounds: list[list[dict]] = []
        for rec in records:
            if not rounds:
                rounds.append([rec])
                continue
            prev_round = str(rounds[-1][0].get("round") or "")
            cur_round = str(rec.get("round") or "")
            if prev_round and prev_round == cur_round:
                # 新的看片记录带轮次标记，直接按轮次归并
                rounds[-1].append(rec)
                continue
            if not prev_round and not cur_round:
                try:
                    prev = datetime.strptime(str(rounds[-1][-1].get("time")), "%Y-%m-%d %H:%M")
                    cur = datetime.strptime(str(rec.get("time")), "%Y-%m-%d %H:%M")
                    close = (cur - prev).total_seconds() <= 600
                except Exception:  # noqa: BLE001
                    close = False
                if close:
                    rounds[-1].append(rec)
                    continue
            rounds.append([rec])
        cutoff = time.time() - max(1, hours) * 3600
        keep = []
        for items in rounds:
            try:
                ts = datetime.strptime(str(items[-1].get("time")), "%Y-%m-%d %H:%M").timestamp()
            except Exception:  # noqa: BLE001
                continue
            if ts >= cutoff:
                keep.append(items)
        return keep

    @staticmethod
    def _snip(value: str, limit: int = 90) -> str:
        text = str(value or "").replace("\n", " ").strip()
        return text[:limit] + "…" if len(text) > limit else text

    def _recent_rows(self, limit: int | None = None) -> list[dict]:
        """清单顺序：刚推给主人的那批按推送顺序排最前，后面接历史看片记录。

        这样「最近刷到的」1./2./3. 就是 QQ 里刚收到的那几个，后面的才是更早看过的。
        """

        pushed = self._pushed_records()
        seen = {r["bvid"] for r in pushed}
        rest = [r for r in self._watch_records() if r["bvid"] not in seen]
        rows = pushed + rest
        # 同一个视频可能被反复刷到：只留最新那一条，别让清单里出现两遍
        uniq: list[dict] = []
        seen_bv: set[str] = set()
        for rec in rows:
            bvid = str(rec.get("bvid") or "")
            if not bvid or bvid in seen_bv:
                continue
            seen_bv.add(bvid)
            uniq.append(rec)
        rows = uniq
        if limit:
            rows = rows[: max(1, int(limit))]
        return rows

    def _records_for_ordinal(self) -> list[dict]:
        """「第几个」和清单用同一份顺序（推送批次在前）。"""

        try:
            limit = int(self.config.get("recent_limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        return self._recent_rows(limit)

    def _ordinal(self, text: str) -> int:
        m = ORDINAL_RE.search(text)
        if not m:
            return 0
        raw = m.group(1)
        if raw.isdigit():
            return int(raw)
        return CN_NUM.get(raw, 0)

    @staticmethod
    def _norm_text(value: str) -> str:
        return re.sub(r"[\s\W_]+", "", str(value or "")).lower()

    # 这些词哪条标题里都有，拿它们重合等于没重合
    MENTION_STOP = (
        "视频", "那条", "这个", "那个", "哪些", "讲了", "说的", "什么", "今天",
        "刚才", "刚刚", "一下", "帮我", "看看", "内容", "意思", "怎么", "回事",
    )

    def _guess_record_by_mention(self, text: str) -> dict | None:
        """「悬链线那条讲了什么」→ 按片名模糊找回那条看片记录。

        先把「视频 / 那条 / 讲了 / 什么」这类废话剔掉，只拿剩下的真词去比；
        连续重合 3 个字起算，3 个字还必须落在标题开头，免得误伤。
        """

        probe = self._norm_text(text)
        for word in self.MENTION_STOP:
            probe = probe.replace(word, "")
        if len(probe) < 3:
            return None
        best: dict | None = None
        best_size = 0
        for rec in self._watch_records():
            title = self._norm_text(rec.get("title"))
            if not title:
                continue
            match = difflib.SequenceMatcher(None, probe, title, autojunk=False).find_longest_match(
                0, len(probe), 0, len(title))
            size = int(match.size)
            if size <= 0:
                continue
            chunk = probe[match.a:match.a + size]
            if any(word in chunk for word in self.MENTION_STOP):
                continue
            if size == 3 and match.b >= 3:
                # 三个字的重合只认标题开头那一下（「悬链线在石拱桥上…」）
                continue
            if size > best_size:
                best_size, best = size, rec
        if best is None or best_size < 3:
            return None
        return best

    @filter.llm_tool(name="bili_bridge2_last_round")
    async def tool_last_round(self, event: AstrMessageEvent, hours: int = 24) -> str:
        """回忆最近刷视频的情况：什么时候刷的、刷到了哪几个。

        用户问「上一次刷视频是什么时候」「最近刷到什么了」时调用。
        返回每一轮的时间、条数、标题、BV 号、评分和内容摘要；要更细的内容再用
        bili_bridge2_read 按 BV 读。

        Args:
            hours(int): 往回看几小时，默认 24
        """

        rounds = self._rounds(max(1, min(int(hours or 6), 48)))
        if not rounds:
            return f"最近 {hours} 小时没有刷片记录。"
        lines = []
        for items in reversed(rounds[-3:]):
            when = items[-1].get("time") or "?"
            lines.append(f"【{when}】看了 {len(items)} 个：")
            for r in items:
                score = r.get("score") or 0
                score_txt = f"{score:g} 分" if score else "没评上分"
                lines.append(f"- {r.get('title') or '(无标题)'}（{r.get('up') or '?'}） {r.get('bvid')} · {score_txt}")
                summary = self._snip(r.get("summary") or r.get("review") or "", 90)
                if summary:
                    lines.append(f"  {summary}")
        lines.append("要某一条的完整内容，直接给我 BV 号或问「那条讲了什么」。")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_bridge2_find")
    async def tool_find(self, event: AstrMessageEvent, keyword: str = "", days: int = 3) -> str:
        """按关键词在最近刷到的视频里找 BV（标题、摘要、感想都算）。

        用户用描述指代某条视频（「那个捕鱼的」「讲悬链线那条」）时调用，
        拿到 BV 后再用 bili_bridge2_read 读细节。

        Args:
            keyword(string): 关键词，比如「捕鱼」「悬链线」
            days(int): 往回找几天，默认 3
        """

        kw = str(keyword or "").strip()
        if not kw:
            return "要给个关键词，比如「捕鱼」「悬链线」。"
        cutoff = time.time() - max(1, min(int(days or 3), 30)) * 86400
        hits = []
        for r in self._watch_records():
            try:
                ts = datetime.strptime(str(r.get("time")), "%Y-%m-%d %H:%M").timestamp()
            except Exception:  # noqa: BLE001
                continue
            if ts < cutoff:
                continue
            blob = f"{r.get('title') or ''} {r.get('summary') or ''} {r.get('review') or ''}"
            if kw.lower() in blob.lower():
                hits.append(r)
        if not hits:
            return f"最近 {days} 天里没找到和「{kw}」有关的视频。"
        # 同一标题可能被反复刷到：只留最新一条（_watch_records 已按时间倒序）
        seen_titles = set()
        picked = []
        for r in hits:
            key = re.sub(r"\s+", "", str(r.get("title") or ""))[:30]
            if key and key in seen_titles:
                continue
            seen_titles.add(key)
            picked.append(r)
        lines = [f"和「{kw}」有关的 {min(len(picked), 5)} 条（同一标题只留最新）："]
        for r in picked[:5]:
            lines.append(f"- {r.get('title') or '(无标题)'} · {r.get('bvid')} · {r.get('time')}")
            summary = self._snip(r.get("summary") or r.get("review") or "", 80)
            if summary:
                lines.append(f"  {summary}")
        return "\n".join(lines)

    # ── 消息处理 ────────────────────────────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def on_private(self, event: AstrMessageEvent):
        if str(getattr(getattr(event.message_obj, "raw_message", None), "post_type", "") or "") == "notice":
            return
        if not self._is_owner(event):
            return
        # 每隔几分钟顺手把记忆里的「B站近况」刷成最新一轮（内容没变就不写）。
        # 健康记忆中枢接管跨端之后，这块等于第二份简报，和中枢里的看片记录重复，
        # 所以默认关；要恢复 1 版的每轮简报行为，把 core_digest_enable 打开。
        if self._digest_enabled() and time.time() - float(getattr(self, "_digest_checked", 0.0)) > 300:
            self._digest_checked = time.time()
            try:
                asyncio.create_task(self._refresh_core_digest())
            except Exception:  # noqa: BLE001
                pass
        try:
            text = str(event.message_str or "")
        except Exception:  # noqa: BLE001
            text = ""
        origin = getattr(event, "unified_msg_origin", "") or ""

        # ① 口令：去刷一轮（交给 B站 AI Bot 自己的工具，避免自建冷却）
        if self.config.get("enable_watch_trigger", True) and text.strip():
            extra = str(self.config.get("watch_words") or "")
            words = list(DEFAULT_WATCH_WORDS) + [w.strip() for w in re.split(r"[,，]", extra) if w.strip()]
            # 只认祈使句：「刷视频 / 去刷点B站 / 帮我刷一轮视频」；
            # 「我刷了一条视频」「上次刷视频是什么时候」这类一律不算
            m_cmd = WATCH_CMD_RE.search(text)
            hit = m_cmd.group(0).strip() if m_cmd else ""
            # 防误判：「我刷了一条视频」「刚刷到个视频」这种是在说事，不是叫她去刷
            if hit and re.search(r"(我|刚|刚刚|昨天|今天)?(刷了|看了|刷到|看到|刷过|看过)", text):
                logger.info("[Bridge2] 看着像陈述句，不触发：%s", text[:40])
                hit = ""
            # 在问「上次刷到什么/那条讲了什么」这类回忆问题，不是叫她再去刷
            if hit and any(word in text for word in WATCH_BLOCKERS):
                logger.info("[Bridge2] 看着像在问刷片的旧事，不触发：%s", text[:40])
                hit = ""
            if hit and len(text.strip()) > 24:
                logger.info("[Bridge2] 句子太长，不像单纯叫她刷，不触发：%s", text[:40])
                hit = ""
            if hit:
                inst = self._ai_bot_instance()
                if self._ai_bot_busy(inst):
                    logger.info("[Bridge2] 上一轮还在跑，先不叠加：%s", hit)
                    self._attach_watch_note(event, "busy")
                    self._suppress_bili_hook(event)
                    return
                gap = int(self.config.get("watch_min_gap") or 180)
                last_ts = self._last_activity_ts()
                if time.time() - last_ts < gap:
                    left = max(1, int(gap - (time.time() - last_ts)))
                    logger.info("[Bridge2] 口令太密（还剩 %s 秒），跳过：%s", left, hit)
                    self._attach_watch_note(event, "gap")
                    self._suppress_bili_hook(event)
                    return
                if inst is not None:
                    # 直接调 B站 AI Bot 的刷片流程，不靠模型愿不愿意调工具
                    self._last_watch = time.time()
                    try:
                        count = inst._manual_watch_count()
                    except Exception:  # noqa: BLE001
                        count = random.randint(3, 5)
                    started = False
                    try:
                        acts = getattr(inst, "activities", None)
                        if acts is not None and hasattr(acts, "_run_activity_now"):
                            started = acts._run_activity_now("proactive", max_watch=count) is not None
                    except Exception as exc:  # noqa: BLE001
                        logger.info("[Bridge2] 走 activities 触发失败，改用任务方式：%s", exc)
                    if not started:
                        entry = getattr(inst, "proactive_browse", None) or inst
                        try:
                            task = asyncio.create_task(entry._run_proactive(max_watch=count))
                            inst._proactive_task = task
                            started = True
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[Bridge2] 触发刷一轮失败：%s", exc)
                    if started:
                        logger.info("[Bridge2] 口令直接触发 AI Bot 刷一轮：%s 个", count)
                        self._attach_watch_note(event, "started")
                        self._suppress_bili_hook(event)
                        return
                self._last_watch = time.time()
                # 不篡改用户消息；改成挂一条临时附加内容，模型照样看得到
                try:
                    event.set_extra("bridge2_watch_directive", True)
                except Exception:  # noqa: BLE001
                    pass
                if TextPart is None:
                    # 没有 TextPart 就只记日志、不篡改用户消息
                    logger.warning(
                        "[Bridge2] TextPart 不可用，口令注入被跳过（不改写用户消息）"
                    )
                logger.info("[Bridge2] 口令触发（附加内容注入指令）：%s", hit)
                return

        # ①.5 用户复述一遍「我说刷到什么」：这是重复要，别再列一次
        if re.search(r"我说.{0,10}(刷|看)(?:到|了|过)?(?:什么|啥)", text):
            try:
                repeat_window = int(self.config.get("list_repeat_window", 600) or 600)
            except (TypeError, ValueError):
                repeat_window = 600
            if time.time() - float(getattr(self, "_last_list_at", 0.0)) <= repeat_window:
                logger.info("[Bridge2] 复述「我说刷到什么」，交给模型回")
                try:
                    event.set_extra("bridge2_repeat_list", True)
                except Exception:  # noqa: BLE001
                    pass
                return

        # ② 问「最近刷到什么」→ 问内容才回清单；只问时间/刷没刷就一句话
        if RECENT_Q_RE.search(text) or RECENT_Q_RE2.search(text):
            hours = int(self.config.get("watch_memory_hours") or 24)
            rounds = self._rounds(hours)
            last_round = rounds[-1] if rounds else []
            when = (last_round[-1].get("time") if last_round else "") or "?"
            content_text = re.sub(r"什么时候|啥时候|几点|多久|多早", "", text)
            content_ask = bool(re.search(r"什么|啥|哪些|哪几个|哪条|讲了|内容|刷到", content_text))
            try:
                list_max = int(self.config.get("recent_list_max_chars", 12) or 12)
            except (TypeError, ValueError):
                list_max = 12
            try:
                low = int(self.config.get("recent_limit_min") or 5)
            except (TypeError, ValueError):
                low = 5
            try:
                high = int(self.config.get("recent_limit") or 10)
            except (TypeError, ValueError):
                high = 10
            low = max(1, min(low, high))
            high = max(low, high)
            now = time.time()
            try:
                repeat_window = int(self.config.get("list_repeat_window", 600) or 600)
            except (TypeError, ValueError):
                repeat_window = 600
            if now - float(getattr(self, "_last_list_at", 0.0)) <= repeat_window:
                since = now - float(getattr(self, "_last_list_at", 0.0))
                # 又问一遍：还是让她说，但把刚才那份清单原样塞给她当参考，
                # 免得她手里没数据、照着记忆总结编出几天前的片名。
                logger.info("[Bridge2] 刚发过清单（%.0f 秒内），交给她说（清单当参考）", since)
                try:
                    event.set_extra("bridge2_repeat_list", True)
                except Exception:  # noqa: BLE001
                    pass
                return
            if content_ask and len(text.strip()) > list_max:
                # 问得长/夹着别的问题：不甩整张清单，交给模型结合注入的近况自然答
                logger.info("[Bridge2] 内容问题太长（%s 字），不直接发清单", len(text.strip()))
                return
            if not content_ask:
                await asyncio.sleep(random.uniform(1.0, 2.0))

                if last_round:
                    reply = self._casual_last_round(last_round)
                    logger.info("[Bridge2] 只问时间/有没有刷，回一句：%s", reply)
                    yield event.plain_result(reply)
                else:
                    yield event.plain_result("最近还没有看片记录（B站 AI Bot 还没刷过）。")
                return
            await asyncio.sleep(random.uniform(2.0, 5.0))
            try:
                store_limit = int(self.config.get("recent_store_limit") or 20)
            except (TypeError, ValueError):
                store_limit = 20
            store_limit = max(high, min(store_limit, 60))
            full_rows = self._recent_rows(store_limit)
            if full_rows:
                # 清单里存全（默认 20 条），发出去的还是 5~10 条
                self._save_recent(full_rows)
                take = random.randint(low, high) if len(full_rows) >= low else len(full_rows)
                records = full_rows[:take]
                self._last_list_shown = len(records)
                logger.info("[Bridge2] 最近清单：存 %s 条，发 %s 条（%s~%s 随机）",
                            len(full_rows), len(records), low, high)
                self._last_list_at = time.time()
                try:
                    asyncio.create_task(self._refresh_core_digest(full_rows))
                except Exception:  # noqa: BLE001
                    pass
                yield event.plain_result(self._format_list(records))
            else:
                yield event.plain_result("最近还没有看片记录（B站 AI Bot 还没刷过）。")
            return

        # ②.5 问「你推过什么 / 上周发给我哪些」→ 直接从推荐账本翻，别指望记忆检索
        if PUSH_RECALL_RE.search(text) and not BV_RE.search(text):
            days = 7
            m_days = re.search(r"(?:近|最近|这)\s*([0-9一两二三四五六七八九十]+)\s*天", text)
            if m_days:
                raw = m_days.group(1)
                try:
                    days = CN_NUM.get(raw) or int(raw)
                except (TypeError, ValueError):
                    days = 7
            rows = self._push_history(days)
            if rows:
                lines = [
                    f"{i}. {r['time'][5:16]} {str(r.get('title') or '(无标题)')[:26]} · {r['bvid']}"
                    for i, r in enumerate(rows[:12], 1)
                ]
                logger.info("[Bridge2] 推送历史 %s 条（近 %s 天）", len(lines), days)
                await asyncio.sleep(random.uniform(1.0, 2.5))
                yield event.plain_result(
                    f"近 {days} 天推给你的，翻到 {len(rows)} 条：\n" + "\n".join(lines)
                    + "\n\n想听哪条说「第几个讲了什么」。")
            else:
                yield event.plain_result(f"近 {days} 天没有推给你的记录。")
            return

        # ③ 问「第几个讲了什么」→ 序号换 BV 再拉素材
        idx = self._ordinal(text)
        if idx and ("讲" in text or "说" in text or "内容" in text or "什么" in text or "是啥" in text):
            records = self._records_for_ordinal()
            if not records:
                yield event.plain_result("手头没有清单，先问我「最近刷到什么」吧。")
                return
            if not 1 <= idx <= len(records):
                yield event.plain_result(f"清单里只有 {len(records)} 条，没有第 {idx} 个。")
                return
            bvid = records[idx - 1]["bvid"]
            logger.info("[Bridge2] 第 %s 个 → %s", idx, bvid)
            try:
                material = await self._fetch_material(bvid)
                self._pending[origin] = material
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Bridge2] 取第 %s 个素材失败：%s", idx, exc)
            return

        # ③.8 提片名问细节（「悬链线那条讲了什么」）→ 按标题模糊找回那条，再把素材挂上
        if DETAIL_WORD_RE.search(text) and not BV_RE.search(text) and not self._ordinal(text):
            rec = self._guess_record_by_mention(text)
            if rec:
                try:
                    hours = int(self.config.get("watch_memory_hours") or 24)
                    material = self._cached_detail(rec["bvid"], hours)
                    if not material:
                        material = await self._fetch_material(rec["bvid"])
                    self._pending[origin] = (
                        f"【他问的就是这一条：《{rec.get('title') or ''}》 {rec['bvid']}"
                        f"（{rec.get('time') or ''} 刷到的）】\n" + material
                    )
                    logger.info("[Bridge2] 按片名命中 %s（%s）", rec["bvid"], rec.get("title"))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Bridge2] 按片名找素材失败：%s", exc)

        # ③.5 追问细节：上一条是用 24 小时缓存答的，再追问就现拉一次，别拿旧解析糊弄
        if DETAIL_Q_RE.search(text):
            hit = self._cache_hit.get(origin)
            if hit and time.time() - hit[1] <= 1800:
                cached_bvid = hit[0]
                try:
                    material = await self._fetch_material(cached_bvid)
                    self._pending[origin] = (
                        "【现拉的完整素材（用户在追问，重新读了一遍）】\n" + material
                    )
                    logger.info("[Bridge2] 追问细节，现拉 %s", cached_bvid)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Bridge2] 追问时现拉失败：%s", exc)

        # ④ 分享卡片 / 短链 / 纯链接（卡片里通常只有 b23.tv 短链，得跟 302 才拿得到 BV）
        kind, vid = await self._resolve_video_ref(event)
        if not vid:
            self._dump_card(event)
            try:
                segs = []
                raw = getattr(event.message_obj, "raw_message", None)
                for m in (getattr(raw, "message", None) or []):
                    if isinstance(m, dict):
                        segs.append((str(m.get("type")), str(m.get("data"))[:200]))
                comps = [(type(c).__name__, str(getattr(c, "data", ""))[:160])
                         for c in (getattr(event.message_obj, "message", None) or [])]
                logger.info("[Bridge2] 没认出视频｜onebot=%s｜comps=%s｜text=%r",
                            segs, comps, str(text)[:80])
            except Exception as exc:  # noqa: BLE001
                logger.info("[Bridge2] 结构诊断失败：%s", exc)
        if vid:
            logger.info("[Bridge2] 分享解析到 %s %s", kind, vid)
            # 卡片消息的文本是空的；AstrBot 会把空消息判成无效、根本不去叫模型。
            # 1 版就是靠这一步救活的：把一句可读文字写回消息。
            if (not text.strip()) or (vid not in text):
                filled = f"（我分享了一个 B站 视频：https://www.bilibili.com/video/{vid}，帮我看看）"
                event.message_str = filled
                try:
                    event.message_obj.message_str = filled
                except Exception:  # noqa: BLE001
                    pass
                logger.info("[Bridge2] 卡片补文字：%s", vid)
            try:
                material = ""
                if kind == "BV":
                    hours = int(self.config.get("watch_memory_hours") or 24)
                    material = self._cached_detail(vid, hours)
                    if material:
                        logger.info("[Bridge2] 命中近 %s 小时缓存：%s", hours, vid)
                        self._cache_hit[origin] = (vid, time.time())
                    else:
                        material = await self._fetch_material(vid)
                self._pending[origin] = material
                if material:
                    self._last_share_material[vid] = (time.time(), str(material))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Bridge2] 取素材失败：%s", exc)
            # 真看一遍：后台跑，写完记忆会镜像进记忆中枢
            self._start_share_watch(vid)

    @filter.on_llm_request()
    async def inject_watch_note(self, event: AstrMessageEvent, req: ProviderRequest):
        """把「刷一轮」的后台说明临时挂到这次请求上，让模型用自己的语气回。"""

        status = getattr(event, "bridge2_watch_note", None)
        if not status:
            return
        try:
            setattr(event, "bridge2_watch_note", None)
        except Exception:  # noqa: BLE001
            pass
        if TextPart is None:
            logger.info("[Bridge2] 没有 TextPart，刷一轮说明发不出去")
            return
        try:
            part = TextPart(text=self._watch_note_text(str(status)))
            for attr in ("mark_as_temp", "make_as_temp"):
                marker = getattr(part, attr, None)
                if callable(marker):
                    try:
                        marked = marker()
                        if marked is not None:
                            part = marked
                    except Exception:  # noqa: BLE001
                        pass
                    break
            req.extra_user_content_parts.append(part)
            logger.info("[Bridge2] 已挂上刷一轮说明（%s）", status)
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 挂刷一轮说明失败：%s", exc)

    @filter.on_llm_request()
    async def inject_watch_directive(self, event: AstrMessageEvent, req: ProviderRequest):
        """AI Bot 实例缺席时，用临时附加内容告诉模型「现在去刷」，不改用户消息。"""

        try:
            wanted = bool(event.get_extra("bridge2_watch_directive"))
        except Exception:  # noqa: BLE001
            wanted = False
        if not wanted:
            return
        if TextPart is None:
            return
        try:
            part = TextPart(text=(
                "（现在就去刷一轮 B 站：立刻调用你的 bili_watch_videos 工具，"
                "不要先问、不要解释、不要复述这句括号；刷完按你自己的口气回一句。）"
            ))
            for attr in ("mark_as_temp", "make_as_temp"):
                marker = getattr(part, attr, None)
                if callable(marker):
                    try:
                        marked = marker()
                        if marked is not None:
                            part = marked
                    except Exception:  # noqa: BLE001
                        pass
                    break
            req.extra_user_content_parts.append(part)
            logger.info("[Bridge2] 已挂上「去刷一轮」指令（附加内容）")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 挂「去刷一轮」指令失败：%s", exc)

    @filter.on_llm_request()
    async def inject_recent_round(self, event: AstrMessageEvent, req: ProviderRequest):
        """问到刷片相关的事时，先把「最近一轮」的概况塞给模型，省得它编时间。"""

        if not self._is_owner(event):
            return
        try:
            text = str(getattr(event, "message_str", "") or "")
        except Exception:  # noqa: BLE001
            return
        try:
            repeat_list = bool(event.get_extra("bridge2_repeat_list"))
        except Exception:  # noqa: BLE001
            repeat_list = False
        if repeat_list:
            if TextPart is None:
                return
            await asyncio.sleep(random.uniform(0.6, 1.2))
            records = self._load_recent() or self._recent_rows(20)
            if records:
                shown = int(getattr(self, "_last_list_shown", 0) or 0)
                lines = [
                    f"{i}. {str(r.get('title') or '')[:24]}"
                    f"{'（' + str(r.get('up'))[:8] + '）' if r.get('up') else ''} {r.get('bvid')}"
                    for i, r in enumerate(records, 1)
                ]
                if 0 < shown < len(records):
                    scope = (f"刚才你只把前 {shown} 条发给了他，后面 {len(records) - shown} 条"
                             "他没在消息里看到，别当成他都知道。")
                else:
                    scope = "刚才发给他的可能只是其中一部分。"
                note = (
                    f"（后台资料：这是完整的清单，共 {len(records)} 条（最近的）：\n"
                    + "\n".join(lines)
                    + f"\n{scope}"
                    "他这次又问了一遍——别复读清单，用你自己的口吻挑两三条说；"
                    "可以带上你对这几条的看法、之前聊过的一点印象（记忆里有就顺手带一句，"
                    "让这次跟刚才那份清单有点区别）；但片名只能从上面这份里挑，"
                    "地名、人名、标题别自己加，记忆/总结里更早的旧片名别当成「最近」报给他。）"
                )
            else:
                note = "（后台资料：刚才已经把最近刷到的清单列过了，别再重复列；用一两句话带过，问他想听哪条。）"
            part = TextPart(text=note)
            for attr in ("mark_as_temp", "make_as_temp"):
                marker = getattr(part, attr, None)
                if callable(marker):
                    try:
                        marked = marker()
                        if marked is not None:
                            part = marked
                    except Exception:  # noqa: BLE001
                        pass
                    break
            req.extra_user_content_parts.append(part)
            logger.info("[Bridge2] 重复要清单：已注入清单参考（%s 条）", len(records) if records else 0)
            return
        if not WATCH_RECALL_RE.search(text):
            return
        hours = int(self.config.get("watch_memory_hours") or 24)
        rounds = self._rounds(hours)
        if not rounds:
            return
        last = rounds[-1]
        when = self._approx_when(last[-1].get("time") or "")
        flat = [r for items in rounds for r in items][-10:]
        content_text = re.sub(r"什么时候|啥时候|几点|多久|多早", "", text)
        content_ask = bool(re.search(r"什么|啥|哪些|哪几个|哪条|讲了|内容|刷到", content_text))
        try:
            list_max = int(self.config.get("recent_list_max_chars", 12) or 12)
        except (TypeError, ValueError):
            list_max = 12
        long_ask = len(text.strip()) > list_max
        if content_ask and long_ask:
            # 长句/混合问题：只给时间和条数，挑两三条提一下，别列整张清单
            waited = random.uniform(1.0, 2.0)
            await asyncio.sleep(waited)
            picks = "、".join(str(r.get("title") or "")[:16] for r in flat[:3])
            note = (
                f"（后台资料：近 {hours} 小时刷了 {len(flat)} 个，最近一轮在 {when}。"
                f"挑着提两三条就行（比如 {picks}），别列整张清单、别贴一串 BV；"
                "记忆总结里的旧片名不算最近的。）"
            )
        elif content_ask:
            # 问「刷到了什么」才给清单，且别秒回：先压 2~5 秒
            waited = random.uniform(2.0, 5.0)
            await asyncio.sleep(waited)
            lines = [
                f"- {str(r.get('time') or '')[5:]} {str(r.get('title') or '')[:20]}"
                f"（{str(r.get('up') or '')[:8]}） {r.get('bvid')}"
                for r in flat
            ]
            note = (
                f"（后台资料：近 {hours} 小时刷了 {len(flat)} 个，最近一轮在 {when}。清单：\n"
                + "\n".join(lines)
                + "\n直接照这个答，别编具体分秒；要某条细节按 BV 调 "
                "bili_bridge2_read（近 24 小时有缓存，不用现拉）。"
                "这份是刚查的、以它为准——记忆总结里那些更早的片名别再往「最近」里算。）"
            )
        else:
            # 只问时间 / 只问「刷没刷」：一两句话说清，别列清单
            waited = random.uniform(1.0, 2.0)
            await asyncio.sleep(waited)
            note = (
                f"（后台资料：最近一轮刷片在 {when}，一共 {len(last)} 个。"
                "他没问具体内容，一两句话回答时间或有没有刷就行，"
                "别主动列清单、别贴一串标题。）"
            )
        if TextPart is None:
            return
        try:
            part = TextPart(text=note)
            for attr in ("mark_as_temp", "make_as_temp"):
                marker = getattr(part, attr, None)
                if callable(marker):
                    try:
                        marked = marker()
                        if marked is not None:
                            part = marked
                    except Exception:  # noqa: BLE001
                        pass
                    break
            req.extra_user_content_parts.append(part)
            logger.info("[Bridge2] 已注入刷片近况（%s 条，延迟 %.1fs）", len(flat), waited)
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 注入刷片近况失败：%s", exc)

    # ── 素材注入（只加临时附加内容，不动 system_prompt）─────────────────
    @staticmethod
    def _temp_part(text: str):
        """把一段文字包成临时 content part：用完就丢，不落进历史上下文。

        所有「后台资料」注入都走这里，别直接 append 裸 TextPart——
        裸 part 会被 AstrBot 当正式用户内容存进历史，下一轮还在，越滚越长。
        """
        part = TextPart(text=text)
        for attr in ("mark_as_temp", "make_as_temp"):
            marker = getattr(part, attr, None)
            if callable(marker):
                try:
                    marked = marker()
                    if marked is not None:
                        part = marked
                except Exception:  # noqa: BLE001
                    pass
                break
        return part

    @filter.on_llm_request()
    async def inject_material(self, event: AstrMessageEvent, req: ProviderRequest):
        origin = getattr(event, "unified_msg_origin", "") or ""
        # 老大分享过来、刚刚真看完的视频：让她能主动接一句「我看完了」
        done = self._share_done
        if done and time.time() - done[0] < 1800:
            self._share_done = None
            if TextPart is not None:
                try:
                    req.extra_user_content_parts.append(self._temp_part(
                        "（补充：你刚把老大分享的那个视频真看完了，下面是你的看片记录。"
                        "可以自然地说一句你看到了什么，不要整段复述，"
                        "也不要把「已完成视频分析」这类字样说出来）\n" + done[2]
                    ))
                    logger.info("[Bridge2] 已注入真看结果：%s", done[1])
                except Exception:  # noqa: BLE001
                    pass
        material = self._pending.pop(origin, None)
        if not material:
            # 前一步没留下素材（比如短链补正文没生效），这里再解析一次兜底
            bvid = await self._resolve_bvid(event)
            if bvid:
                logger.info("[Bridge2] 注入前兜底解析到 %s", bvid)
                try:
                    material = await self._fetch_material(bvid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Bridge2] 兜底取素材失败：%s", exc)
                    return
        if not material:
            return
        if TextPart is None:
            logger.info("[Bridge2] 没有 TextPart，跳过注入（仍可用 bili_bridge2_read 工具）")
            return
        try:
            req.extra_user_content_parts.append(self._temp_part(material))
            logger.info("[Bridge2] 已注入素材（%s 字）", len(material))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge2] 注入失败：%s", exc)

    # ── LLM 工具 ────────────────────────────────────────────────────────
    @filter.llm_tool(name="bili_bridge2_recent")
    async def tool_recent(self, event: AstrMessageEvent, limit: int = 10) -> str:
        """列出 B站 AI Bot 最近刷到的视频（带序号）。

        Args:
            limit(int): 最多几条，默认 10
        """
        records = self._watch_records()[: max(1, min(int(limit or 10), 30))]
        if not records:
            return "还没有看片记录。"
        self._save_recent(records)
        return self._format_list(records)

    @filter.llm_tool(name="bili_bridge2_read")
    async def tool_read(self, event: AstrMessageEvent, target: str = "", refresh: bool = False) -> str:
        """按 BV 号或「第几个」读取 B站视频内容。

        默认先用近 24 小时的缓存解析；用户追问更具体的细节、或缓存内容不足以回答时，
        带 refresh=true 重新拉一次字幕。

        Args:
            target(string): BV 号或序号（如「3」）
            refresh(bool): 为 true 时跳过缓存，现拉字幕
        """
        target = str(target or "").strip()
        bvid = ""
        m = BV_RE.search(target)
        if m:
            bvid = m.group(1)
        elif target.isdigit():
            records = self._records_for_ordinal()
            i = int(target)
            if 1 <= i <= len(records):
                bvid = records[i - 1]["bvid"]
        if not bvid:
            return "没认出 BV 号或序号。"
        if not refresh:
            hours = int(self.config.get("watch_memory_hours") or 24)
            cached = self._cached_detail(bvid, hours)
            if cached:
                origin = getattr(event, "unified_msg_origin", "") or ""
                self._cache_hit[origin] = (bvid, time.time())
                logger.info("[Bridge2] 命中近 %s 小时缓存：%s", hours, bvid)
                return cached
        return await self._fetch_material(bvid)

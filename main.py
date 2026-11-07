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
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
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
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
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
ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]{1,2})\s*(?:个|条|支|部)?")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 「再具体讲讲 / 展开说说 / 后来呢」这类是在追问上一条的细节
DETAIL_Q_RE = re.compile(r"(再|具体|详细|展开|细说|多说|深入|后来|然后呢|为什么|怎么回事)")


@register(PLUGIN_NAME, "paopao", "把 B站 AI Bot 刷到的视频接进 QQ", "1.0.0")
class BiliBridge2(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._pending: dict[str, str] = {}
        self._cache_hit: dict[str, tuple[str, float]] = {}
        self._last_watch = 0.0
        self._recent_path = Path(__file__).resolve().parent / "recent.json"

    # ── 配置/工具 ────────────────────────────────────────────────────────
    def _owners(self) -> set[str]:
        raw = str(self.config.get("owner_uids") or "1792559819")
        return {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) in self._owners()
        except Exception:  # noqa: BLE001
            return False

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
    def _watch_records(self) -> list[dict]:
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
            return [r for r in data if isinstance(r, dict) and r.get("bvid")]
        except Exception:  # noqa: BLE001
            return []

    def _format_list(self, records: list[dict]) -> str:
        lines = []
        for i, r in enumerate(records, 1):
            title = str(r.get("title") or "(无标题)").strip()
            if len(title) > 35:
                title = title[:35] + "…"
            up = f"（{r['up']}）" if r.get("up") else ""
            lines.append(f"{i}. {title}{up} · {r['bvid']}")
        return "最近刷到的：\n" + "\n".join(lines) + "\n\n想问哪条直接说「第几个讲了什么」。"

    def _cached_detail(self, bvid: str, hours: int = 24) -> str:
        """近 N 小时看过的视频，直接从 video_memory.json 拿当时的完整解析，省一次 bili-cli。"""

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
        if "[bili-cli" in subtitle or len(subtitle.strip()) < 20:
            subtitle = await self._bili("video", "summary", bvid)
        cap = int(self.config.get("subtitle_max_chars") or 1800)
        return (
            f"【B站视频素材 {bvid}】\n"
            f"— 基本信息 —\n{info.strip()[:1200]}\n"
            f"— 内容（字幕/官方总结）—\n{subtitle.strip()[:cap]}\n"
            "用法：保持人设用你自己的话说，别复述标题、别写成评测稿。"
        )

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
            path = Path(__file__).resolve().parent / "card_debug.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 卡片诊断落盘失败：%s", exc)

    def _ai_bot_instance(self):
        """拿到 B站 AI Bot 的插件实例，用来判断在不在跑、直接触发刷一轮。"""

        try:
            meta = self.context.get_registered_star("astrbot_plugin_bilibili_ai_bot")
            inst = getattr(meta, "star_cls", None)
            if inst is not None and hasattr(inst, "_run_proactive"):
                return inst
        except Exception as exc:  # noqa: BLE001
            logger.info("[Bridge2] 拿不到 AI Bot 实例：%s", exc)
        return None

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

    def _pushed_records(self) -> list[dict]:
        """最近一次推给主人的那一批，按 QQ 消息里的顺序。"""

        path = self._ai_bot_dir() / "owner_last_push.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            return []
        bvids = [str(b) for b in (data.get("bvids") or []) if b]
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
                task = getattr(inst, "_proactive_task", None) if inst else None
                if task is not None and not task.done():
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
                        count = 1
                    # 必须挂到 AI Bot 自己的 _proactive_task 上，否则它的口令钩子看不到
                    # 「正在跑」，会在同一条消息上再开一轮
                    inst._proactive_task = asyncio.create_task(inst._run_proactive(max_watch=count))
                    logger.info("[Bridge2] 口令直接触发 AI Bot 刷一轮：%s 个", count)
                    self._attach_watch_note(event, "started")
                    self._suppress_bili_hook(event)
                    return
                self._last_watch = time.time()
                directive = (
                    "（现在就去刷一轮 B 站：立刻调用你的 bili_watch_videos 工具，"
                    "不要先问、不要解释、不要复述这句括号；刷完按你自己的口气回一句。）"
                )
                # 写进消息本身，比附加内容更强 —— 模型一定会看到
                event.message_str = directive
                try:
                    event.message_obj.message_str = directive
                except Exception:  # noqa: BLE001
                    pass
                logger.info("[Bridge2] 口令触发（已改写成指令）：%s", hit)
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
        if RECENT_Q_RE.search(text):
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
            now = time.time()
            try:
                repeat_window = int(self.config.get("list_repeat_window", 600) or 600)
            except (TypeError, ValueError):
                repeat_window = 600
            if now - float(getattr(self, "_last_list_at", 0.0)) <= repeat_window:
                logger.info("[Bridge2] 刚发过清单（%.0f 秒内），交给模型回", now - float(getattr(self, "_last_list_at", 0.0)))
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
                logger.info("[Bridge2] 只问时间/有没有刷，回一句（%s）", when)
                if last_round:
                    yield event.plain_result(
                        f"最近一轮在 {when}，一共 {len(last_round)} 个。"
                    )
                else:
                    yield event.plain_result("最近还没有看片记录（B站 AI Bot 还没刷过）。")
                return
            await asyncio.sleep(random.uniform(2.0, 5.0))
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
            rows = self._recent_rows(high)
            if rows:
                take = random.randint(low, high) if len(rows) >= low else len(rows)
                records = rows[:take]
                self._save_recent(records)
                logger.info("[Bridge2] 最近清单 %s 条（%s~%s 随机）", len(records), low, high)
                self._last_list_at = time.time()
                yield event.plain_result(self._format_list(records))
            else:
                yield event.plain_result("最近还没有看片记录（B站 AI Bot 还没刷过）。")
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

        # ④ 分享卡片 / 短链（QQ 卡片里通常只有 b23.tv 短链，得跟 302 才拿得到 BV）
        bvid = await self._resolve_bvid(event)
        if not bvid:
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
        if bvid:
            logger.info("[Bridge2] 分享卡片解析到 %s", bvid)
            # 卡片消息的文本是空的；AstrBot 会把空消息判成无效、根本不去叫模型。
            # 1 版就是靠这一步救活的：把一句可读文字写回消息。
            if (not text.strip()) or (bvid not in text):
                filled = f"（我分享了一个 B站 视频：https://www.bilibili.com/video/{bvid}，帮我看看）"
                event.message_str = filled
                try:
                    event.message_obj.message_str = filled
                except Exception:  # noqa: BLE001
                    pass
                logger.info("[Bridge2] 卡片补文字：%s", bvid)
            try:
                hours = int(self.config.get("watch_memory_hours") or 24)
                material = self._cached_detail(bvid, hours)
                if material:
                    logger.info("[Bridge2] 命中近 %s 小时缓存：%s", hours, bvid)
                    self._cache_hit[origin] = (bvid, time.time())
                else:
                    material = await self._fetch_material(bvid)
                self._pending[origin] = material
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Bridge2] 取素材失败：%s", exc)

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
            part = TextPart(text="（后台资料：刚才已经把最近刷到的清单列过了，别再重复列；用一两句话带过，问他想听哪条。）")
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
            logger.info("[Bridge2] 重复要清单：已注入「别重复」说明")
            return
        if not WATCH_RECALL_RE.search(text):
            return
        hours = int(self.config.get("watch_memory_hours") or 24)
        rounds = self._rounds(hours)
        if not rounds:
            return
        last = rounds[-1]
        when = last[-1].get("time") or "?"
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
                f"挑着提两三条就行（比如 {picks}），别列整张清单、别贴一串 BV。）"
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
                "bili_bridge2_read（近 24 小时有缓存，不用现拉）。）"
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
    @filter.on_llm_request()
    async def inject_material(self, event: AstrMessageEvent, req: ProviderRequest):
        origin = getattr(event, "unified_msg_origin", "") or ""
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
            part = TextPart(text=material)
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

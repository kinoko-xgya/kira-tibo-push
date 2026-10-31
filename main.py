"""tibo_push - poll tibo.cc for Codex reset notices and push on change.

- Polls the API every `poll_seconds`, writes /tmp/tibo_push.heartbeat each round
  so an external watchdog can judge liveness without touching the process.
- State lives in the plugin data dir, isolated from the script version's state.
- Fetch failures stay silent until they last longer than `fail_alert_seconds`,
  so a flaky upstream never becomes a notification storm.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from core.plugin import BasePlugin, logger, on
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.provider import LLMRequest
from core.utils.tool_utils import BaseTool

API = "https://tibo.cc/api/v1/status"
UA = ("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
      "Chrome/120.0 Mobile Safari/537.36")
CST = timezone(timedelta(hours=8))

DEFAULT_TARGET = ""  # empty = resolved at runtime via ctx
DEFAULT_POLL = 300
FAIL_ALERT_AFTER = 600
HEARTBEAT = Path("/tmp/tibo_push.heartbeat")


def _cst(s):
    if not s:
        return "未知"
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return str(s)[:32]


def _brief(item):
    if not item:
        return None
    tr = (item.get("translation") or {}).get("text") or ""
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "type": item.get("reset_type"),
        "announced": _cst(item.get("announced_at")),
        "scheduled": _cst(item.get("scheduled_for")),
        "text": (item.get("text") or "")[:200],
        "zh": tr[:200],
    }


def _int(value, default):
    """Schema ints may arrive as str from older configs; keep 0 valid."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pick(value, allowed, default):
    """Return value if it is one of allowed, else default."""
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    if text:
        logger.warning(f"[tibo_push] invalid option {value!r}, fallback {default}")
    return default



def _adapters(ctx=None) -> list:
    """Adapter instance names the host knows about, best effort.

    KiraAI exposes them through ctx.adapter_mgr.get_adapters() (dict) or
    get_adapters_info() (list of AdapterInfo). Both are tried; a wrong guess
    must never raise, because this runs on the config path.
    """
    try:
        mgr = getattr(ctx, "adapter_mgr", None) if ctx is not None else None
        if mgr is None:
            return []
        names = []
        info_list = mgr.get_adapters_info() or []
        for info in info_list:
            name = getattr(info, "name", None) or getattr(info, "adapter_name", None)
            if name:
                names.append(str(name))
        if not names:
            names = [str(k) for k in (mgr.get_adapters() or {}).keys()]
        return names
    except Exception:
        return []


def _qualify(raw: str, ctx=None, kind: str = "dm") -> str:
    """Return a fully-qualified sid, or "" if it cannot be inferred safely.

    A bare id is completed with the host's adapter name instead of a hardcoded
    "QQ", so the same build works on any platform. If the host reports zero or
    several adapter names we return "" rather than guess -- pushing a stranger's
    DM is worse than not pushing at all.
    """
    target = (raw or "").strip()
    if not target:
        return ""
    if target.count(":") == 2:
        return target
    if ":" in target:          # e.g. "qq:dm:123" already has the adapter
        return target
    names = _adapters(ctx)
    if len(names) == 1:
        return f"{names[0]}:{kind}:{target}"
    return ""


def _fallback_target(ctx) -> str:
    """Best-effort default target: the owner/admin DM declared by the host.

    Kept intentionally quiet: if nothing can be inferred, we return "" and the
    plugin logs a clear warning instead of pushing to a stranger's DM.
    """
    try:
        cfg = getattr(ctx, "config", None) or {}
        for key in ("admin_users", "admins", "owner"):
            v = cfg.get(key) if isinstance(cfg, dict) else None
            if isinstance(v, (list, tuple)) and v:
                return _qualify(str(v[0]), ctx, "dm")
            if isinstance(v, (str, int)) and v:
                return _qualify(str(v), ctx, "dm")
    except Exception:
        pass
    return ""


class TiboStatusTool(BaseTool):
    name = "tibo_status"
    description = "Fetch tibo.cc once (latest Codex reset + scheduled notice) and push it immediately."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, plugin):
        super().__init__()
        self._plugin = plugin

    async def execute(self, *_args, **_kwargs):
        return await self._plugin.check_once(force_report=True)


class TiboPushPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        # Push modes:
        #   direct = send a machine-formatted notice straight to the target sid
        #   notice = wake the agent in the target session and let it handle it
        # There is deliberately no LLM-rewrite mode: a half-human half-machine
        # voice makes it impossible to tell who is speaking.
        adv = cfg.get("section_advanced", {}) or {}
        target = cfg.get("target_session")
        self.target = str(target).strip() if target else DEFAULT_TARGET
        # target_session may be a bare id when the schema selector strips the prefix
        if self.target and self.target.count(":") != 2:
            self.target = _qualify(self.target, getattr(self, "ctx", None), "gm")
        if not self.target:
            self.target = _fallback_target(ctx)
        self.mode = _pick(cfg.get("mode"), ("direct", "notice"), "direct")
        # poll_seconds lives under section_advanced but tolerate a flat key too
        self.poll = max(60, _int(adv.get("poll_seconds", cfg.get("poll_seconds")), DEFAULT_POLL))
        self.fail_alert = max(60, _int(adv.get("fail_alert_seconds"), FAIL_ALERT_AFTER))
        self._task = None
        self._state = {}
        self._fail_since = None
        self._fail_notified = False
        data_dir = None
        try:
            data_dir = ctx.get_plugin_data_dir()
        except Exception:
            pass
        self._state_file = Path(data_dir or "/tmp") / "tibo_push_state.json"

    def _load_state(self):
        try:
            self._state = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            self._state = {}

    def _save_state(self):
        try:
            self._state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[tibo_push] state save failed: {e}")

    def _beat(self):
        try:
            HEARTBEAT.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except Exception:
            pass

    async def _fetch(self):
        async with httpx.AsyncClient(timeout=20,
                                     headers={"User-Agent": UA,
                                              "Accept": "application/json"}) as c:
            r = await c.get(API)
            r.raise_for_status()
            return (r.json().get("data") or {})

    def _format(self, cur, changed):
        latest, sched = cur.get("latest"), cur.get("scheduled")
        parts = []
        if "latest" in changed:
            parts.append("最近重置")
        if "scheduled" in changed:
            parts.append("待执行公告")
        lines = ["tibo 有更新：" + "/".join(parts)]
        if latest:
            lines.append(f"最近重置 {latest['announced']}｜{latest['zh'] or latest['text']}")
        if sched:
            lines.append(f"待执行 公告 {sched['announced']}｜预计 {sched['scheduled']}｜"
                         f"{sched['zh'] or sched['text']}")
        return "\n".join(lines)

    def _sid(self) -> str:
        """Return a fully-qualified sid, or "" when nothing usable is configured."""
        target = (self.target or "").strip()
        if target.count(":") == 2:
            return target
        return _qualify(target, getattr(self, "ctx", None))

    async def _push(self, raw_text: str):
        """direct: plain machine notice. notice: hand the facts to the agent.

        Any failure falls back to direct, so a real change is never dropped.
        """
        sid = self._sid()
        if not sid:
            logger.warning("[tibo_push] no target session configured, drop this report: "
                           + raw_text.replace("\n", " | "))
            return False
        if self.mode == "notice":
            try:
                await self.ctx.publish_notice(
                    sid, MessageChain([Text(f"[tibo 监控] {raw_text}")]), is_mentioned=True
                )
                logger.info(f"[tibo_push] notice handed to agent for {sid}")
                return True
            except Exception as e:
                logger.warning(f"[tibo_push] notice failed ({type(e).__name__}: {e}), "
                               f"fallback direct")
        return await self._send_raw(f"[tibo] {raw_text}", sid)

    async def _send_raw(self, text: str, sid: str = "") -> bool:
        try:
            await self.ctx.send_message_chain(session=sid or self._sid(),
                                              chain=MessageChain([Text(text)]))
            logger.info(f"[tibo_push] pushed to {sid or self._sid()}")
            return True
        except Exception:
            logger.exception("[tibo_push] push failed")
            return False

    async def check_once(self, force_report=False):
        self._beat()
        try:
            data = await self._fetch()
        except Exception as e:
            now = time.time()
            if self._fail_since is None:
                self._fail_since = now
            waited = now - self._fail_since
            logger.warning(f"[tibo_push] fetch failed ({waited:.0f}s): {type(e).__name__}: {e}")
            if waited >= self.fail_alert and not self._fail_notified:
                self._fail_notified = True
                await self._push(f"tibo 接口连不上了，已经 {int(waited // 60)} 分钟拉不到数据"
                                 f"（{type(e).__name__}）")
            return f"FETCH_FAIL {type(e).__name__}"

        self._fail_since = None
        self._fail_notified = False

        latest = _brief(data.get("latest_reset"))
        sched = _brief(data.get("scheduled_reset"))
        cur = {"latest": latest, "scheduled": sched}
        old = self._state

        changed = []
        if (latest or {}).get("id") and latest["id"] != (old.get("latest") or {}).get("id"):
            changed.append("latest")
        if (sched or {}).get("id") and sched["id"] != (old.get("scheduled") or {}).get("id"):
            changed.append("scheduled")

        if force_report and not changed and latest:
            changed = ["latest"]

        if changed:
            await self._push(self._format(cur, changed))

        self._state = cur
        self._save_state()
        return ("CHANGE " + ",".join(changed)) if changed else "NOCHANGE"

    async def _loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[tibo_push] unexpected error in loop")
            await asyncio.sleep(self.poll)

    async def initialize(self):
        self._load_state()
        self._beat()
        if not self._sid():
            logger.warning("[tibo_push] no target_session configured; reports will be "
                           "logged only. Set it in the plugin settings.")
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[tibo_push] started | target={self.target} poll={self.poll}s "
                    f"mode={self.mode} state={self._state_file}")

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[tibo_push] stopped")

    @on.llm_request()
    async def inject_tool(self, _event, req: LLMRequest, *_):
        req.tool_set.add(TiboStatusTool(self))

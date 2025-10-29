from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Deque, Tuple
from collections import defaultdict, deque

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig  # per docs: from astrbot.api import AstrBotConfig

# 工具函数
def _ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)
    return p

def _now_tz(tz_name: str | None) -> datetime:
    try:
        if tz_name:
            import zoneinfo
            return datetime.now(zoneinfo.ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.now()

def _parse_hhmm(s: str) -> Optional[Tuple[int,int]]:
    if not s:
        return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def _in_quiet(now: datetime, quiet: str) -> bool:
    if not quiet or "-" not in quiet:
        return False
    a, b = quiet.split("-", 1)
    p1 = _parse_hhmm(a); p2 = _parse_hhmm(b)
    if not p1 or not p2: return False
    t1 = time(p1[0], p1[1]); t2 = time(p2[0], p2[1])
    nt = now.time()
    if t1 <= t2:
        return t1 <= nt <= t2
    else:
        return nt >= t1 or nt <= t2

def _fmt_now(fmt: str, tz: str | None) -> str:
    return _now_tz(tz).strftime(fmt)

# 数据结构定义
@dataclass
class UserProfile:
    """用户订阅信息和个性化设置"""
    subscribed: bool = False  # 订阅状态
    idle_after_minutes: int | None = None  # 自动续聊时间（分钟），None表示使用全局默认
    daily_reminders_enabled: bool = True  # 是否开启每日提醒
    daily_reminder_count: int = 3  # 每日提醒数量

@dataclass
class SessionState:
    """运行时会话状态（内存中维护）"""
    last_ts: float = 0.0  # 最后活跃时间戳
    last_fired_tag: str = ""  # 最后触发标签
    last_user_reply_ts: float = 0.0  # 用户最后回复时间戳
    consecutive_no_reply_count: int = 0  # 连续无回复次数
    next_idle_ts: float = 0.0  # 下一次延时问候触发时间戳（0表示未计划）

@dataclass
class Reminder:
    id: str
    umo: str
    content: str
    at: str           # "YYYY-MM-DD HH:MM" 或 "HH:MM|daily"
    created_at: float

# 主插件
@register("Conversa", "柯尔", "AI 定时主动续聊 · 支持人格与上下文记忆", "1.0.0", "https://github.com/Luna-channel/astrbot_plugin_Conversa")
class Conversa(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        self._states: Dict[str, SessionState] = {}  # 运行时会话状态
        self._user_profiles: Dict[str, UserProfile] = {}  # 用户订阅信息和设置
        self._context_caches: Dict[str, Deque[Dict]] = {}  # 聊天上下文缓存
        self._reminders: Dict[str, Reminder] = {}  # 用户设置的提醒

        root = os.getcwd()
        self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_conversa"))
        self._state_path = os.path.join(self._data_dir, "state.json")  # 运行时状态（兼容旧版）
        self._user_profiles_path = os.path.join(self._data_dir, "user_profiles.json")  # 用户订阅信息
        self._context_cache_path = os.path.join(self._data_dir, "context_cache.json")  # 聊天缓存
        self._remind_path = os.path.join(self._data_dir, "reminders.json")  # 用户提醒
        self._session_states_path = os.path.join(self._data_dir, "session_states.json") # 运行时状态
        self._load_user_profiles()
        self._load_context_caches()
        self._load_reminders()
        self._load_session_states()
        self._sync_subscribed_users_from_config()  # 从配置同步订阅列表到内部状态

        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[Conversa] scheduler started.")

    # 数据持久化
    def _load_user_profiles(self):
        """加载用户订阅信息和个性化设置"""
        if os.path.exists(self._user_profiles_path):
            try:
                with open(self._user_profiles_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for umo, profile_data in d.get("profiles", {}).items():
                    profile = UserProfile(
                        subscribed=profile_data.get("subscribed", False),
                        idle_after_minutes=profile_data.get("idle_after_minutes"),
                        daily_reminders_enabled=profile_data.get("daily_reminders_enabled", True),
                        daily_reminder_count=profile_data.get("daily_reminder_count", 3)
                    )
                    self._user_profiles[umo] = profile
            except Exception as e:
                logger.error(f"[Conversa] load user profiles error: {e}")

    def _save_user_profiles(self):
        """保存用户订阅信息和个性化设置"""
        try:
            dump = {
                "profiles": {
                    umo: {
                        "subscribed": profile.subscribed,
                        "idle_after_minutes": profile.idle_after_minutes,
                        "daily_reminders_enabled": profile.daily_reminders_enabled,
                        "daily_reminder_count": profile.daily_reminder_count
                    } for umo, profile in self._user_profiles.items()
                }
            }
            with open(self._user_profiles_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2)
            
            # 同步订阅用户列表到配置（以用户ID形式存储，方便WebUI管理）
            subscribed_ids = []
            for umo, profile in self._user_profiles.items():
                if profile.subscribed:
                    # 提取用户ID（去掉平台前缀）
                    user_id = umo.split(":")[-1] if ":" in umo else umo
                    subscribed_ids.append(user_id)
            
            logger.debug(f"[Conversa] _save_user_profiles: 同步 {len(subscribed_ids)} 个订阅用户到配置: {subscribed_ids}")
            self.cfg["subscribed_users"] = subscribed_ids
            self.cfg.save_config()
            logger.debug(f"[Conversa] _save_user_profiles: 配置已保存")

        except Exception as e:
            logger.error(f"[Conversa] save user profiles error: {e}")

    def _load_context_caches(self):
        """加载聊天上下文缓存"""
        if os.path.exists(self._context_cache_path):
            try:
                with open(self._context_cache_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for umo, cache_data in d.get("caches", {}).items():
                    context_cache = deque(maxlen=32)
                    for item in cache_data:
                        context_cache.append(item)
                    self._context_caches[umo] = context_cache
            except Exception as e:
                logger.error(f"[Conversa] load context caches error: {e}")

    def _save_context_caches(self):
        """保存聊天上下文缓存"""
        try:
            dump = {
                "caches": {
                    umo: list(cache) for umo, cache in self._context_caches.items()
                }
            }
            with open(self._context_cache_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Conversa] save context caches error: {e}")

    def _load_reminders(self):
        """从磁盘加载所有提醒事项（一次性提醒和每日提醒）"""
        if os.path.exists(self._remind_path):
            try:
                with open(self._remind_path, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                for it in arr:
                    r = Reminder(**it)
                    self._reminders[r.id] = r
            except Exception as e:
                logger.error(f"[Conversa] load reminders error: {e}")

    def _save_reminders(self):
        """保存所有提醒事项到磁盘"""
        try:
            arr = [r.__dict__ for r in self._reminders.values()]
            with open(self._remind_path, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Conversa] save reminders error: {e}")
    
    def _load_session_states(self):
        """加载运行时会话状态"""
        if os.path.exists(self._session_states_path):
            try:
                with open(self._session_states_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for umo, st_data in d.items():
                    self._states[umo] = SessionState(**st_data)
            except Exception as e:
                logger.error(f"[Conversa] load session states error: {e}")

    def _save_session_states(self):
        """保存运行时会话状态"""
        try:
            dump = {
                umo: state.__dict__ for umo, state in self._states.items()
            }
            with open(self._session_states_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Conversa] save session states error: {e}")
    
    def _sync_subscribed_users_from_config(self):
        """
        从配置文件同步订阅用户列表到内部状态
        
        功能：
        - 读取配置中的 subscribed_users 列表（纯用户ID）
        - 将这些用户标记为已订阅
        - 支持用户在 WebUI 中直接编辑订阅列表
        
        注意：
        - 配置中存储的是纯用户ID（如 "49025031"）
        - 内部 _states 的 key 是完整的 umo（如 "aulus-beta:FriendMessage:49025031"）
        - 需要遍历所有 _states，匹配 ID 后缀来应用订阅状态
        """
        try:
            config_subscribed_ids = self.cfg.get("subscribed_users") or []
            if not isinstance(config_subscribed_ids, list):
                logger.warning(f"[Conversa] subscribed_users 配置格式错误，应为列表")
                return
            
            # 将配置中的用户ID应用到用户配置
            for umo, profile in self._user_profiles.items():
                user_id = umo.split(":")[-1] if ":" in umo else umo
                if user_id in config_subscribed_ids:
                    profile.subscribed = True
                    logger.debug(f"[Conversa] 从配置同步订阅状态: {umo}")

            # 为配置中但尚未存在于 _user_profiles 的用户创建配置（标记为已订阅）
            # 注意：这些用户的完整 umo 要等到他们第一次发消息时才能确定
            # 所以这里只是做个标记，实际订阅会在 _on_any_message 中生效
            
            logger.info(f"[Conversa] 已从配置同步 {len(config_subscribed_ids)} 个订阅用户ID: {config_subscribed_ids}")
            
            # 显示当前所有已订阅的会话
            subscribed_sessions = [umo for umo, profile in self._user_profiles.items() if profile.subscribed]
            logger.info(f"[Conversa] 当前已订阅的会话数: {len(subscribed_sessions)}")
            
        except Exception as e:
            logger.error(f"[Conversa] 同步订阅用户配置失败: {e}")

    # 消息处理
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        """
        监听所有消息事件的 Handler
        
        功能：
        1. 更新会话的最后活跃时间戳（用于触发定时回复）
        2. 更新用户最后回复时间（用于自动退订检测）
        3. 重置连续无回复计数器
        4. 如果是自动订阅模式，自动订阅新会话
        5. 记录用户消息到轻量历史缓存（供上下文获取降级使用）
        
        注意：这个 handler 会捕获所有消息，包括机器人自己发的消息
        """
        umo = event.unified_msg_origin
        if umo not in self._states:
            self._states[umo] = SessionState()
        if umo not in self._user_profiles:
            self._user_profiles[umo] = UserProfile()
        if umo not in self._context_caches:
            self._context_caches[umo] = deque(maxlen=32)

        st = self._states[umo]
        profile = self._user_profiles[umo]
        context_cache = self._context_caches[umo]

        now_ts = _now_tz(self.cfg.get("timezone") or None).timestamp()
        st.last_ts = now_ts
        st.last_user_reply_ts = now_ts  # 记录用户最后回复时间
        st.consecutive_no_reply_count = 0  # 重置无回复计数

        # 检查订阅状态：支持自动订阅模式
        if (self.cfg.get("subscribe_mode") or "manual") == "auto":
            profile.subscribed = True

        # 只为订阅用户记录上下文缓存（双向对话）
        try:
            if profile.subscribed:
                role = "assistant" if event.is_self else "user"
                content = event.message_str or ""
                if content:
                    context_cache.append({"role": role, "content": content})
        except Exception:
            pass

        # 计算下一次延时问候触发时间（优先使用用户个性化设置）
        try:
            if profile.subscribed and bool(self.cfg.get("enable_idle_greetings", True)):
                delay_m = profile.idle_after_minutes  # 优先使用用户设置
                
                # 如果用户未设置，则使用全局设置
                if delay_m is None:
                    mode = (self.cfg.get("idle_trigger_mode") or "fixed").strip().lower()
                    if mode == "random_window":
                        min_m = int(self.cfg.get("idle_after_min_minutes") or 30)
                        max_m = int(self.cfg.get("idle_after_max_minutes") or 90)
                        delay_m = random.randint(min_m, max_m) if max_m > min_m else min_m
                    else:  # fixed mode
                        delay_m = int(self.cfg.get("idle_after_minutes") or 45)
                
                st.next_idle_ts = now_ts + delay_m * 60
        except Exception as e:
            logger.warning(f"[Conversa] 计算 next_idle_ts 失败: {e}")


        self._save_session_states()
        self._save_user_profiles()
        self._save_context_caches()

    # QQ命令处理
    @filter.command("conversa", aliases=["cvs"])
    async def _cmd_conversa(self, event: AstrMessageEvent):
        """
        Conversa 插件的命令处理器
        
        支持的子命令：
        - help: 显示帮助信息
        - debug: 显示当前配置和调试信息
        - on/off: 启用/停用插件
        - watch: 订阅当前会话（开始接收主动回复）
        - unwatch: 退订当前会话（停止接收主动回复）
        - show: 显示当前会话的配置和状态
        - set after <分钟>: 设置消息后多久触发主动回复
        - set daily1/daily2 <HH:MM>: 设置每日定时回复时间
        - set quiet <HH:MM-HH:MM>: 设置免打扰时间段
        - set history <N>: 设置上下文历史条数
        - prompt list/add/del/clear: 管理自定义提示词
        - remind add/list/del: 管理提醒事项
        
        用法示例：
        /conversa watch - 订阅当前会话
        /conversa set after 30 - 设置30分钟无消息后主动回复
        /conversa prompt add 现在是{now}，请继续聊天 - 添加自定义提示词
        """
        text = (event.message_str or "").strip()
        lower = text.lower()

        def reply(msg: str):
            return event.plain_result(msg)

        if "help" in lower or text.strip() == "/conversa" or text.strip() == "/cvs":
            yield reply(self._help_text())
            return

        if " debug" in lower:
            # 调试信息
            debug_info = []
            debug_info.append(f"插件启用状态: {self.cfg.get('enable', True)}")
            debug_info.append(f"订阅模式: {self.cfg.get('subscribe_mode', 'manual')}")
            debug_info.append(f"当前用户: {event.unified_msg_origin}")
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            debug_info.append(f"用户订阅状态: {self._user_profiles.get(umo).subscribed if self._user_profiles.get(umo) else False}")
            debug_info.append(f"间隔触发设置: {self.cfg.get('after_last_msg_minutes', 0)}分钟")
            debug_info.append(f"免打扰时间: {self.cfg.get('quiet_hours', '')}")
            debug_info.append(f"最大无回复天数: {self.cfg.get('max_no_reply_days', 0)}")
            yield reply("🔍 调试信息:\n" + "\n".join(debug_info))
            return

        if " on" in lower:
            self.cfg["enable"] = True
            self.cfg.save_config()
            yield reply("✅ 已启用 Conversa")
            return
        if " off" in lower:
            self.cfg["enable"] = False
            self.cfg.save_config()
            yield reply("🛑 已停用 Conversa")
            return

        if " watch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = True
            logger.info(f"[Conversa] 用户执行 watch 命令: {umo}")
            self._save_user_profiles()
            yield reply(f"📌 已订阅当前会话")
            return

        if " unwatch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = False
            self._save_user_profiles()
            yield reply(f"📭 已退订当前会话")
            return

        if " show" in lower:
            umo = event.unified_msg_origin
            profile = self._user_profiles.get(umo)
            st = self._states.get(umo)
            # 计算 next_idle_ts 友好显示
            tz = self.cfg.get("timezone") or None
            next_idle_str = "未计划"
            if st and st.next_idle_ts and st.next_idle_ts > 0:
                try:
                    dt = datetime.fromtimestamp(st.next_idle_ts, tz=_now_tz(tz).tzinfo)
                    next_idle_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    next_idle_str = str(st.next_idle_ts)
            info = {
                "enable": self.cfg.get("enable"),
                "timezone": self.cfg.get("timezone"),
                "enable_daily_greetings": self.cfg.get("enable_daily_greetings", True),
                "enable_idle_greetings": self.cfg.get("enable_idle_greetings", True),
                "idle_trigger_mode": self.cfg.get("idle_trigger_mode", "fixed"),
                "idle_after_minutes": self.cfg.get("idle_after_minutes"),
                "idle_after_min_minutes": self.cfg.get("idle_after_min_minutes"),
                "idle_after_max_minutes": self.cfg.get("idle_after_max_minutes"),
                "next_idle_at": next_idle_str,
                "daily": self.cfg.get("daily_prompts"),
                "quiet_hours": self.cfg.get("quiet_hours"),
                "history_depth": self.cfg.get("history_depth"),
                "subscribed": bool(profile and profile.subscribed),
                "user_idle_after_minutes": profile.idle_after_minutes if profile else None,
                "user_daily_reminders_enabled": profile.daily_reminders_enabled if profile else True,
                "user_daily_reminder_count": profile.daily_reminder_count if profile else 3,
            }
            yield reply("当前配置/状态：\n" + json.dumps(info, ensure_ascii=False, indent=2))
            return

        m = re.search(r"set\s+after\s+(\d+)", lower)
        if m:
            self.cfg["after_last_msg_minutes"] = int(m.group(1))
            self.cfg.save_config()
            yield reply(f"⏱️ 已设置 last_msg 后触发：{m.group(1)} 分钟")
            return

        m = re.search(r"set\s+daily([1-3])\s+(\d{1,2}:\d{2})", lower)
        if m:
            n = m.group(1)
            t = m.group(2)
            d = self.cfg.get("daily_prompts") or {}
            d[f"time{n}"] = t
            self.cfg["daily_prompts"] = d
            self.cfg.save_config()
            yield reply(f"🗓️ 已设置 daily{n}：{t}")
            return

        m = re.search(r"set\s+quiet\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", lower)
        if m:
            self.cfg["quiet_hours"] = f"{m.group(1)}-{m.group(2)}"
            self.cfg.save_config()
            yield reply(f"🔕 已设置免打扰：{self.cfg['quiet_hours']}")
            return

        mp = re.search(r"set\s+history\s+(\d+)", lower)
        if mp:
            self.cfg["history_depth"] = int(mp.group(1))
            self.cfg.save_config()
            yield reply(f"🧵 已设置历史条数：{mp.group(1)}")
            return

        # 移除了 prompt 管理命令，因为现在通过 WebUI 配置
        if " prompt " in lower:
            yield reply("📝 提示词管理功能已移至 WebUI 配置页面，请在那里设置“间隔触发”和“每日定时”的专属提示词。")
            return

        if " remind " in lower or lower.endswith(" remind"):
            parts = text.split()
            if len(parts) >= 3 and parts[1].lower() == "remind":
                sub = parts[2].lower()
                if sub == "list":
                    yield reply(self._remind_list_text(event.unified_msg_origin))
                    return
                if sub == "del" and len(parts) >= 4:
                    rid = parts[3].strip()
                    if rid in self._reminders and self._reminders[rid].umo == event.unified_msg_origin:
                        del self._reminders[rid]
                        self._save_reminders()
                        yield reply(f"🗑️ 已删除提醒 {rid}")
                    else:
                        yield reply("未找到该提醒 ID")
                    return
                if sub == "add":
                    txt = text.split("add", 1)[1].strip()
                    m1 = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$", txt)
                    m2 = re.match(r"^(\d{1,2}:\d{2})\s+(.+?)\s+daily$", txt, flags=re.I)
                    rid = f"R{int(datetime.now().timestamp())}"
                    if m1:
                        self._reminders[rid] = Reminder(
                            id=rid, umo=event.unified_msg_origin, content=m1.group(2).strip(),
                            at=m1.group(1).strip(), created_at=datetime.now().timestamp()
                        )
                        self._save_reminders()
                        yield reply(f"⏰ 已添加一次性提醒 {rid}")
                        return
                    elif m2:
                        hhmm = m2.group(1)
                        self._reminders[rid] = Reminder(
                            id=rid, umo=event.unified_msg_origin, content=m2.group(2).strip(),
                            at=f"{hhmm}|daily", created_at=datetime.now().timestamp()
                        )
                        self._save_reminders()
                        yield reply(f"⏰ 已添加每日提醒 {rid}")
                        return
            yield reply("用法：/conversa remind add <YYYY-MM-DD HH:MM> <内容>  或  /conversa remind add <HH:MM> <内容> daily")
            return

        yield reply(self._help_text())

    def _help_text(self) -> str:
        """返回插件的帮助文本，展示所有可用命令"""
        return (
            "Conversa 帮助 (指令: /conversa 或 /cvs)：\n"
            "/conversa on|off - 启用/停用插件\n"
            "/conversa watch - 订阅当前会话\n"
            "/conversa unwatch - 退订当前会话\n"
            "/conversa show - 显示当前配置\n"
            "/conversa debug - 显示调试信息\n"
            "/conversa set after <分钟> - 设置间隔触发\n"
            "/conversa set daily[1-3] <HH:MM> - 设置三个每日定时触发时间\n"
            "/conversa set quiet <HH:MM-HH:MM> - 设置免打扰\n"
            "/conversa set history <N> - 设置历史条数\n"
            "（提示词管理已移至WebUI）\n"
            "/conversa remind add/list/del - 管理提醒\n"
        )

    def _remind_list_text(self, umo: str) -> str:
        """生成指定用户的提醒列表文本"""
        arr = [r for r in self._reminders.values() if r.umo == umo]
        if not arr:
            return "暂无提醒"
        arr.sort(key=lambda x: x.created_at)
        return "提醒列表：\n" + "\n".join(f"{r.id} | {r.at} | {r.content}" for r in arr)

    # 上下文获取方法
    async def _safe_get_full_contexts(self, umo: str, conversation=None) -> List[Dict]:
        """
        安全获取完整上下文，使用多重降级策略
        
        参数:
            umo: 统一消息来源
            conversation: 已获取的对话对象（可选）
        """
        contexts = []
        
        # 策略1：从传入的 conversation 对象获取
        if conversation:
            try:
                # 1.1 尝试从 messages 属性获取
                if hasattr(conversation, "messages") and conversation.messages:
                    contexts = self._normalize_messages(conversation.messages)
                    if contexts:
                        logger.debug(f"[Conversa] 从conversation.messages获取{len(contexts)}条历史")
                        return contexts
                
                # 1.2 尝试调用 get_messages() 方法
                if hasattr(conversation, "get_messages"):
                    try:
                        messages = await conversation.get_messages()
                        if messages:
                            contexts = self._normalize_messages(messages)
                            if contexts:
                                logger.debug(f"[Conversa] 从conversation.get_messages()获取{len(contexts)}条历史")
                                return contexts
                    except Exception:
                        pass
                
                # 1.3 尝试从 history 属性解析JSON
                if hasattr(conversation, 'history') and conversation.history:
                    if isinstance(conversation.history, str):
                        try:
                            history = json.loads(conversation.history)
                            if history:
                                contexts = self._normalize_messages(history)
                                if contexts:
                                    logger.debug(f"[Conversa] 从conversation.history(JSON)获取{len(contexts)}条历史")
                                    return contexts
                        except json.JSONDecodeError:
                            pass
                    elif isinstance(conversation.history, list):
                        contexts = self._normalize_messages(conversation.history)
                        if contexts:
                            logger.debug(f"[Conversa] 从conversation.history(list)获取{len(contexts)}条历史")
                            return contexts
            except Exception as e:
                logger.warning(f"[Conversa] 从传入的conversation获取失败: {e}")
        
        # 策略2：通过 conversation_manager 重新获取最新对话
        try:
            if hasattr(self.context, "conversation_manager"):
                conv_mgr = self.context.conversation_manager
                conversation_id = await conv_mgr.get_curr_conversation_id(umo)
                if conversation_id:
                    # 2.2 根据ID获取完整的对话对象
                    conversation = await conv_mgr.get_conversation(umo, conversation_id)
                    if conversation:
                        # 尝试 messages 属性
                        if hasattr(conversation, "messages") and conversation.messages:
                            contexts = self._normalize_messages(conversation.messages)
                            if contexts:
                                logger.debug(f"[Conversa] 从conversation_manager.messages获取{len(contexts)}条历史")
                                return contexts
                        
                        # 尝试 history 属性
                        if hasattr(conversation, 'history') and conversation.history:
                            if isinstance(conversation.history, str):
                                try:
                                    history = json.loads(conversation.history)
                                    if history:
                                        contexts = self._normalize_messages(history)
                                        if contexts:
                                            logger.debug(f"[Conversa] 从conversation_manager.history获取{len(contexts)}条历史")
                                            return contexts
                                except json.JSONDecodeError:
                                    pass
                            elif isinstance(conversation.history, list):
                                contexts = self._normalize_messages(conversation.history)
                                if contexts:
                                    logger.debug(f"[Conversa] 从conversation_manager.history(list)获取{len(contexts)}条历史")
                                    return contexts
        except Exception as e:
            logger.warning(f"[Conversa] 从conversation_manager获取历史失败: {e}")
        
        # 策略3：使用插件的轻量历史缓存（最后的降级方案）
        try:
            profile = self._user_profiles.get(umo)
            context_cache = self._context_caches.get(umo)
            if profile and profile.subscribed and context_cache:
                contexts = list(context_cache)
                logger.debug(f"[Conversa] 使用插件上下文缓存，共{len(contexts)}条")
                return contexts
        except Exception as e:
            logger.warning(f"[Conversa] 从插件上下文缓存获取失败: {e}")
        
        logger.warning(f"[Conversa] ⚠️ 无法获取 {umo} 的对话历史，将使用空上下文")
        return contexts

    def _normalize_messages(self, msgs) -> List[Dict]:
        """
        标准化消息格式，兼容多种形态
        """
        if not msgs:
            return []
        
        # 如果是字典且包含 messages 键
        if isinstance(msgs, dict) and "messages" in msgs:
            msgs = msgs["messages"]
        
        normalized = []
        for m in msgs:
            if isinstance(m, dict):
                role = m.get("role") or m.get("speaker") or m.get("from")
                content = m.get("content") or m.get("text") or ""
                if role in ("user", "assistant", "system") and isinstance(content, str) and content:
                    normalized.append({"role": role, "content": content})
        
        return normalized

    # 调度器模块
    async def _scheduler_loop(self):
        """
        后台调度循环任务，每30秒检查一次是否需要触发主动回复
        
        这是插件的核心后台任务，在插件初始化时通过 asyncio.create_task() 启动。
        会持续运行直到插件被卸载或停用。
        
        每次循环会调用 _tick() 方法来检查：
        - 是否有会话达到间隔触发条件
        - 是否有会话需要每日定时回复
        - 是否有提醒需要触发
        """
        try:
            while True:
                await asyncio.sleep(30)
                await self._tick()
        except asyncio.CancelledError:
            logger.info("[Conversa] scheduler stopped.")
        except Exception as e:
            logger.error(f"[Conversa] scheduler error: {e}")

    async def _tick(self):
        """
        单次调度检查（每30秒执行一次）
        
        检查逻辑：
        1. 如果插件被停用，直接返回
        2. 遍历所有已订阅的会话，检查是否需要主动回复：
           a. 间隔触发：距离最后一条消息超过设定分钟数
           b. 每日定时1/2：到达设定的时间点（如每天早上9点）
        3. 检查是否在免打扰时间段内，如果是则跳过
        4. 检查是否需要自动退订（用户连续多天未回复）
        5. 检查并触发提醒事项
        6. 保存状态到磁盘
        
        注意：每个触发条件都会记录一个唯一的 tag，防止同一时刻重复触发
        """
        if not self.cfg.get("enable", True):
            logger.debug("[Conversa] Tick: 插件被停用，跳过")
            return
        
        logger.debug("[Conversa] Tick: 开始检查...")

        tz = self.cfg.get("timezone") or None
        now = _now_tz(tz)
        quiet = self.cfg.get("quiet_hours", "") or ""
        hist_n = int(self.cfg.get("history_depth") or 8)

        daily = self.cfg.get("daily_prompts") or {}
        t1 = _parse_hhmm(str(daily.get("time1", "") or ""))
        t2 = _parse_hhmm(str(daily.get("time2", "") or ""))
        t3 = _parse_hhmm(str(daily.get("time3", "") or ""))

        # 确保时间点唯一，避免重复触发
        times = {t for t in (t1, t2, t3) if t}
        unique_times = sorted(list(times))
        t1, t2, t3 = (unique_times + [None, None, None])[:3]

        curr_min_tag_1 = f"daily1@{now.strftime('%Y-%m-%d')} {t1[0]:02d}:{t1[1]:02d}" if t1 else ""
        curr_min_tag_2 = f"daily2@{now.strftime('%Y-%m-%d')} {t2[0]:02d}:{t2[1]:02d}" if t2 else ""
        curr_min_tag_3 = f"daily3@{now.strftime('%Y-%m-%d')} {t3[0]:02d}:{t3[1]:02d}" if t3 else ""

        subscribed_count = sum(1 for profile in self._user_profiles.values() if profile.subscribed)
        logger.debug(f"[Conversa] Tick: 当前时间={now.strftime('%Y-%m-%d %H:%M')}, 订阅用户数={subscribed_count}, 免打扰={quiet}")

        for umo, profile in list(self._user_profiles.items()):
            if not profile.subscribed:
                continue
            
            if _in_quiet(now, quiet):
                logger.debug(f"[Conversa] Tick: {umo} 在免打扰时间，跳过")
                continue

            # 检查是否需要自动退订
            st = self._states.get(umo)  # 获取运行时状态用于检查
            if st and await self._should_auto_unsubscribe(umo, profile, st, now):
                logger.debug(f"[Conversa] Tick: {umo} 被自动退订")
                continue
            
            logger.debug(f"[Conversa] Tick: 检查 {umo}, last_ts={st.last_ts}, last_fired_tag={st.last_fired_tag}")

            # 延时问候（基于 next_idle_ts 和用户个性化设置）
            if bool(self.cfg.get("enable_idle_greetings", True)):
                st = self._states.get(umo)  # 获取运行时状态
                if st and st.next_idle_ts and now.timestamp() >= st.next_idle_ts:
                    tag = f"idle@{now.strftime('%Y-%m-%d %H:%M')}"
                    if st.last_fired_tag != tag:
                        idle_prompts = self.cfg.get("idle_prompt_templates") or []
                        if idle_prompts:
                            prompt_template = random.choice(idle_prompts)
                            logger.info(f"[Conversa] Tick: 触发延时问候 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = tag
                                # 触发后清零 next_idle_ts，等待用户下次消息重置
                                st.next_idle_ts = 0.0
                            else:
                                st.consecutive_no_reply_count += 1
                    else:
                        logger.debug(f"[Conversa] Tick: {umo} 已触发过 {tag}")

            # 每日定时1
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                st = self._states.get(umo)  # 获取运行时状态
                if st and t1 and now.hour == t1[0] and now.minute == t1[1]:
                    if st.last_fired_tag != curr_min_tag_1:
                        prompt_template = daily.get("prompt1")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时1回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_1
                            else:
                                st.consecutive_no_reply_count += 1
                    else:
                        logger.debug(f"[Conversa] Tick: {umo} 已触发过 {curr_min_tag_1}")
                        
            # 每日定时2
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                st = self._states.get(umo)  # 获取运行时状态
                if st and t2 and now.hour == t2[0] and now.minute == t2[1]:
                    if st.last_fired_tag != curr_min_tag_2:
                        prompt_template = daily.get("prompt2")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时2回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_2
                            else:
                                st.consecutive_no_reply_count += 1
                    else:
                        logger.debug(f"[Conversa] Tick: {umo} 已触发过 {curr_min_tag_2}")

            # 每日定时3
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                st = self._states.get(umo)  # 获取运行时状态
                if st and t3 and now.hour == t3[0] and now.minute == t3[1]:
                    if st.last_fired_tag != curr_min_tag_3:
                        prompt_template = daily.get("prompt3")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时3回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_3
                            else:
                                st.consecutive_no_reply_count += 1
                    else:
                        logger.debug(f"[Conversa] Tick: {umo} 已触发过 {curr_min_tag_3}")

        await self._check_reminders(now, tz)
        self._save_session_states()

    async def _should_auto_unsubscribe(self, umo: str, profile: UserProfile, st: SessionState, now: datetime) -> bool:
        """
        检查是否需要自动退订（根据用户无回复天数）

        参数：
            umo: 统一消息来源（用户标识）
            profile: 该用户的订阅信息
            st: 该用户的会话状态
            now: 当前时间

        返回：
            True: 已自动退订该用户
            False: 不需要退订

        逻辑：
        - 如果配置了 max_no_reply_days > 0
        - 且用户最后回复时间距今超过设定天数
        - 则自动将该用户的 subscribed 状态设为 False
        - 这样可以避免长期无人回复的会话持续消耗 LLM 额度
        """
        max_days = int(self.cfg.get("max_no_reply_days") or 0)
        if max_days <= 0:
            return False

        if st.last_user_reply_ts > 0:
            last_reply = datetime.fromtimestamp(st.last_user_reply_ts, tz=now.tzinfo)
            days_since_reply = (now - last_reply).days

            if days_since_reply >= max_days:
                profile.subscribed = False
                logger.info(f"[Conversa] 自动退订 {umo}：用户{days_since_reply}天未回复")
                self._save_user_profiles()
                return True

        return False


    async def _proactive_reminder_reply(self, umo: str, reminder_content: str) -> bool:
        """
        执行由 AI 生成的主动提醒回复
        
        参数:
            umo: 统一消息来源（会话标识）
            reminder_content: 提醒的核心内容
            
        返回:
            True: 成功发送提醒
            False: 发送失败或回复为空
        """
        try:
            hist_n = int(self.cfg.get("history_depth") or 8)
            
            # 1. 获取 Provider 和 Conversation (与 _proactive_reply 逻辑类似)
            fixed_provider = (self.cfg.get("_special") or {}).get("provider") or ""
            provider = self.context.get_provider_by_id(fixed_provider) if fixed_provider else self.context.get_using_provider(umo=umo)
            if not provider:
                logger.warning(f"[Conversa] reminder provider missing for {umo}")
                return False

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, curr_cid)

            # 2. 获取 System Prompt (复用 _proactive_reply 中的逻辑)
            # (为简化，这里直接调用 _proactive_reply 的部分逻辑，未来可重构为公共函数)
            system_prompt = await self._get_system_prompt(umo, conversation)

            # 3. 获取上下文
            contexts = await self._safe_get_full_contexts(umo, conversation)
            if contexts and hist_n > 0:
                contexts = contexts[-hist_n:]

            # 4. 构造提醒专用的 Prompt
            prompt_template = self.cfg.get("reminder_prompt_template") or "用户提醒：{reminder_content}"
            prompt = prompt_template.format(reminder_content=reminder_content)

            logger.info(f"[Conversa] 触发 AI 提醒 for {umo}: {reminder_content}")

            # 5. 调用 LLM
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""

            if not text.strip():
                return False

            # 6. 发送消息
            await self._send_text(umo, f"⏰ {text}") # 在AI提醒前加上图标
            logger.info(f"[Conversa] 已发送 AI 提醒给 {umo}: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"[Conversa] proactive reminder error({umo}): {e}")
            return False

    async def _get_system_prompt(self, umo: str, conversation) -> str:
        """
        一个辅助函数，用于从 _proactive_reply 中提取获取 system_prompt 的逻辑。
        这样可以被 _proactive_reminder_reply 复用。
        """
        system_prompt = ""
        persona_obj = None
        
        # 优先使用配置文件中的自定义人格
        if (self.cfg.get("persona_override") or "").strip():
            system_prompt = self.cfg.get("persona_override")
            logger.debug(f"[Conversa] 使用配置文件中的自定义人格")
        else:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr:
                fixed_persona = (self.cfg.get("_special") or {}).get("persona") or ""
                persona_id = fixed_persona or (getattr(conversation, "persona_id", "") or "")
                
                if persona_id:
                    try:
                        if asyncio.iscoroutinefunction(persona_mgr.get_persona):
                            persona_obj = await persona_mgr.get_persona(persona_id)
                        else:
                            persona_obj = persona_mgr.get_persona(persona_id)
                    except Exception as e:
                        logger.warning(f"[Conversa] 获取指定人格 {persona_id} 失败: {e}")

                if not persona_obj and conversation:
                    persona_obj = getattr(conversation, "persona", None)

                if not persona_obj:
                    for getter_name in ("get_default_persona_v3", "get_default_persona", "get_default"):
                        getter = getattr(persona_mgr, getter_name, None)
                        if callable(getter):
                            try:
                                if asyncio.iscoroutinefunction(getter):
                                    persona_obj = await getter(umo)
                                else:
                                    persona_obj = getter(umo)
                            except TypeError:
                                if asyncio.iscoroutinefunction(getter):
                                    persona_obj = await getter()
                                else:
                                    persona_obj = getter()
                            if persona_obj:
                                break
            
            if persona_obj:
                for attr in ("system_prompt", "prompt", "content", "text"):
                    if hasattr(persona_obj, attr):
                        prompt_value = getattr(persona_obj, attr, None)
                        if isinstance(prompt_value, str) and prompt_value.strip():
                            system_prompt = prompt_value.strip()
                            break
                    if isinstance(persona_obj, dict) and attr in persona_obj:
                        prompt_value = persona_obj[attr]
                        if isinstance(prompt_value, str) and prompt_value.strip():
                            system_prompt = prompt_value.strip()
                            break
            
            if not system_prompt and conversation:
                for attr in ("system_prompt", "prompt"):
                    if hasattr(conversation, attr):
                        prompt_value = getattr(conversation, attr, None)
                        if isinstance(prompt_value, str) and prompt_value.strip():
                            system_prompt = prompt_value.strip()
                            break
                            
        return system_prompt or ""

    async def _check_reminders(self, now: datetime, tz: Optional[str]):
        """
        检查并触发到期的提醒事项
        
        支持两种提醒类型：
        1. 一次性提醒：格式 "YYYY-MM-DD HH:MM"，触发后自动删除
        2. 每日提醒：格式 "HH:MM|daily"，每天相同时间触发，不删除
        """
        fired_ids = []
        for rid, r in list(self._reminders.items()): # 使用 list 副本以安全地在循环中删除
            try:
                if "|daily" in r.at:
                    hhmm = r.at.split("|", 1)[0]
                    t = _parse_hhmm(hhmm)
                    if not t: 
                        continue
                    if now.hour == t[0] and now.minute == t[1]:
                        # 调用 AI 提醒
                        await self._proactive_reminder_reply(r.umo, r.content)
                else:
                    dt = datetime.strptime(r.at, "%Y-%m-%d %H:%M")
                    if now.strftime("%Y-%m-%d %H:%M") == dt.strftime("%Y-%m-%d %H:%M"):
                        # 调用 AI 提醒
                        await self._proactive_reminder_reply(r.umo, r.content)
                        fired_ids.append(rid)
            except Exception as e:
                logger.error(f"[Conversa] 检查提醒 {r.id} 时出错: {e}")
                continue
        
        for rid in fired_ids:
            self._reminders.pop(rid, None)
        if fired_ids:
            self._save_reminders()

    # 主动回复
    async def _proactive_reply(self, umo: str, hist_n: int, tz: Optional[str], prompt_template: str) -> bool:
        """
        执行主动回复的核心方法
        
        参数：
            umo: 统一消息来源（会话标识）
            hist_n: 需要获取的历史消息条数
            tz: 时区名称（用于时间格式化）
            prompt_template: 用于格式化提示词的模板字符串
            
        返回：
            True: 成功发送回复
            False: 发送失败或回复内容为空
            
        完整流程：
        1. 获取 LLM Provider（支持固定provider配置）
        2. 获取当前对话对象（通过 conversation_manager）
        3. 获取人格/系统提示词（多策略降级）：
           - 优先：配置中的 persona_override
           - 其次：指定的 persona_id
           - 降级：conversation.persona
           - 兜底：默认人格（get_default_persona_v3等）
        4. 获取完整上下文历史（调用 _safe_get_full_contexts，多策略降级）
        5. 使用传入的、特定场景的提示词模板
        6. 调用 LLM 的 text_chat 接口（注意参数名是 contexts 复数！）
        7. 如果配置了 append_time_field，在回复前添加时间戳
        8. 发送消息并更新会话状态
        
        重要修复点：
        - persona 获取必须使用 await（如果是异步方法）
        - LLM 调用参数名必须是 contexts（复数），不是 context（单数）
        - 上下文获取要有多层降级策略，确保健壮性
        """
        try:
            fixed_provider = (self.cfg.get("_special") or {}).get("provider") or ""
            provider = None
            if fixed_provider:
                provider = self.context.get_provider_by_id(fixed_provider)
            if not provider:
                provider = self.context.get_using_provider(umo=umo)
            if not provider:
                logger.warning(f"[Conversa] provider missing for {umo}")
                return False

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, curr_cid)

            # 获取 system_prompt（已重构为公共函数）
            system_prompt = await self._get_system_prompt(umo, conversation)
            
            if not system_prompt:
                logger.warning(f"[Conversa] 未能获取任何 system_prompt，将使用空值")

            # 获取完整上下文
            contexts: List[Dict] = []
            try:
                contexts = await self._safe_get_full_contexts(umo, conversation)
                
                # 限制历史条数
                if contexts and hist_n > 0:
                    contexts = contexts[-hist_n:]
                
                logger.info(f"[Conversa] 为 {umo} 获取到 {len(contexts)} 条上下文")
            except Exception as e:
                logger.error(f"[Conversa] 获取上下文时出错: {e}")
                contexts = []

            # 使用传入的、特定场景的提示词模板
            if prompt_template:
                last_user = ""
                last_ai = ""
                for m in reversed(contexts):
                    if not last_user and m.get("role") == "user":
                        last_user = m.get("content", "")
                    if not last_ai and m.get("role") == "assistant":
                        last_ai = m.get("content", "")
                    if last_user and last_ai:
                        break
                prompt = prompt_template.format(now=_fmt_now(self.cfg.get("time_format") or "%Y-%m-%d %H:%M", tz), last_user=last_user, last_ai=last_ai, umo=umo)
            else:
                # 降级为默认提示词
                prompt = "请自然地延续对话，与用户继续交流。"

            if self.cfg.get("debug_mode", False):
                logger.info(f"[Conversa] ========== 调试模式开始 ==========")
                logger.info(f"[Conversa] 用户: {umo}")
                logger.info(f"[Conversa] 系统提示词长度: {len(system_prompt) if system_prompt else 0} 字符")
                if system_prompt:
                    logger.info(f"[Conversa] 系统提示词前100字符: {system_prompt[:100]}...")
                else:
                    logger.warning(f"[Conversa] ⚠️ 警告：system_prompt 为空！")
                logger.info(f"[Conversa] 用户提示词: {prompt}")
                logger.info(f"[Conversa] 上下文历史共 {len(contexts)} 条:")
                if contexts:
                    for i, ctx in enumerate(contexts):
                        role = ctx.get("role", "unknown")
                        content = ctx.get("content", "")
                        logger.info(f"[Conversa]   [{i+1}] {role}: {content[:100]}{'...' if len(content) > 100 else ''}")
                else:
                    logger.warning(f"[Conversa] ⚠️ 警告：上下文为空！这会导致AI无法记住之前的对话")
                logger.info(f"[Conversa] ========== 调试模式结束 ==========")

            # 调用 LLM（注意：参数名是 contexts 复数！！！）
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,  # ← 修复：使用 contexts（复数）。
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""

            if not text.strip():
                return False

            if bool(self.cfg.get("append_time_field")):
                text = f"[{_fmt_now(self.cfg.get('time_format') or '%Y-%m-%d %H:%M', tz)}] " + text

            await self._send_text(umo, text)
            logger.info(f"[Conversa] 已发送主动回复给 {umo}: {text[:50]}...")

            # 更新最后时间戳为AI发送消息的时间，并把AI回复写入上下文缓存（仅订阅用户）
            now_ts = _now_tz(tz).timestamp()
            st = self._states.get(umo)
            profile = self._user_profiles.get(umo)
            context_cache = self._context_caches.get(umo)
            if st and profile and profile.subscribed:
                st.last_ts = now_ts
                try:
                    context_cache.append({"role": "assistant", "content": text})
                except Exception:
                    pass
                self._save_session_states()
                self._save_context_caches()
            
            return True
        except Exception as e:
            logger.error(f"[Conversa] proactive error({umo}): {e}")
            return False

    # 消息发送
    async def _send_text(self, umo: str, text: str):
        """
        发送纯文本消息到指定会话，并记录到插件的历史缓存
        
        参数：
            umo: 统一消息来源（会话标识）
            text: 要发送的文本内容
            
        功能：
        1. 构造消息链（MessageChain）
        2. 通过 context.send_message 发送消息
        3. 将消息记录到插件的轻量历史缓存（作为 assistant 角色）
        
        注意：
        - 这里记录的历史仅供降级使用（当conversation_manager无法获取历史时）
        - 历史缓存使用 deque(maxlen=32)，会自动丢弃最旧的消息
        """
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[Conversa] send_message error({umo}): {e}")

    async def terminate(self):
        """
        插件卸载/停用时的清理方法
        
        功能：
        1. 停止后台调度循环任务（_scheduler_loop）
        2. 根据插件是卸载还是停用，执行不同的清理策略：
           
           卸载（检测到插件文件不存在）：
           - 清除所有用户配置（重置为默认值）
           - 删除所有数据文件（state.json, reminders.json）
           - 删除数据目录（如果为空）
           
           停用（插件文件仍存在）：
           - 仅保存当前状态到磁盘
           - 保留所有配置和数据
        
        注意：
        - 这个方法在 AstrBot 卸载/停用插件时自动调用
        - 卸载检测可能不可靠（文件可能还在磁盘上），建议在WebUI提供明确的清理选项
        """
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except Exception:
                pass
        
        # 检查插件是否被卸载（通过检查插件主文件是否存在）
        plugin_main_file = os.path.abspath(__file__)
        is_uninstall = not os.path.exists(plugin_main_file)
        
        if is_uninstall:
            # 插件被卸载 - 清除所有数据
            logger.info("[Conversa] 检测到插件卸载，开始清理数据...")
            
            # 清除用户配置
            try:
                # 重置所有配置项为默认值
                self.cfg["enable"] = True
                self.cfg["custom_prompts"] = []
                self.cfg["max_no_reply_days"] = 0
                self.cfg["persona_override"] = ""
                self.cfg["quiet_hours"] = ""
                self.cfg["timezone"] = ""
                self.cfg["time_format"] = "%Y-%m-%d %H:%M"
                self.cfg["history_depth"] = 8
                self.cfg["after_last_msg_minutes"] = 0
                self.cfg["append_time_field"] = False
                self.cfg["daily"] = {}
                self.cfg["daily_prompts"] = {} # 新增：清理每日定时提示词
                self.cfg["idle_prompt_templates"] = [] # 新增：清理空闲触发提示词
                self.cfg["subscribe_mode"] = "manual"
                self.cfg["debug_mode"] = False
                self.cfg["_special"] = {}
                # 保存配置以确保清除生效
                self.cfg.save_config()
                logger.info("[Conversa] 已清除用户配置")
            except Exception as e:
                logger.error(f"[Conversa] 清除用户配置时出错: {e}")
            
            # 清理数据文件
            try:
                if os.path.exists(self._state_path):
                    os.remove(self._state_path)
                    logger.info(f"[Conversa] 已删除状态文件: {self._state_path}")
                if os.path.exists(self._user_profiles_path):
                    os.remove(self._user_profiles_path)
                    logger.info(f"[Conversa] 已删除用户配置文件: {self._user_profiles_path}")
                if os.path.exists(self._context_cache_path):
                    os.remove(self._context_cache_path)
                    logger.info(f"[Conversa] 已删除上下文缓存文件: {self._context_cache_path}")
                if os.path.exists(self._remind_path):
                    os.remove(self._remind_path)
                    logger.info(f"[Conversa] 已删除提醒文件: {self._remind_path}")
                if os.path.exists(self._session_states_path):
                    os.remove(self._session_states_path)
                    logger.info(f"[Conversa] 已删除会话状态文件: {self._session_states_path}")

                # 如果数据目录为空，删除整个目录
                if os.path.exists(self._data_dir) and not os.listdir(self._data_dir):
                    os.rmdir(self._data_dir)
                    logger.info(f"[Conversa] 已删除数据目录: {self._data_dir}")
            except Exception as e:
                logger.error(f"[Conversa] 清理数据文件时出错: {e}")
        else:
            # 插件被停用 - 只保存状态，不清理数据
            logger.info("[Conversa] 检测到插件停用，保存状态...")
            try:
                self._save_session_states()
                self._save_user_profiles()
                self._save_context_caches()
                self._save_reminders()
                logger.info("[Conversa] 状态已保存")
            except Exception as e:
                logger.error(f"[Conversa] 保存状态时出错: {e}")
        
        logger.info("[Conversa] terminated.")

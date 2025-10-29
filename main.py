"""
Conversa - AI 定时主动续聊插件
支持人格与上下文记忆、定时问候、延时提醒、用户提醒等功能
"""
from __future__ import annotations

# === 标准库导入 ===
import asyncio
import json
import os
import random
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Deque, Tuple

# === AstrBot API 导入 ===
import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

# 工具函数
def _ensure_dir(p: str) -> str:
    """确保目录存在，不存在则创建"""
    os.makedirs(p, exist_ok=True)
    return p


def _now_tz(tz_name: str | None) -> datetime:
    """获取指定时区的当前时间，失败则返回本地时间"""
    try:
        if tz_name:
            import zoneinfo
            return datetime.now(zoneinfo.ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.now()


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    """解析 HH:MM 格式时间字符串，返回 (小时, 分钟) 或 None"""
    if not s:
        return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _in_quiet(now: datetime, quiet: str) -> bool:
    """检查当前时间是否在免打扰时间段内（支持跨天）"""
    if not quiet or "-" not in quiet:
        return False
    a, b = quiet.split("-", 1)
    p1 = _parse_hhmm(a)
    p2 = _parse_hhmm(b)
    if not p1 or not p2:
        return False
    t1 = time(p1[0], p1[1])
    t2 = time(p2[0], p2[1])
    nt = now.time()
    if t1 <= t2:
        return t1 <= nt <= t2
    else:
        return nt >= t1 or nt <= t2


def _fmt_now(fmt: str, tz: str | None) -> str:
    """格式化当前时间为指定格式"""
    return _now_tz(tz).strftime(fmt)

# 数据类定义
@dataclass
class UserProfile:
    """用户订阅信息和个性化设置"""
    subscribed: bool = False
    idle_after_minutes: int | None = None  # None 表示使用全局默认
    daily_reminders_enabled: bool = True
    daily_reminder_count: int = 3

    def to_dict(self):
        return {
            "subscribed": self.subscribed,
            "idle_after_minutes": self.idle_after_minutes,
            "daily_reminders_enabled": self.daily_reminders_enabled,
            "daily_reminder_count": self.daily_reminder_count
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            subscribed=data.get("subscribed", False),
            idle_after_minutes=data.get("idle_after_minutes"),
            daily_reminders_enabled=data.get("daily_reminders_enabled", True),
            daily_reminder_count=data.get("daily_reminder_count", 3)
        )

@dataclass
class SessionState:
    """运行时会话状态（内存中维护）"""
    last_ts: float = 0.0
    last_fired_tag: str = ""
    last_user_reply_ts: float = 0.0
    consecutive_no_reply_count: int = 0
    next_idle_ts: float = 0.0

    def to_dict(self):
        return {
            "last_ts": self.last_ts,
            "last_fired_tag": self.last_fired_tag,
            "last_user_reply_ts": self.last_user_reply_ts,
            "consecutive_no_reply_count": self.consecutive_no_reply_count,
            "next_idle_ts": self.next_idle_ts
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            last_ts=data.get("last_ts", 0.0),
            last_fired_tag=data.get("last_fired_tag", ""),
            last_user_reply_ts=data.get("last_user_reply_ts", 0.0),
            consecutive_no_reply_count=data.get("consecutive_no_reply_count", 0),
            next_idle_ts=data.get("next_idle_ts", 0.0)
        )


@dataclass
class Reminder:
    """用户设置的提醒事项"""
    id: str
    umo: str
    content: str
    at: str  # "YYYY-MM-DD HH:MM" 或 "HH:MM|daily"
    created_at: float

    def to_dict(self):
        return {
            "id": self.id,
            "umo": self.umo,
            "content": self.content,
            "at": self.at,
            "created_at": self.created_at
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            id=data.get("id"),
            umo=data.get("umo"),
            content=data.get("content"),
            at=data.get("at"),
            created_at=data.get("created_at")
        )


# 主插件类

@register("Conversa", "柯尔", "AI 定时主动续聊 · 支持人格与上下文记忆", "1.0.0", 
          "https://github.com/Luna-channel/astrbot_plugin_Conversa")
class Conversa(Star):
    """
    Conversa 主动对话插件
    
    核心功能：
    1. 延时问候：在用户消息后一段时间主动发起对话
    2. 每日定时：在固定时间点主动问候（最多3个时段）
    3. 用户提醒：支持一次性和每日提醒，由AI生成自然语言提醒
    4. 订阅管理：支持手动/自动订阅模式，按用户粒度管理
    5. 上下文记忆：多级降级策略获取对话历史
    """

    # 初始化
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        
        # 运行时数据
        self._states: Dict[str, SessionState] = {}
        self._user_profiles: Dict[str, UserProfile] = {}
        self._context_caches: Dict[str, Deque[Dict]] = {}
        self._reminders: Dict[str, Reminder] = {}
        
        # 数据文件路径
        root = os.getcwd()
        self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_conversa"))
        self._user_data_path = os.path.join(self._data_dir, "user_data.json")
        self._session_data_path = os.path.join(self._data_dir, "session_data.json")
        
        # 配置辅助函数
        def _get_cfg(group_key: str, sub_key: str, default=None):
            """安全地从嵌套配置字典中获取值"""
            group = self.cfg.get(group_key) or {}
            return group.get(sub_key, default)
        
        self._get_cfg = _get_cfg
        
        # 加载数据
        self._load_user_data()
        self._load_session_data()
        self._sync_subscribed_users_from_config()
        
        # 启动后台调度器
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[Conversa] Scheduler started.")

    # 数据持久化
    def _load_user_data(self):
        """加载用户配置和提醒事项（从 user_data.json）"""
        if not os.path.exists(self._user_data_path):
            return
        try:
            with open(self._user_data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                profiles_data = data.get("profiles", {})
                for user_id, profile_dict in profiles_data.items():
                    self._user_profiles[user_id] = UserProfile.from_dict(profile_dict)
                logger.info(f"[Conversa] Loaded {len(self._user_profiles)} user profiles.")
                
                reminders_data = data.get("reminders", {})
                for reminder_id, reminder_dict in reminders_data.items():
                    self._reminders[reminder_id] = Reminder.from_dict(reminder_dict)
                logger.info(f"[Conversa] Loaded {len(self._reminders)} reminders.")
        
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[Conversa] Failed to load user data: {e}")
    
    def _save_user_data(self):
        """保存用户配置和提醒事项（到 user_data.json）"""
        try:
            profiles_dict = {uid: profile.to_dict() for uid, profile in self._user_profiles.items()}
            reminders_dict = {rid: reminder.to_dict() for rid, reminder in self._reminders.items()}
            data = {
                "profiles": profiles_dict,
                "reminders": reminders_dict
            }
            with open(self._user_data_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"[Conversa] Failed to save user data: {e}")
    
    def _load_session_data(self):
        """加载运行时状态和上下文缓存（从 session_data.json）"""
        if not os.path.exists(self._session_data_path):
            return
        try:
            with open(self._session_data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                states_data = data.get("states", {})
                for conv_id, state_dict in states_data.items():
                    self._states[conv_id] = SessionState.from_dict(state_dict)
                logger.info(f"[Conversa] Loaded {len(self._states)} session states.")
                
                caches_data = data.get("caches", {})
                max_len = self._get_cfg("basic_settings", "context_cache_max_len", 10)
                for conv_id, cache_list in caches_data.items():
                    self._context_caches[conv_id] = deque(cache_list, maxlen=max_len)
                logger.info(f"[Conversa] Loaded {len(self._context_caches)} context caches.")
        
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[Conversa] Failed to load session data: {e}")
    
    def _save_session_data(self):
        """保存运行时状态和上下文缓存（到 session_data.json）"""
        try:
            states_dict = {cid: state.to_dict() for cid, state in self._states.items()}
            caches_dict = {cid: list(cache) for cid, cache in self._context_caches.items()}
            data = {
                "states": states_dict,
                "caches": caches_dict
            }
            with open(self._session_data_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"[Conversa] Failed to save session data: {e}")
    
    def _sync_subscribed_users_from_config(self):
        """从配置文件同步订阅用户列表到内部状态"""
        try:
            config_subscribed_ids = self._get_cfg("basic_settings", "subscribed_users") or []
            if not isinstance(config_subscribed_ids, list):
                logger.warning(f"[Conversa] subscribed_users 配置格式错误，应为列表")
                return
            
            for user_id, profile in self._user_profiles.items():
                if user_id in config_subscribed_ids:
                    profile.subscribed = True
                    logger.debug(f"[Conversa] 从配置同步订阅状态: {user_id}")

            logger.info(f"[Conversa] 已从配置同步 {len(config_subscribed_ids)} 个订阅用户ID")
            
            subscribed_sessions = [user_id for user_id, profile in self._user_profiles.items() if profile.subscribed]
            logger.info(f"[Conversa] 当前已订阅的会话数: {len(subscribed_sessions)}")
            
        except Exception as e:
            logger.error(f"[Conversa] 同步订阅用户配置失败: {e}")

    def _sync_subscribed_users_to_config(self):
        """将插件内部订阅状态同步回配置文件"""
        subscribed_users = []
        for user_id, profile in self._user_profiles.items():
            if profile.subscribed:
                subscribed_users.append(user_id)
        
        basic_settings = self.cfg.get("basic_settings") or {}
        basic_settings["subscribed_users"] = subscribed_users
        self.cfg.set("basic_settings", basic_settings)
        logger.info("[Conversa] Subscribed users config updated.")
    
    def _save_user_profiles(self):
        """兼容旧API，实际调用整合后的保存函数"""
        self._save_user_data()
    
    def _save_context_caches(self):
        """兼容旧API，实际调用整合后的保存函数"""
        self._save_session_data()
    
    # 事件处理 
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        """
        监听所有消息事件
        
        功能：
        1. 更新会话的最后活跃时间戳
        2. 更新用户最后回复时间（用于自动退订检测）
        3. 重置连续无回复计数器
        4. 自动订阅模式下自动订阅新会话
        5. 记录用户消息到轻量历史缓存
        6. 计算下一次延时问候触发时间
        """
        umo = event.unified_msg_origin
        
        # 初始化数据结构
        if umo not in self._states:
            self._states[umo] = SessionState()
        if umo not in self._user_profiles:
            self._user_profiles[umo] = UserProfile()
        if umo not in self._context_caches:
            self._context_caches[umo] = deque(maxlen=32)

        st = self._states[umo]
        profile = self._user_profiles[umo]
        context_cache = self._context_caches[umo]

        # 更新时间戳
        now_ts = _now_tz(self._get_cfg("basic_settings", "timezone") or None).timestamp()
        st.last_ts = now_ts
        st.last_user_reply_ts = now_ts
        st.consecutive_no_reply_count = 0

        # 自动订阅模式
        if (self._get_cfg("basic_settings", "subscribe_mode") or "manual") == "auto":
            profile.subscribed = True

        # 记录上下文缓存（仅订阅用户）
        try:
            if profile.subscribed:
                role = "assistant" if event.is_self else "user"
                content = event.message_str or ""
                if content:
                    context_cache.append({"role": role, "content": content})
        except Exception:
            pass

        # 计算下一次延时问候触发时间
        try:
            if profile.subscribed and bool(self._get_cfg("idle_greetings", "enable_idle_greetings", True)):
                delay_m = profile.idle_after_minutes
                
                if delay_m is None:
                    base_delay_m = int(self._get_cfg("idle_greetings", "idle_after_minutes") or 45)
                    fluctuation_m = int(self._get_cfg("idle_greetings", "idle_random_fluctuation_minutes") or 15)
                    delay_m = base_delay_m + random.randint(-fluctuation_m, fluctuation_m)
                    delay_m = max(30, delay_m)
                
                st.next_idle_ts = now_ts + delay_m * 60
        except Exception as e:
            logger.warning(f"[Conversa] 计算 next_idle_ts 失败: {e}")

        # 保存状态
        self._save_session_data()
        self._save_user_data()

    @filter.command("conversa", aliases=["cvs"])
    async def _cmd_conversa(self, event: AstrMessageEvent):
        """
        Conversa 命令处理器
        
        支持的子命令：
        - help: 显示帮助信息
        - debug: 显示调试信息
        - on/off: 启用/停用插件
        - watch/unwatch: 订阅/退订当前会话
        - show: 显示当前配置和状态
        - set after <小时>: 设置专属延时问候时间
        - set daily[1-3] <HH:MM>: 设置每日定时回复时间
        - set quiet <HH:MM-HH:MM>: 设置免打扰时间段
        - set history <N>: 设置上下文历史条数
        - remind add/list/del: 管理提醒事项
        """
        text = (event.message_str or "").strip()
        lower = text.lower()

        def reply(msg: str):
            return event.plain_result(msg)

        # 帮助信息
        if "help" in lower or text.strip() in ["/conversa", "/cvs"]:
            yield reply(self._help_text())
            return

            # 调试信息
        if " debug" in lower:
            debug_info = [
                f"插件启用状态: {self.cfg.get('enable', True)}",
                f"订阅模式: {self._get_cfg('basic_settings', 'subscribe_mode', 'manual')}",
                f"当前用户: {event.unified_msg_origin}",
            ]
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            profile = self._user_profiles.get(umo)
            debug_info.append(f"用户订阅状态: {profile.subscribed if profile else False}")
            debug_info.append(f"延时基准: {self._get_cfg('idle_greetings', 'idle_after_minutes', 0)}分钟")
            debug_info.append(f"免打扰时间: {self._get_cfg('basic_settings', 'quiet_hours', '')}")
            debug_info.append(f"最大无回复天数: {self._get_cfg('basic_settings', 'max_no_reply_days', 0)}")
            yield reply("🔍 调试信息:\n" + "\n".join(debug_info))
            return

        # 启用/停用插件
        if " on" in lower:
            self.cfg["enable"] = True
            self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
            self.cfg["basic_settings"]["enable"] = True
            self.cfg.save_config()
            yield reply("✅ 已启用 Conversa")
            return
        
        if " off" in lower:
            self.cfg["enable"] = False
            self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
            self.cfg["basic_settings"]["enable"] = False
            self.cfg.save_config()
            yield reply("🛑 已停用 Conversa")
            return

        # 订阅/退订
        if " watch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = True
            logger.info(f"[Conversa] 用户执行 watch 命令: {umo}")
            self._save_user_data()
            yield reply("📌 已订阅当前会话")
            return

        if " unwatch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = False
            self._save_user_data()
            yield reply("📭 已退订当前会话")
            return

        # 显示配置
        if " show" in lower:
            umo = event.unified_msg_origin
            profile = self._user_profiles.get(umo)
            st = self._states.get(umo)
            
            tz = self._get_cfg("basic_settings", "timezone") or None
            next_idle_str = "未计划"
            if st and st.next_idle_ts and st.next_idle_ts > 0:
                try:
                    dt = datetime.fromtimestamp(st.next_idle_ts, tz=_now_tz(tz).tzinfo)
                    next_idle_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    next_idle_str = str(st.next_idle_ts)
            
            info = {
                "enable": self.cfg.get("enable"),
                "timezone": self._get_cfg("basic_settings", "timezone"),
                "enable_daily_greetings": self.cfg.get("enable_daily_greetings", True),
                "enable_idle_greetings": self._get_cfg("idle_greetings", "enable_idle_greetings", True),
                "idle_after_minutes": self._get_cfg("idle_greetings", "idle_after_minutes"),
                "idle_random_fluctuation_minutes": self._get_cfg("idle_greetings", "idle_random_fluctuation_minutes"),
                "next_idle_at": next_idle_str,
                "daily": self.cfg.get("daily_prompts"),
                "quiet_hours": self._get_cfg("basic_settings", "quiet_hours"),
                "history_depth": self._get_cfg("basic_settings", "history_depth"),
                "subscribed": bool(profile and profile.subscribed),
                "user_idle_after_minutes": profile.idle_after_minutes if profile else None,
                "user_daily_reminders_enabled": profile.daily_reminders_enabled if profile else True,
                "user_daily_reminder_count": profile.daily_reminder_count if profile else 3,
            }
            yield reply("当前配置/状态：\n" + json.dumps(info, ensure_ascii=False, indent=2))
            return

        # set after 命令
        m = re.search(r"set\s+after\s+(\d+)", text, re.I)
        if m:
            umo = event.unified_msg_origin
            profile = self._user_profiles.get(umo)
            if not profile:
                self._user_profiles[umo] = UserProfile()
                profile = self._user_profiles[umo]
            
            try:
                hours = int(m.group(1))
                if 2 <= hours <= 48:
                    profile.idle_after_minutes = hours * 60
                    self._save_user_data()
                    yield reply(f"⏱️ 已为您设置专属延时问候：{hours} 小时后触发（会叠加后台随机波动）")
                else:
                    yield reply("⏱️ 请输入2到48之间的小时数。")
            except ValueError:
                yield reply("⏱️ 请输入有效的小时数。")
            return

        # set daily 命令
        m = re.search(r"set\s+daily([1-3])\s+(\d{1,2}:\d{2})", lower)
        if m:
            n = int(m.group(1))
            t = m.group(2)
            
            slot_cfg = self.cfg.get("daily_prompts") or {}
            slot_cfg[f"slot{n}"] = slot_cfg.get(f"slot{n}", {})
            slot_cfg[f"slot{n}"]["time"] = t
            slot_cfg[f"slot{n}"]["enable"] = True
            self.cfg["daily_prompts"] = slot_cfg
            
            self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
            self.cfg["basic_settings"]["enable_daily_greetings"] = True
            self.cfg.save_config()
            yield reply(f"🗓️ 已设置 daily{n}：{t}")
            return

        # set quiet 命令
        m = re.search(r"set\s+quiet\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", lower)
        if m:
            settings = self.cfg.get("basic_settings") or {}
            settings["quiet_hours"] = f"{m.group(1)}-{m.group(2)}"
            self.cfg["basic_settings"] = settings
            self.cfg.save_config()
            yield reply(f"🔕 已设置免打扰：{settings['quiet_hours']}")
            return

        # set history 命令
        mp = re.search(r"set\s+history\s+(\d+)", lower)
        if mp:
            settings = self.cfg.get("basic_settings") or {}
            settings["history_depth"] = int(mp.group(1))
            self.cfg["basic_settings"] = settings
            self.cfg.save_config()
            yield reply(f"🧵 已设置历史条数：{mp.group(1)}")
            return

        # prompt 命令（已移至 WebUI）
        if " prompt " in lower:
            yield reply("📝 提示词管理功能已移至 WebUI 配置页面。")
            return

        # remind 命令
        if " remind " in lower or lower.endswith(" remind"):
            if not bool(self._get_cfg("reminders_settings", "enable_reminders", True)):
                yield reply("提醒功能已被管理员禁用。")
                return
            
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
                        self._save_user_data()
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
                            id=rid,
                            umo=event.unified_msg_origin,
                            content=m1.group(2).strip(),
                            at=m1.group(1).strip(),
                            created_at=datetime.now().timestamp()
                        )
                        self._save_user_data()
                        yield reply(f"⏰ 已添加一次性提醒 {rid}")
                        return
                    elif m2:
                        hhmm = m2.group(1)
                        self._reminders[rid] = Reminder(
                            id=rid,
                            umo=event.unified_msg_origin,
                            content=m2.group(2).strip(),
                            at=f"{hhmm}|daily",
                            created_at=datetime.now().timestamp()
                        )
                        self._save_user_data()
                        yield reply(f"⏰ 已添加每日提醒 {rid}")
                        return
            
            yield reply("用法：/conversa remind add <YYYY-MM-DD HH:MM> <内容>  或  /conversa remind add <HH:MM> <内容> daily")
            return

        # 默认显示帮助
        yield reply(self._help_text())

    def _help_text(self) -> str:
        """返回插件的帮助文本"""
        return (
            "Conversa 帮助 (指令: /conversa 或 /cvs)：\n"
            "/conversa on|off - 启用/停用插件\n"
            "/conversa watch - 订阅当前会话\n"
            "/conversa unwatch - 退订当前会话\n"
            "/conversa show - 显示当前配置\n"
            "/conversa debug - 显示调试信息\n"
            "/conversa set after <小时> - 设置专属延时问候(2-48小时)\n"
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

    # 调度器
    
    async def _scheduler_loop(self):
        """后台调度循环任务，每30秒检查一次是否需要触发主动回复"""
        try:
            while True:
                await asyncio.sleep(30)
                await self._tick()
        except asyncio.CancelledError:
            logger.info("[Conversa] Scheduler stopped.")
        except Exception as e:
            logger.error(f"[Conversa] Scheduler error: {e}")

    async def _tick(self):
        """
        单次调度检查（每30秒执行一次）
        
        检查逻辑：
        1. 如果插件被停用，直接返回
        2. 遍历所有已订阅的会话，检查是否需要主动回复
        3. 检查是否在免打扰时间段内
        4. 检查是否需要自动退订
        5. 检查并触发提醒事项
        """
        if not self.cfg.get("enable", True):
            logger.debug("[Conversa] Tick: 插件被停用，跳过")
            return
        
        logger.debug("[Conversa] Tick: 开始检查...")

        tz = self._get_cfg("basic_settings", "timezone") or None
        now = _now_tz(tz)
        quiet = self._get_cfg("basic_settings", "quiet_hours", "") or ""
        hist_n = int(self._get_cfg("basic_settings", "history_depth") or 8)
        reply_interval = int(self._get_cfg("basic_settings", "reply_interval_seconds") or 10)

        # 解析每日定时配置
        daily = self.cfg.get("daily_prompts") or {}
        t1 = _parse_hhmm(str(daily.get("time1", "") or "")) if bool(daily.get("daily1_enable", True)) else None
        t2 = _parse_hhmm(str(daily.get("time2", "") or "")) if bool(daily.get("daily2_enable", True)) else None
        t3 = _parse_hhmm(str(daily.get("time3", "") or "")) if bool(daily.get("daily3_enable", True)) else None
        
        times = {t for t in (t1, t2, t3) if t}
        unique_times = sorted(list(times))
        t1, t2, t3 = (unique_times + [None, None, None])[:3]

        curr_min_tag_1 = f"daily1@{now.strftime('%Y-%m-%d')} {t1[0]:02d}:{t1[1]:02d}" if t1 else ""
        curr_min_tag_2 = f"daily2@{now.strftime('%Y-%m-%d')} {t2[0]:02d}:{t2[1]:02d}" if t2 else ""
        curr_min_tag_3 = f"daily3@{now.strftime('%Y-%m-%d')} {t3[0]:02d}:{t3[1]:02d}" if t3 else ""

        subscribed_count = sum(1 for profile in self._user_profiles.values() if profile.subscribed)
        logger.debug(f"[Conversa] Tick: 当前时间={now.strftime('%Y-%m-%d %H:%M')}, 订阅用户数={subscribed_count}")

        # 遍历所有已订阅用户
        for umo, profile in list(self._user_profiles.items()):
            if not profile.subscribed:
                continue
            
            if _in_quiet(now, quiet):
                logger.debug(f"[Conversa] Tick: {umo} 在免打扰时间，跳过")
                continue

            st = self._states.get(umo)
            if st and await self._should_auto_unsubscribe(umo, profile, st, now):
                logger.debug(f"[Conversa] Tick: {umo} 被自动退订")
                continue
            
            logger.debug(f"[Conversa] Tick: 检查 {umo}, last_fired_tag={st.last_fired_tag if st else 'N/A'}")

            # 延时问候
            if bool(self._get_cfg("idle_greetings", "enable_idle_greetings", True)):
                if st and st.next_idle_ts and now.timestamp() >= st.next_idle_ts:
                    tag = f"idle@{now.strftime('%Y-%m-%d %H:%M')}"
                    if st.last_fired_tag != tag:
                        idle_prompts = self._get_cfg("idle_greetings", "idle_prompt_templates") or []
                        if idle_prompts:
                            prompt_template = random.choice(idle_prompts)
                            logger.info(f"[Conversa] Tick: 触发延时问候 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = tag
                                st.next_idle_ts = 0.0
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                            else:
                                st.consecutive_no_reply_count += 1

            # 每日定时1
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                if st and t1 and now.hour == t1[0] and now.minute == t1[1]:
                    if st.last_fired_tag != curr_min_tag_1:
                        prompt_template = daily.get("prompt1")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时1回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_1
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                            else:
                                st.consecutive_no_reply_count += 1
                        
            # 每日定时2
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                if st and t2 and now.hour == t2[0] and now.minute == t2[1]:
                    if st.last_fired_tag != curr_min_tag_2:
                        prompt_template = daily.get("prompt2")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时2回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_2
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                            else:
                                st.consecutive_no_reply_count += 1

            # 每日定时3
            if bool(self.cfg.get("enable_daily_greetings", True)) and profile.daily_reminders_enabled:
                if st and t3 and now.hour == t3[0] and now.minute == t3[1]:
                    if st.last_fired_tag != curr_min_tag_3:
                        prompt_template = daily.get("prompt3")
                        if prompt_template:
                            logger.info(f"[Conversa] Tick: 触发每日定时3回复 {umo}")
                            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                            if ok:
                                st.last_fired_tag = curr_min_tag_3
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                            else:
                                st.consecutive_no_reply_count += 1

        # 检查提醒
        await self._check_reminders(now, tz, reply_interval)
        self._save_session_data()

    async def _should_auto_unsubscribe(self, umo: str, profile: UserProfile, st: SessionState, now: datetime) -> bool:
        """检查是否需要自动退订（根据用户无回复天数）"""
        max_days = int(self._get_cfg("basic_settings", "max_no_reply_days") or 0)
        if max_days <= 0:
            return False

        if st.last_user_reply_ts > 0:
            last_reply = datetime.fromtimestamp(st.last_user_reply_ts, tz=now.tzinfo)
            days_since_reply = (now - last_reply).days

            if days_since_reply >= max_days:
                profile.subscribed = False
                logger.info(f"[Conversa] 自动退订 {umo}：用户{days_since_reply}天未回复")
                self._save_user_data()
                return True

        return False

    async def _check_reminders(self, now: datetime, tz: Optional[str], reply_interval: int):
        """检查并触发到期的提醒事项"""
        if not bool(self._get_cfg("reminders_settings", "enable_reminders", True)):
            return
        
        fired_ids = []
        for rid, r in list(self._reminders.items()):
            try:
                if "|daily" in r.at:
                    hhmm = r.at.split("|", 1)[0]
                    t = _parse_hhmm(hhmm)
                    if not t:
                        continue
                    if now.hour == t[0] and now.minute == t[1]:
                        ok = await self._proactive_reminder_reply(r.umo, r.content)
                        if ok and reply_interval > 0:
                            await asyncio.sleep(reply_interval)
                else:
                    dt = datetime.strptime(r.at, "%Y-%m-%d %H:%M")
                    if now.strftime("%Y-%m-%d %H:%M") == dt.strftime("%Y-%m-%d %H:%M"):
                        ok = await self._proactive_reminder_reply(r.umo, r.content)
                        if ok:
                            fired_ids.append(rid)
                            if reply_interval > 0:
                                await asyncio.sleep(reply_interval)
            except Exception as e:
                logger.error(f"[Conversa] 检查提醒 {r.id} 时出错: {e}")
                continue
        
        for rid in fired_ids:
            self._reminders.pop(rid, None)
        if fired_ids:
            self._save_user_data()
    
    # 主动回复
    
    async def _proactive_reply(self, umo: str, hist_n: int, tz: Optional[str], prompt_template: str) -> bool:
        """
        执行主动回复的核心方法
        
        完整流程：
        1. 获取 LLM Provider
        2. 获取当前对话对象
        3. 获取人格/系统提示词（多策略降级）
        4. 获取完整上下文历史
        5. 格式化提示词模板
        6. 调用 LLM
        7. 发送消息并更新状态
        """
        try:
            # 获取 Provider
            fixed_provider = (self.cfg.get("special") or {}).get("provider") or ""
            provider = None
            if fixed_provider:
                provider = self.context.get_provider_by_id(fixed_provider)
            if not provider:
                provider = self.context.get_using_provider(umo=umo)
            if not provider:
                logger.warning(f"[Conversa] provider missing for {umo}")
                return False
            
            # 获取 Conversation
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            
            # 获取 System Prompt
            system_prompt = await self._get_system_prompt(umo, conversation)
            if not system_prompt:
                logger.warning(f"[Conversa] 未能获取任何 system_prompt，将使用空值")
            
            # 获取上下文
            contexts: List[Dict] = []
            try:
                contexts = await self._safe_get_full_contexts(umo, conversation)
                if contexts and hist_n > 0:
                    contexts = contexts[-hist_n:]
                logger.info(f"[Conversa] 为 {umo} 获取到 {len(contexts)} 条上下文")
            except Exception as e:
                logger.error(f"[Conversa] 获取上下文时出错: {e}")
                contexts = []
            
            # 格式化提示词
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
                prompt = prompt_template.format(
                    now=_fmt_now(self._get_cfg("basic_settings", "time_format") or "%Y-%m-%d %H:%M", tz),
                    last_user=last_user,
                    last_ai=last_ai,
                    umo=umo
                )
            else:
                prompt = "请自然地延续对话，与用户继续交流。"
            
            # 调试模式
            if (self.cfg.get("special") or {}).get("debug_mode", False):
                logger.info(f"[Conversa] ========== 调试模式开始 ==========")
                logger.info(f"[Conversa] 用户: {umo}")
                logger.info(f"[Conversa] 系统提示词长度: {len(system_prompt) if system_prompt else 0} 字符")
                if system_prompt:
                    logger.info(f"[Conversa] 系统提示词前100字符: {system_prompt[:100]}...")
                else:
                    logger.warning(f"[Conversa] ⚠️ 警告：system_prompt 为空！")
                logger.info(f"[Conversa] 用户提示词: {prompt}")
                logger.info(f"[Conversa] 上下文历史共 {len(contexts)} 条")
                logger.info(f"[Conversa] ========== 调试模式结束 ==========")
            
            # 调用 LLM
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""
            
            if not text.strip():
                return False
            
            # 添加时间戳
            if bool(self._get_cfg("basic_settings", "append_time_field")):
                text = f"[{_fmt_now(self._get_cfg('basic_settings', 'time_format') or '%Y-%m-%d %H:%M', tz)}] " + text
            
            # 发送消息
            await self._send_text(umo, text)
            logger.info(f"[Conversa] 已发送主动回复给 {umo}: {text[:50]}...")
            
            # 更新状态
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
                self._save_session_data()
            
            return True
        
        except Exception as e:
            logger.error(f"[Conversa] proactive error({umo}): {e}")
            return False
    
    async def _proactive_reminder_reply(self, umo: str, reminder_content: str) -> bool:
        """执行由 AI 生成的主动提醒回复"""
        try:
            hist_n = int(self._get_cfg("basic_settings", "history_depth") or 8)
            
            # 获取 Provider
            fixed_provider = (self.cfg.get("special") or {}).get("provider") or ""
            provider = self.context.get_provider_by_id(fixed_provider) if fixed_provider else self.context.get_using_provider(umo=umo)
            if not provider:
                logger.warning(f"[Conversa] reminder provider missing for {umo}")
                return False

            # 获取 Conversation
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, curr_cid)

            # 获取 System Prompt
            system_prompt = await self._get_system_prompt(umo, conversation)

            # 获取上下文
            contexts = await self._safe_get_full_contexts(umo, conversation)
            if contexts and hist_n > 0:
                contexts = contexts[-hist_n:]

            # 构造提醒专用的 Prompt
            prompt_template = self._get_cfg("reminders_settings", "reminder_prompt_template") or "用户提醒：{reminder_content}"
            prompt = prompt_template.format(
                reminder_content=reminder_content,
                now=_fmt_now(
                    self._get_cfg("basic_settings", "time_format") or "%Y-%m-%d %H:%M",
                    self._get_cfg("basic_settings", "timezone")
                )
            )

            logger.info(f"[Conversa] 触发 AI 提醒 for {umo}: {reminder_content}")

            # 调用 LLM
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""

            if not text.strip():
                return False

            # 发送消息
            await self._send_text(umo, f"⏰ {text}")
            logger.info(f"[Conversa] 已发送 AI 提醒给 {umo}: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"[Conversa] proactive reminder error({umo}): {e}")
            return False

    async def _get_system_prompt(self, umo: str, conversation) -> str:
        """获取系统提示词（支持多种降级策略）"""
        system_prompt = ""
        persona_obj = None
        
        # 优先使用配置文件中的自定义人格
        if (self._get_cfg("basic_settings", "persona_override") or "").strip():
            system_prompt = self._get_cfg("basic_settings", "persona_override")
            logger.debug(f"[Conversa] 使用配置文件中的自定义人格")
        else:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr:
                fixed_persona = (self.cfg.get("special") or {}).get("persona") or ""
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

    async def _safe_get_full_contexts(self, umo: str, conversation=None) -> List[Dict]:
        """安全获取完整上下文，使用多重降级策略"""
        contexts = []
        
        # 策略1：从传入的 conversation 对象获取
        if conversation:
            try:
                if hasattr(conversation, "messages") and conversation.messages:
                    contexts = self._normalize_messages(conversation.messages)
                    if contexts:
                        logger.debug(f"[Conversa] 从conversation.messages获取{len(contexts)}条历史")
                        return contexts
                
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
        
        # 策略2：通过 conversation_manager 重新获取
        try:
            if hasattr(self.context, "conversation_manager"):
                conv_mgr = self.context.conversation_manager
                conversation_id = await conv_mgr.get_curr_conversation_id(umo)
                if conversation_id:
                    conversation = await conv_mgr.get_conversation(umo, conversation_id)
                    if conversation:
                        if hasattr(conversation, "messages") and conversation.messages:
                            contexts = self._normalize_messages(conversation.messages)
                            if contexts:
                                logger.debug(f"[Conversa] 从conversation_manager.messages获取{len(contexts)}条历史")
                                return contexts
                        
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
        
        # 策略3：使用插件的轻量历史缓存
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
        """标准化消息格式，兼容多种形态"""
        if not msgs:
            return []
        
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
    
    async def _send_text(self, umo: str, text: str):
        """发送纯文本消息到指定会话"""
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[Conversa] send_message error({umo}): {e}")

    # 生命周期管理
    
    def terminate(self):
        """插件卸载/停用时的清理方法"""
        if self._loop_task:
            self._loop_task.cancel()
            logger.info("[Conversa] Scheduler stopped.")
            
            # 清理数据文件
        files_to_delete = [self._user_data_path, self._session_data_path]
        for file_path in files_to_delete:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"[Conversa] Cleaned up data file: {file_path}")
            except OSError as e:
                logger.error(f"[Conversa] Error cleaning up data file {file_path}: {e}")
        
        # 尝试删除数据目录（如果为空）
        try:
                if os.path.exists(self._data_dir) and not os.listdir(self._data_dir):
                    os.rmdir(self._data_dir)
                logger.info(f"[Conversa] Cleaned up data directory: {self._data_dir}")
        except OSError as e:
            logger.error(f"[Conversa] Error cleaning up data directory {self._data_dir}: {e}")
        
        logger.info("[Conversa] Terminated.")

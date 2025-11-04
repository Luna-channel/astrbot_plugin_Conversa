
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

# 尝试导入 StarTools（如果可用）
try:
    from astrbot.api.star import StarTools
    HAS_STARTOOLS = True
except ImportError:
    HAS_STARTOOLS = False

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
            try:
                return datetime.now(zoneinfo.ZoneInfo(tz_name))
            except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
                logger.warning(f"[Conversa] 无效时区 '{tz_name}': {e}，使用系统默认时区")
                return datetime.now()
    except ImportError:
        # Python < 3.9 需要 backports.zoneinfo
        try:
            from backports import zoneinfo
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
    idle_after_minutes: int | None = None  
    daily_reminders_enabled: bool = True
    daily_reminder_count: int = 3
    quiet_hours: str | None = None  # 用户专属免打扰时间 "HH:MM-HH:MM"

    def to_dict(self):
        return {
            "subscribed": self.subscribed,
            "idle_after_minutes": self.idle_after_minutes,
            "daily_reminders_enabled": self.daily_reminders_enabled,
            "daily_reminder_count": self.daily_reminder_count,
            "quiet_hours": self.quiet_hours
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            subscribed=data.get("subscribed", False),
            idle_after_minutes=data.get("idle_after_minutes"),
            daily_reminders_enabled=data.get("daily_reminders_enabled", True),
            daily_reminder_count=data.get("daily_reminder_count", 3),
            quiet_hours=data.get("quiet_hours")
        )

@dataclass
class SessionState:
    """运行时会话状态（内存中维护）"""
    last_ts: float = 0.0
    last_fired_tag: str = ""  # 保留用于向后兼容
    last_fired_tags: dict = None  # 改为字典：{tag: timestamp}，支持过期清理
    last_user_reply_ts: float = 0.0
    consecutive_no_reply_count: int = 0
    next_idle_ts: float = 0.0
    
    def __post_init__(self):
        """初始化后处理"""
        if self.last_fired_tags is None:
            self.last_fired_tags = {}
            # 迁移旧数据
            if self.last_fired_tag:
                self.last_fired_tags[self.last_fired_tag] = _now_tz(None).timestamp()

    def to_dict(self):
        return {
            "last_ts": self.last_ts,
            "last_fired_tag": self.last_fired_tag,  # 保留用于向后兼容
            "last_fired_tags": self.last_fired_tags if self.last_fired_tags else {},
            "last_user_reply_ts": self.last_user_reply_ts,
            "consecutive_no_reply_count": self.consecutive_no_reply_count,
            "next_idle_ts": self.next_idle_ts
        }

    @classmethod
    def from_dict(cls, data: dict):
        tags_dict = data.get("last_fired_tags", {})
        if not isinstance(tags_dict, dict):
            tags_dict = {}
        
        return cls(
            last_ts=data.get("last_ts", 0.0),
            last_fired_tag=data.get("last_fired_tag", ""),
            last_fired_tags=tags_dict,
            last_user_reply_ts=data.get("last_user_reply_ts", 0.0),
            consecutive_no_reply_count=data.get("consecutive_no_reply_count", 0),
            next_idle_ts=data.get("next_idle_ts", 0.0)
        )
    
    def has_fired(self, tag: str) -> bool:
        """检查某个标记是否已触发（支持过期清理）"""
        if not self.last_fired_tags:
            return False
        return tag in self.last_fired_tags
    
    def mark_fired(self, tag: str):
        """标记某个事件已触发"""
        if self.last_fired_tags is None:
            self.last_fired_tags = {}
        self.last_fired_tags[tag] = _now_tz(None).timestamp()
        # 同时更新 last_fired_tag 用于向后兼容
        self.last_fired_tag = tag
        
        # 清理过期标记（保留最近7天的记录）
        now_ts = _now_tz(None).timestamp()
        expired_tags = [t for t, ts in self.last_fired_tags.items() if now_ts - ts > 7 * 86400]
        for t in expired_tags:
            del self.last_fired_tags[t]


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
@register("Conversa", "柯尔", "Conversa能够让AI在会话沉寂一段时间后，像真人一样重新发起聊天，或者在每日的特定时间点送上问候，或以自然的方式进行定时提醒。", "1.3.0", 
          "https://github.com/Luna-channel/astrbot_plugin_Conversa")
class Conversa(Star):

    # 初始化
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        
        # 运行时数据
        self._states: Dict[str, SessionState] = {}
        self._user_profiles: Dict[str, UserProfile] = {}
        self._reminders: Dict[str, Reminder] = {}
        
        # 文件保存去抖相关
        self._save_user_data_task: Optional[asyncio.Task] = None
        self._save_session_data_task: Optional[asyncio.Task] = None
        self._save_delay_seconds = 2.0  # 去抖延迟：2秒
        
        # 数据文件路径（使用规范的方式获取插件数据目录）
        if HAS_STARTOOLS:
            # 使用 StarTools 获取规范的数据目录
            data_dir_path = StarTools.get_data_dir() / "astrbot_plugin_conversa"
            self._data_dir = str(data_dir_path)
            os.makedirs(self._data_dir, exist_ok=True)
        else:
            # 后备方案：使用更可靠的方式获取数据目录
            # 尝试从 context 获取，如果不可用则使用当前文件的相对路径
            try:
                # 尝试使用 context 获取数据路径
                if hasattr(context, 'get_data_path') or hasattr(self, 'get_data_path'):
                    data_path_func = getattr(context, 'get_data_path', None) or getattr(self, 'get_data_path', None)
                    if data_path_func:
                        base_path = data_path_func()
                        self._data_dir = _ensure_dir(os.path.join(base_path, "astrbot_plugin_conversa"))
                    else:
                        raise AttributeError
                else:
                    raise AttributeError
            except (AttributeError, TypeError):
                # 最终后备：基于当前工作目录，但添加警告
                import warnings
                warnings.warn("[Conversa] 无法使用 StarTools，使用 os.getcwd() 作为后备方案")
                root = os.getcwd()
                self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_conversa"))
        
        self._user_data_path = os.path.join(self._data_dir, "user_data.json")
        self._session_data_path = os.path.join(self._data_dir, "session_data.json")
        
        # 加载数据
        self._load_user_data()
        self._load_session_data()
        self._sync_subscribed_users_from_config()
        
        # 启动后台调度器
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[Conversa] Scheduler started.")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查事件发送者是否为AstrBot管理员"""
        return event.role == "admin"

    def _get_cfg(self, group_key: str, sub_key: str, default=None):
        group = self.cfg.get(group_key)
        if not isinstance(group, dict):
            return default
        return group.get(sub_key, default)

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
        except (IOError, OSError) as e:
            logger.error(f"[Conversa] Failed to read user data file: {e}")
    
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
        except (IOError, OSError) as e:
            logger.error(f"[Conversa] Failed to write user data file: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"[Conversa] Failed to serialize user data: {e}")
    
    def _load_session_data(self):
        """加载运行时状态（从 session_data.json）"""
        if not os.path.exists(self._session_data_path):
            return
        try:
            with open(self._session_data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                states_data = data.get("states", {})
                for conv_id, state_dict in states_data.items():
                    self._states[conv_id] = SessionState.from_dict(state_dict)
                logger.info(f"[Conversa] Loaded {len(self._states)} session states.")
        
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[Conversa] Failed to load session data: {e}")
        except (IOError, OSError) as e:
            logger.error(f"[Conversa] Failed to read session data file: {e}")
    
    def _save_session_data(self):
        """保存运行时状态（到 session_data.json）"""
        try:
            states_dict = {cid: state.to_dict() for cid, state in self._states.items()}
            data = {"states": states_dict}
            with open(self._session_data_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except (IOError, OSError) as e:
            logger.error(f"[Conversa] Failed to write session data file: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"[Conversa] Failed to serialize session data: {e}")
    
    async def _debounced_save_user_data(self):
        """
        去抖保存用户数据：在最后一次调用后的指定延迟后执行一次保存
        避免高频消息时的频繁磁盘I/O
        """
        # 取消之前的保存任务（如果存在）
        if self._save_user_data_task and not self._save_user_data_task.done():
            self._save_user_data_task.cancel()
        
        async def delayed_save():
            try:
                await asyncio.sleep(self._save_delay_seconds)
                self._save_user_data()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[Conversa] Debounced save user data failed: {e}")
        
        # 创建新的延迟保存任务
        self._save_user_data_task = asyncio.create_task(delayed_save())
    
    async def _debounced_save_session_data(self):
        """
        去抖保存会话数据：在最后一次调用后的指定延迟后执行一次保存
        避免高频消息时的频繁磁盘I/O
        """
        # 取消之前的保存任务（如果存在）
        if self._save_session_data_task and not self._save_session_data_task.done():
            self._save_session_data_task.cancel()
        
        async def delayed_save():
            try:
                await asyncio.sleep(self._save_delay_seconds)
                self._save_session_data()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[Conversa] Debounced save session data failed: {e}")
        
        # 创建新的延迟保存任务
        self._save_session_data_task = asyncio.create_task(delayed_save())
    
    def _sync_subscribed_users_from_config(self):
        """从配置文件同步订阅用户列表到内部状态"""
        try:
            config_subscribed_ids = self._get_cfg("basic_settings", "subscribed_users") or []
            if not isinstance(config_subscribed_ids, list):
                logger.warning(f"[Conversa] subscribed_users 配置格式错误，应为列表")  # noqa: F541
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
        try:
            subscribed_users = []
            for user_id, profile in self._user_profiles.items():
                if profile.subscribed:
                    subscribed_users.append(user_id)
            
            # 直接更新配置
            if "basic_settings" not in self.cfg:
                self.cfg["basic_settings"] = {}
            self.cfg["basic_settings"]["subscribed_users"] = subscribed_users
            self.cfg.save_config()
            logger.info(f"[Conversa] 已同步 {len(subscribed_users)} 个订阅用户到配置文件")
        except Exception as e:
            logger.error(f"[Conversa] 同步订阅用户到配置失败: {e}")
    
    def _save_user_profiles(self):
        """兼容旧API，实际调用整合后的保存函数"""
        self._save_user_data()
    
    
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
        5. 计算下一次延时问候触发时间
        """
        umo = event.unified_msg_origin
        
        # 初始化数据结构
        if umo not in self._states:
            self._states[umo] = SessionState()
        if umo not in self._user_profiles:
            self._user_profiles[umo] = UserProfile()

        st = self._states[umo]
        profile = self._user_profiles[umo]

        # 更新时间戳
        now_ts = _now_tz(self._get_cfg("basic_settings", "timezone") or None).timestamp()
        st.last_ts = now_ts
        st.last_user_reply_ts = now_ts
        st.consecutive_no_reply_count = 0

        # 自动订阅模式
        if (self._get_cfg("basic_settings", "subscribe_mode") or "manual") == "auto":
            profile.subscribed = True
        
        # 自动重新激活：如果用户主动聊天，且曾经订阅过（被自动退订），则重新激活
        if not profile.subscribed and st.last_user_reply_ts > 0:
            # 检查是否是因为超时被自动退订的（有历史活跃记录）
            auto_resubscribe = bool(self._get_cfg("basic_settings", "auto_resubscribe", True))
            if auto_resubscribe:
                # 用户主动发消息，重新激活订阅
                profile.subscribed = True
                logger.info(f"[Conversa] 自动重新激活订阅: {umo} (用户主动聊天)")


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

        # 保存状态（使用去抖机制，减少高频磁盘I/O）
        self._debounced_save_session_data()
        self._debounced_save_user_data()

    @filter.after_message_sent()
    async def _after_message_sent(self, event: AstrMessageEvent):
        """监听消息发送后事件，用于日志确认"""
        try:
            # 框架会自动处理消息历史，我们只需要确认
            if event._result and hasattr(event._result, "chain"):
                message_text = "".join([i.text for i in event._result.chain if hasattr(i, "text")])
                if message_text:
                    logger.debug(f"[Conversa] 消息已发送: {message_text[:50]}...")
            
        except Exception as e:
            logger.debug(f"[Conversa] 消息发送后处理: {e}")

    @filter.command("conversa")
    async def _cmd_conversa(self, event: AstrMessageEvent):
        """
        Conversa 命令处理器
        
        支持的子命令：
        - help: 显示帮助信息
        - debug: 显示调试信息
        - on/off: 启用/停用插件
        - watch/unwatch: 订阅/退订当前会话
        - set after <小时>: 设置专属延时问候时间
        - set daily[1-3] <HH:MM>: 设置每日定时回复时间
        - set quiet <HH:MM-HH:MM>: 设置免打扰时间段
        - set history <N>: 设置上下文历史条数
        - remind add/list/del: 管理提醒事项
        """
        text = (event.message_str or "").strip()
        
        # 动态处理主命令和别名
        command_parts = text.lstrip('/').split()
        if not command_parts:
            return
        
        # 提取真实命令和参数
        args_str = " ".join(command_parts[1:]) if len(command_parts) > 1 else ""
        
        # 将参数字符串分割成子命令和值
        args = args_str.split()
        sub_command = args[0] if args else ""

        def reply(msg: str):
            return event.plain_result(msg)

        # 帮助信息
        if not sub_command or sub_command == "help":
            yield reply(self._help_text())
            return
            
        # 调试信息
        if sub_command == "debug":
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
            debug_info.append(f"用户专属免打扰: {profile.quiet_hours if profile and profile.quiet_hours else '未设置(使用全局)'}")
            debug_info.append(f"全局免打扰时间: {self._get_cfg('basic_settings', 'quiet_hours', '未设置')}")
            debug_info.append(f"延时基准: {self._get_cfg('idle_greetings', 'idle_after_minutes', 0)}分钟")
            debug_info.append(f"最大无回复天数: {self._get_cfg('basic_settings', 'max_no_reply_days', 0)}")
            debug_info.append(f"自动重新激活: {bool(self._get_cfg('basic_settings', 'auto_resubscribe', True))}")
            yield reply("🔍 调试信息:\n" + "\n".join(debug_info))
            return

        # 启用/停用插件
        if sub_command == "on":
            if not self._is_admin(event):
                yield event.plain_result("错误：此命令仅限管理员使用。")
                return
            self.cfg["enable"] = True
            self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
            self.cfg["basic_settings"]["enable"] = True
            self.cfg.save_config()
            yield reply("✅ 已启用 Conversa")
            return
        
        if sub_command == "off":
            if not self._is_admin(event):
                yield event.plain_result("错误：此命令仅限管理员使用。")
                return
            self.cfg["enable"] = False
            self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
            self.cfg["basic_settings"]["enable"] = False
            self.cfg.save_config()
            yield reply("🛑 已停用 Conversa")
            return

        # 订阅/退订
        if sub_command == "watch":
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = True
            logger.info(f"[Conversa] 用户执行 watch 命令: {umo}")
            self._save_user_data()
            self._sync_subscribed_users_to_config()
            yield reply("📌 已订阅当前会话")
            return

        if sub_command == "unwatch":
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            self._user_profiles[umo].subscribed = False
            self._save_user_data()
            self._sync_subscribed_users_to_config()
            yield reply("📭 已退订当前会话")
            return

        # 设置命令
        if sub_command == "set":
            if len(args) < 3:
                yield reply("❌ 参数不足。用法: /conversa set <目标> <值>")
                return
            
            target = args[1].lower()
            value = args[2]

            if target == "after":
                umo = event.unified_msg_origin
                profile = self._user_profiles.get(umo)
                if not profile:
                    self._user_profiles[umo] = UserProfile()
                    profile = self._user_profiles[umo]
                
                try:
                    hours = float(value)
                    if hours >= 0.5:
                        minutes = int(hours * 60)
                        profile.idle_after_minutes = minutes
                        
                        # 立即更新 next_idle_ts，使设置立即生效
                        if umo not in self._states:
                            self._states[umo] = SessionState()
                        st = self._states[umo]
                        tz = self._get_cfg("basic_settings", "timezone") or None
                        now_ts = _now_tz(tz).timestamp()
                        st.next_idle_ts = now_ts + minutes * 60
                        
                        self._save_user_data()
                        await self._debounced_save_session_data()
                        yield reply(f"⏱️ 已为您设置专属延时问候：{hours} 小时后触发")
                    else:
                        yield reply("⏱️ 延时问候的小时数不能少于 0.5 (30分钟)。")
                except ValueError:
                    yield reply("⏱️ 请输入有效的小时数 (例如 1, 1.5, 2)。")
                return

            elif target.startswith("daily"):
                match = re.match(r"daily([1-3])", target)
                if match:
                    n = int(match.group(1))
                    time_val = value
                    if not _parse_hhmm(time_val):
                        yield reply("❌ 时间格式错误，请使用 HH:MM 格式。")
                        return

                    slot_cfg = self.cfg.get("daily_prompts") or {}
                    if not isinstance(slot_cfg, dict):
                        slot_cfg = {}
                        
                    slot_cfg[f"slot{n}"] = slot_cfg.get(f"slot{n}", {})
                    slot_cfg[f"slot{n}"]["time"] = time_val
                    slot_cfg[f"slot{n}"]["enable"] = True
                    self.cfg["daily_prompts"] = slot_cfg
                    
                    self.cfg["basic_settings"] = self.cfg.get("basic_settings") or {}
                    self.cfg["basic_settings"]["enable_daily_greetings"] = True
                    self.cfg.save_config()
                    yield reply(f"🗓️ 已设置 daily{n}：{time_val}")
                else:
                    yield reply("❌ 无效的 daily 目标。用法: /conversa set daily[1-3] <HH:MM>")
                return

            elif target == "quiet":
                # 用户可以设置自己的免打扰时间，管理员设置全局
                if re.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", value):
                    umo = event.unified_msg_origin
                    
                    # 检查是否是管理员且想设置全局
                    if self._is_admin(event) and len(args) > 3 and args[3].lower() == "global":
                        # 管理员设置全局免打扰
                        settings = self.cfg.get("basic_settings") or {}
                        settings["quiet_hours"] = value
                        self.cfg["basic_settings"] = settings
                        self.cfg.save_config()
                        yield reply(f"🔕 已设置全局免打扰：{value}")
                    else:
                        # 用户设置自己的免打扰时间
                        if umo not in self._user_profiles:
                            self._user_profiles[umo] = UserProfile()
                        self._user_profiles[umo].quiet_hours = value
                        self._save_user_data()
                        yield reply(f"🔕 已为您设置专属免打扰：{value}")
                else:
                    yield reply("格式错误，请使用 HH:MM-HH:MM 格式。例如: 23:00-07:00")
                return
            
            elif target == "history":
                if not self._is_admin(event):
                    yield reply("错误：此命令仅限管理员使用。")
                    return
                try:
                    depth = int(value)
                    settings = self.cfg.get("basic_settings") or {}
                    settings["history_depth"] = depth
                    self.cfg["basic_settings"] = settings
                    self.cfg.save_config()
                    yield reply(f"🧵 已设置历史条数：{depth}")
                except ValueError:
                    yield reply("请输入有效的数字。")
                return
            
            yield reply(f"❌ 未知的 set 目标 '{target}'。可用: after, daily[1-3], quiet, history。")
            return

        # remind 命令
        if sub_command == "remind":
            if not bool(self._get_cfg("reminders_settings", "enable_reminders", True)):
                yield reply("提醒功能已被管理员禁用。")
                return
            
            remind_sub_command = args[1].lower() if len(args) > 1 else ""

            if remind_sub_command == "list":
                yield reply(self._remind_list_text(event.unified_msg_origin))
                return
            
            if remind_sub_command == "del" and len(args) >= 3:
                # 支持通过序号或 ID 删除
                identifier = args[2].strip()
                umo = event.unified_msg_origin
                
                # 尝试解析为序号（整数）
                try:
                    index = int(identifier)
                    # 获取用户的提醒列表并排序
                    user_reminders = self._get_user_reminders_sorted(umo)
                    if 1 <= index <= len(user_reminders):
                        rid = user_reminders[index - 1].id  # 序号从 1 开始
                        del self._reminders[rid]
                        self._save_user_data()
                        yield reply(f"🗑️ 已删除提醒 #{index}")
                    else:
                        yield reply(f"❌ 序号超出范围，当前共有 {len(user_reminders)} 个提醒")
                    return
                except ValueError:
                    # 不是数字，尝试作为 ID 删除（向后兼容）
                    rid = identifier
                    if rid in self._reminders and self._reminders[rid].umo == umo:
                        del self._reminders[rid]
                        self._save_user_data()
                        yield reply(f"🗑️ 已删除提醒 {rid}")
                    else:
                        yield reply("❌ 未找到该提醒，请使用 `/conversa remind list` 查看可用序号")
                return
            
            if remind_sub_command == "add":
                remind_content = " ".join(args[2:])
                # 匹配 HH:MM 格式
                m_daily = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", remind_content)
                # 匹配 YYYY-MM-DD HH:MM 格式
                m_once = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$", remind_content)
                
                rid = f"R{int(datetime.now().timestamp())}"
                
                if m_once:
                    at_time, content = m_once.groups()
                    self._reminders[rid] = Reminder(
                        id=rid,
                        umo=event.unified_msg_origin,
                        content=content.strip(),
                        at=at_time.strip(),
                        created_at=datetime.now().timestamp()
                    )
                    self._save_user_data()
                    yield reply(f"⏰ 已添加一次性提醒 {rid}")
                    return
                elif m_daily:
                    hhmm, content = m_daily.groups()
                    self._reminders[rid] = Reminder(
                        id=rid,
                        umo=event.unified_msg_origin,
                        content=content.strip(),
                        at=f"{hhmm}|daily",
                        created_at=datetime.now().timestamp()
                    )
                    self._save_user_data()
                    yield reply(f"⏰ 已添加每日提醒 {rid}")
                    return
            
            yield reply(self._help_text())
            return

        # 默认显示帮助
        yield reply(self._help_text())


    def _help_text(self) -> str:
        """返回插件的帮助文本"""
        return (
            "--- Conversa 插件帮助 (指令: /conversa) ---\n"
            "/conversa on/off - (管理员)全局启用或禁用插件\n"
            "/conversa watch/unwatch - 订阅或退订当前会话\n"
            "/conversa set after <小时> - x小时后主动问候（最低0.5）\n"
            "/conversa set quiet <HH:MM-HH:MM> - 设置您的专属免打扰时间\n"
            "/conversa set quiet <HH:MM-HH:MM> global - (管理员)设置全局免打扰\n"
            "/conversa remind <add/list/del> [参数...]\n"
            "  - add <HH:MM> <提醒内容> - 添加每日提醒\n"
            "  - add <YYYY-MM-DD HH:MM> <提醒内容> - 添加一次性提醒\n"
            "  - list - 显示当前会话的所有提醒\n"
            "  - del <序号> - 删除指定序号的提醒"
        )

    def _get_user_reminders_sorted(self, umo: str) -> List[Reminder]:
        """获取指定用户的提醒列表并排序"""
        arr = [r for r in self._reminders.values() if r.umo == umo]
        arr.sort(key=lambda x: x.created_at)
        return arr
    
    def _remind_list_text(self, umo: str) -> str:
        """生成指定用户的提醒列表文本（显示序号）"""
        arr = self._get_user_reminders_sorted(umo)
        if not arr:
            return "暂无提醒"
        lines = []
        for idx, r in enumerate(arr, start=1):
            # 格式化时间显示
            time_display = r.at.replace("|daily", " (每日)")
            lines.append(f"{idx}. {time_display} | {r.content}")
        # 使用换行符连接，确保每个提醒单独一行
        # 提示信息放在末尾，避免某些消息平台过滤括号内容
        return "提醒列表：\n" + "\n".join(lines)

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
            return

        tz = self._get_cfg("basic_settings", "timezone") or None
        now = _now_tz(tz)
        quiet = self._get_cfg("basic_settings", "quiet_hours", "") or ""
        hist_n = int(self._get_cfg("basic_settings", "history_depth") or 8)
        reply_interval = int(self._get_cfg("basic_settings", "reply_interval_seconds") or 10)

        # 解析每日定时配置（修复：使用 slot1/slot2/slot3 而非 time1/time2/time3）
        daily_slots = self._parse_daily_slots(now)

        # 遍历所有已订阅用户（添加错误隔离，防止单个用户错误影响整体调度）
        for umo, profile in list(self._user_profiles.items()):
            try:
                if not profile.subscribed:
                    continue
                
                # 优先使用用户专属免打扰时间，否则使用全局设置
                user_quiet = profile.quiet_hours if profile.quiet_hours else quiet
                if _in_quiet(now, user_quiet):
                    continue

                st = self._states.get(umo)
                if st and await self._should_auto_unsubscribe(umo, profile, st, now):
                    continue

                # 检查延时问候
                await self._check_idle_greeting(umo, st, now, hist_n, tz, reply_interval)

                # 检查每日定时问候
                await self._check_daily_greetings(umo, st, profile, now, daily_slots, hist_n, tz, reply_interval)
            except Exception as e:
                logger.error(f"[Conversa] 处理用户 {umo} 的 tick 任务时发生错误: {e}", exc_info=True)
                continue  # 继续处理下一个用户，不影响整体调度

        # 检查提醒
        await self._check_reminders(now, tz, reply_interval)
        # 调度器结束时使用去抖保存，减少磁盘I/O
        await self._debounced_save_session_data()

    def _parse_daily_slots(self, now: datetime) -> List[Tuple[int, Optional[Tuple[int, int]], str, dict]]:
        """
        解析每日定时配置，返回槽位信息列表
        
        支持两种配置结构：
        1. 扁平结构（WebUI）: time1, prompt1, daily1_enable
        2. 嵌套结构（命令）: slot1.time, slot1.prompt, slot1.enable
        
        返回: [(slot_num, time_tuple, tag, slot_cfg), ...]
        """
        daily = self.cfg.get("daily_prompts") or {}
        slots_info = []
        
        for slot_num in [1, 2, 3]:
            # 优先尝试嵌套结构（slot1/slot2/slot3）
            slot_cfg = daily.get(f"slot{slot_num}", {})
            if slot_cfg:
                # 嵌套结构：slot{n}.time, slot{n}.enable, slot{n}.prompt
                if slot_cfg.get("enable", False):
                    time_str = slot_cfg.get("time", "")
                    prompt_str = slot_cfg.get("prompt", "")
                    time_tuple = _parse_hhmm(time_str)
                    if time_tuple:
                        tag = f"daily{slot_num}@{now.strftime('%Y-%m-%d')} {time_tuple[0]:02d}:{time_tuple[1]:02d}"
                        slots_info.append((slot_num, time_tuple, tag, {"prompt": prompt_str}))
            else:
                # 扁平结构：time1, prompt1, daily1_enable
                enable_key = f"daily{slot_num}_enable"
                time_key = f"time{slot_num}"
                prompt_key = f"prompt{slot_num}"
                
                if daily.get(enable_key, False):
                    time_str = daily.get(time_key, "")
                    prompt_str = daily.get(prompt_key, "")
                    time_tuple = _parse_hhmm(time_str)
                    if time_tuple:
                        tag = f"daily{slot_num}@{now.strftime('%Y-%m-%d')} {time_tuple[0]:02d}:{time_tuple[1]:02d}"
                        slots_info.append((slot_num, time_tuple, tag, {"prompt": prompt_str}))
        
        return slots_info

    async def _check_idle_greeting(self, umo: str, st: Optional[SessionState], now: datetime, 
                                   hist_n: int, tz: Optional[str], reply_interval: int):
        """检查并触发延时问候"""
        if not bool(self._get_cfg("idle_greetings", "enable_idle_greetings", True)):
            return
        
        if not st:
            return
        
        # 向后兼容：如果 next_idle_ts 未设置或为0，自动初始化
        if not st.next_idle_ts or st.next_idle_ts <= 0:
            profile = self._user_profiles.get(umo)
            if profile and profile.subscribed:
                delay_m = profile.idle_after_minutes
                if delay_m is None:
                    base_delay_m = int(self._get_cfg("idle_greetings", "idle_after_minutes") or 45)
                    fluctuation_m = int(self._get_cfg("idle_greetings", "idle_random_fluctuation_minutes") or 15)
                    delay_m = base_delay_m + random.randint(-fluctuation_m, fluctuation_m)
                    delay_m = max(30, delay_m)
                
                # 基于最后活跃时间计算
                base_ts = st.last_ts if st.last_ts > 0 else now.timestamp()
                st.next_idle_ts = base_ts + delay_m * 60
                logger.info(f"[Conversa] 向后兼容：为 {umo} 初始化 next_idle_ts = {st.next_idle_ts}")
                await self._debounced_save_session_data()
                return  # 本次不触发，等下次检查
        
        if now.timestamp() < st.next_idle_ts:
            return
        
        tag = f"idle@{now.strftime('%Y-%m-%d %H:%M')}"
        if st.has_fired(tag):
            return
        
        idle_prompts = self._get_cfg("idle_greetings", "idle_prompt_templates") or []
        if not idle_prompts:
            return
        
        prompt_template = random.choice(idle_prompts)
        logger.info(f"[Conversa] 触发延时问候 {umo}")
        ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
        if ok:
            st.mark_fired(tag)
            st.next_idle_ts = 0.0
            if reply_interval > 0:
                await asyncio.sleep(reply_interval)
        else:
            st.consecutive_no_reply_count += 1

    async def _check_daily_greetings(self, umo: str, st: Optional[SessionState], profile: UserProfile,
                                     now: datetime, daily_slots: List[Tuple], hist_n: int, 
                                     tz: Optional[str], reply_interval: int):
        """检查并触发每日定时问候"""
        if not bool(self.cfg.get("enable_daily_greetings", True)) or not profile.daily_reminders_enabled:
            return
        
        if not st:
            return
        
        for slot_num, slot_time, tag, slot_cfg in daily_slots:
            if slot_time and now.hour == slot_time[0] and now.minute == slot_time[1]:
                if st.has_fired(tag):
                    continue
                
                prompt_template = slot_cfg.get("prompt", "")
                if prompt_template:
                    logger.info(f"[Conversa] 触发每日定时{slot_num}回复 {umo}")
                    ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
                    if ok:
                        st.mark_fired(tag)
                        if reply_interval > 0:
                            await asyncio.sleep(reply_interval)
                    else:
                        st.consecutive_no_reply_count += 1
                break  # 同一分钟只触发一个定时任务

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
                st = self._states.get(r.umo)
                if not st:
                    logger.warning(f"[Conversa] Reminder check skipped for {r.umo}: no session state found.")
                    continue

                if "|daily" in r.at:
                    hhmm = r.at.split("|", 1)[0]
                    t = _parse_hhmm(hhmm)
                    if not t:
                        continue
                    
                    if now.hour == t[0] and now.minute == t[1]:
                        # 为每日提醒创建唯一标记（每天一个）
                        tag = f"remind_daily_{r.id}@{now.strftime('%Y-%m-%d')}"
                        if not st.has_fired(tag):
                            logger.info(f"[Conversa] Firing daily reminder {r.id} for {r.umo}")
                            ok = await self._proactive_reminder_reply(r.umo, r.content)
                            if ok:
                                st.mark_fired(tag)  # 记录已触发
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                else:
                    # 一次性提醒：比较时间字符串（精确到分钟）
                    try:
                        # 使用字符串比较，避免时区问题
                        reminder_time_str = r.at  # 格式: "YYYY-MM-DD HH:MM"
                        now_time_str = now.strftime("%Y-%m-%d %H:%M")
                        
                        # 使用字符串比较，当前时间 >= 提醒时间即触发
                        if now_time_str >= reminder_time_str:
                            # 为一次性提醒创建唯一标记（防止重复）
                            tag = f"remind_once_{r.id}@{reminder_time_str}"
                            if not st.has_fired(tag):
                                logger.info(f"[Conversa] Firing one-time reminder {r.id} for {r.umo} (due: {r.at}, now: {now_time_str})")
                                ok = await self._proactive_reminder_reply(r.umo, r.content)
                                # 无论发送成功与否，一次性提醒都应该被删除，避免无限重试
                                st.mark_fired(tag)
                                fired_ids.append(rid)
                                if not ok:
                                    logger.warning(f"[Conversa] One-time reminder {r.id} failed to send, but will be deleted to prevent infinite retry")
                                if reply_interval > 0:
                                    await asyncio.sleep(reply_interval)
                    except Exception as e:
                        logger.warning(f"[Conversa] Error processing one-time reminder {r.id}: {e}")
                        continue
            except Exception as e:
                logger.error(f"[Conversa] Error checking reminder {r.id}: {e}")
                continue
        
        if fired_ids:
            for rid in fired_ids:
                self._reminders.pop(rid, None)
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
                logger.warning(f"[Conversa] 未能获取任何 system_prompt，将使用空值")  # noqa: F541
            
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
            
            # 记录关键信息
            logger.info(f"[Conversa] 准备主动回复 {umo}，上下文: {len(contexts)}条，系统提示词: {'已获取' if system_prompt else '空'}")
            
            # 调用 LLM 生成回复
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""
            
            if not text.strip():
                return False
            
            # 添加时间戳（在存档到历史之前，保存原始文本用于发送）
            response_text = text
            if bool(self._get_cfg("basic_settings", "append_time_field")):
                response_text = f"[{_fmt_now(self._get_cfg('basic_settings', 'time_format') or '%Y-%m-%d %H:%M', tz)}] " + text
            
            # 手动将模拟的用户 prompt 和 AI 回复添加到对话历史
            await self._add_message_pair_to_history(umo, curr_cid, conversation, prompt, response_text)
            
            # 发送消息
            await self._send_text(umo, response_text)
            logger.info(f"[Conversa] 已发送主动回复给 {umo}: {response_text[:50]}...")
            
            # 更新状态
            now_ts = _now_tz(tz).timestamp()
            st = self._states.get(umo)
            profile = self._user_profiles.get(umo)
            if st and profile and profile.subscribed:
                st.last_ts = now_ts
                # 主动回复后使用去抖保存，减少磁盘I/O
                await self._debounced_save_session_data()
            
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

            # 调用 LLM 生成提醒回复
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""

            if not text.strip():
                return False

            # 手动将模拟的用户 prompt 和 AI 回复添加到对话历史
            await self._add_message_pair_to_history(umo, curr_cid, conversation, prompt, text)

            # 发送提醒消息
            await self._send_reminder_message(umo, text)
            logger.info(f"[Conversa] 已发送 AI 提醒给 {umo}: {text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"[Conversa] proactive reminder error({umo}): {e}")
            return False

    async def _add_message_pair_to_history(self, umo: str, conversation_id: str, conversation, user_prompt: str, assistant_response: str):
        """
        手动将模拟的用户 prompt 和 AI 回复添加到对话历史
        
        根据 GitHub issue #3216 的解决方案：
        - 需要同时将"模拟的用户 Prompt"和"AI的回复"作为一个完整的 user -> assistant 对
        - 一起追加到 history 列表的末尾，然后再调用 update_conversation
        """
        try:
            # 检查 conversation_id 是否有效
            if not conversation_id:
                logger.warning("[Conversa] conversation_id 为空，无法更新历史")
                return
            
            # 重新获取 conversation 以确保获取最新状态
            conv_mgr = self.context.conversation_manager
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            if not conversation:
                logger.warning("[Conversa] 无法获取 conversation 对象")
                return
            
            # 参考 issue反馈的解决方案：直接使用 conversation.history（JSON字符串）
            current_history = []
            if hasattr(conversation, "history") and conversation.history:
                try:
                    # conversation.history 是 JSON 字符串，需要解析
                    current_history = json.loads(conversation.history)
                    if not isinstance(current_history, list):
                        logger.warning("[Conversa] 解析后的 history 不是列表格式")
                        current_history = []
                    logger.info(f"[Conversa] 从 conversation.history 获取到 {len(current_history)} 条历史记录")
                except json.JSONDecodeError as e:
                    logger.warning(f"[Conversa] 无法解析 history JSON: {e}")
                    current_history = []
            else:
                logger.info("[Conversa] conversation.history 为空，将创建新历史")
                current_history = []
            
            # 1. 存档我们模拟的 "user" 消息
            user_record = {"role": "user", "content": user_prompt}
            current_history.append(user_record)
            
            # 2. 存档 AI 生成的 "assistant" 消息
            assistant_record = {"role": "assistant", "content": assistant_response}
            current_history.append(assistant_record)
            
            logger.info(f"[Conversa] 准备更新历史，当前历史记录数: {len(current_history)}")
            
            # 3. 将包含了完整"一问一答"的新历史，写回数据库
            # 根据官方文档：update_conversation(unified_msg_origin, conversation_id, history, title, persona_id)
            await conv_mgr.update_conversation(
                unified_msg_origin=umo,
                conversation_id=conversation_id,
                history=current_history
            )
            
            logger.info(f"[Conversa] ✅ 已将主动回复添加到历史：user({len(user_prompt)}字符) + assistant({len(assistant_response)}字符)，总记录数: {len(current_history)}")
            
        except Exception as e:
            logger.error(f"[Conversa] ❌ 添加消息对到历史失败: {e}", exc_info=True)
            # 不抛出异常，允许继续执行发送消息的操作

    async def _get_system_prompt(self, umo: str, conversation) -> str:
        """获取系统提示词，支持配置覆盖和降级策略"""
        # 优先使用配置覆盖
        persona_override = (self._get_cfg("basic_settings", "persona_override") or "").strip()
        if persona_override:
            return persona_override
        
        # 使用人格管理器获取提示词
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if not persona_mgr:
                return ""
            
            # 1. 尝试会话专属人格
            if conversation and getattr(conversation, "persona_id", None):
                persona = await persona_mgr.get_persona(conversation.persona_id)
                if persona and getattr(persona, "system_prompt", None):
                    logger.info(f"[Conversa] 使用会话人格: {conversation.persona_id}")
                    return persona.system_prompt
            
            # 2. 使用默认人格
            default_persona = await persona_mgr.get_default_persona_v3(umo=umo)
            if default_persona and default_persona.get("prompt"):
                logger.info(f"[Conversa] 使用默认人格: {default_persona.get('name', 'Unknown')}")
                return default_persona["prompt"]
                
        except Exception as e:
            logger.warning(f"[Conversa] 获取系统提示词失败: {e}")
        
        return ""

    async def _safe_get_full_contexts(self, umo: str, conversation=None) -> List[Dict]:
        """安全获取完整上下文，使用多重降级策略确保稳定性"""
        contexts = []
        
        # 策略1：从传入的conversation对象获取
        contexts = await self._try_get_from_conversation(conversation)
        if contexts:
            logger.info(f"[Conversa] ✅ 策略1成功: 获取{len(contexts)}条历史")
            return contexts
        
        # 策略2：通过conversation_manager重新获取
        contexts = await self._try_get_from_manager(umo)
        if contexts:
            logger.info(f"[Conversa] ✅ 策略2成功: 获取{len(contexts)}条历史")
            return contexts
        
        logger.warning(f"[Conversa] ⚠️ 无法获取 {umo} 的对话历史，将使用空上下文")
        return []
    
    async def _try_get_from_conversation(self, conversation) -> List[Dict]:
        """尝试从conversation对象获取历史"""
        if not conversation:
            return []
        
        # 尝试多种数据源
        sources = [
            ("messages", lambda: getattr(conversation, "messages", None)),
            ("get_messages", lambda: self._safe_call(conversation.get_messages) if hasattr(conversation, "get_messages") else None),
            ("history", lambda: getattr(conversation, "history", None))
        ]
        
        for source_name, getter in sources:
            try:
                data = await getter() if asyncio.iscoroutinefunction(getter) else getter()
                if data:
                    contexts = self._extract_contexts_from_data(data)
                    if contexts:
                        logger.debug(f"[Conversa] 从{source_name}获取{len(contexts)}条历史")
                        return contexts
            except Exception as e:
                logger.debug(f"[Conversa] {source_name}获取失败: {e}")
        
        return []
    
    async def _try_get_from_manager(self, umo: str) -> List[Dict]:
        """尝试通过conversation_manager获取历史"""
        try:
            if not hasattr(self.context, "conversation_manager"):
                return []
            
            conv_mgr = self.context.conversation_manager
            conversation_id = await conv_mgr.get_curr_conversation_id(umo)
            if not conversation_id:
                return []
            
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            return await self._try_get_from_conversation(conversation)
        
        except Exception as e:
            logger.debug(f"[Conversa] conversation_manager获取失败: {e}")
            return []
    
    def _extract_contexts_from_data(self, data) -> List[Dict]:
        """从各种数据格式中提取上下文"""
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return self._normalize_messages(parsed)
            except json.JSONDecodeError:
                return []
        elif isinstance(data, list):
            return self._normalize_messages(data)
        elif hasattr(data, '__iter__'):
            try:
                return self._normalize_messages(list(data))
            except Exception:
                return []
        return []
    
    async def _safe_call(self, func, *args, **kwargs):
        """安全调用可能是异步的函数"""
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)
        except Exception:
            return None
    
    def _normalize_messages(self, msgs) -> List[Dict]:
        """标准化消息格式，兼容多种数据源"""
        if not msgs:
            return []
        
        # 处理嵌套结构
        if isinstance(msgs, dict) and "messages" in msgs:
            msgs = msgs["messages"]
        
        if not isinstance(msgs, list):
            return []
        
        normalized = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            
            # 提取角色和内容
            role = msg.get("role") or msg.get("speaker") or msg.get("from")
            content = msg.get("content") or msg.get("text") or msg.get("message") or ""
            
            # 验证并添加
            if role in ("user", "assistant", "system") and content and isinstance(content, str):
                normalized.append({"role": role, "content": content.strip()})
        
        return normalized
    
    async def _send_text(self, umo: str, text: str):
        """发送主动回复消息到指定会话"""
        try:
            # 使用文档推荐的方式构造消息链
            message_chain = MessageChain().message(text)
            await self.context.send_message(umo, message_chain)
            logger.info(f"[Conversa] ✅ 消息已发送: {text[:50]}...")
            
        except Exception as e:
            logger.error(f"[Conversa] ❌ 发送消息失败({umo}): {e}")
    
    async def _send_reminder_message(self, umo: str, text: str):
        """发送提醒消息到指定会话"""
        await self._send_text(umo, text)

    # 生命周期管理
    async def terminate(self):
        """插件销毁"""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass  # 预期的取消异常

        logger.info("[Conversa] Performing final data save before termination...")
        if self._save_user_data_task and not self._save_user_data_task.done():
            self._save_user_data_task.cancel()
        if self._save_session_data_task and not self._save_session_data_task.done():
            self._save_session_data_task.cancel()
        self._save_user_data()
        self._save_session_data()
        
        logger.info("[Conversa] 插件已停止")
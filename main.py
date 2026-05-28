
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

# 尝试导入新的Message模型（新版本astrbot）
try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        UserMessageSegment,
        TextPart,
    )
    HAS_NEW_MESSAGE_API = True
except ImportError:
    HAS_NEW_MESSAGE_API = False

# 尝试导入 llm_tool（Agent 工具注册装饰器）
try:
    from astrbot.api import llm_tool
    HAS_LLM_TOOL = True
except ImportError:
    # 兼容旧版本：提供空装饰器
    def llm_tool(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    HAS_LLM_TOOL = False

# 导入官方 Agent Pipeline API（用于主动回复走合规调用）
try:
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.astr_main_agent import build_main_agent, MainAgentBuildConfig
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.utils.history_saver import persist_agent_history
    HAS_AGENT_PIPELINE = True
except ImportError:
    HAS_AGENT_PIPELINE = False

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


def _format_time_delta(seconds: float) -> str:
    """将时间差（秒）格式化为友好的文本
    
    示例：
    - 180秒 -> "3分钟"
    - 3600秒 -> "1小时"
    - 7200秒 -> "2小时"
    - 86400秒 -> "1天"
    - 90000秒 -> "1天1小时"
    """
    if seconds < 60:
        return "不到1分钟"
    
    minutes = int(seconds / 60)
    hours = int(minutes / 60)
    days = int(hours / 24)
    
    if days > 0:
        remaining_hours = hours % 24
        if remaining_hours > 0:
            return f"{days}天{remaining_hours}小时"
        return f"{days}天"
    elif hours > 0:
        remaining_minutes = minutes % 60
        if remaining_minutes > 0:
            return f"{hours}小时{remaining_minutes}分钟"
        return f"{hours}小时"
    else:
        return f"{minutes}分钟"

# 数据类定义
@dataclass
class UserProfile:
    """用户订阅信息和个性化设置"""
    subscribed: bool = False
    idle_after_minutes: int | None = None  
    daily_reminders_enabled: bool = True
    daily_reminder_count: int = 3
    quiet_hours: str | None = None  # 用户专属免打扰时间 "HH:MM-HH:MM"
    manual_unsubscribe: bool = False  # 标记是否是手动退订（强开关）
    auto_unsubscribed: bool = False  # 标记是否是自动退订（用于自动重新激活判断）

    def to_dict(self):
        return {
            "subscribed": self.subscribed,
            "idle_after_minutes": self.idle_after_minutes,
            "daily_reminders_enabled": self.daily_reminders_enabled,
            "daily_reminder_count": self.daily_reminder_count,
            "quiet_hours": self.quiet_hours,
            "manual_unsubscribe": self.manual_unsubscribe,
            "auto_unsubscribed": self.auto_unsubscribed
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            subscribed=data.get("subscribed", False),
            idle_after_minutes=data.get("idle_after_minutes"),
            daily_reminders_enabled=data.get("daily_reminders_enabled", True),
            daily_reminder_count=data.get("daily_reminder_count", 3),
            quiet_hours=data.get("quiet_hours"),
            manual_unsubscribe=data.get("manual_unsubscribe", False),
            auto_unsubscribed=data.get("auto_unsubscribed", False)
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
    enhancement_chain_count: int = 0  # 连续插件主动回复计数（用于指数递减）
    last_proactive_reply_ts: float = 0.0  # 最近一次主动回复时间戳
    
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
            "next_idle_ts": self.next_idle_ts,
            "enhancement_chain_count": self.enhancement_chain_count,
            "last_proactive_reply_ts": self.last_proactive_reply_ts
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
            next_idle_ts=data.get("next_idle_ts", 0.0),
            enhancement_chain_count=data.get("enhancement_chain_count", 0),
            last_proactive_reply_ts=data.get("last_proactive_reply_ts", 0.0)
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
@register("Conversa", "柯尔", "Conversa能够让AI在会话沉寂一段时间后，像真人一样重新发起聊天，或者在每日的特定时间点送上问候，或以自然的方式进行定时提醒。", "3.0", 
          "https://github.com/Luna-channel/astrbot_plugin_Conversa")
class Conversa(Star):

    # 初始化
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        self._stopped: bool = False  # 插件停止标志
        
        # 运行时数据
        self._states: Dict[str, SessionState] = {}
        self._user_profiles: Dict[str, UserProfile] = {}
        self._reminders: Dict[str, Reminder] = {}
        
        # 文件保存去抖相关
        self._save_user_data_task: Optional[asyncio.Task] = None
        self._save_session_data_task: Optional[asyncio.Task] = None
        self._save_delay_seconds = 2.0  # 去抖延迟：2秒
        
        # 对话增强相关
        self._enhancement_tasks: Dict[str, asyncio.Task] = {}
        
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
        self._migrate_config()

    def _migrate_config(self):
        """一次性配置迁移：旧位置 -> 新位置"""
        try:
            changed = False
            advanced = self.cfg.get("advanced") or {}
            basic = self.cfg.get("basic_settings") or {}

            # special.provider -> advanced.fixed_provider
            special = self.cfg.get("special")
            if isinstance(special, dict) and special.get("provider"):
                if not advanced.get("fixed_provider"):
                    advanced["fixed_provider"] = special["provider"]
                    changed = True
                    logger.info("[Conversa] 已迁移 special.provider -> advanced.fixed_provider")

            # basic_settings.fixed_provider -> advanced.fixed_provider
            if basic.get("fixed_provider") and not advanced.get("fixed_provider"):
                advanced["fixed_provider"] = basic["fixed_provider"]
                changed = True
                logger.info("[Conversa] 已迁移 basic_settings.fixed_provider -> advanced.fixed_provider")

            # basic_settings.persona_override -> advanced.persona_override
            if basic.get("persona_override") and not advanced.get("persona_override"):
                advanced["persona_override"] = basic["persona_override"]
                changed = True
                logger.info("[Conversa] 已迁移 basic_settings.persona_override -> advanced.persona_override")

            if changed:
                self.cfg["advanced"] = advanced
                self.cfg.save_config()
        except Exception as e:
            logger.debug(f"[Conversa] 配置迁移检查: {e}")

    async def _migrate_reminders_to_cron(self) -> str:
        """将旧版 Conversa 提醒迁移到 AstrBot 原生 cron 系统（幂等，重复执行安全）"""
        if not self._reminders:
            return "没有需要迁移的提醒。"
        cron_mgr = getattr(self.context, "cron_manager", None)
        if not cron_mgr:
            return "❌ cron_manager 不可用，无法迁移。"

        # 预取已有 jobs 用于幂等检查
        existing_jobs = await cron_mgr.list_jobs()
        existing_map = {j.name: j for j in existing_jobs}

        migrated = 0
        failed = 0
        for rid, reminder in list(self._reminders.items()):
            try:
                at = reminder.at
                umo = reminder.umo
                content = reminder.content

                job_name = f"conversa_migrate_{rid}"
                template = self._get_cfg("reminders_settings", "reminder_prompt_template") or "提醒内容：{reminder_content}"
                note = template.replace("{reminder_content}", content)

                # 构建正确的 cron 表达式或一次性参数
                is_daily = "|daily" in at
                cron_expr = None
                run_at = None
                if is_daily:
                    hhmm = at.split("|", 1)[0]
                    t = _parse_hhmm(hhmm)
                    if not t:
                        failed += 1
                        continue
                    cron_expr = f"{t[1]} {t[0]} * * *"
                else:
                    try:
                        run_at = datetime.strptime(at, "%Y-%m-%d %H:%M")
                    except ValueError:
                        failed += 1
                        continue

                # 幂等检查：同名 job 已存在则删除重建（覆盖）
                existing = existing_map.get(job_name)
                if existing:
                    await cron_mgr.delete_job(existing.job_id)

                if is_daily:
                    await cron_mgr.add_active_job(
                        name=job_name,
                        cron_expression=cron_expr,
                        payload={"session": umo, "note": note, "origin": "conversa_migrate"},
                        description=f"[Conversa迁移] {content[:60]}",
                        run_once=False,
                    )
                else:
                    await cron_mgr.add_active_job(
                        name=job_name,
                        cron_expression=None,
                        payload={"session": umo, "note": note, "origin": "conversa_migrate"},
                        description=f"[Conversa迁移] {content[:60]}",
                        run_once=True,
                        run_at=run_at,
                    )
                migrated += 1

            except Exception as e:
                logger.error(f"[Conversa] 迁移提醒 {rid} 失败: {e}")
                failed += 1

        parts = []
        if migrated > 0:
            parts.append(f"✅ 已迁移 {migrated} 个提醒到 AstrBot 原生定时任务")
            parts.append("旧数据已保留，可继续通过 /conversa remind 管理")
        if failed > 0:
            parts.append(f"❌ {failed} 个迁移失败")
        if not parts:
            parts.append("没有需要迁移的提醒。")
        return "\n".join(parts)

    async def initialize(self):
        """插件激活时的初始化方法（框架生命周期）"""
        # 启动后台调度器
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[Conversa] Scheduler started.")
        
        # Agent 订阅工具：仅在 agent 模式下激活
        if HAS_LLM_TOOL:
            mode = self._get_cfg("basic_settings", "subscribe_mode") or "manual"
            if mode == "agent":
                self.context.activate_llm_tool("conversa_subscribe")
                logger.info("[Conversa] Agent 订阅工具已激活")
            else:
                try:
                    self.context.deactivate_llm_tool("conversa_subscribe")
                except Exception:
                    pass  # 工具可能未注册，忽略

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
                logger.debug(f"[Conversa] Loaded {len(self._user_profiles)} user profiles.")
                
                reminders_data = data.get("reminders", {})
                for reminder_id, reminder_dict in reminders_data.items():
                    self._reminders[reminder_id] = Reminder.from_dict(reminder_dict)
                logger.debug(f"[Conversa] Loaded {len(self._reminders)} reminders.")
        
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
                logger.debug(f"[Conversa] Loaded {len(self._states)} session states.")
        
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
    
    def _sync_subscribed_users_from_config(self, silent: bool = False):
        """
        从配置文件同步订阅用户列表到内部状态
        
        Args:
            silent: 是否静默模式（不打印日志，仅在状态变化时打印）
        """
        try:
            config_subscribed_ids = self._get_cfg("basic_settings", "subscribed_users") or []
            if not isinstance(config_subscribed_ids, list):
                logger.warning(f"[Conversa] subscribed_users 配置格式错误，应为列表")  # noqa: F541
                return
            
            # 记录变化
            changes = {"added": [], "removed": []}
            
            # 同步所有用户的订阅状态（包括设置为 True 和 False）
            for user_id, profile in self._user_profiles.items():
                if user_id in config_subscribed_ids:
                    if not profile.subscribed:
                        profile.subscribed = True
                        profile.manual_unsubscribe = False  # 清除手动退订标记
                        profile.auto_unsubscribed = False  # 清除自动退订标记
                        changes["added"].append(user_id)
                        if not silent:
                            logger.debug(f"[Conversa] 从配置同步订阅状态(启用): {user_id}")
                else:
                    # 如果用户不在配置列表中，设置为未订阅（来自 WebUI 的手动退订）
                    if profile.subscribed:
                        profile.subscribed = False
                        profile.manual_unsubscribe = True  # 标记为手动退订（WebUI操作视为手动）
                        profile.auto_unsubscribed = False  # 清除自动退订标记
                        changes["removed"].append(user_id)
                        if not silent:
                            logger.debug(f"[Conversa] 从配置同步订阅状态(禁用): {user_id}")
            
            # 只在有变化或非静默模式时打印信息
            if not silent or changes["added"] or changes["removed"]:
                if changes["added"]:
                    logger.info(f"[Conversa] 配置热重载：新增订阅 {changes['added']}")
                if changes["removed"]:
                    logger.info(f"[Conversa] 配置热重载：取消订阅 {changes['removed']}")
                
                if not silent and not changes["added"] and not changes["removed"]:
                    logger.debug(f"[Conversa] 已从配置同步 {len(config_subscribed_ids)} 个订阅用户ID")
                    subscribed_sessions = [user_id for user_id, profile in self._user_profiles.items() if profile.subscribed]
                    logger.debug(f"[Conversa] 当前已订阅的会话数: {len(subscribed_sessions)}")
            
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
            logger.debug(f"[Conversa] 已同步 {len(subscribed_users)} 个订阅用户到配置文件")
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

        # 判断是否为有实际内容的真实消息（过滤输入状态等空事件）
        message_text = event.message_str.strip() if hasattr(event, 'message_str') and event.message_str else ""
        is_real_message = bool(message_text)

        # 只有真实消息才取消待执行的对话增强任务
        if is_real_message:
            old_task = self._enhancement_tasks.pop(umo, None)
            if old_task and not old_task.done():
                old_task.cancel()

        # 保存旧的 last_user_reply_ts 用于判断是否是老用户
        old_last_user_reply_ts = st.last_user_reply_ts

        # 更新时间戳
        now_ts = _now_tz(self._get_cfg("basic_settings", "timezone") or None).timestamp()
        st.last_ts = now_ts
        if is_real_message:
            st.last_user_reply_ts = now_ts
        st.consecutive_no_reply_count = 0
        if is_real_message:
            st.enhancement_chain_count = 0  # 用户发了真实消息，重置主动回复链计数

        # 自动订阅模式：仅在首次创建用户时自动订阅
        if (self._get_cfg("basic_settings", "subscribe_mode") or "manual") == "auto":
            # 只在用户第一次发消息时（old_last_user_reply_ts == 0）自动订阅
            if old_last_user_reply_ts == 0 and not profile.manual_unsubscribe:
                profile.subscribed = True
                profile.auto_unsubscribed = False  # 清除自动退订标记
                logger.info(f"[Conversa] 自动订阅模式：新用户 {umo} 已自动订阅")
                self._sync_subscribed_users_to_config()  # 同步到配置文件
        
        # 自动重新激活：仅对"被自动退订"的用户生效，手动退订的用户不会被自动重新激活
        if not profile.subscribed and profile.auto_unsubscribed and not profile.manual_unsubscribe:
            auto_resubscribe = bool(self._get_cfg("basic_settings", "auto_resubscribe", True))
            if auto_resubscribe:
                # 用户主动发消息，重新激活订阅
                profile.subscribed = True
                profile.auto_unsubscribed = False  # 清除自动退订标记
                logger.info(f"[Conversa] 自动重新激活订阅: {umo} (用户在自动退订后主动聊天)")
                self._sync_subscribed_users_to_config()  # 同步到配置文件


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
        await self._debounced_save_session_data()
        await self._debounced_save_user_data()

    @filter.on_llm_response()
    async def _on_llm_response_enhancement(self, event: AstrMessageEvent, _response=None):
        """对话增强：LLM 回复后检查是否应触发短期追回复"""
        try:
            umo = event.unified_msg_origin
            if self._should_trigger_enhancement(umo):
                self._schedule_enhancement(umo)
        except Exception as e:
            logger.debug(f"[Conversa] 对话增强检查异常: {e}")

    # Agent 订阅工具
    @llm_tool(name="conversa_subscribe")
    async def _tool_subscribe(self, event: AstrMessageEvent, action: str):
        '''管理主动对话功能。当用户希望你能主动找他聊天、保持联系时开启；当用户明确不需要时关闭。

        Args:
            action(string): "on" 开启主动对话, "off" 关闭主动对话
        '''
        # 运行时检查：仅在 agent 模式下工作
        mode = self._get_cfg("basic_settings", "subscribe_mode") or "manual"
        if mode != "agent":
            return "主动对话的订阅方式当前不是 agent 模式，无法通过工具操作。"
        
        umo = event.unified_msg_origin
        if umo not in self._user_profiles:
            self._user_profiles[umo] = UserProfile()
        profile = self._user_profiles[umo]
        
        if action == "on":
            profile.subscribed = True
            profile.manual_unsubscribe = False
            profile.auto_unsubscribed = False
            logger.info(f"[Conversa] Agent 工具订阅: {umo}")
            self._save_user_data()
            self._sync_subscribed_users_to_config()
            return "已开启主动对话订阅，我会在合适的时候主动找你聊天。"
        elif action == "off":
            profile.subscribed = False
            profile.manual_unsubscribe = True
            profile.auto_unsubscribed = False
            logger.info(f"[Conversa] Agent 工具退订: {umo}")
            self._save_user_data()
            self._sync_subscribed_users_to_config()
            return "已关闭主动对话订阅，我不会再主动发起聊天了。"
        else:
            return f"无效的操作 '{action}'，请使用 'on' 或 'off'。"

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
            
            # 显示订阅/退订状态标记
            if profile:
                if profile.manual_unsubscribe:
                    debug_info.append("退订类型: 手动退订（强制，不会自动重新激活）")
                elif profile.auto_unsubscribed:
                    debug_info.append("退订类型: 自动退订（可自动重新激活）")
                elif profile.subscribed:
                    debug_info.append("订阅类型: 正常订阅")
            
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
            profile = self._user_profiles[umo]
            profile.subscribed = True
            profile.manual_unsubscribe = False  # 清除手动退订标记
            profile.auto_unsubscribed = False  # 清除自动退订标记
            logger.info(f"[Conversa] 用户执行 watch 命令: {umo}")
            self._save_user_data()
            self._sync_subscribed_users_to_config()
            yield reply("📌 已订阅当前会话")
            return

        if sub_command == "unwatch":
            umo = event.unified_msg_origin
            if umo not in self._user_profiles:
                self._user_profiles[umo] = UserProfile()
            profile = self._user_profiles[umo]
            profile.subscribed = False
            profile.manual_unsubscribe = True  # 设置手动退订标记（强开关）
            profile.auto_unsubscribed = False  # 清除自动退订标记
            logger.info(f"[Conversa] 用户执行 unwatch 命令（手动退订）: {umo}")
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
                    settings = self.cfg.get("advanced") or {}
                    settings["history_depth"] = depth
                    self.cfg["advanced"] = settings
                    self.cfg.save_config()
                    yield reply(f"🧵 已设置历史条数：{depth}")
                except ValueError:
                    yield reply("请输入有效的数字。")
                return
            
            yield reply(f"❌ 未知的 set 目标 '{target}'。可用: after, daily[1-3], quiet, history。")
            return

        # migrate-reminders 命令（管理员）
        if sub_command == "migrate-reminders":
            if not self._is_admin(event):
                yield reply("错误：此命令仅限管理员使用。")
                return
            result = await self._migrate_reminders_to_cron()
            yield reply(result)
            return

        # remind 命令（旧功能，推荐使用 AstrBot 原生定时提醒）
        if sub_command == "remind":
            if not bool(self._get_cfg("reminders_settings", "enable_reminders", True)):
                yield reply("提醒功能已被管理员禁用。\n💡 推荐直接对 AI 说「提醒我...」使用 AstrBot 原生定时提醒。")
                return
            
            remind_sub_command = args[1].lower() if len(args) > 1 else ""

            if remind_sub_command == "list":
                list_text = self._remind_list_text(event.unified_msg_origin)
                yield reply(f"{list_text}\n\n💡 提示：推荐直接对 AI 说「提醒我...」使用 AstrBot 原生定时提醒。")
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
                    yield reply(f"⏰ 已添加一次性提醒 {rid}\n💡 提示：推荐直接对 AI 说「提醒我...」使用 AstrBot 原生定时提醒。")
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
                    yield reply(f"⏰ 已添加每日提醒 {rid}\n💡 提示：推荐直接对 AI 说「提醒我...」使用 AstrBot 原生定时提醒。")
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
            "/conversa remind <add/list/del> [参数...] - (旧功能)管理提醒\n"
            "/conversa migrate-reminders - (管理员)迁移旧提醒到 AstrBot 原生定时任务"
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

    # 对话增强（短期随机追回复）

    def _should_trigger_enhancement(self, umo: str) -> bool:
        """判断是否应该触发对话增强"""
        try:
            if not self.cfg.get("enable", True):
                logger.debug("[Conversa] 对话增强跳过: 插件已禁用")
                return False
            
            enable_val = self._get_cfg("enhancement", "enable_enhancement", False)
            if not bool(enable_val):
                logger.debug(f"[Conversa] 对话增强跳过: enable_enhancement={enable_val} (raw cfg enhancement={self.cfg.get('enhancement')})")
                return False
            
            # 对话增强仅私聊生效
            if "GroupMessage" in umo:
                logger.debug("[Conversa] 对话增强跳过: 群聊不触发")
                return False
            
            profile = self._user_profiles.get(umo)
            if not profile or not profile.subscribed:
                logger.debug(f"[Conversa] 对话增强跳过: 用户未订阅 (profile={profile is not None}, subscribed={profile.subscribed if profile else 'N/A'})")
                return False
            
            # 调度时不检查免打扰（用户刚发了消息说明在线），执行时再检查
            
            # 已有待执行的增强任务
            if umo in self._enhancement_tasks and not self._enhancement_tasks[umo].done():
                logger.debug("[Conversa] 对话增强跳过: 已有待执行任务")
                return False
            
            # 计算概率（带指数递减）
            base_prob = int(self._get_cfg("enhancement", "enhancement_probability") or 20)
            st = self._states.get(umo)
            if not st:
                logger.debug("[Conversa] 对话增强跳过: 无 SessionState")
                return False
            
            chain_count = st.enhancement_chain_count
            
            # 指数递减
            decay_rate = float(self._get_cfg("enhancement", "enhancement_decay_rate") or 0.1)
            effective_prob = base_prob * (decay_rate ** chain_count)
            
            roll = random.random() * 100
            triggered = roll < effective_prob
            
            if triggered:
                logger.info(f"[Conversa] 对话增强触发: {umo} (概率={effective_prob:.2f}%, chain={chain_count}, roll={roll:.2f})")
            else:
                logger.debug(f"[Conversa] 对话增强未触发: {umo} (概率={effective_prob:.2f}%, chain={chain_count}, roll={roll:.2f})")
            
            return triggered
        except Exception as e:
            logger.error(f"[Conversa] 对话增强判断出错: {e}")
            return False

    def _schedule_enhancement(self, umo: str):
        """调度一个延迟的对话增强任务"""
        min_delay = int(self._get_cfg("enhancement", "enhancement_min_delay") or 30)
        max_delay = min(int(self._get_cfg("enhancement", "enhancement_max_delay") or 1800), 1800)
        if min_delay > max_delay:
            min_delay = max_delay
        delay = random.randint(min_delay, max_delay)
        
        logger.info(f"[Conversa] 已调度对话增强: {umo}, {delay}秒后执行")
        task = asyncio.create_task(self._delayed_enhancement(umo, delay))
        self._enhancement_tasks[umo] = task

    async def _delayed_enhancement(self, umo: str, delay: int):
        """延迟执行对话增强回复"""
        try:
            # 记录触发时的用户最后消息时间戳
            st = self._states.get(umo)
            if not st:
                return
            trigger_user_ts = st.last_user_reply_ts
            
            await asyncio.sleep(delay)
            
            # 再次检查：用户是否在等待期间发了新消息
            st = self._states.get(umo)
            if not st:
                return
            if st.last_user_reply_ts > trigger_user_ts:
                logger.debug(f"[Conversa] 对话增强取消: {umo} (用户在等待期间发了新消息)")
                return
            
            # 检查订阅状态
            profile = self._user_profiles.get(umo)
            if not profile or not profile.subscribed:
                return
            
            tz = self._get_cfg("basic_settings", "timezone") or None
            
            # 执行时检查免打扰（延迟期间可能已进入免打扰时段）
            now = _now_tz(tz)
            quiet = self._get_cfg("basic_settings", "quiet_hours", "") or ""
            user_quiet = profile.quiet_hours if profile.quiet_hours else quiet
            if _in_quiet(now, user_quiet):
                logger.debug(f"[Conversa] 对话增强取消: {umo} (当前处于免打扰时段)")
                return
            
            # 选择提示词模板
            prompts = self._get_cfg("enhancement", "enhancement_prompt_templates") or []
            if not prompts:
                return
            prompt_template = random.choice(prompts)
            
            hist_n = int(self._get_cfg("advanced", "history_depth") or 8)
            logger.info(f"[Conversa] 执行对话增强回复: {umo}")
            ok = await self._proactive_reply(umo, hist_n, tz, prompt_template)
            if ok:
                logger.info(f"[Conversa] 对话增强回复成功: {umo}")
            
        except asyncio.CancelledError:
            logger.debug(f"[Conversa] 对话增强任务被取消: {umo}")
        except Exception as e:
            logger.error(f"[Conversa] 对话增强执行出错({umo}): {e}")
        finally:
            self._enhancement_tasks.pop(umo, None)

    # 调度器
    
    async def _scheduler_loop(self):
        """后台调度循环任务，每30秒检查一次是否需要触发主动回复"""
        try:
            while not self._stopped:
                await asyncio.sleep(30)
                if self._stopped:
                    break
                await self._tick()
        except asyncio.CancelledError:
            pass  # 正常取消，不需要日志
        except Exception as e:
            logger.error(f"[Conversa] Scheduler error: {e}")
        finally:
            logger.info("[Conversa] Scheduler stopped.")

    async def _tick(self):
        """
        单次调度检查（每30秒执行一次）
        
        检查逻辑：
        1. 如果插件被停用，直接返回
        2. 从配置同步订阅状态（实现配置热重载）
        3. 遍历所有已订阅的会话，检查是否需要主动回复
        4. 检查是否在免打扰时间段内
        5. 检查是否需要自动退订
        6. 检查并触发提醒事项
        """
        # 检查插件是否已停止（框架禁用插件时会调用terminate设置此标志）
        if self._stopped:
            return
        
        if not self.cfg.get("enable", True):
            return
        
        # 从配置同步订阅状态（实现配置热重载，静默模式，只在有变化时打印日志）
        self._sync_subscribed_users_from_config(silent=True)

        tz = self._get_cfg("basic_settings", "timezone") or None
        now = _now_tz(tz)
        quiet = self._get_cfg("basic_settings", "quiet_hours", "") or ""
        hist_n = int(self._get_cfg("advanced", "history_depth") or 8)
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
                logger.debug(f"[Conversa] 向后兼容：为 {umo} 初始化 next_idle_ts = {st.next_idle_ts}")
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
        # 手动退订的用户不会被自动退订逻辑处理
        if profile.manual_unsubscribe:
            return False
        
        max_days = int(self._get_cfg("basic_settings", "max_no_reply_days") or 0)
        if max_days <= 0:
            return False

        if st.last_user_reply_ts > 0:
            last_reply = datetime.fromtimestamp(st.last_user_reply_ts, tz=now.tzinfo)
            days_since_reply = (now - last_reply).days

            if days_since_reply >= max_days:
                profile.subscribed = False
                profile.auto_unsubscribed = True  # 标记为自动退订
                profile.manual_unsubscribe = False  # 确保不是手动退订状态
                logger.info(f"[Conversa] 自动退订 {umo}：用户{days_since_reply}天未回复（可自动重新激活）")
                self._save_user_data()
                self._sync_subscribed_users_to_config()  # 同步到配置文件
                return True

        return False

    async def _check_reminders(self, now: datetime, tz: Optional[str], reply_interval: int):
        """检查并触发到期的提醒事项"""
        if not bool(self._get_cfg("reminders_settings", "enable_reminders", True)):
            return
        
        fired_ids = []
        for rid, r in list(self._reminders.items()):
            try:
                # 检查用户订阅状态
                profile = self._user_profiles.get(r.umo)
                if not profile or not profile.subscribed:
                    continue
                
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
        
        v3 改造：通过官方 CronMessageEvent + build_main_agent 走合规 Agent Pipeline，
        支持完整的工具调用、人格注入、历史管理。
        当框架 API 不可用时降级到旧的 provider.text_chat 方式。
        """
        try:
            # --- 格式化 prompt（保留原有的占位符替换逻辑） ---
            now = _now_tz(tz)
            time_fmt = self._get_cfg("basic_settings", "time_format") or "%Y-%m-%d %H:%M"
            now_str = now.strftime(time_fmt)

            st = self._states.get(umo)
            time_since_last_chat = "未知"
            if st and st.last_user_reply_ts > 0:
                time_delta = now.timestamp() - st.last_user_reply_ts
                time_since_last_chat = _format_time_delta(time_delta)

            last_user, last_ai = await self._get_last_messages(umo)

            if prompt_template:
                try:
                    prompt = prompt_template.format(
                        now=now_str,
                        last_user=last_user,
                        last_ai=last_ai,
                        umo=umo,
                        time_since_last_chat=time_since_last_chat
                    )
                except KeyError as e:
                    logger.warning(f"[Conversa] prompt 模板格式化失败，未知占位符: {e}")
                    prompt = prompt_template
            else:
                prompt = "请自然地延续对话，与用户继续交流。"

            logger.info(f"[Conversa] 准备主动回复 {umo}")

            # --- 走官方 Agent Pipeline ---
            if HAS_AGENT_PIPELINE:
                response_text = await self._run_agent_pipeline(umo, prompt, tz)
            else:
                # 降级：旧版本框架不支持 CronMessageEvent
                response_text = await self._run_legacy_llm(umo, prompt)

            if not response_text:
                return False

            # 发送消息（如果 Agent 没有通过工具自行发送）
            if not getattr(self, '_last_cron_event_sent', False):
                await self._send_text(umo, response_text)
            logger.info(f"[Conversa] 已发送主动回复给 {umo}: {response_text[:50]}...")

            # --- 更新状态 ---
            now_ts = now.timestamp()
            if umo not in self._states:
                self._states[umo] = SessionState()
            st = self._states[umo]
            st.last_ts = now_ts
            st.enhancement_chain_count += 1
            st.last_proactive_reply_ts = now_ts
            await self._debounced_save_session_data()

            return True

        except Exception as e:
            logger.error(f"[Conversa] proactive error({umo}): {e}", exc_info=True)
            return False

    async def _run_agent_pipeline(self, umo: str, prompt: str, tz: Optional[str] = None) -> Optional[str]:
        """通过官方 CronMessageEvent + build_main_agent 执行 Agent Pipeline"""
        self._last_cron_event_sent = False

        session = MessageSession.from_str(umo)
        cron_event = CronMessageEvent(
            context=self.context,
            session=session,
            message=prompt,
        )

        # 构建 Agent 配置（与框架 cron 系统一致）
        config = MainAgentBuildConfig(
            tool_call_timeout=120,
            llm_safety_mode=False,
            streaming_response=False,
        )

        # 固定 provider（如果配置了）
        fixed_provider_id = self._get_cfg("advanced", "fixed_provider", "") or ""
        provider = None
        if fixed_provider_id:
            provider = self.context.get_provider_by_id(fixed_provider_id)

        # 人格覆盖（如果配置了，传给 ProviderRequest 的 system_prompt）
        persona_override = (self._get_cfg("advanced", "persona_override") or "").strip()
        req = None
        if persona_override:
            req = ProviderRequest()
            req.prompt = prompt
            req.system_prompt = persona_override

        result = await build_main_agent(
            event=cron_event,
            plugin_context=self.context,
            config=config,
            provider=provider,
            req=req,
        )

        if not result or not result.agent_runner:
            logger.warning(f"[Conversa] build_main_agent 返回空结果: {umo}")
            return None

        runner = result.agent_runner
        async for _ in runner.step_until_done(30):
            pass

        llm_resp = runner.get_final_llm_resp()
        if not llm_resp or not llm_resp.completion_text:
            logger.debug(f"[Conversa] Agent 无文本响应: {umo}")
            return None

        response_text = llm_resp.completion_text.strip()
        if not response_text:
            return None

        # 记录 Agent 是否已通过工具发送了消息
        self._last_cron_event_sent = getattr(cron_event, '_has_send_oper', False)

        # 保存对话历史（与框架 cron 系统一致）
        try:
            summary = f"[Conversa主动发起对话] {response_text[:100]}"
            await persist_agent_history(
                self.context.conversation_manager,
                event=cron_event,
                req=result.provider_request,
                summary_note=summary,
            )
        except Exception as e:
            logger.warning(f"[Conversa] persist_agent_history 失败: {e}")

        return response_text

    async def _run_legacy_llm(self, umo: str, prompt: str) -> Optional[str]:
        """降级方案：直接调用 provider.text_chat()（旧版本框架兼容）"""
        fixed_provider_id = self._get_cfg("advanced", "fixed_provider", "") or ""
        provider = None
        if fixed_provider_id:
            provider = self.context.get_provider_by_id(fixed_provider_id)
        if not provider:
            provider = self.context.get_using_provider(umo=umo)
        if not provider:
            logger.warning(f"[Conversa] provider missing for {umo}")
            return None

        self._last_cron_event_sent = False

        llm_resp = await provider.text_chat(
            prompt=prompt,
            contexts=[],
            system_prompt=""
        )
        text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""
        return text.strip() if text else None
    
    async def _proactive_reminder_reply(self, umo: str, reminder_content: str) -> bool:
        """
        执行由 AI 生成的主动提醒回复
        
        v3 改造：复用 _run_agent_pipeline / _run_legacy_llm，走合规调用。
        """
        try:
            tz = self._get_cfg("basic_settings", "timezone") or None
            now = _now_tz(tz)
            time_fmt = self._get_cfg("basic_settings", "time_format") or "%Y-%m-%d %H:%M"
            now_str = now.strftime(time_fmt)

            st = self._states.get(umo)
            time_since_last_chat = "未知"
            if st and st.last_user_reply_ts > 0:
                time_delta = now.timestamp() - st.last_user_reply_ts
                time_since_last_chat = _format_time_delta(time_delta)

            last_user, last_ai = await self._get_last_messages(umo)

            # 使用提醒 prompt 模板
            template = self._get_cfg("reminders_settings", "reminder_prompt_template") or "用户提醒：{reminder_content}"
            try:
                prompt = template.format(
                    reminder_content=reminder_content,
                    now=now_str,
                    umo=umo,
                    time_since_last_chat=time_since_last_chat,
                    last_user=last_user,
                    last_ai=last_ai
                )
            except KeyError as e:
                logger.warning(f"[Conversa] 提醒模板格式化失败，未知占位符: {e}，使用默认模板")
                prompt = f"用户提醒：{reminder_content}"

            logger.info(f"[Conversa] 触发 AI 提醒 for {umo}: {reminder_content}")

            # 走官方 Agent Pipeline 或降级
            if HAS_AGENT_PIPELINE:
                response_text = await self._run_agent_pipeline(umo, prompt, tz)
            else:
                response_text = await self._run_legacy_llm(umo, prompt)

            if not response_text:
                return False

            # 发送消息（如果 Agent 没有通过工具自行发送）
            if not getattr(self, '_last_cron_event_sent', False):
                await self._send_text(umo, response_text)
            logger.info(f"[Conversa] 已发送 AI 提醒给 {umo}: {response_text[:50]}...")

            # 更新状态
            if umo not in self._states:
                self._states[umo] = SessionState()
            st = self._states[umo]
            st.enhancement_chain_count += 1
            st.last_proactive_reply_ts = _now_tz(tz).timestamp()
            await self._debounced_save_session_data()

            return True

        except Exception as e:
            logger.error(f"[Conversa] proactive reminder error({umo}): {e}", exc_info=True)
            return False

    async def _add_message_pair_to_history(self, umo: str, conversation_id: str, conversation, user_prompt: str, assistant_response: str):
        """
        将消息对添加到对话历史（使用官方 API）
        
        注意：走 build_main_agent + persist_agent_history 的主动回复已经自动保存历史，
        此方法仅用于降级路径或其他需要手动追加历史的场景。
        """
        try:
            if not conversation_id:
                logger.warning("[Conversa] conversation_id 为空，无法更新历史")
                return

            conv_mgr = self.context.conversation_manager

            if HAS_NEW_MESSAGE_API:
                try:
                    user_msg = UserMessageSegment(content=[TextPart(text=user_prompt)])
                    assistant_msg = AssistantMessageSegment(content=[TextPart(text=assistant_response)])
                    await conv_mgr.add_message_pair(
                        cid=conversation_id,
                        user_message=user_msg,
                        assistant_message=assistant_msg,
                    )
                    logger.debug(f"[Conversa] 已添加消息对到历史: {conversation_id}")
                    return
                except Exception as e:
                    logger.warning(f"[Conversa] add_message_pair 失败: {e}")

            # 降级：使用 dict 格式
            await conv_mgr.add_message_pair(
                cid=conversation_id,
                user_message={"role": "user", "content": user_prompt},
                assistant_message={"role": "assistant", "content": assistant_response},
            )
            logger.debug(f"[Conversa] 已添加消息对到历史(dict): {conversation_id}")

        except Exception as e:
            logger.error(f"[Conversa] 添加消息对到历史失败: {e}", exc_info=True)

    async def _get_last_messages(self, umo: str) -> Tuple[str, str]:
        """从官方 conversation 历史中获取最近的 user 和 assistant 消息（供占位符使用）"""
        last_user = ""
        last_ai = ""
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return last_user, last_ai

            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if not conversation or not conversation.history:
                return last_user, last_ai

            history = json.loads(conversation.history) if isinstance(conversation.history, str) else conversation.history
            if not isinstance(history, list):
                return last_user, last_ai

            for msg in reversed(history):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                content = str(content)[:200]
                if role == "user" and not last_user:
                    last_user = content
                elif role == "assistant" and not last_ai:
                    last_ai = content
                if last_user and last_ai:
                    break
        except Exception as e:
            logger.debug(f"[Conversa] 获取最近消息失败: {e}")
        return last_user, last_ai

    def _apply_segmentation(self, text: str) -> list[str]:
        """应用分段回复逻辑（模拟 AstrBot 的分段正则处理）
        
        Returns:
            分段后的文本列表
        """
        try:
            # 获取分段配置
            seg_config = self.context.get_config().get("platform_settings", {}).get("segmented_reply", {})
            
            # 检查是否启用分段
            if not seg_config.get("enable", False):
                return [text]
            
            # 获取配置参数
            words_threshold = int(seg_config.get("words_count_threshold", 1000))
            regex_pattern = seg_config.get("regex", r"[^。！？\n]+[。！？\n]?")
            cleanup_rule = seg_config.get("content_cleanup_rule", "")
            
            # 如果文本过长，不分段（与 AstrBot 逻辑一致）
            if len(text) > words_threshold:
                return [text]
            
            # 应用分段正则
            segments = re.findall(regex_pattern, text, re.DOTALL | re.MULTILINE)
            
            if not segments:
                return [text]
            
            # 清理并过滤空段落
            result = []
            for seg in segments:
                if cleanup_rule:
                    seg = re.sub(cleanup_rule, "", seg)
                if seg.strip():
                    result.append(seg)
            
            return result if result else [text]
            
        except Exception as e:
            logger.warning(f"[Conversa] 分段处理失败，使用原始文本: {e}")
            return [text]

    async def _send_text(self, umo: str, text: str):
        """发送主动回复消息到指定会话"""
        try:
            # 检查 umo 是否缺少 session_id（例如：platform:MessageType:None）
            # 如果是，尝试从 conversation_manager 获取完整的 umo
            if umo.endswith(":None") or ":None" in umo:
                try:
                    conv_mgr = self.context.conversation_manager
                    # 尝试获取当前会话ID
                    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                    if curr_cid:
                        # 重新构造完整的 umo
                        # umo 格式通常是 platform:MessageType:session_id
                        parts = umo.split(":")
                        if len(parts) >= 2:
                            # 使用获取到的 conversation_id 替换 None
                            umo = f"{parts[0]}:{parts[1]}:{curr_cid}"
                            logger.debug(f"[Conversa] 修复 umo: {umo}")
                except Exception as e:
                    logger.warning(f"[Conversa] 尝试修复 umo 失败: {e}")
            
            # 应用分段逻辑
            segments = self._apply_segmentation(text)
            
            # 发送每个分段
            for segment in segments:
                message_chain = MessageChain().message(segment)
                await self.context.send_message(umo, message_chain)
                logger.debug(f"[Conversa] ✅ 消息片段已发送: {segment[:50]}...")
                
                # 如果有多个分段，添加短暂延迟（模拟分段回复的间隔）
                if len(segments) > 1:
                    await asyncio.sleep(1.5)
            
        except Exception as e:
            logger.error(f"[Conversa] ❌ 发送消息失败({umo}): {e}")
    
    async def _send_reminder_message(self, umo: str, text: str):
        """发送提醒消息到指定会话"""
        await self._send_text(umo, text)

    # 生命周期管理
    async def terminate(self):
        """插件销毁"""
        self._stopped = True  # 设置停止标志，让调度器循环退出
        
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass  # 预期的取消异常

        # 取消所有对话增强任务
        for umo, task in list(self._enhancement_tasks.items()):
            if task and not task.done():
                task.cancel()
        self._enhancement_tasks.clear()

        logger.info("[Conversa] Performing final data save before termination...")
        if self._save_user_data_task and not self._save_user_data_task.done():
            self._save_user_data_task.cancel()
        if self._save_session_data_task and not self._save_session_data_task.done():
            self._save_session_data_task.cancel()
        self._save_user_data()
        self._save_session_data()
        
        logger.info("[Conversa] 插件已停止")
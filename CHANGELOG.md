# Conversa 更新日志

## v3.0.0 (2026-05-28)

---

> ## ⚠️ 重要提醒：旧版用户请执行迁移指令
>
> 如果你从 v2.x 升级，且使用过 `/conversa remind` 设置过提醒，请在 AstrBot 聊天中执行：
>
> ```
> /conversa migrate-reminders
> ```
>
> **此指令会将你所有的旧提醒一次性迁移到 AstrBot 原生定时任务系统。旧数据保留不删除，可继续通过 /conversa remind 管理。可重复执行，已迁移的会自动跳过。**

---

### 核心改造：主动回复走官方 Agent Pipeline

v3 最大的变化是主动回复不再绕过框架。之前的版本直接调用 `provider.text_chat()` 裸调 LLM，导致没有 Agent 工具调用、没有其他插件 hook、没有框架级人格注入。

v3 改为使用 AstrBot 框架的 `CronMessageEvent` + `build_main_agent()` 走完整 Agent Pipeline，与框架自己的 cron 系统用的是同一套方案。

**改造前后对比：**

| 维度 | v2 | v3 |
|---|---|---|
| 主动回复调用方式 | `provider.text_chat()` 裸调 | `CronMessageEvent` + `build_main_agent()` |
| Agent 工具调用 | 不支持 | 完整支持 |
| 其他插件 hook | 不触发 | 正常触发 |
| 人格注入 | 手动获取，容易出错 | 框架自动处理 |
| 对话历史加载 | 三级降级手动解析 | 框架自动加载 |
| 对话历史保存 | 手动写占位符 | `persist_agent_history()` 自动保存 |
| 旧版本框架兼容 | - | 自动降级到 `provider.text_chat()` |

### 配置项整理

- 删除 `special` 配置组（3 个从未生效的死配置 + 1 个改造后不需要的清理正则）
- `special.provider` 迁移到 `advanced.fixed_provider`
- `persona_override`、`history_depth` 移入 `advanced` 高级设置组
- `reminders_settings`（命令式提醒）标记为待废弃，引导使用 AstrBot 原生定时提醒
- `daily_prompts` 标记为旧功能（仍有独特价值：对所有订阅用户触发）
- 更新各功能组的描述文案

### 删除的代码

删除了 8 个因走官方 Pipeline 而不再需要的方法（共约 220 行）：

- `_get_system_prompt()` — 框架自动处理人格
- `_safe_get_full_contexts()` 及其 4 个辅助方法 — 框架自动加载历史
- `_normalize_messages()` / `_extract_content_text()` — 消息格式兼容层
- `_clean_response_text()` — 走 Pipeline 后不需要手动清理插件标记

### 新增的代码

- `_run_agent_pipeline()` — 通过 `CronMessageEvent` + `build_main_agent()` 执行完整 Agent Pipeline
- `_run_legacy_llm()` — 旧版本框架降级方案
- `_get_last_messages()` — 从官方 conversation 历史提取最近消息（供占位符使用）
- `_migrate_config()` — 自动迁移旧版配置到新位置
- `_migrate_reminders_to_cron()` — 将旧版提醒迁移到 AstrBot 原生 cron 系统（管理员指令触发）
- `/conversa migrate-reminders` — 管理员迁移指令，幂等可重复执行

### 向后兼容

- 框架 API 不可用时自动降级到旧的 `provider.text_chat()` 方式
- 旧版配置（`special.provider`、`basic_settings.fixed_provider` 等）在启动时自动迁移
- `/conversa remind` 命令功能保留，但推荐使用 AstrBot 原生定时提醒
## v2.0.1 (2026-03-29)

### 🐛 Bug修复 

#### **修复了框架升级后插件参数对不上的问题**

## v2.0.0 (2026-03-21)

### ✨ 新功能

#### 1. **对话增强（短期随机追回复）**
- AI 正常回复用户后，有可配置概率在延迟一段时间后追加一条主动消息
- 支持配置：触发概率、最短/最长延迟（上限1800秒）、提示词模板
- **指数递减防刷屏**：连续插件主动回复时，概率按指数级衰减（默认每次降为10%），用户发真实消息后重置
- 用户在等待期间发新消息时自动取消追回复

#### 2. **Agent 订阅模式**
- 新增 `subscribe_mode: agent` 选项，AI 可通过 Agent 工具调用自动开启/关闭用户订阅
- 注册 `conversa_subscribe` LLM Tool，当用户表达"希望你主动找我聊天"时 AI 可自动开启
- 与手动 `/conversa watch` 完全兼容，互不冲突
- 兼容旧版 AstrBot（无 `llm_tool` API 时自动降级）

#### 3. **主动回复历史占位符**
- 主动回复写入对话历史时，用简短占位符替代完整提示词模板
- 默认占位符：`[Conversa主动发起对话]`，可在配置中自定义
- 大幅节省对话历史的 token 消耗

### 🔧 配置变更

- **新增** `basic_settings.proactive_history_placeholder`：主动回复历史占位符文本
- **新增** `enhancement` 配置段：对话增强全部配置（开关、概率、延迟、衰减系数、提示词模板）
- **变更** `basic_settings.subscribe_mode`：新增 `agent` 选项

## v1.4.5 (2026-02-24)

### 🐛 Bug修复 - 紧急修复

#### 1. **修复异步函数调用问题** (致命错误)
- **位置**: `main.py:580-581` (`_on_any_message` 方法)
- **问题**: 在异步函数中调用异步方法时忘记使用 `await`
  ```python
  # 错误 ❌
  self._debounced_save_session_data()
  self._debounced_save_user_data()
  
  # 正确 ✅
  await self._debounced_save_session_data()
  await self._debounced_save_user_data()
  ```
- **影响**: 
  - 导致插件在处理消息时抛出 `RuntimeWarning: coroutine was never awaited`
  - 框架检测到异常后自动终止并重新加载插件
  - 形成无限重启循环
- **根源**: 上一个推送的重构中引入的错误

#### 2. **修复生命周期问题**
- **位置**: `main.py:309-313`
- **问题**: 在同步的 `__init__` 中创建异步任务违反了框架生命周期规范
- **修复**: 使用框架提供的 `async def initialize()` 生命周期方法启动调度器
- **影响**: 解决了"无法启动主动回复"的问题

### 📝 技术细节
- **异步编程规范**: 在异步函数中调用异步函数必须使用 `await`
- **框架生命周期**: 
  - `__init__`: 同步初始化,不应创建异步任务
  - `initialize()`: 异步初始化,用于启动异步任务
  - `terminate()`: 异步清理,用于停止异步任务

## v1.4.4
- 之前的版本...

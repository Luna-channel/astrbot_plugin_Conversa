# Conversa 更新日志
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

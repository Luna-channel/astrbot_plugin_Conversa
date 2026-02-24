# Conversa 更新日志

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

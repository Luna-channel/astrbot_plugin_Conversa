# Conversa · AI 主动续聊插件 for AstrBot

> **作者**：柯尔 (Luna-channel)  
> **版本**：v1.2.0  
> **仓库**：<https://github.com/Luna-channel/astrbot_plugin_Conversa>  

Conversa 是一款为 AstrBot 设计的 AI 主动对话插件。它能够在会话沉寂一段时间后，像真人一样重新发起聊天，或者在每日的特定时间点送上问候，或以自然的方式进行定时提醒。

目前有bug，无法正确将主动回复的信息加入上下文，请注意。
---

## 📝 版本说明

本项目是 [astrbot_plugin_AIReplay](https://github.com/oyxning/astrbot_plugin_AIReplay) 的 v1.0 重构版本，修复了原版本的多个核心bug：
- 🔥 修复上下文丢失问题（多层降级策略，确保AI能记住对话历史）
- 🔥 修复 Persona 获取失败（5层降级策略，确保系统提示词正确加载）
- ✨ 新增 WebUI 订阅列表管理（可视化管理，支持双向同步）
- ⚡ 多用户触发优化（可配置间隔，避免触发API限流）
- 🛠️ 代码质量提升（完全符合 AstrBot 官方开发规范，添加完整文档）

---

## ✨ 更新日志

### **v1.2.0 (2025-11-01) - 测试版**

- **🔥 优化代码结构**：优化了一些历史遗留问题
- **📝 大幅增强 调试日志系统**：
  - 为上下文获取添加了详细的分步日志记录，包括策略选择、数据类型、长度等关键信息
  - 为人格获取添加了全程追踪日志，包括ID获取、对象提取、属性检查等每个步骤
  - 为消息标准化过程添加了详细记录，帮助识别数据格式问题
  - 主动回复时记录关键信息，便于问题排查
- **🛠️ 代码质量提升**：
  - **简化上下文获取**：从嵌套if-else优化为清晰的策略分层函数
  - **简化人格获取**：从5层降级优化为2层降级
  - **优化消息发送**：使用文档推荐的 `MessageChain().message()` 方式，符合AstrBot官方规范
  - **改进错误处理**：避免单个消息处理失败影响整体功能
- **⚡ 架构优化**：
  - 采用函数分解替代累赘的if-else嵌套，提高代码可维护性
  - 保持必要的稳定性功能（多重降级策略），同时大幅简化代码结构
  - 代码行数减少，可读性和维护性显著提升
- **⚠️ 已知问题**：插件主动发送的AI消息目前无法进入对话历史。

### **v1.1.0 (2025-10-29)**

- **新增 管理员权限系统**：引入基于 `event.role == "admin"` 的管理员识别机制，为关键的全局设置指令（如 `on/off`, `set quiet`, `set daily`, `set history`）增加了权限校验，非管理员无法使用。
- **重构 命令解析器**：全面重构了插件的命令解析逻辑，从模糊的 `in` 判断升级为精确的子命令匹配，修复了 `/cvs` 别名下部分指令无法触发的问题，并提升了代码的健壮性和可读性。
- **优化 配置文件结构**：对 `_conf_schema.json` 进行了多次迭代优化，使其在 WebUI 中的显示更加清晰、有条理，并将所有配置项整合到逻辑分组中。
- **优化 数据持久化**：将原本分散的 `user_profiles.json`, `reminders.json`, `session_states.json`, `context_cache.json` 四个数据文件整合为 `user_data.json` 和 `session_data.json` 两个文件，简化了数据管理。
- **修复 兼容性问题**：通过在 `requirements.txt` 中添加 `backports.zoneinfo`，确保插件在低于 Python 3.9 的环境中也能正常处理时区。
- **恢复 并发间隔机制**：恢复了 `reply_interval_seconds` 配置，允许在多次连续触发主动回复时（如多个用户同时满足条件），在每次回复间增加一个可配置的延迟，防止对 API 造成冲击。
- **代码质量提升**：
  - 修复了多处潜在的 bug，包括 `_safe_get_full_contexts` 方法中的拼写错误和多处缩进错误。
  - 移除了冗余的代码定义，提升了代码整洁度。
- **文档与帮助文本优化**：
  - 优化了 `/conversa help` 命令返回的帮助文本，使其描述更清晰、更人性化。

---

## ✨ 功能特性

- **延时问候**：在会话无新消息一段时间后，AI 会根据上下文，在合适的时机主动发起新话题。
- **每日问候**：支持配置最多三个每日定时任务，例如早安、午安、晚安问候。
- **智能提醒**：用户可以通过自然语言设置一次性或每日提醒，AI 会在指定时间以人性化的方式发出提醒。
- **灵活的订阅机制**：支持管理员手动配置，也支持用户通过指令自行订阅/退订服务，更可以开启自动订阅模式。
- **深度上下文感知**：采用多级降级策略，优先从 AstrBot 核心获取最完整的对话历史，确保主动发起的话题与当前语境高度相关。
- **人格继承**：完全继承并使用您为 AstrBot 配置的 AI 人格（System Prompt），确保 AI 的每一句话都符合其既定角色。
- **管理员权限控制**：关键的全局设置指令（如开关插件、设置免打扰时段）仅限管理员使用，保障配置安全。
- **高度可配置**：几乎所有功能均可通过插件的 WebUI 进行详细配置，包括各类提示词模板、触发时间、免打扰时段等。

## 📝 更新日志

### **v1.1.0 (2025-10-29)**

## 📦 安装

1. 从 Release 或本仓库获取插件包，解压到：  
   `AstrBot/data/plugins/astrbot_plugin_conversa/`
2. 启动（或重启）AstrBot。
3. 进入 WebUI → **插件** → 启用 **conversa**。
4. （可选）在插件配置页完成参数设置。

目录结构示例：

```
AstrBot/
└─ data/
   └─ plugins/
      └─ astrbot_plugin_conversa/
         ├─ main.py
         ├─ _conf_schema.json
         ├─ metadata.yaml
         ├─ README.md
         └─ requirements.txt
```

插件会在 `AstrBot/data/` 目录下创建自己的数据文件夹：

```
AstrBot/
└─ data/
   └─ plugin_data/
      └─ astrbot_plugin_conversa/
         ├─ state.json
         └─ reminders.json
```

---

## 🧩 配置项（`_conf_schema.json`）

| 键名 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `enable` | bool | `true` | 启用/停用插件。 |
| `timezone` | string | `""` | IANA 时区（示例 `Asia/Shanghai`、`America/Los_Angeles`）。为空使用系统时区。 |
| `idle_after_minutes` | int | `1200` | 延时问候基准时间（分钟）。实际触发时间为"基准 ± 随机波动"。 |
| `daily.time1` | string | `""` | 每日触发时间 1（`HH:MM`，24h）。 |
| `daily.time2` | string | `""` | 每日触发时间 2（`HH:MM`）。若与 `time1` 相同会自动 **+1 分钟** 错峰。 |
| `quiet_hours` | string | `""` | 免打扰时段（`HH:MM-HH:MM`），支持跨天。免打扰内不主动触发。 |
| `history_depth` | int | `8` | 携带最近聊天条数（从当前会话历史提取）。 |
| `idle_prompt_templates` | list | `[内置...` | **【v1.1 新增】**【间隔触发】的提示词模板列表（N分钟未回复时触发，从中随机选择）。 |
| `daily_prompts` | object | `{...}` | **【v1.1 新增】**【每日定时触发】的三个时间点及其专属提示词。 |
| `persona_override` | text | `""` | 覆盖默认人格的 System Prompt（留空使用当前会话人格）。 |
| `max_no_reply_days` | int | `7` | 用户无回复多少天后自动退订（0表示不自动退订）。 |
| `debug_mode` | bool | `false` | 调试模式：显示主动回复的完整上下文内容。 |
| `append_time_field` | bool | `false` | 在主动消息前追加时间字段。 |
| `time_format` | string | `"%Y-%m-%d %H:%M"` | 时间字段格式（Python `strftime`）。 |
| `subscribe_mode` | string(`manual`/`auto`) | `manual` | 订阅方式：`manual` 需要手动 `/conversa watch`；`auto` 有互动即自动纳入。 |
| `reply_interval_seconds` | int | `20` | 多用户同时触发时的间隔秒数（避免API限流，推荐10-20）。 |
| `subscribed_users` | list | `[]` | 订阅用户ID列表，支持在 WebUI 直接添加/删除。 |
| `reminder_prompt_template` | text | `[见下文]` | **【v1.1 新增】** AI 主动提醒的提示词模板。必须包含 `{reminder_content}` 占位符。 |
| `_special.provider` | string | `""` | （可选）固定使用的 Provider ID，留空按会话默认。 |
| `_special.persona` | string | `""` | （可选）固定人格 ID，留空按会话默认。 |

### 内置提示词示例

插件内置了两个优化的**间隔触发**提示词模板 (`idle_prompt_templates`)：

**提示词1 - 时间导向型：**
```text
【任务：这是一条静默提醒，专门用于触发你在【一对一私聊】场景下的主动回复，请你根据下列行动指南，主动继续与用户的对话。在任何情况下，都绝对不能透露你收到了"系统提醒"或"任务指令"。你的行为必须完全符合你的人设，就像一个真正的人在和朋友私聊。行动指南：现在的时间：{now}；根据这个时间对用户进行一些符合上述聊天记录内容或生活化的问候。】
```

**提示词2 - 进展导向型：**
```text
【任务：这是一条静默提醒，专门用于触发你在【一对一私聊】场景下的主动回复，请你根据下列行动指南，主动继续与用户的对话。在任何情况下，都绝对不能透露你收到了"系统提醒"或"任务指令"。你的行为必须完全符合你的人设，就像一个真正的人在和朋友私聊。行动指南：分析之前的聊天，注意到距离上次聊天已经过了一段时间，自然的询问事情的进展或刚才聊过的话题中的某一个】
```

### 每日定时提示词
`daily_prompts` 中的 `prompt1`, `prompt2`, `prompt3` 默认为简单的早安、晚安问候，你可以根据需要自定义。
- 如果 `timeX` 或 `promptX` 未配置，则该时段不会触发。

### 自定义提示词占位符

所有提示词模板都支持以下占位符：
- `{now}` - 当前时间
- `{last_user}` - 用户最后一条消息
- `{last_ai}` - AI最后一条回复
- `{umo}` - 会话ID

---

## 🚀 快速上手

1. **订阅会话**（两种方式任选其一）：

   **方式A：使用命令**（在会话中发送）
   ```
   /conversa on
   /conversa watch
   ```
   
   **方式B：在 WebUI 配置** ⭐ 推荐
   - 进入插件配置页面
   - 找到 `订阅用户列表` 字段
   - 点击 `添加`，输入用户ID（如 `12345678`）
   - 点击 `保存`

2. 设定触发方式（在 WebUI 或使用命令）：

- **间隔触发**：
  - WebUI: 找到 `idle_after_minutes` 设置延时基准时间。
  - 命令: `/conversa set after 0.75` (设置45分钟后触发)

- **每日触发**：
  - WebUI: 找到 `daily_prompts`，设置 `time1`, `time2`, `time3` 的时间 (HH:MM)，并修改对应的提示词。
  - 命令:
    ```
    /conversa set daily1 09:00
    /conversa set daily2 13:00
    /conversa set daily3 22:00
    ```

3. （可选）设置免打扰：  
   ` /conversa set quiet 23:00-07:00 `
   
4. （可选）管理提示词：
   - **间隔触发**的提示词在 WebUI 的 `idle_prompt_templates` 中管理。
   - **每日触发**的提示词在 WebUI 的 `daily_prompts` 中管理。

---

## 💬 指令全集 (`/conversa`)

- 基础
  - `/conversa help` — 显示帮助信息
  - `/conversa debug` — 显示调试信息
  - `/conversa on` / `/conversa off` — (管理员)启用/停用插件
  - `/conversa watch` / `/conversa unwatch` — 订阅/退订当前会话
- 触发设置
  - `/conversa set after <小时>` — 设置专属延时问候时间（最低0.5小时）
  - `/conversa set daily[1-3] <HH:MM>` — (管理员)设置每日触发时间 1/2/3
  - `/conversa set quiet <HH:MM-HH:MM>` — (管理员)设置免打扰时段（可跨天）
  - `/conversa set history <N>` — (管理员)设置携带最近聊天条数
- 提醒
  - `/conversa remind add <YYYY-MM-DD HH:MM> <内容>` — 新增**一次性**提醒
  - `/conversa remind add <HH:MM> <内容>` — 新增**每日**提醒
  - `/conversa remind list` — 查看提醒列表
  - `/conversa remind del <ID>` — 删除提醒

---

## 🧠 工作原理（简述）

- **调度循环**：插件内部维护一个 ~30 秒的调度循环 tick：
  1. 若 `enable=false`，跳过。
  2. 读取时区、免打扰、历史深度与每日时间点。
  3. 对每个**已订阅**会话：
     - 若当前处于免打扰，跳过。
     - 检查是否需要自动退订（用户超过指定天数未回复）。
     - 若距离最后消息已超过用户设定的延时时间，触发"间隔续聊"。
     - 若当前分钟命中 `daily.time1/time2`，触发"每日续聊"。
     - 利用"上次触发标签"去重（同一分钟不重复）。
  4. 扫描**提醒**：到点后调用 **AI 生成提醒内容**并发送；每日提醒按 HH:MM 命中即发。
- **智能退订**：用户超过 `max_no_reply_days` 天未回复时自动退订，用户主动回复时自动重新激活。
- **随机提示词**：从 `custom_prompts` 列表中随机选择一个提示词模板。
- **上下文拼接**：从 `ConversationManager` 取会话历史；若不可用则退化使用本插件的轻量历史缓存。
- **人格策略**：默认沿用当前会话人格，可通过 `persona_override` 或 `_special.persona` 覆盖。
- **Provider 选择**：默认使用会话当前 Provider，可通过 `_special.provider` 固定。

---

## 🧪 使用建议

- **避免骚扰**：生产环境建议维持 `subscribe_mode=manual`，只对明确订阅的会话主动续聊。  
- **智能退订**：合理设置 `max_no_reply_days`，避免骚扰不活跃用户。
- **时间与时区**：跨时区部署时建议显式设置 `timezone`，并在 UI 中确认每日触发时刻。  
- **提示词管理**：使用 `/conversa prompt` 命令管理多个提示词，让AI回复更加多样化。
- **历史深度**：`history_depth` 不宜过大，以免模型成本上涨或上下文"漂移"。  

---

## 🔧 故障排查

- **没有主动消息？**
  - 检查 `/conversa show` 是否显示 `subscribed=true`、`enable=true`。
  - 目标平台是否支持“主动消息”？（以 AstrBot 支持为准）
  - 处于免打扰时段？触发时刻是否刚好被挡住？
  - `after_last_msg_minutes=0` 且未设置 `daily.time1/time2`？
  - `timezone` 是否正确？
- **续聊内容空白或很机械？**
  - 使用 `/conversa prompt list` 查看当前提示词。
  - 使用 `/conversa prompt add` 添加更多样化的提示词。
  - 提高或降低 `history_depth`，并检查是否携带到上一轮用户与 AI 内容。
  - 考虑设定更贴合任务的人格（`persona_override`）。
- **提醒未触发？**
  - 一次性提醒使用本地时间字符串对比（精确到分钟）；检查格式与分钟级命中。  
  - 每日提醒需在命中 `HH:MM` 时刻才触发。
- **提醒内容不智能？**
  - 在 WebUI 配置中，修改 `reminder_prompt_template` 提示词，引导 AI 更好地组织语言。

---

## 📄 示例

**设置"45分钟延时问候"，每日 09:00/20:00 定时问候**

```
/conversa on
/conversa watch
/conversa set after 0.75
/conversa set daily1 09:00
/conversa set daily2 20:00
```

**注意**：提示词模板需要在 WebUI 的 `idle_prompt_templates` 和 `daily_prompts` 中配置。

**添加提醒**

```
/conversa remind add 2025-10-22 09:30 项目早会
/conversa remind add 21:45 休息眼睛 daily
```

---

## 🗂️ 数据与持久化

- 插件数据目录：`AstrBot/data/plugin_data/astrbot_plugin_conversa/`
  - `state.json`：会话订阅状态、上次触发标签等。
  - `reminders.json`：提醒列表。

> 数据存储在全局 `plugin_data` 目录下，遵循AstrBot插件开发规范。文件仅存储必要元数据，不保存模型输出。**插件卸载时会自动清理所有数据文件**，请注意服务器文件权限和备份策略。

---

## 🤝 贡献

欢迎 Issue 与 PR！
- Repo：<https://github.com/Luna-channel/astrbot_plugin_Conversa>
- 建议方向：多时区会话粒度覆盖、更多重复触发保护策略、可视化提醒管理、更多 Scheduler 精度选项。

---

## 🧾 版本信息

**当前版本：v1.2.0** ⭐

### v1.0.0 主要特性

- ✅ 定时/间隔主动续聊（支持间隔触发和每日定时）
- ✅ 智能上下文记忆（多策略降级获取，确保AI记住对话历史）
- ✅ 人格系统支持（5层降级策略，确保系统提示词正确加载）
- ✅ 订阅用户 WebUI 管理（支持直接添加/删除，与命令双向同步）
- ✅ 随机提示词选择（支持多个模板和占位符）
- ✅ 智能自动退订（避免骚扰不活跃用户）
- ✅ 免打扰时段（支持跨天设置）
- ✅ 定时提醒功能（一次性/每日提醒）
- ✅ 调试模式（查看完整上下文）
- ✅ 多用户触发优化（可配置间隔，避免API限流）

---

## 📜 许可证

MIT

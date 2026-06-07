# Reply Gate 群聊回复门控插件

Reply Gate 是一个用于 AstrBot 群聊主动接话的回复门控插件。

它不替代 AstrBot 的主回复模型，而是在回复链路前后各增加一道模型判断：先判断当前群聊消息是否适合让 AstrBot 接话；AstrBot 生成候选回复后，再判断这条候选回复是否符合当前群聊氛围，只有通过审查才发送。

## 工作流程

```text
接收到群聊消息
-> Reply Gate 调用判断模型，输出 continue/no_reply
-> continue 时交给 AstrBot 主流程生成候选回复
-> Reply Gate 把候选回复作为“待发送”消息插入群聊上下文末尾
-> Reply Gate 再次调用判断模型，输出 send/cancel
-> send 才发送，cancel 则取消发送
```

## 功能特点

- 模型直接判断是否接话，不再依赖回复阈值、权重或综合评分。
- 判断人格与 AstrBot 主人格分离，不会改动 AstrBot 的角色设定。
- 接话判断提示词、发送前审查提示词、判断人格提示词都可以在插件配置中自定义。
- 回复仍由 AstrBot 主流程生成，保留 AstrBot 的主模型、人格、记忆、工具和装饰流程。
- 发送前会把候选回复临时追加到群聊上下文末尾，再判断它是否自然、合适、符合氛围。
- 支持丢弃过期候选回复，避免新消息已经改变话题后又发送旧回复。
- 支持群聊白名单、最短回复间隔、上下文条数和 JSON 重试次数配置。

## 模型输出格式

接话判断模型必须输出 JSON：

```json
{
  "action": "continue 或 no_reply",
  "reasoning": "简短说明为什么这样判断",
  "confidence": 0.0
}
```

发送前审查模型必须输出 JSON：

```json
{
  "action": "send 或 cancel",
  "reasoning": "简短说明为什么发送或取消"
}
```

如果判断模型返回无效 JSON，插件会按配置重试。最终仍失败时，接话判断会保守处理为 `no_reply`，发送前审查会保守处理为 `cancel`。

## 配置说明

必要配置：

- `enable_heartflow`：启用 Reply Gate。字段名沿用旧版本，保留兼容。
- `judge_provider_name`：判断模型提供商，建议使用响应快、成本低的小模型。

常用配置：

- `judge_persona_prompt`：判断模型人格提示词，只影响接话判断和发送前审查。
- `reply_decision_prompt`：接话判断提示词模板。
- `reply_review_prompt`：发送前审查提示词模板。
- `judge_context_count`：传给判断模型的群聊上下文条数。
- `context_messages_count`：基础上下文消息数量。
- `min_reply_interval_seconds`：两次主动触发之间的最短间隔。
- `drop_stale_replies`：是否丢弃过期候选回复。
- `judge_max_retries`：接话判断 JSON 解析失败时的最大重试次数。
- `review_max_retries`：发送前审查 JSON 解析失败时的最大重试次数。

群聊范围配置：

- `whitelist_enabled`：启用群聊白名单。
- `chat_whitelist`：允许触发 Reply Gate 的群聊 sid 列表，可通过 `/sid` 获取。

状态配置：

- `energy_decay_rate`：发送通过后精力下降幅度。
- `energy_recovery_rate`：不回复或取消发送时精力恢复幅度。

精力状态会进入判断上下文，但不会再参与阈值打分。

## 提示词占位符

`reply_decision_prompt` 可用占位符：

- `{judge_persona_prompt}`：插件配置的判断人格。
- `{chat_context}`：群聊状态摘要。
- `{judge_context_count}`：判断上下文条数。
- `{recent_messages}`：最近群聊记录。
- `{last_bot_reply}`：上次机器人回复。
- `{sender_name}`：当前消息发送者昵称。
- `{sender_id}`：当前消息发送者 ID。
- `{current_message}`：当前待判断消息。
- `{current_time}`：当前时间。
- `{chat_id}`：当前群聊统一会话 ID。
- `{self_id}`：机器人自身 ID。
- `{minutes_since_last_reply}`：距离上次发送通过的回复过去了多少分钟。

`reply_review_prompt` 可用占位符：

- `{judge_persona_prompt}`：插件配置的判断人格。
- `{recent_messages_with_candidate}`：最近群聊记录，并在末尾追加候选回复。
- `{candidate_reply}`：AstrBot 生成的候选回复。
- `{decision_reasoning}`：第一次接话判断的理由。
- `{chat_id}`：当前群聊统一会话 ID。
- `{self_id}`：机器人自身 ID。
- `{current_time}`：当前时间。

## 管理命令

推荐命令：

- `/reply_gate`：查看当前群聊的门控状态。
- `/reply_gate_reset`：重置当前群聊的状态统计。
- `/reply_gate_cache`：查看系统提示词缓存状态。
- `/reply_gate_cache_clear`：清除系统提示词缓存。

兼容旧命令：

- `/heartflow`
- `/heartflow_reset`
- `/heartflow_cache`
- `/heartflow_cache_clear`

## 调试建议

- 如果完全不回复，先检查 `enable_heartflow`、`judge_provider_name` 和白名单配置。
- 如果回复太频繁，调高 `min_reply_interval_seconds`，或让 `reply_decision_prompt` 更克制。
- 如果经常生成后又被取消，检查 `reply_review_prompt` 是否过严，或检查 AstrBot 主人格/主模型回复质量。
- 如果模型经常返回非 JSON，提高 `judge_max_retries`、`review_max_retries`，并强化提示词里的 JSON 输出要求。

## 许可证

本插件遵循原项目许可证。

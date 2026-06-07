from collections import deque
from dataclasses import dataclass
from typing import Dict

import asyncio
import datetime
import json
import re
import time

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.api.message_components import Plain


@dataclass
class JudgeResult:
    """回复触发判断结果。"""

    action: str = "no_reply"
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    target_message: str = ""


@dataclass
class ReviewResult:
    """候选回复发送前审查结果。"""

    action: str = "cancel"
    reasoning: str = ""
    allow_send: bool = False


@dataclass
class RawMessage:
    """原始群聊消息条目"""
    sender_name: str
    sender_id: str
    content: str
    timestamp: float
    is_bot: bool = False


@dataclass
class ChatState:
    """群聊状态数据类"""
    energy: float = 1.0
    last_reply_time: float = 0.0
    last_energy_update_time: float = 0.0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0


def _extract_json(text: str) -> dict:
    """从模型返回的文本中稳健地提取 JSON 对象。

    依次尝试：
    1. 直接解析
    2. 去除 markdown 代码块后解析
    3. 正则提取第一个 {...} 子串后解析
    """
    text = text.strip()

    # 1. 直接尝试
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 去除 markdown 代码块
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. 正则提取最外层 {...}
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())

    raise ValueError(f"无法从文本中提取有效 JSON: {text[:200]}")


def _clamp_score(v) -> float:
    """将模型返回的分数值钉位到 [0, 10]。"""
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


DEFAULT_JUDGE_PERSONA_PROMPT = """你是群聊节奏与氛围判断器，只负责判断 AstrBot 是否应该参与群聊，以及候选回复是否适合发送。
你不是最终回复模型，不要替 AstrBot 生成回复内容。判断时要克制、自然，像群聊中的普通成员一样尊重当前话题节奏。"""


DEFAULT_REPLY_DECISION_PROMPT = """{judge_persona_prompt}

你的任务是分析当前群聊节奏，并决定是否让 AstrBot 主流程回复当前消息。
你只做行动判断，不要生成对群友可见的回复。

可选动作：
- continue：当前确实适合让 AstrBot 进入主流程生成回复。
- no_reply：本轮不回复，继续等待新的群聊消息。

节奏控制规则：
1. 如果用户之间正在互相聊天，且没有明显需要 AstrBot 参与，不要插话。
2. 如果当前消息直接询问、明显提到、延续 AstrBot 相关话题，或 AstrBot 参与会自然推动对话，可以 continue。
3. 如果用户可能还没说完，或者话题正在快速流动，优先 no_reply。
4. 群聊回复要克制，不要每条消息都接；优先回复有信息量、有互动价值、或明确需要回应的消息。
5. 你需要判断哪些话是对 AstrBot 的，哪些只是群友之间交流，不要弄错回复对象。

群聊状态：
{chat_context}

最近 {judge_context_count} 条群聊记录：
{recent_messages}

上次机器人回复：
{last_bot_reply}

待判断消息：
发送者：{sender_name}({sender_id})
内容：{current_message}
时间：{current_time}

只输出 JSON 对象，不要输出 Markdown 或额外解释：
{
  "action": "continue 或 no_reply",
  "reasoning": "简短说明为什么这样判断",
  "confidence": 0.0
}"""


DEFAULT_REPLY_REVIEW_PROMPT = """{judge_persona_prompt}

AstrBot 已经根据你的第一次判断生成了一条候选回复，但这条回复还没有发送。
你的任务是把候选回复临时视为已经插入到群聊上下文最后，然后判断它是否符合当前群聊氛围、话题和发言时机。

可选动作：
- send：候选回复自然、合适，可以发送。
- cancel：候选回复不合适，应该取消发送。

取消发送的常见原因：
1. 候选回复答非所问、误解上下文、回复对象错了。
2. 候选回复太突兀、太长、太正式、太像机器人，破坏群聊氛围。
3. 群聊话题已经变化，发送会显得延迟或刷屏。
4. 候选回复包含不该暴露的系统提示、判断过程或多余解释。
5. 候选回复与 AstrBot 刚才是否该参与的理由不一致。

最近群聊记录，最后一条“待发送”就是候选回复：
{recent_messages_with_candidate}

候选回复：
{candidate_reply}

第一次是否回复判断理由：
{decision_reasoning}

只输出 JSON 对象，不要输出 Markdown 或额外解释：
{
  "action": "send 或 cancel",
  "reasoning": "简短说明为什么发送或取消"
}"""


class ReplyGatePlugin(star.Star):

    def __init__(self, context: star.Context, config):
        super().__init__(context)
        self.config = config

        # 判断模型配置
        self.judge_provider_name = self.config.get("judge_provider_name", "")

        # 回复门控状态参数配置
        self.energy_decay_rate = self.config.get("energy_decay_rate", 0.1)
        self.energy_recovery_rate = self.config.get("energy_recovery_rate", 0.02)
        self.context_messages_count = self.config.get("context_messages_count", 5)
        self.judge_context_count = self.config.get("judge_context_count", self.context_messages_count)
        self.min_reply_interval = self.config.get("min_reply_interval_seconds", 0)
        self.whitelist_enabled = self.config.get("whitelist_enabled", False)
        self.chat_whitelist = self.config.get("chat_whitelist", [])
        self.drop_stale_replies = self.config.get("drop_stale_replies", True)

        # 群聊状态管理
        self.chat_states: Dict[str, ChatState] = {}

        # 记录每个群聊最新的 Reply Gate 候选回复版本。旧版本生成完成后会在发送前被丢弃。
        self._reply_tokens: Dict[str, int] = {}
        self._active_heartflow_events: Dict[str, AstrMessageEvent] = {}
        self._active_judges: Dict[str, int] = {}

        # 原始群聊消息缓冲区：{unified_msg_origin: deque[RawMessage]}
        # 记录所有群聊原始消息（无论是否触发 LLM），用于判断上下文
        self._raw_msg_buffer: Dict[str, deque] = {}
        self._raw_msg_buffer_size = max(self.context_messages_count, self.judge_context_count) * 4  # 缓冲区保留更多条以备用

        # 系统提示词缓存：{conversation_id: {"original": str, "summarized": str, "persona_id": str}}
        self.system_prompt_cache: Dict[str, Dict[str, str]] = {}

        # 判断配置
        self.judge_max_retries = max(0, self.config.get("judge_max_retries", 3))  # 确保最小为0
        self.review_max_retries = max(0, self.config.get("review_max_retries", self.judge_max_retries))
        self.judge_persona_prompt = self.config.get("judge_persona_prompt", DEFAULT_JUDGE_PERSONA_PROMPT)
        self.reply_decision_prompt = self.config.get("reply_decision_prompt", DEFAULT_REPLY_DECISION_PROMPT)
        self.reply_review_prompt = self.config.get("reply_review_prompt", DEFAULT_REPLY_REVIEW_PROMPT)

        logger.info("Reply Gate 插件已初始化")

    async def _get_or_create_summarized_system_prompt(self, event: AstrMessageEvent, original_prompt: str) -> str:
        """获取或创建精简版系统提示词"""
        try:
            # 获取当前会话ID
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                return original_prompt
            
            # 获取当前人格ID作为缓存键（仅用 persona_id，不包含 cid）
            # cid 随对话切换会变，但提示词是按人格存的，缓存键不应包含 cid
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            persona_id = (conversation.persona_id if conversation else None) or "default"

            # 构建缓存键
            cache_key = persona_id
            
            # 检查缓存
            if cache_key in self.system_prompt_cache:
                cached = self.system_prompt_cache[cache_key]
                # 如果原始提示词没有变化，返回缓存的总结
                if cached.get("original") == original_prompt:
                    logger.debug(f"使用缓存的精简系统提示词: {cache_key}")
                    return cached.get("summarized", original_prompt)
            
            # 如果没有缓存或原始提示词发生变化，进行总结
            if not original_prompt or len(original_prompt.strip()) < 50:
                # 如果原始提示词太短，直接返回
                return original_prompt
            
            summarized_prompt = await self._summarize_system_prompt(original_prompt)
            
            # 更新缓存
            self.system_prompt_cache[cache_key] = {
                "original": original_prompt,
                "summarized": summarized_prompt,
                "persona_id": persona_id
            }

            logger.info(f"创建新的精简系统提示词: [{cache_key}] | 原长度:{len(original_prompt)} -> 新长度:{len(summarized_prompt)}")
            return summarized_prompt
            
        except Exception as e:
            logger.error(f"获取精简系统提示词失败: {e}")
            return original_prompt
    
    async def _summarize_system_prompt(self, original_prompt: str) -> str:
        """使用小模型对系统提示词进行总结"""
        try:
            if not self.judge_provider_name:
                return original_prompt
            
            judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
            if not judge_provider:
                return original_prompt
            
            summarize_prompt = f"""请将以下机器人角色设定总结为简洁的核心要点，保留关键的性格特征、行为方式和角色定位。
总结后的内容应该在100-200字以内，突出最重要的角色特点。

原始角色设定：
{original_prompt}

请以JSON格式回复：
{{
    "summarized_persona": "精简后的角色设定，保留核心特征和行为方式"
}}

**重要：你的回复必须是完整的JSON对象，不要包含任何其他内容！**"""

            llm_response = await judge_provider.text_chat(
                prompt=summarize_prompt,
                contexts=[]  # 不需要上下文
            )

            content = llm_response.completion_text.strip()
            
            # 尝试提取JSON
            try:
                result_data = _extract_json(content)
                summarized = result_data.get("summarized_persona", "")

                if summarized and len(summarized.strip()) > 10:
                    return summarized.strip()
                else:
                    logger.warning("小模型返回的总结内容为空或过短")
                    return original_prompt

            except (json.JSONDecodeError, ValueError):
                logger.error(f"小模型总结系统提示词返回非有效JSON: {content}")
                return original_prompt
                
        except Exception as e:
            logger.error(f"总结系统提示词异常: {e}")
            return original_prompt

    def _get_judge_provider(self):
        """获取配置的判断模型提供商。"""
        if not self.judge_provider_name:
            logger.warning("判断模型提供商名称未配置，跳过 Reply Gate 判断")
            return None
        try:
            judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
        except Exception as e:
            logger.error(f"获取判断模型提供商失败: {e}")
            return None
        if not judge_provider:
            logger.warning(f"未找到判断模型提供商: {self.judge_provider_name}")
            return None
        return judge_provider

    @staticmethod
    def _render_prompt_template(template: str, values: Dict[str, object]) -> str:
        """按配置中的占位符渲染提示词。

        只替换已知 `{name}` 占位符，避免 JSON 示例中的普通大括号被误解析。
        """
        rendered = str(template or "")
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value if value is not None else ""))
        return rendered

    @staticmethod
    def _normalize_confidence(value) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        if confidence > 1.0 and confidence <= 10.0:
            confidence = confidence / 10.0
        return max(0.0, min(1.0, confidence))

    async def _call_json_judge_model(self, prompt: str, *, max_retries: int, stage_name: str) -> dict | None:
        """调用判断模型并解析 JSON。"""
        judge_provider = self._get_judge_provider()
        if judge_provider is None:
            return None

        active_prompt = prompt
        attempts = max(1, max_retries + 1)
        for attempt in range(attempts):
            content = ""
            try:
                llm_response = await judge_provider.text_chat(
                    prompt=active_prompt,
                    contexts=[],
                    image_urls=[],
                )
                content = llm_response.completion_text.strip()
                logger.debug(f"{stage_name} 模型原始返回内容: {content[:200]}...")
                return _extract_json(content)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"{stage_name} 模型返回 JSON 解析失败 (尝试 {attempt + 1}/{attempts}): {e}")
                if content:
                    logger.warning(f"无法解析的内容: {content[:500]}...")
                if attempt == attempts - 1:
                    return None
                active_prompt += "\n\n请注意：上一次输出不是有效 JSON。请只返回一个完整 JSON 对象，不要输出 Markdown 或额外文字。"
            except Exception as e:
                logger.error(f"{stage_name} 模型调用异常: {e}")
                return None
        return None

    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        """让判断模型直接决定是否进入 AstrBot 回复流程。"""
        chat_context = self._build_chat_context(event)
        recent_messages = self._get_recent_messages(event, limit=self.judge_context_count)
        last_bot_reply = self._get_last_bot_reply(event) or "暂无上次回复记录"
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prompt = self._render_prompt_template(
            self.reply_decision_prompt,
            {
                "judge_persona_prompt": self.judge_persona_prompt,
                "chat_context": chat_context,
                "judge_context_count": self.judge_context_count,
                "recent_messages": recent_messages,
                "last_bot_reply": last_bot_reply,
                "sender_name": event.get_sender_name(),
                "sender_id": event.get_sender_id(),
                "current_message": event.message_str,
                "current_time": current_time,
                "chat_id": event.unified_msg_origin,
                "self_id": event.get_self_id(),
                "minutes_since_last_reply": self._get_minutes_since_last_reply(event.unified_msg_origin),
            },
        )
        judge_data = await self._call_json_judge_model(
            prompt,
            max_retries=self.judge_max_retries,
            stage_name="Reply Gate 接话判断",
        )
        if not judge_data:
            return JudgeResult(action="no_reply", should_reply=False, reasoning="判断模型未返回有效 JSON")

        action = str(judge_data.get("action") or "").strip().lower()
        if action in {"reply", "send", "yes", "true"}:
            action = "continue"
        if action not in {"continue", "no_reply"}:
            action = "no_reply"

        return JudgeResult(
            action=action,
            reasoning=str(judge_data.get("reasoning") or "").strip(),
            should_reply=action == "continue",
            confidence=self._normalize_confidence(judge_data.get("confidence", 0.0)),
            target_message=str(judge_data.get("target_message") or "").strip(),
        )

    async def review_candidate_reply(self, event: AstrMessageEvent, candidate_reply: str) -> ReviewResult:
        """发送前审查 AstrBot 生成的候选回复是否适合当前群聊。"""
        judge_result = event.get_extra("heartflow_judge_result")
        decision_reasoning = judge_result.reasoning if isinstance(judge_result, JudgeResult) else ""
        prompt = self._render_prompt_template(
            self.reply_review_prompt,
            {
                "judge_persona_prompt": self.judge_persona_prompt,
                "recent_messages_with_candidate": self._get_recent_messages_with_candidate(
                    event,
                    candidate_reply,
                    limit=self.judge_context_count,
                ),
                "candidate_reply": candidate_reply,
                "decision_reasoning": decision_reasoning or "无",
                "chat_id": event.unified_msg_origin,
                "self_id": event.get_self_id(),
                "current_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        review_data = await self._call_json_judge_model(
            prompt,
            max_retries=self.review_max_retries,
            stage_name="Reply Gate 回复审查",
        )
        if not review_data:
            return ReviewResult(action="cancel", allow_send=False, reasoning="回复审查模型未返回有效 JSON")

        action = str(review_data.get("action") or "").strip().lower()
        if action in {"allow", "approve", "yes", "true", "continue"}:
            action = "send"
        if action not in {"send", "cancel"}:
            action = "cancel"
        return ReviewResult(
            action=action,
            allow_send=action == "send",
            reasoning=str(review_data.get("reasoning") or "").strip(),
        )

    def _record_raw_message(self, event: AstrMessageEvent, is_bot: bool = False) -> None:
        """将消息写入原始消息缓冲区"""
        umo = event.unified_msg_origin
        if umo not in self._raw_msg_buffer:
            self._raw_msg_buffer[umo] = deque(maxlen=self._raw_msg_buffer_size)
        self._raw_msg_buffer[umo].append(RawMessage(
            sender_name=event.get_sender_name(),
            sender_id=str(event.get_sender_id()),
            content=event.message_str,
            timestamp=time.time(),
            is_bot=is_bot,
        ))

    def _next_reply_token(self, umo: str) -> int:
        """生成群聊内单调递增的主动回复版本号。"""
        token = self._reply_tokens.get(umo, 0) + 1
        self._reply_tokens[umo] = token
        return token

    def _is_reply_token_current(self, umo: str, token: int) -> bool:
        return self._reply_tokens.get(umo) == token

    def _mark_heartflow_trigger(self, event: AstrMessageEvent, judge_result: JudgeResult) -> int:
        """标记当前消息走 AstrBot 完整主流程，并让旧 Reply Gate 候选回复失效。"""
        umo = event.unified_msg_origin
        old_event = self._active_heartflow_events.get(umo)
        if old_event and old_event is not event:
            old_event.set_extra("heartflow_stale", True)
            old_event.set_extra("agent_stop_requested", True)
            logger.info(f"🧹 Reply Gate 标记旧回复过期 | {umo[:20]}...")

        token = self._next_reply_token(umo)
        event.is_at_or_wake_command = True
        event.set_extra("heartflow_triggered", True)
        event.set_extra("heartflow_token", token)
        event.set_extra("heartflow_judge_result", judge_result)
        event.set_extra("enable_streaming", False)
        event.set_extra("heartflow_result_recorded", False)
        self._active_heartflow_events[umo] = event
        return token

    def _clear_heartflow_event(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        if self._active_heartflow_events.get(umo) is event:
            self._active_heartflow_events.pop(umo, None)

    def _begin_judge(self, umo: str) -> None:
        self._active_judges[umo] = self._active_judges.get(umo, 0) + 1

    def _end_judge(self, umo: str) -> None:
        count = self._active_judges.get(umo, 0) - 1
        if count > 0:
            self._active_judges[umo] = count
        else:
            self._active_judges.pop(umo, None)

    async def _wait_for_current_judges(self, umo: str, token: int) -> bool:
        """有新消息正在判断时，先别发送旧回复，给新 token 生效的机会。"""
        waited = False
        while self._active_judges.get(umo, 0) > 0:
            waited = True
            if not self._is_reply_token_current(umo, token):
                return False
            await asyncio.sleep(0.1)
        if waited:
            logger.debug(f"Reply Gate 发送前等待新判断完成 | {umo[:20]}... | token:{token}")
        return self._is_reply_token_current(umo, token)

    def _clean_heartflow_leaks_in_chain(self, result) -> None:
        leak_patterns = [
            "（注意：本次是你主动参与群聊的，不是用户叫你。回复应自然随意，像普通群成员一样加入话题。）",
            "注意：本次是你主动参与群聊的，不是用户叫你。回复应自然随意，像普通群成员一样加入话题。",
            "（注意：本次是你主动参与群聊的，不是用户叫你。",
            "注意：本次是你主动参与群聊的，不是用户叫你。",
            "本次是你主动参与群聊的，不是用户叫你",
            "回复应自然随意，像普通群成员一样加入话题",
        ]

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                for pattern in leak_patterns:
                    text = text.replace(pattern, "")
                comp.text = text.replace("（）", "").replace("()", "").strip()

    def _get_raw_buffer(self, umo: str) -> list[RawMessage]:
        """获取缓冲区中的消息列表（时间顺序）"""
        return list(self._raw_msg_buffer.get(umo, []))

    @staticmethod
    def _extract_plain_text_from_result(result) -> str:
        """从 AstrBot 结果链中提取纯文本回复。"""
        if result is None or not getattr(result, "chain", None):
            return ""
        return "".join(comp.text for comp in result.chain if isinstance(comp, Plain)).strip()

    @staticmethod
    def _format_raw_message(m: RawMessage) -> str:
        prefix = f"[机器人({m.sender_id})]" if m.is_bot else f"[{m.sender_name}({m.sender_id})]"
        return f"{prefix}: {m.content}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊消息处理入口"""

        # 检查基本条件
        if not self._should_process_message(event):
            return

        # 第一时间记录原始消息，无论是否最终触发 LLM
        self._record_raw_message(event, is_bot=False)

        if event.get_sender_id() == event.get_self_id():
            return

        # 显式唤醒交给 AstrBot 主流程处理，同时让旧的 Reply Gate 候选回复失效。
        if event.is_at_or_wake_command:
            old_event = self._active_heartflow_events.get(event.unified_msg_origin)
            if old_event:
                old_event.set_extra("heartflow_stale", True)
                old_event.set_extra("agent_stop_requested", True)
                self._next_reply_token(event.unified_msg_origin)
            logger.debug(f"跳过已被标记为唤醒的消息: {event.message_str}")
            return

        if not self._should_allow_reply_now(event):
            self._update_passive_state(event, JudgeResult(reasoning="冷却中"))
            return

        try:
            # 小参数模型判断是否需要回复
            self._begin_judge(event.unified_msg_origin)
            try:
                judge_result = await self.judge_with_tiny_model(event)
            finally:
                self._end_judge(event.unified_msg_origin)

            # 普通日志级别输出每次判断结果，方便观察为什么回复/不回复
            msg_preview = (event.message_str or "").replace("\n", " ").replace("\r", " ")[:80]
            reasoning_preview = (judge_result.reasoning or "").replace("\n", " ").replace("\r", " ")[:120]
            logger.info(
                f"🧠 Reply Gate 判断结果 | {event.unified_msg_origin[:20]}... "
                f"| 发送者:{event.get_sender_name()} "
                f"| 内容:{msg_preview} "
                f"| 动作:{judge_result.action} "
                f"| 结果:{'✅ 回复' if judge_result.should_reply else '❌ 不回复'} "
                f"| confidence:{judge_result.confidence:.2f} "
                f"| 原因:{reasoning_preview}"
            )

            if judge_result.should_reply:
                logger.info(f"🔥 Reply Gate 触发主动回复 | {event.unified_msg_origin[:20]}... | 置信度:{judge_result.confidence:.2f}")

                # 交给 AstrBot 完整主流程调用主模型，发送前再检查是否已被新消息替换。
                token = self._mark_heartflow_trigger(event, judge_result)

                logger.info(f"💖 Reply Gate 设置唤醒标志 | {event.unified_msg_origin[:20]}... | token:{token} | 动作:{judge_result.action} | {judge_result.reasoning[:50]}...")
                
                return
            else:
                # 记录被动状态
                logger.debug(f"Reply Gate 判断不通过 | {event.unified_msg_origin[:20]}... | 动作:{judge_result.action} | 原因: {judge_result.reasoning[:30]}...")
                self._update_passive_state(event, judge_result)

        except Exception as e:
            logger.error(f"Reply Gate 插件处理消息异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后将机器人的回复写入原始消息缓冲区，以便后续判断参考"""
        if not self.config.get("enable_heartflow", False):
            return

        result = event.get_result()
        if result is None or not result.chain:
            if event.get_extra("heartflow_triggered"):
                self._clear_heartflow_event(event)
            return

        if event.get_extra("heartflow_triggered") and event.get_extra("heartflow_result_recorded"):
            self._clear_heartflow_event(event)
            return

        # 提取回复的纯文本内容
        reply_text = self._extract_plain_text_from_result(result)
        if not reply_text:
            if event.get_extra("heartflow_triggered"):
                self._clear_heartflow_event(event)
            return

        umo = event.unified_msg_origin
        if umo not in self._raw_msg_buffer:
            self._raw_msg_buffer[umo] = deque(maxlen=self._raw_msg_buffer_size)
        self._raw_msg_buffer[umo].append(RawMessage(
            sender_name="bot",
            sender_id=str(event.get_self_id() or "bot"),
            content=reply_text,
            timestamp=time.time(),
            is_bot=True,
        ))
        if event.get_extra("heartflow_triggered"):
            event.set_extra("heartflow_result_recorded", True)
            self._clear_heartflow_event(event)
        logger.debug(f"机器人回复已写入缓冲区: {umo[:20]}... | {reply_text[:40]}...")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """Reply Gate 触发时不再向主模型注入额外提示，避免提示词泄露到群聊回复中。"""
        if not event.get_extra("heartflow_triggered"):
            return
        return

    @filter.on_decorating_result()
    async def clean_heartflow_leak(self, event: AstrMessageEvent):
        """发送前清理候选回复，并审查是否适合发送。"""
        result = event.get_result()
        if not result or not result.chain:
            return

        if event.get_extra("heartflow_triggered"):
            token = event.get_extra("heartflow_token")
            is_current_after_wait = await self._wait_for_current_judges(event.unified_msg_origin, token)
            is_stale = event.get_extra("heartflow_stale") or not is_current_after_wait
            if self.drop_stale_replies and is_stale:
                logger.info(
                    f"🧹 Reply Gate 丢弃过期回复 | {event.unified_msg_origin[:20]}... | token:{token}"
                )
                event.clear_result()
                self._clear_heartflow_event(event)
                return

        leak_patterns = [
            "（注意：本次是你主动参与群聊的，不是用户叫你。回复应自然随意，像普通群成员一样加入话题。）",
            "注意：本次是你主动参与群聊的，不是用户叫你。回复应自然随意，像普通群成员一样加入话题。",
            "（注意：本次是你主动参与群聊的，不是用户叫你。",
            "注意：本次是你主动参与群聊的，不是用户叫你。",
            "本次是你主动参与群聊的，不是用户叫你",
            "回复应自然随意，像普通群成员一样加入话题",
        ]

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                for pattern in leak_patterns:
                    text = text.replace(pattern, "")
                text = text.replace("（）", "").replace("()", "").strip()
                comp.text = text

        if event.get_extra("heartflow_triggered"):
            candidate_reply = self._extract_plain_text_from_result(result)
            if not candidate_reply:
                logger.info(f"🧹 Reply Gate 候选回复为空，取消发送 | {event.unified_msg_origin[:20]}...")
                event.clear_result()
                self._clear_heartflow_event(event)
                return

            review_result = await self.review_candidate_reply(event, candidate_reply)
            logger.info(
                f"🧪 Reply Gate 回复审查 | {event.unified_msg_origin[:20]}... "
                f"| 动作:{review_result.action} | 原因:{review_result.reasoning[:120]}"
            )
            if not review_result.allow_send:
                event.clear_result()
                self._clear_heartflow_event(event)
                self._update_passive_state(event, JudgeResult(reasoning=f"回复审查取消: {review_result.reasoning}"))
                return

            judge_result = event.get_extra("heartflow_judge_result")
            if isinstance(judge_result, JudgeResult):
                self._update_active_state(event, judge_result)

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        """检查是否应该处理这条消息"""

        # 检查插件是否启用
        if not self.config.get("enable_heartflow", False):
            return False

        # 检查白名单
        if self.whitelist_enabled:
            if not self.chat_whitelist:
                logger.debug(f"白名单为空，跳过处理: {event.unified_msg_origin}")
                return False

            if event.unified_msg_origin not in self.chat_whitelist:
                logger.debug(f"群聊不在白名单中，跳过处理: {event.unified_msg_origin}")
                return False

        # 跳过空消息
        if not event.message_str or not event.message_str.strip():
            return False

        return True

    def _should_allow_reply_now(self, event: AstrMessageEvent) -> bool:
        """检查当前是否允许触发新的主动回复。"""

        # 冷却时间校验：防止短时间内连续触发
        if self.min_reply_interval > 0:
            minutes = self._get_minutes_since_last_reply(event.unified_msg_origin)
            elapsed_seconds = minutes * 60
            if elapsed_seconds < self.min_reply_interval:
                logger.debug(f"冷却中，距上次回复还有 {self.min_reply_interval - elapsed_seconds:.0f}s")
                return False

        return True

    def _get_chat_state(self, chat_id: str) -> ChatState:
        """获取群聊状态"""
        if chat_id not in self.chat_states:
            self.chat_states[chat_id] = ChatState()

        # 检查日期重置
        today = datetime.date.today().isoformat()
        state = self.chat_states[chat_id]

        if state.last_reset_date != today:
            state.last_reset_date = today
            # 每日重置时恒复一些精力
            state.energy = min(1.0, state.energy + 0.2)

        # 基于时间流逝自然恢复精力（每 5 分钟回复 energy_recovery_rate * 5 精力）
        now = time.time()
        if state.last_energy_update_time == 0:
            state.last_energy_update_time = now
        else:
            elapsed_minutes = (now - state.last_energy_update_time) / 60.0
            time_recovery = elapsed_minutes * (self.energy_recovery_rate * 5)
            state.energy = min(1.0, state.energy + time_recovery)
            state.last_energy_update_time = now  # 重置计时起点，避免重复累加

        return state

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        """获取距离上次回复的分钟数"""
        chat_state = self._get_chat_state(chat_id)

        if chat_state.last_reply_time == 0:
            return 999  # 从未回复过

        return int((time.time() - chat_state.last_reply_time) / 60)

    def _get_recent_contexts(self, event: AstrMessageEvent) -> list:
        """从原始消息缓冲区获取最近对话上下文（用于传递给小参数模型）。

        使用本地缓冲区而非 conversation_manager，以便包含所有群聊消息，
        而不仅仅是触发过 LLM 的消息。
        """
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        # 排除当前这条消息（已被 _record_raw_message 写入），取之前的若干条
        if msgs and msgs[-1].content == event.message_str:
            msgs = msgs[:-1]
        recent = msgs[-self.context_messages_count:] if len(msgs) > self.context_messages_count else msgs

        contexts = []
        for m in recent:
            if m.is_bot:
                contexts.append({"role": "assistant", "content": f"[机器人({m.sender_id})]: {m.content}"})
            else:
                contexts.append({"role": "user", "content": f"[{m.sender_name}({m.sender_id})]: {m.content}"})
        return contexts

    def _get_recent_messages(
        self,
        event: AstrMessageEvent,
        limit: int | None = None,
        *,
        exclude_current: bool = True,
    ) -> str:
        """从原始消息缓冲区获取最近的消息历史（用于小参数模型判断）。

        包含所有群聊成员的消息，而非仅 LLM 处理过的消息。
        """
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        # 排除当前这条消息（已被 _record_raw_message 写入），取之前的若干条
        if exclude_current and msgs and msgs[-1].content == event.message_str:
            msgs = msgs[:-1]
        active_limit = max(1, int(limit or self.context_messages_count))
        recent = msgs[-active_limit:] if len(msgs) > active_limit else msgs

        if not recent:
            return "暂无对话历史"

        lines = [self._format_raw_message(m) for m in recent]
        return "\n".join(lines)

    def _get_recent_messages_with_candidate(
        self,
        event: AstrMessageEvent,
        candidate_reply: str,
        limit: int | None = None,
    ) -> str:
        """构造包含待发送候选回复的上下文。"""
        base_context = self._get_recent_messages(event, limit=limit, exclude_current=False)
        candidate_line = f"[机器人({event.get_self_id() or 'bot'})][待发送]: {candidate_reply}"
        if not base_context or base_context == "暂无对话历史":
            return candidate_line
        return f"{base_context}\n{candidate_line}"

    def _get_last_bot_reply(self, event: AstrMessageEvent) -> str | None:
        """从原始消息缓冲区获取上次机器人的回复内容。"""
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        for m in reversed(msgs):
            if m.is_bot and m.content.strip():
                return m.content
        return None

    def _build_chat_context(self, event: AstrMessageEvent) -> str:
        """构建群聊上下文摘要信息。"""
        chat_state = self._get_chat_state(event.unified_msg_origin)

        # 检查上次机器人回复后群里有没有人接话（评估回复质量）
        msgs = self._get_raw_buffer(event.unified_msg_origin)
        post_reply_engagement = ""
        found_bot = False
        user_msgs_after_bot = 0
        for m in reversed(msgs):
            if m.is_bot:
                found_bot = True
                break
            user_msgs_after_bot += 1
        if found_bot:
            if user_msgs_after_bot >= 3:
                post_reply_engagement = "（上次回复后群里进行了热烈讨论）"
            elif user_msgs_after_bot == 0:
                post_reply_engagement = "（上次回复后无人接话）"

        if chat_state.total_messages > 100:
            activity_level = "高"
        elif chat_state.total_messages > 20:
            activity_level = "中"
        else:
            activity_level = "低"

        context_info = f"最近活跃度: {activity_level}\n"
        context_info += f"历史回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%\n"
        context_info += f"当前精力水平: {chat_state.energy:.1f}/1.0\n"
        context_info += f"上次发言: {self._get_minutes_since_last_reply(event.unified_msg_origin)}分钟前\n"
        context_info += f"当前时间: {datetime.datetime.now().strftime('%H:%M')}"

        if post_reply_engagement:
            context_info += f"\n回复效果: {post_reply_engagement}"
            
        return context_info

    def _update_active_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新主动回复状态"""
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        # 更新回复相关状态
        now = time.time()
        chat_state.last_reply_time = now
        chat_state.last_energy_update_time = now
        if not getattr(event, "_heartflow_active_state_updated", False):
            chat_state.total_replies += 1
            chat_state.total_messages += 1
            setattr(event, "_heartflow_active_state_updated", True)

        # 精力消耗（回复后精力下降）
        if not getattr(event, "_heartflow_energy_decayed", False):
            chat_state.energy = max(0.1, chat_state.energy - self.energy_decay_rate)
            setattr(event, "_heartflow_energy_decayed", True)

        logger.debug(f"更新主动状态: {chat_id[:20]}... | 精力: {chat_state.energy:.2f}")

    def _update_passive_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新被动状态（未回复）"""
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        # 更新消息计数
        chat_state.total_messages += 1

        # 精力恢复（不回复时精力缓慢恢复）
        chat_state.energy = min(1.0, chat_state.energy + self.energy_recovery_rate)

        logger.debug(f"更新被动状态: {chat_id[:20]}... | 精力: {chat_state.energy:.2f} | 原因: {judge_result.reasoning[:30]}...")

    async def _send_reply_gate_status(self, event: AstrMessageEvent):
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        status_info = f"""
🔮 Reply Gate 状态报告

📊 **当前状态**
- 群聊ID: {event.unified_msg_origin}
- 精力水平: {chat_state.energy:.2f}/1.0 {'🟢' if chat_state.energy > 0.7 else '🟡' if chat_state.energy > 0.3 else '🔴'}
- 上次回复: {self._get_minutes_since_last_reply(chat_id)}分钟前

📈 **历史统计**
- 总消息数: {chat_state.total_messages}
- 总回复数: {chat_state.total_replies}
- 回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%

⚙️ **配置参数**
- 判断提供商: {self.judge_provider_name}
- 接话判断重试次数: {self.judge_max_retries}
- 回复审查重试次数: {self.review_max_retries}
- 普通日志输出每次判断: ✅ 开启
- 白名单模式: {'✅ 开启' if self.whitelist_enabled else '❌ 关闭'}
- 白名单群聊数: {len(self.chat_whitelist) if self.whitelist_enabled else 0}

🧠 **判断方式**
- 接话判断: 模型直接输出 continue/no_reply
- 发送前审查: 模型直接输出 send/cancel
- 判断人格: 使用插件独立配置，不读取 AstrBot 人格

🎯 **插件状态**: {'✅ 已启用' if self.config.get('enable_heartflow', False) else '❌ 已禁用'}
"""

        event.set_result(event.plain_result(status_info))

    # 管理员命令：查看 Reply Gate 状态
    @filter.command("reply_gate")
    async def reply_gate_status(self, event: AstrMessageEvent):
        """查看 Reply Gate 状态"""
        await self._send_reply_gate_status(event)

    # 兼容旧命令：查看 Reply Gate 状态
    @filter.command("heartflow")
    async def heartflow_status(self, event: AstrMessageEvent):
        """查看 Reply Gate 状态"""
        await self._send_reply_gate_status(event)

    async def _reset_reply_gate_state(self, event: AstrMessageEvent):
        chat_id = event.unified_msg_origin
        if chat_id in self.chat_states:
            del self.chat_states[chat_id]

        event.set_result(event.plain_result("✅ Reply Gate 状态已重置"))
        logger.info(f"Reply Gate 状态已重置: {chat_id}")

    # 管理员命令：重置 Reply Gate 状态
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reply_gate_reset")
    async def reply_gate_reset(self, event: AstrMessageEvent):
        """重置 Reply Gate 状态"""
        await self._reset_reply_gate_state(event)

    # 兼容旧命令：重置 Reply Gate 状态
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_reset")
    async def heartflow_reset(self, event: AstrMessageEvent):
        """重置 Reply Gate 状态"""
        await self._reset_reply_gate_state(event)

    # 管理员命令：查看系统提示词缓存
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reply_gate_cache")
    async def reply_gate_cache_status(self, event: AstrMessageEvent):
        """查看系统提示词缓存状态"""
        
        cache_info = "🧠 系统提示词缓存状态\n\n"
        
        if not self.system_prompt_cache:
            cache_info += "📭 当前无缓存记录"
        else:
            cache_info += f"📝 总缓存数量: {len(self.system_prompt_cache)}\n\n"
            
            for cache_key, cache_data in self.system_prompt_cache.items():
                original_len = len(cache_data.get("original", ""))
                summarized_len = len(cache_data.get("summarized", ""))
                persona_id = cache_data.get("persona_id", "unknown")
                
                cache_info += f"🔑 **缓存键**: {cache_key}\n"
                cache_info += f"👤 **人格ID**: {persona_id}\n"
                cache_info += f"📏 **压缩率**: {original_len} -> {summarized_len} ({(1-summarized_len/max(1,original_len))*100:.1f}% 压缩)\n"
                cache_info += f"📄 **精简内容**: {cache_data.get('summarized', '')[:100]}...\n\n"
        
        event.set_result(event.plain_result(cache_info))

    # 兼容旧命令：查看系统提示词缓存
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_cache")
    async def heartflow_cache_status(self, event: AstrMessageEvent):
        """查看系统提示词缓存状态"""
        await self.reply_gate_cache_status(event)

    # 管理员命令：清除系统提示词缓存
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reply_gate_cache_clear")
    async def reply_gate_cache_clear(self, event: AstrMessageEvent):
        """清除系统提示词缓存"""
        
        cache_count = len(self.system_prompt_cache)
        self.system_prompt_cache.clear()
        
        event.set_result(event.plain_result(f"✅ 已清除 {cache_count} 个系统提示词缓存"))
        logger.info(f"系统提示词缓存已清除，共清除 {cache_count} 个缓存")

    # 兼容旧命令：清除系统提示词缓存
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("heartflow_cache_clear")
    async def heartflow_cache_clear(self, event: AstrMessageEvent):
        """清除系统提示词缓存"""
        await self.reply_gate_cache_clear(event)

    async def _get_persona_system_prompt(self, event: AstrMessageEvent) -> str:
        """获取当前对话的人格系统提示词"""
        try:
            persona_mgr = self.context.persona_manager

            # 获取当前对话，尝试拿到会话绑定的 persona_id
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            persona_id: str | None = None
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                if conversation:
                    persona_id = conversation.persona_id

            # 用户显式取消人格
            if persona_id == "[%None]":
                return ""

            if persona_id:
                # 直接通过 PersonaManager 查询数据库
                try:
                    persona = await persona_mgr.get_persona(persona_id)
                    return persona.system_prompt or ""
                except ValueError:
                    logger.debug(f"未找到人格 {persona_id}，回退到默认人格")

            # 无 persona_id 或查询失败，使用默认人格
            default_persona = await persona_mgr.get_default_persona_v3(event.unified_msg_origin)
            return default_persona.get("prompt", "")

        except Exception as e:
            logger.debug(f"获取人格系统提示词失败: {e}")
            return ""

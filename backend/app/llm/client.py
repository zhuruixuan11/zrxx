"""LLM 客户端：多 Provider 统一接口 + Mock 模式兜底。"""
from __future__ import annotations

import random
from typing import Optional

from loguru import logger

from ..config import get_settings


class LLMError(Exception):
    pass


class LLMClient:
    """统一 async 接口，背后路由到 DeepSeek/OpenAI/Qwen/Mock。"""

    def __init__(self):
        self.settings = get_settings()
        self._provider = self.settings.LLM_PROVIDER.lower()
        self._llm = None  # 延迟初始化

    @property
    def provider_name(self) -> str:
        return self._provider

    def is_mock(self) -> bool:
        """无 API Key 或显式 mock → 用本地兜底。"""
        # 微调模型由百炼（DashScope）托管，走 qwen 兼容接口
        if _model_override or self.settings.QWEN_FINETUNED_MODEL:
            return not self.settings.QWEN_API_KEY
        if self._provider == "mock":
            return True
        # 检查对应 key 是否配置
        key_map = {
            "deepseek": self.settings.DEEPSEEK_API_KEY,
            "openai": self.settings.OPENAI_API_KEY,
            "qwen": self.settings.QWEN_API_KEY,
        }
        key = key_map.get(self._provider)
        return not key

    def _get_langchain_client(self):
        """延迟构建 LangChain ChatOpenAI 实例。"""
        if self._llm is not None:
            return self._llm
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise LLMError("langchain-openai 未安装")

        provider = self._provider
        # 统一加超时与重试上限：避免网络异常时请求挂几分钟、前端一直转圈
        common = dict(temperature=0.5, timeout=40, max_retries=1, max_tokens=1500)

        # 微调模型优先：无论主 Provider 是什么，微调产出都由百炼托管，
        # 必须走 DashScope 的 OpenAI 兼容端点，否则会拿模型名去别的厂商请求而 404。
        finetuned = _model_override or self.settings.QWEN_FINETUNED_MODEL
        if finetuned:
            self._llm = ChatOpenAI(
                api_key=self.settings.QWEN_API_KEY,
                model=finetuned,
                base_url=self.settings.QWEN_BASE_URL,
                **common,
            )
            logger.info("[LLM] 使用微调模型 model={}", finetuned)
            return self._llm

        if provider == "deepseek":
            self._llm = ChatOpenAI(
                api_key=self.settings.DEEPSEEK_API_KEY,
                model=self.settings.DEEPSEEK_MODEL,
                base_url=self.settings.DEEPSEEK_BASE_URL,
                **common,
            )
        elif provider == "openai":
            self._llm = ChatOpenAI(
                api_key=self.settings.OPENAI_API_KEY,
                model=self.settings.OPENAI_MODEL,
                base_url=self.settings.OPENAI_BASE_URL,
                **common,
            )
        elif provider == "qwen":
            self._llm = ChatOpenAI(
                api_key=self.settings.QWEN_API_KEY,
                model=self.settings.QWEN_MODEL,
                base_url=self.settings.QWEN_BASE_URL,
                **common,
            )
        else:
            raise LLMError(f"不支持的 LLM Provider: {provider}")
        logger.info("[LLM] 已初始化 provider=%s model=%s", provider, getattr(self._llm, 'model_name', 'unknown'))
        return self._llm

    async def achat(self, messages: list[dict], system_prompt: Optional[str] = None) -> str:
        """统一 chat 接口：传入 [{'role':'user'/'assistant', 'content':'...'}]，返回文本。"""
        if self.is_mock():
            return self._mock_reply(messages)

        # 注入当前日期时间锚点（LLM 训练数据截止，不知道"今天"是什么时候）
        import datetime as _dt
        weekday_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
        now = _dt.datetime.now()
        today_anchor = (
            f"【当前日期和时间锚点】今天是 {now.strftime('%Y-%m-%d')} "
            f"（星期{weekday_map[now.weekday()]}），{now.strftime('%H:%M')}。"
            f" 所有涉及'今天'、'现在'、'当前'的描述，都以这个时间为准。"
            f" 如果用户问日期/年份/时间，必须根据此锚点回答，不能用你训练数据里的任何日期。"
        )

        # 组装 LangChain 消息
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        lc_msgs = []
        # 第一条 system: 日期锚点（最重要！）
        lc_msgs.append(SystemMessage(content=today_anchor))
        if system_prompt:
            lc_msgs.append(SystemMessage(content=system_prompt))
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                lc_msgs.append(SystemMessage(content=content))
            elif role == "assistant":
                lc_msgs.append(AIMessage(content=content))
            else:
                lc_msgs.append(HumanMessage(content=content))

        llm = self._get_langchain_client()
        try:
            resp = await llm.ainvoke(lc_msgs)
            return resp.content if hasattr(resp, "content") else str(resp)
        except Exception as e:
            logger.error("[LLM] 调用失败 provider=%s err=%s，回退 Mock", self._provider, e)
            return self._mock_reply(messages, f"（原 LLM 调用失败：{e}）")

    def _extract_user_question(self, content: str) -> str:
        """从可能包含 system instruction 的 prompt 中提取用户真正的问题。"""
        import re
        c = content.strip()

        # 1. 优先找「用户：」「用户问题：」「问题：」后面的内容
        patterns = [
            r"用户[：:]\s*(.+?)(?:\n|$)",
            r"用户问题[：:]\s*(.+?)(?:\n|$)",
            r"问题[：:]\s*(.+?)(?:\n|$)",
            r"请.*?[回答|分析].*?[：:]\s*(.+?)(?:\n|$)",
        ]
        for p in patterns:
            m = re.search(p, c, re.DOTALL)
            if m:
                q = m.group(1).strip()
                if 3 < len(q) < 100:
                    return q

        # 2. 如果内容很短（< 30 字），直接返回
        if len(c) <= 30:
            return c

        # 3. 取最后一行（通常是用户的问题）
        lines = [l.strip() for l in c.split("\n") if l.strip()]
        if lines:
            last = lines[-1]
            # 过滤掉 instruction 结尾
            for suffix in ["。", "！", "？"]:
                if len(last) > 10:
                    return last[:80]
            return last[:80]

        return c[:50]

    def _mock_reply(self, messages: list[dict], prefix: str = "") -> str:
        """无 API Key 时的兜底：返回简洁友好的占位文本。"""
        # 只取最后一条用户消息（跳过 system/assistant）
        last_user_raw = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_raw = m.get("content", "")
                break

        question = self._extract_user_question(last_user_raw)

        snippets = [
            f"💬 关于「{question}」：",
            "",
            "📌 这是 **Mock 模式回复**（还没配置真实 LLM）。",
            "启用真实 AI：编辑 backend/.env 设置 `LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY`。",
            "DeepSeek 有免费额度，推荐先用它开发调试 ✨",
        ]
        if prefix:
            snippets.insert(0, prefix)
        return "\n".join(snippets)


_client: Optional[LLMClient] = None

# 运行时模型覆盖：微调产出的模型名（None 表示用配置里的默认模型）
_model_override: Optional[str] = None


def set_model_override(name: Optional[str]) -> None:
    """切换运行时模型。清空已缓存的 ChatOpenAI 实例以触发重建。"""
    global _model_override
    _model_override = (name or "").strip() or None
    if _client is not None:
        _client._llm = None
    logger.info("[LLM] 模型 override → {}", _model_override or "(未设置)")


def get_model_override() -> Optional[str]:
    return _model_override


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
        logger.info("[LLM] provider=%s (mock=%s)", _client.provider_name, _client.is_mock())
    return _client

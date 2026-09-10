"""生成层（M3）：LLM 调用、提示词构造、引用校验（04 §5）。

提示词约束（第一层防线）：只基于提供的块作答，逐句标注 `[n]`。
引用校验（第二层，**纯规则必执行**）：解析回答中的 `[n]`，断言其落在
「本次提供的块编号集合」内——越界即幻觉引用。生成不可控，校验必须可控。

拒答（04 §6）：L1 阈值判定在服务层（素材够不够格），L2 自检在此层提供接口。
"""

import logging
import re
from typing import TYPE_CHECKING, Protocol

import httpx

from .core.config import settings
from .retrieval import RetrievedChunk

if TYPE_CHECKING:                      # 仅类型检查期导入，避免运行期循环依赖
    from .session import Turn as TurnRef

logger = logging.getLogger(__name__)

# 引用标注：形如 [1]、[12]
_CITATION_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT_TEMPLATE = """你是一个严谨的知识库问答助手。只能依据用户提供的资料片段作答。

回答方式：
1. 先直接给出结论，再用资料里的具体内容解释**为什么**或**怎么做**——把机制、步骤、条件、区别、原因展开写清楚，资料里写到的细节不要省略。
2. 资料涉及多个相关点时，分点或分段组织，每点各自标注来源。
3. 资料里没有的内容一律不写，也不要补充你自己的常识；宁可少写，不可编造。
4. 用简洁的书面语直接回答，不要用「根据资料」「资料显示」这类套话开头。

引用与边界：
5. 每个陈述句后必须标注来源编号，格式为 [n]，n 是资料片段的编号。
6. **本次资料片段只有 {count} 条，合法编号仅限 [1] 到 [{count}]**，不得使用其它编号。
7. 资料不足以回答时，只回复：资料不足，无法回答。
"""


def build_system_prompt(count: int) -> str:
    """系统提示：显式声明合法引用编号范围（实测可降低越界引用率）。

    同时要求「用资料内容展开解释」——只要求引用、不要求解释时，模型会给出
    一句话结论（实测问题：回答正确但读不到逻辑依据）。
    """
    return SYSTEM_PROMPT_TEMPLATE.format(count=count)


class LLMError(RuntimeError):
    """LLM 调用失败（未配置 key / 网络错误 / 服务端错误）。"""


class LLMProvider(Protocol):
    """对话生成能力接口：实现可替换（云 API / 本地模型）。"""

    def complete(self, system: str, user: str) -> str:
        """返回模型生成的纯文本回答。"""
        ...


class OpenAICompatibleLLM:
    """OpenAI 兼容 /chat/completions 客户端（httpx 直连）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        try:
            response = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,          # 问答要稳定，不要发挥
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM 服务返回 {exc.response.status_code}: {exc.response.text[:200]}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from None

        payload = response.json()
        return payload["choices"][0]["message"]["content"]


def get_llm_provider() -> LLMProvider:
    """按配置构造 LLM provider；未配置 key 时立刻失败（不静默降级）。"""
    if not settings.llm_api_key:
        raise LLMError("未配置 LLM_API_KEY（见 .env.example）")
    return OpenAICompatibleLLM(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )


def build_user_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    """把候选块按 [1..n] 编号拼进提示：编号即回答可引用的全部来源。"""
    blocks = "\n\n".join(
        f"[{index}] {chunk.content.strip()}" for index, chunk in enumerate(chunks, start=1)
    )
    return (
        f"资料片段：\n\n{blocks}\n\n问题：{question}\n\n"
        "请先给结论，再用资料里的内容解释原因或做法；资料写到几个要点就展开几点，逐句标注引用编号。"
    )


def parse_citations(content: str) -> list[int]:
    """抽取回答中的引用编号（按出现顺序去重）。"""
    seen: list[int] = []
    for match in _CITATION_RE.finditer(content):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def find_invalid_citations(content: str, provided_count: int) -> list[int]:
    """引用越界校验（04 §5 第二层，纯规则）。

    :param provided_count: 本次提供给模型的块数量（合法编号范围 1..provided_count）
    :return: 越界编号列表（升序）；空列表 = 通过
    """
    return sorted(n for n in set(parse_citations(content)) if n < 1 or n > provided_count)


def generate_answer(
    question: str,
    chunks: list[RetrievedChunk],
    provider: LLMProvider | None = None,
) -> str:
    """调用 LLM 生成回答（不做校验——校验由调用方按 04 §5 执行）。"""
    llm = provider or get_llm_provider()
    system = build_system_prompt(len(chunks))
    return llm.complete(system, build_user_prompt(question, chunks)).strip()


# ── 追问改写（04 DR5） ───────────────────────────────────────

REWRITE_SYSTEM_PROMPT = """把用户的追问改写成一个不依赖上下文、可以独立用于检索的完整问题。

要求：
1. 把指代词（它、这个、那、上面说的）替换成上文中的具体主题。
2. 只输出改写后的问题本身，不要解释、不要引号、不要前缀。
3. 追问本身已完整时，原样输出。"""


def build_rewrite_prompt(question: str, history: list["TurnRef"]) -> str:
    """构造改写提示：只带最近三轮，且回答截断（控制上下文长度）。"""
    recent = "\n\n".join(
        f"用户：{turn.question}\n助手：{turn.answer[:200]}" for turn in history[-3:]
    )
    return f"最近对话：\n\n{recent}\n\n追问：{question}\n\n改写后的问题："


def rewrite_query(
    question: str,
    history: list["TurnRef"],
    provider: LLMProvider | None = None,
) -> str:
    """把追问改写为自包含问题（04 DR5）。

    边界：无历史或改写结果为空时返回原问题——**增强件不得成为链路单点**，
    改写失败也必须能检索（失败时由调用方捕获 LLMError 后沿用原问题）。
    """
    if not history:
        return question
    llm = provider or get_llm_provider()
    rewritten = llm.complete(
        REWRITE_SYSTEM_PROMPT, build_rewrite_prompt(question, history)
    ).strip()
    return rewritten or question

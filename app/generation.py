"""生成层（M3）：LLM 调用、提示词构造、引用校验（04 §5）。

提示词约束（第一层防线）：只基于提供的块作答，为独立论点标注 `[n]`。
引用校验（第二层，**纯规则必执行**）：解析回答中的 `[n]`，断言其落在
「本次提供的块编号集合」内——越界即幻觉引用。生成不可控，校验必须可控。

拒答（04 §6）：L1 阈值判定在服务层（素材够不够格），L2 自检在此层提供接口。
"""

import json
import logging
import re
import unicodedata
from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

import httpx

from .core.config import settings
from .http_client import get_http_client
from .retrieval import RetrievedChunk

if TYPE_CHECKING:                      # 仅类型检查期导入，避免运行期循环依赖
    from .evidence import CitationData
    from .session import Turn as TurnRef

logger = logging.getLogger(__name__)

# 引用标注：形如 [1]、[12]
_CITATION_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT_TEMPLATE = """你是一个严谨的知识库问答助手。只能依据用户提供的资料片段作答。

回答方式：
1. 先直接给出结论，再用资料里的具体内容解释**为什么**或**怎么做**——把机制、步骤、条件、区别、原因展开写清楚，资料里写到的细节不要省略。
2. 资料涉及多个相关点时，分点或分段组织，每个独立论点各自标注来源。
3. 资料里没有的内容一律不写，也不要补充你自己的常识；宁可少写，不可编造。
4. 用简洁的书面语直接回答，不要用「根据资料」「资料显示」这类套话开头。

引用与边界：
5. 来源编号写在论点句末标点之后，例如“结论及依据。[1]”。同一论点引用同一资料片段只标一次；若多个片段内容重复，引用最直接的一处即可。不要在段首、行首堆叠编号。相邻句共同说明同一论点且依据相同时，可在该论点末统一标注。
6. **本次资料片段只有 {count} 条，合法编号仅限 [1] 到 [{count}]**，不得使用其它编号。
7. 保留原文的否定、条件、转折与适用范围；片段注明的上下文来源必须一起阅读，不能只摘结论。无法确认指代或前提时，只回复：上下文不足，无法确定。
8. 资料不足以回答时，只回复：资料不足，无法回答。
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


class StreamingLLMProvider(Protocol):
    """可逐段返回文本的可选生成能力。"""

    def stream(self, system: str, user: str) -> Iterator[str]:
        """按供应商返回顺序产生文本增量。"""
        ...


class OpenAICompatibleLLM:
    """OpenAI 兼容 /chat/completions 客户端（httpx 直连）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout = timeout
        self._client = client

    def complete(self, system: str, user: str) -> str:
        try:
            client = self._client if self._client is not None else get_http_client()
            response = client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=self._request_payload(system, user),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM 服务返回 {exc.response.status_code}: {exc.response.text[:200]}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from None

        return _completion_content(_response_json(response))

    def stream(self, system: str, user: str) -> Iterator[str]:
        """读取 OpenAI 兼容 SSE；不支持流式的兼容服务退回单段 JSON。"""
        client = self._client if self._client is not None else get_http_client()
        try:
            with client.stream(
                "POST",
                self._url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "text/event-stream",
                },
                json=self._request_payload(system, user, stream=True),
                timeout=self._timeout,
            ) as response:
                if response.is_error:
                    response.read()
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    response.read()
                    content = _completion_content(_response_json(response))
                    if content:
                        yield content
                    return

                for line in response.iter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        raise LLMError("LLM 流式响应包含无效 JSON") from None
                    delta = _stream_delta(payload)
                    if delta:
                        yield delta
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM 服务返回 {exc.response.status_code}: {_safe_response_text(exc.response)[:200]}"
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from None

    def _request_payload(
        self,
        system: str,
        user: str,
        *,
        stream: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,          # 问答要稳定，不要发挥
        }
        if stream:
            payload["stream"] = True
        return payload


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


def _source_context(
    chunks: list[RetrievedChunk],
    citations: list["CitationData"] | None = None,
) -> str:
    """编号原文并附来源元数据，不夹带回答任务指令。"""
    sources = {citation.chunk_id: citation for citation in citations or []}
    indexes = {chunk.chunk_id: i for i, chunk in enumerate(chunks, 1)}
    blocks: list[str] = []
    numbered = list(enumerate(chunks, start=1))
    if any(c.context_chunk_ids for c in sources.values()):
        numbered.sort(key=lambda item: (item[1].doc_id, item[1].char_start, item[1].char_end))
    for index, chunk in numbered:
        source = sources.get(chunk.chunk_id)
        metadata = (
            f"文档名称：{source.doc_title}\n原文字符位置：{source.char_start}–{source.char_end}\n"
            if source is not None else ""
        )
        if source is not None and source.context_chunk_ids:
            related = "、".join(f"[{indexes[cid]}]" for cid in source.context_chunk_ids if cid in indexes)
            metadata += f"须结合上下文来源：{related}；条件与指代不得省略。\n"
        blocks.append(f"[{index}] {metadata}原文：\n{chunk.content.strip()}")
    return "\n\n".join(blocks)


def build_user_prompt(
    question: str,
    chunks: list[RetrievedChunk],
    citations: list["CitationData"] | None = None,
    *,
    resolved_question: str | None = None,
) -> str:
    """知识回答的原文与输出要求；出处核对使用独立指令。"""
    context = _source_context(chunks, citations)
    followup = (
        f"根据对话解析的完整问题：{resolved_question}\n"
        "完整问题只用于理解指代，不是事实证据；原始问题的格式、范围和输出要求仍须保留。\n\n"
        if resolved_question and resolved_question != question else ""
    )
    return (
        f"资料片段：\n\n{context}\n\n问题：{question}\n\n{followup}"
        "请先给结论，再用资料里的内容解释原因或做法；资料写到几个要点就展开几点，每个独立论点在句末标点后标注来源，避免重复堆叠编号。"
        "要求出处时使用所给文档名称与引用；未记载的作者、书名或原始论文出处不可补写。比较多个对象时，只比较原文实际支持的内容，并说明未覆盖的部分。"
    )


class InvalidSourceSelection(ValueError):
    """模型返回的来源选择不是合法编号列表。"""


SOURCE_SELECTION_PROMPT = """核对本次问题在前序正式引用的原文中的出处，不重新回答知识问题。
本类步骤整理的是前序已经涉及的内容：问“判别方法的出处”时，核对前序所用的精确定义、存在条件或判别依据的原文位置，不要求原文出现“判别方法”字样。不要将这种出处核对误解为穷举本领域所有方法。
1. 识别本次问题所指的具体对象、定义/判据及范围，只选择直接记载这些内容的原文。不要把共同主题、背景、类比或另一个对象的内容当作本项出处。
2. 不依据文档名称、前序回答或常识判断。多个对象可能在同一文档中，但不能因此互相借用出处。
3. 每个匹配必须提供 subject 和 quote。subject 是问题与片段原文共同出现的具体对象名，必须逐字提取，不要附加“判别方法”“出处”等提问要求；例如对象名是“函数极限”时，不写成“函数极限判别方法”。quote 从该片段连续复制，至少 8 个非空白字符，直接记载定义、条件或方法，不能只摘标题；对象名可以由该片段标题或明确标注的上下文来源确认，无需强行重复在摘录里，不能借用未关联的其他片段。可忽略空白差别，不得改写摘录。
4. 区分某一种定义/判据与全部判别方法；原文不支持所问范围时返回空列表。不要增加问题未要求的对象。
5. 只输出 JSON 对象：{{"source_matches": [{{"source_index": 1, "subject": "目标对象", "quote": "原文证据摘录"}}]}}；无出处为 {{"source_matches": []}}。合法编号只有 1 到 {count}，不添加其它字段。"""


def _normalize_source_text(value: str) -> str:
    """仅忽略 Unicode 表示与空白差异，不替换或改写证据内容。"""
    return "".join(unicodedata.normalize("NFKC", value).split())


def select_source_indexes(
    question: str,
    chunks: list[RetrievedChunk],
    citations: list["CitationData"],
    provider: LLMProvider | None = None,
) -> list[int]:
    """一次局部核对：验证编号、问题对象及逐字原文证据，不执行检索。"""
    llm = provider or get_llm_provider()
    raw = llm.complete(
        SOURCE_SELECTION_PROMPT.format(count=len(chunks)),
        f"前序正式引用的原文：\n\n{_source_context(chunks, citations)}\n\n问题：{question}\n\n"
        "只核对本问题的直接出处，按指定 JSON 返回对象名与证据摘录；不要输出比较结论或扩展到其它对象。",
    ).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidSourceSelection("来源选择不是 JSON") from exc
    if not isinstance(data, dict) or set(data) != {"source_matches"}:
        raise InvalidSourceSelection("来源选择字段不合法")
    matches = data["source_matches"]
    if not isinstance(matches, list):
        raise InvalidSourceSelection("来源匹配列表不合法")
    indexes: list[int] = []
    citations_by_id = {c.chunk_id: c for c in citations}
    index_by_id = {c.chunk_id: i for i, c in enumerate(chunks, 1)}
    normalized_question = _normalize_source_text(question)
    for match in matches:
        if not isinstance(match, dict) or set(match) != {"source_index", "subject", "quote"}:
            raise InvalidSourceSelection("来源匹配字段不合法")
        index, subject, quote = match["source_index"], match["subject"], match["quote"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(chunks):
            raise InvalidSourceSelection("来源编号不合法")
        if not isinstance(subject, str) or not isinstance(quote, str):
            raise InvalidSourceSelection("来源对象或摘录类型不合法")
        subject, quote = _normalize_source_text(subject), _normalize_source_text(quote)
        original_text = _normalize_source_text(chunks[index - 1].content)
        source = citations_by_id.get(chunks[index - 1].chunk_id)
        context_ids = source.context_chunk_ids if source else ()
        subject_text = original_text + "".join(
            _normalize_source_text(chunks[index_by_id[cid] - 1].content)
            for cid in context_ids if cid in index_by_id
        )
        if (
            not subject or subject not in normalized_question or subject not in subject_text
            or len(quote) < 8 or quote not in original_text
        ):
            raise InvalidSourceSelection("来源对象或证据摘录无法与问题及原文对应")
        indexes.append(index)
        indexes.extend(index_by_id[cid] for cid in context_ids if cid in index_by_id)
    return list(dict.fromkeys(indexes))


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
    *,
    citations: list["CitationData"] | None = None,
    resolved_question: str | None = None,
) -> str:
    """调用 LLM 生成回答（不做校验——校验由调用方按 04 §5 执行）。"""
    llm = provider or get_llm_provider()
    system = build_system_prompt(len(chunks))
    return llm.complete(system, build_user_prompt(question, chunks, citations, resolved_question=resolved_question)).strip()


def stream_answer(
    question: str,
    chunks: list[RetrievedChunk],
    provider: LLMProvider | None = None,
    *,
    citations: list["CitationData"] | None = None,
    resolved_question: str | None = None,
) -> Iterator[str]:
    """逐段生成回答；旧式 provider 自动退回单段输出，保持可替换性。"""
    llm = provider or get_llm_provider()
    system = build_system_prompt(len(chunks))
    user = build_user_prompt(question, chunks, citations, resolved_question=resolved_question)
    stream = getattr(llm, "stream", None)
    if callable(stream):
        yield from stream(system, user)
        return
    content = llm.complete(system, user)
    if content:
        yield content


def _response_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        raise LLMError("LLM 服务返回的内容不是有效 JSON") from None


def _completion_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LLMError("LLM 服务响应格式无效")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM 服务响应缺少 choices")
    first = choices[0]
    if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
        raise LLMError("LLM 服务响应缺少 message")
    content = first["message"].get("content")
    if not isinstance(content, str):
        raise LLMError("LLM 服务响应缺少文本 content")
    return content


def _stream_delta(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LLMError("LLM 流式响应格式无效")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM 流式响应缺少 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("LLM 流式响应 choice 格式无效")
    delta = first.get("delta")
    if not isinstance(delta, dict):
        raise LLMError("LLM 流式响应缺少 delta")
    content = delta.get("content")
    if content is None:                 # role / finish_reason 等非文本事件
        return ""
    if not isinstance(content, str):
        raise LLMError("LLM 流式响应 content 格式无效")
    return content


def _safe_response_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (httpx.ResponseNotRead, httpx.StreamClosed):
        return ""


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

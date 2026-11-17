"""原始历史聊天记录下钻检索工具（BaseTool 子类化实现）。"""

from __future__ import annotations

import json
from typing import Any, Optional
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

from .base import tokenize_chinese


class GetMessagesInput(BaseModel):
    """原始历史聊天记录检索工具入参定义。"""
    message_ids: Optional[list[str]] = Field(
        default_factory=list,
        description="待核查的具体消息 ID 列表（例如 ['msg_01', 'msg_02']）。优先使用 open_items 中已有 ID。"
    )
    keywords: Optional[str] = Field(
        default="",
        description="用于精确定位的 2~4 字实体关键词，仅在没有具体 message_id 时作为备选。"
    )
    limit: int = Field(
        default=15,
        ge=1,
        le=30,
        description="返回消息条数上限（含每条命中消息前后各 1 条上下文），取值范围 1~30，默认 15。"
    )


class GetPastMessagesTool(BaseTool):
    """原始历史聊天记录下钻检索工具（生产级 BaseTool 实现）。"""
    name: str = "get_past_messages"
    description: str = (
        "精准调取当前客户在 analysis_date 之前的原始历史聊天记录。"
        "适用场景：当今日出现文书排期争议、选校/加申分歧、或客户指责‘与前期承诺/约定不符’且快照摘要信息不足时，按 message_ids 或关键词下钻核实双方前期约定原话，以准确界定风控预警类型与责任归属。历史消息严禁作为今日温度变动的直接证据。"
    )
    args_schema: Any = GetMessagesInput

    _all_messages: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def __init__(self, all_messages: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._all_messages = all_messages or []

    def _run(self, message_ids: list[str] | None = None, keywords: str | None = "", limit: int = 15) -> str:
        # 双空参数防御：若未提供 message_ids 且关键词为空，直接返回空列表
        raw_ids = message_ids or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        ids = {str(mid).strip() for mid in raw_ids if str(mid).strip()}
        safe_keywords = keywords or ""
        tokens = tokenize_chinese(safe_keywords)

        if not ids and not tokens:
            return json.dumps([], ensure_ascii=False)

        actual_limit = max(1, min(limit, 30))
        total_msgs = len(self._all_messages)
        kept_id_hits = [
            i for i, m in enumerate(self._all_messages)
            if str(m.get("message_id", "")) in ids
        ][-actual_limit:] if ids else []
        selected_indices: set[int] = set(kept_id_hits)

        def try_add(*candidates: int) -> bool:
            for c in candidates:
                if 0 <= c < total_msgs:
                    selected_indices.add(c)
                    if len(selected_indices) >= actual_limit:
                        return True
            return len(selected_indices) >= actual_limit

        # 优先补充精准命中 ID 的前后各 1 条上下文（从新到旧），不足再按关键词命中分值与时间倒序补充
        for idx in reversed(kept_id_hits):
            if try_add(idx - 1, idx + 1):
                break

        if len(selected_indices) < actual_limit and tokens:
            kw_hits = sorted(
                (
                    (score, i)
                    for i, m in enumerate(self._all_messages)
                    if (score := sum(1 for t in tokens if t in (m.get("text") or "").lower())) > 0
                ),
                reverse=True,
            )
            for _, idx in kw_hits:
                if try_add(idx, idx - 1, idx + 1):
                    break

        return json.dumps(
            [self._all_messages[i] for i in sorted(selected_indices)],
            ensure_ascii=False,
        )

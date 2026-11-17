"""历史每日快照摘要检索工具（BaseTool 子类化实现）。"""

from __future__ import annotations

import json
from typing import Any, Optional
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool

from .base import tokenize_chinese


class SearchSnapshotInput(BaseModel):
    """历史快照摘要检索工具入参定义。"""
    keywords: Optional[str] = Field(
        default="",
        description="检索关键词（空格分隔）。若留空，默认按日期倒序返回最近几天的快照摘要。"
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=5,
        description="返回快照条数上限，取值范围 1~5，默认 5。"
    )


class SearchPastSnapshotsTool(BaseTool):
    """历史每日快照摘要检索工具（生产级 BaseTool 实现）。"""
    name: str = "search_past_snapshots"
    description: str = (
        "检索当前客户在 analysis_date 之前的历史每日快照摘要（包含历史事实总结、历史预警与温度变化理由；keywords 留空默认按倒序返回最近 5 个切片的快照）。"
        "适用场景：1. 今日仅有顾问单向询问/催促或系统 bot 播报申请周期倒计时而客户零发言时，调用本工具（keywords=''）核查最近几个切片是否连续处于催促无回应、材料逾期或语言未达标状态；"
        "2. 今日对话提及‘之前提过的/上次说的’或出现排期、定校、退费、文书争议时，调用本工具了解阶段性历史背景与跨日演变态势。"
    )
    args_schema: Any = SearchSnapshotInput

    # 数据上下文通过 PrivateAttr 注入：大模型不可见、Python 本地可用、完全支持 Pickle
    _snapshots: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _indexed: list[tuple[dict[str, Any], str]] = PrivateAttr(default_factory=list)

    def __init__(self, snapshots: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._snapshots = sorted(
            [s for s in (snapshots or []) if isinstance(s, dict)],
            key=lambda s: str(s.get("analysis_date") or ""),
            reverse=True,
        )
        self._indexed = [(s, str(s.get("summary") or "").lower()) for s in self._snapshots]

    def _run(self, keywords: str | None = "", limit: int = 5) -> str:
        actual_limit = max(1, min(limit, 5))
        tokens = tokenize_chinese(keywords or "")
        if not tokens:
            return json.dumps(self._snapshots[:actual_limit], ensure_ascii=False)

        scored = [
            (sum(1 for t in tokens if t in text), s.get("analysis_date", ""), s)
            for s, text in self._indexed
        ]
        results = [s for score, _, s in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True) if score > 0][:actual_limit]
        return json.dumps(results, ensure_ascii=False)

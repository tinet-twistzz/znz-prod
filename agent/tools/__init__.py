"""检索工具包统一导出与装配工厂。"""

from __future__ import annotations

from typing import Any
from langchain_core.tools import BaseTool

from .base import collect_message_ids, tokenize_chinese
from .snapshot_tools import SearchPastSnapshotsTool, SearchSnapshotInput
from .message_tools import GetMessagesInput, GetPastMessagesTool
from .alert_tools import (
    GetAlertTypeDetailsInput,
    GetAlertTypeDetailsTool,
    load_alert_rules_index,
)


def build_customer_tools(
    payload: dict[str, Any],
    prior_snapshots: list[dict[str, Any]],
    current_day: str,
) -> list[BaseTool]:
    """为指定客户和指定分析日装配只读检索工具（生命周期随本次分析绑定）。
    
    1. 严格只读隔离：仅注入日期早于 current_day 的历史快照与消息；
    2. 上下文瘦身：过滤掉原始消息中的无关冗余元数据；
    3. 面向对象子类化：完全支持 Pickle 跨进程/跨线程序列化。
    """
    # 提取当前分析日之前的历史消息
    all_past_messages = [
        {
            "message_id": m.get("message_id"),
            "sent_at": m.get("sent_at"),
            "sender_name": m.get("sender_name"),
            "role": m.get("role"),
            "text": m.get("text"),
        }
        for day, msgs in payload.get("days", {}).items()
        if day < current_day
        for m in (msgs or [])
    ]

    # 提取当前分析日之前的历史快照瘦身版
    valid_snapshots = [
        {
            "analysis_date": s.get("analysis_date"),
            "summary": s.get("summary"),
            "alert": s.get("alert"),
            "temperature_change": s.get("temperature_change", {}),
        }
        for s in prior_snapshots
        if isinstance(s, dict) and str(s.get("analysis_date") or "") < current_day
    ]

    return [
        SearchPastSnapshotsTool(snapshots=valid_snapshots),
        GetPastMessagesTool(all_messages=all_past_messages),
        GetAlertTypeDetailsTool(),
    ]


__all__ = [
    "tokenize_chinese",
    "collect_message_ids",
    "SearchSnapshotInput",
    "SearchPastSnapshotsTool",
    "GetMessagesInput",
    "GetPastMessagesTool",
    "GetAlertTypeDetailsInput",
    "GetAlertTypeDetailsTool",
    "load_alert_rules_index",
    "build_customer_tools",
]

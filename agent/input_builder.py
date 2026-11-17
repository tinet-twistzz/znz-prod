"""输入数据加载、校验、上下文构建与精简瘦身。"""

from __future__ import annotations

from typing import Any

from .config import MULTI_LABEL


def compact_chat_message(message: dict[str, Any]) -> dict[str, Any]:
    """精简单条消息，剥离无关的来源哈希等元数据，节省输入 Token。"""
    if not isinstance(message, dict):
        return {"message_id": "", "sent_at": "", "sender_name": "", "role": "unknown", "text": ""}
    return {
        "message_id": str(message.get("message_id") or ""),
        "sent_at": str(message.get("sent_at") or ""),
        "sender_name": str(message.get("sender_name") or ""),
        "role": str(message.get("role") or "unknown"),
        "text": str(message.get("text") or ""),
    }


def compact_previous_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """仅向模型透传昨日快照的核心业务结论（保留 summary；若存在非空 alert 亦一并保留并补齐 types 与 detected_at）。"""
    if not isinstance(snapshot, dict):
        return None
    out: dict[str, Any] = {}
    if "summary" in snapshot:
        out["summary"] = snapshot["summary"]
    alert = snapshot.get("alert")
    if isinstance(alert, dict) and (alert.get("types") or alert.get("type")):
        compacted_alert = dict(alert)
        raw_type = compacted_alert.pop("type", None)
        raw_types = compacted_alert.get("types")
        norm_types: list[str] = []
        if isinstance(raw_type, str) and raw_type.strip():
            norm_types.append(raw_type.strip())
        if isinstance(raw_types, list):
            for item in raw_types:
                if isinstance(item, str) and item.strip():
                    t = item.strip()
                    if t not in norm_types:
                        norm_types.append(t)
        if norm_types:
            compacted_alert["types"] = norm_types
            out["alert"] = compacted_alert
    return out


def compact_evidence_ids(ids: Any, max_count: int = 4) -> list[str]:
    """对证据消息 ID 列表进行首尾窗口裁剪瘦身（默认保留首条创建证据 + 最近 max_count - 1 条进展证据）。"""
    if not isinstance(ids, list):
        return []
    valid = [x for x in ids if isinstance(x, str) and x.strip()]
    if len(valid) <= max_count:
        return valid
    return [valid[0], *valid[-(max_count - 1):]]


def build_rolling_input(
    day: str,
    payload: dict[str, Any],
    previous_snapshot: dict[str, Any] | None,
    current_state: dict[str, Any],
    multi_label: bool = MULTI_LABEL,
) -> dict[str, Any]:
    """多日滚动渐进披露输入构建（核心流水线输入装配器）。"""
    items = current_state.get("items", {})
    open_items = []
    if isinstance(items, dict):
        for item in items.values():
            if isinstance(item, dict) and item.get("status") in {"open", "in_progress", "needs_review"}:
                t = item.get("title") or item.get("content") or ""
                entry = {
                    key: value for key, value in item.items()
                    if key in {
                        "id", "kind", "title", "owner", "due_at", "status",
                        "application_id", "progress", "evidence_message_ids",
                    }
                }
                entry["title"] = t
                if "evidence_message_ids" in entry:
                    entry["evidence_message_ids"] = compact_evidence_ids(entry["evidence_message_ids"])
                open_items.append(entry)

    model_state: dict[str, Any] = {
        key: current_state[key]
        for key in ("schema_version", "state_revision", "temperature", "as_of_date")
        if key in current_state
    }
    profile_proj: dict[str, dict[str, Any]] = {}
    for item_id, item in current_state.get("profile", {}).items():
        if isinstance(item, dict):
            entry = {
                key: value for key, value in item.items()
                if key in {"id", "fact", "value", "evidence_message_ids"}
            }
            if "evidence_message_ids" in entry:
                entry["evidence_message_ids"] = compact_evidence_ids(entry["evidence_message_ids"])
            profile_proj[item_id] = entry
    model_state["profile"] = profile_proj

    applications_proj: dict[str, dict[str, Any]] = {}
    for item_id, item in current_state.get("applications", {}).items():
        if isinstance(item, dict):
            entry = {
                key: value for key, value in item.items()
                if key in {
                    "id", "project", "stage", "status", "current_status",
                    "next_step", "owner", "evidence_message_ids",
                }
            }
            if "evidence_message_ids" in entry:
                entry["evidence_message_ids"] = compact_evidence_ids(entry["evidence_message_ids"])
            applications_proj[item_id] = entry
    model_state["applications"] = applications_proj

    messages = (payload.get("days") or {}).get(day) or []
    return {
        "analysis_date": day,
        "as_of_time": max(
            (m.get("sent_at", "") for m in messages if isinstance(m, dict) and m.get("sent_at")),
            default=f"{day}T23:59:59+08:00",
        ),
        "multi_label": bool(multi_label),
        "previous_snapshot": compact_previous_snapshot(previous_snapshot),
        "current_state": model_state,
        "open_items": open_items,
        "messages": [compact_chat_message(m) for m in messages if isinstance(m, dict)],
    }

"""纯代码确定性业务状态机与温度钳制引擎。"""

from __future__ import annotations

import json
from typing import Any


def apply_state_changes(
    current_state: dict[str, Any],
    result: dict[str, Any],
    day: str,
    as_of_time: str | None = None,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """确定性状态变更器（纯 Python 领域规则执行，零模型幻觉）。
    
    1. 应用 profile / applications / items 的 add、update、close 操作；
    2. 计算温度变化并执行 [0, 100] 及前 7 天冷启动最高 70 分的业务底线；
    3. 自增 state_revision 并记录 as_of_date。
    """
    state = json.loads(json.dumps(current_state, ensure_ascii=False))
    for collection in ("profile", "applications", "items"):
        state.setdefault(collection, {})

    for change in result.get("state_changes", []) if isinstance(result.get("state_changes"), list) else []:
        if not isinstance(change, dict):
            continue
        collection = change.get("collection")
        item_id = str(change.get("id") or "").strip()
        op = change.get("op")
        raw_patch = change.get("patch")
        patch: dict[str, Any] = raw_patch if isinstance(raw_patch, dict) else {}

        if collection not in {"profile", "applications", "items"} or not item_id:
            continue

        bucket = state[collection]
        if collection == "items":
            legacy_c = patch.pop("content", None)
            title = patch.get("title") or legacy_c
            if title:
                patch["title"] = title
            if isinstance(bucket.get(item_id), dict):
                bucket[item_id].pop("content", None)

        existing_evidence = bucket.get(item_id, {}).get("evidence_message_ids", []) if isinstance(bucket.get(item_id), dict) else []
        existing_evidence = existing_evidence if isinstance(existing_evidence, list) else []
        new_evidence = change.get("message_ids", [])
        new_evidence = new_evidence if isinstance(new_evidence, list) else []
        evidence_ids = list(dict.fromkeys(existing_evidence + new_evidence))

        changed_at = as_of_time or f"{day}T23:59:59+08:00"

        if op == "add" and item_id not in bucket:
            if collection == "items":
                patch["status"] = patch.get("status") or "open"
            bucket[item_id] = {
                "id": item_id,
                **patch,
                "evidence_message_ids": evidence_ids,
                "created_at": changed_at,
                "updated_at": changed_at,
            }
        elif op == "update" and item_id in bucket:
            bucket[item_id].update(patch)
            bucket[item_id]["evidence_message_ids"] = evidence_ids
            bucket[item_id]["updated_at"] = changed_at
        elif op == "close" and collection == "items" and item_id in bucket:
            bucket[item_id].update(patch)
            bucket[item_id]["evidence_message_ids"] = evidence_ids
            if bucket[item_id].get("status") in {"fulfilled", "cancelled"}:
                # 结案事项移出活跃集合，保留在历史审计中
                del bucket[item_id]

    # 温度增减计算
    raw_delta = result.get("snapshot", {}).get("temperature_change", {}).get("delta", 0)
    try:
        delta = int(raw_delta)
    except (TypeError, ValueError):
        delta = 0

    # 业务幂等锁：防连环扣分。
    # 1) 若今日扣分证据完全是昨日已扣分/风险证据的子集（旧证据复读），或
    # 2) 昨日已存在扣分且今日无实质风险/待办事项变更（避免顺手更新 profile/applications 绕过幂等锁），
    # 强制令 delta = 0，避免同一未解决问题连续多日重复扣分。
    if delta < 0 and isinstance(previous_snapshot, dict):
        raw_prev_change = previous_snapshot.get("temperature_change")
        prev_change = raw_prev_change if isinstance(raw_prev_change, dict) else {}
        try:
            prev_delta = int(prev_change.get("delta", 0))
        except (TypeError, ValueError):
            prev_delta = 0

        prev_penalty_ids: set[str] = set()
        if prev_delta < 0:
            for mid in prev_change.get("message_ids") or []:
                if isinstance(mid, str) and mid.strip():
                    prev_penalty_ids.add(mid)
        prev_alert = previous_snapshot.get("alert")
        if isinstance(prev_alert, dict):
            for mid in prev_alert.get("message_ids") or []:
                if isinstance(mid, str) and mid.strip():
                    prev_penalty_ids.add(mid)

        raw_curr_snapshot = result.get("snapshot")
        curr_snapshot = raw_curr_snapshot if isinstance(raw_curr_snapshot, dict) else {}
        raw_curr_temp = curr_snapshot.get("temperature_change")
        curr_temp = raw_curr_temp if isinstance(raw_curr_temp, dict) else {}
        curr_temp_ids = {
            m for m in (curr_temp.get("message_ids") or []) if isinstance(m, str) and m.strip()
        }
        curr_alert = curr_snapshot.get("alert")
        has_curr_risk_or_alert = bool(curr_alert) and (
            not isinstance(curr_alert, dict) or bool(curr_alert.get("types") or curr_alert.get("type") or curr_alert.get("details"))
        )
        raw_state_changes = result.get("state_changes")
        state_changes = raw_state_changes if isinstance(raw_state_changes, list) else []
        item_changes = [
            c for c in state_changes if isinstance(c, dict) and c.get("collection") == "items"
        ]

        evidence_repeated = bool(prev_penalty_ids and curr_temp_ids and curr_temp_ids.issubset(prev_penalty_ids))
        consecutive_without_escalation = prev_delta < 0 and (
            not state_changes or (not has_curr_risk_or_alert and not item_changes)
        )
        if evidence_repeated or consecutive_without_escalation:
            delta = 0

    # 业务边界钳制：区间 [0, 100]
    temperature = max(0, min(100, int(state.get("temperature", 65)) + delta))
    
    # 冷启动规则：前 7 天有效服务周期内最高不超过 70 分
    current_rev = int(state.get("state_revision", 0))
    if current_rev + 1 < 7:
        temperature = min(70, temperature)

    state["temperature"] = temperature
    state["as_of_date"] = day
    state["state_revision"] = current_rev + 1
    return state

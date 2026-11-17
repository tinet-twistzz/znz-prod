"""Fail-Closed 严格输出校验器与四道证据隔离门禁。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import MULTI_LABEL
from .contracts import (
    ALLOWED_ALERT_TYPES,
    ALLOWED_OWNERS,
    APPLICATION_STAGES,
    APPLICATION_STATUSES,
    DEFAULT_ALERT_OWNER_BY_TYPE,
    ITEM_KINDS,
    ITEM_STATUSES_ACTIVE,
    ITEM_STATUSES_CLOSED,
    PATCH_ALLOWED_FIELDS,
    extract_iso_date,
    normalize_application_stage,
    normalize_application_status,
    normalize_owner,
    normalize_state_patch,
)
from .tools.alert_tools import load_alert_rules_index

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def validate_model_output(
    result: dict[str, Any],
    day: str,
    current_state: dict[str, Any],
    available_evidence_ids: set[str],
    current_message_ids: set[str] | None = None,
    today_customer_message_ids: set[str] | None = None,
    queried_alert_types: set[str] | None = None,
    multi_label: bool = MULTI_LABEL,
) -> None:
    """对大模型分析结果执行 Fail-Closed 强校验与双层证据及渐进式核验门禁。"""

    def exact_keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{path} 必须是对象")
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(f"{path} 字段不匹配；缺少 {missing}，未知 {unknown}")
        return value

    def evidence_ids(value: Any, path: str, allowed: set[str], *, required: bool) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{path} 必须是字符串数组")
        if required and not value:
            raise ValueError(f"{path} 不能为空")
        invalid = set(value) - allowed
        if invalid:
            raise ValueError(f"{path} 包含不可用的 message_id: {sorted(invalid)}")
        return value

    today_all = current_message_ids or set()

    # 1. 顶层结构校验
    exact_keys(result, {"snapshot", "state_changes"}, "顶层")
    if isinstance(result["snapshot"], dict):
        result["snapshot"].setdefault("alert", None)
    snapshot = exact_keys(
        result["snapshot"],
        {"analysis_date", "summary", "alert", "temperature_change"},
        "snapshot",
    )
    if snapshot["analysis_date"] != day:
        raise ValueError(f"snapshot.analysis_date ({snapshot['analysis_date']}) 应为 {day}")
    if not isinstance(snapshot["summary"], str) or not snapshot["summary"].strip():
        raise ValueError("snapshot.summary 必须是非空字符串")

    # 2. 校验 alert（含六字段结构、detected_at UTC+8 校验、单/多标签约束、rule_ids 存在性与归属强校验、渐进式必调工具门禁 1、以及沉默逾期纯历史证据门禁 4）
    raw_alert = snapshot["alert"]
    if raw_alert is not None:
        if isinstance(raw_alert, dict):
            # 1) 兼容旧格式传入的 type，统一归并到 types 并移除冗余字段 type
            legacy_type = raw_alert.pop("type", None)
            if ("types" not in raw_alert or raw_alert.get("types") is None) and isinstance(legacy_type, str) and legacy_type.strip():
                raw_alert["types"] = [legacy_type.strip()]

            # 2) 若尚未包含 detected_at 或值为 None/空白，自动用当前 UTC+8 系统时间补齐
            if "detected_at" not in raw_alert or raw_alert.get("detected_at") is None or (
                isinstance(raw_alert.get("detected_at"), str) and not raw_alert["detected_at"].strip()
            ):
                raw_alert["detected_at"] = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

            # 3) 若尚未包含 owner 或值为 None/空白，按首个预警类型补齐默认责任人角色
            if "owner" not in raw_alert or raw_alert.get("owner") is None or (
                isinstance(raw_alert.get("owner"), str) and not raw_alert["owner"].strip()
            ):
                ts_hint = raw_alert.get("types")
                p_type_hint = str(ts_hint[0]).strip() if isinstance(ts_hint, list) and ts_hint else ""
                raw_alert["owner"] = DEFAULT_ALERT_OWNER_BY_TYPE.get(p_type_hint, "advisor")

        alert = exact_keys(
            raw_alert,
            {"types", "owner", "details", "message_ids", "rule_ids", "detected_at"},
            "snapshot.alert",
        )
        allowed_alert_types: set[str] = ALLOWED_ALERT_TYPES | {
            str(info["alert_type"]) for info in load_alert_rules_index().values() if info.get("alert_type")
        }

        raw_owner = alert["owner"]
        if not isinstance(raw_owner, str) or not raw_owner.strip():
            raise ValueError("snapshot.alert.owner 必须是非空字符串")
        norm_owner = normalize_owner(raw_owner)
        if not norm_owner or norm_owner not in ALLOWED_OWNERS:
            raise ValueError(
                f"snapshot.alert.owner 无效: {raw_owner}，必须属于合法责任人角色: {sorted(ALLOWED_OWNERS)}"
            )
        alert["owner"] = norm_owner

        raw_types = alert["types"]
        if not isinstance(raw_types, list) or not raw_types or any(
            not isinstance(item, str) or not item.strip() for item in raw_types
        ):
            raise ValueError("snapshot.alert.types 必须是非空字符串数组")

        norm_types: list[str] = []
        for item in raw_types:
            t_clean = item.strip()
            if t_clean not in allowed_alert_types:
                raise ValueError(
                    f"snapshot.alert.types 包含无效类型: {t_clean}，必须属于合法预警类型: {sorted(allowed_alert_types)}"
                )
            if t_clean not in norm_types:
                norm_types.append(t_clean)

        if not multi_label and len(norm_types) > 1:
            raise ValueError(
                f"snapshot.alert.types 当前为单标签模式 (multi_label=False)，仅允许输出 1 个最核心的风险类型，实际输出了 {len(norm_types)} 个: {norm_types}"
            )

        alert["types"] = norm_types

        detected_at = alert["detected_at"]
        if not isinstance(detected_at, str) or not detected_at.strip():
            raise ValueError("snapshot.alert.detected_at 必须是非空字符串")
        detected_at_clean = detected_at.strip()
        try:
            dt_parsed = datetime.fromisoformat(detected_at_clean)
        except ValueError as exc:
            raise ValueError(
                f"snapshot.alert.detected_at 必须是合法的 RFC 3339 / ISO 8601 时间字符串: {detected_at}"
            ) from exc
        if dt_parsed.utcoffset() != timedelta(hours=8) or not detected_at_clean.endswith("+08:00"):
            raise ValueError(
                f"snapshot.alert.detected_at 时区必须严格为 UTC+8 (+08:00): {detected_at}"
            )
        alert["detected_at"] = detected_at_clean

        if not isinstance(alert["details"], str) or not alert["details"].strip():
            raise ValueError("snapshot.alert.details 必须是非空字符串")

        ids = evidence_ids(
            alert["message_ids"],
            "snapshot.alert.message_ids",
            available_evidence_ids,
            required=True,
        )

        raw_rule_ids = alert["rule_ids"]
        if not isinstance(raw_rule_ids, list) or any(not isinstance(item, str) for item in raw_rule_ids):
            raise ValueError("snapshot.alert.rule_ids 必须是字符串数组")
        stripped_rids = [item.strip().strip("[]").strip() for item in raw_rule_ids]
        if not stripped_rids or any(not rid for rid in stripped_rids):
            raise ValueError("snapshot.alert.rule_ids 必须是非空字符串数组")
        norm_rule_ids = list(dict.fromkeys(stripped_rids))
        alert["rule_ids"] = norm_rule_ids

        # 门禁 1：渐进式核验必调工具门禁
        if queried_alert_types is not None:
            for t_item in norm_types:
                if t_item not in queried_alert_types:
                    raise ValueError(
                        f"snapshot.alert 触发了『{t_item}』预警，但本轮未调用 get_alert_type_details 工具核实该类型的 details！请先调用 get_alert_type_details 查询后再输出预警结论。"
                    )

        rules_index = load_alert_rules_index()
        allowed_types_set = set(norm_types)
        allowed_for_types = sorted(
            rid for rid, info in rules_index.items() if info.get("alert_type") in allowed_types_set
        )
        covered_types: set[str] = set()
        for rid in norm_rule_ids:
            if rid not in rules_index:
                raise ValueError(
                    f"snapshot.alert.rule_ids 包含不存在的规则编号: {rid}；"
                    f"当前『{'/'.join(norm_types)}』下所有合法的 rule_id 列表: {allowed_for_types}"
                )
            actual_type = rules_index[rid].get("alert_type")
            if actual_type not in allowed_types_set:
                target_desc = f"『{norm_types[0]}』" if len(norm_types) == 1 else f"『{'/'.join(norm_types)}』"
                raise ValueError(
                    f"snapshot.alert.rule_ids 类别归属不匹配: 规则 '{rid}' 属于『{actual_type}』，而非当前触发的{target_desc}；"
                    f"当前{target_desc}下所有合法的 rule_id 列表: {allowed_for_types}"
                )
            covered_types.add(actual_type)

        for t_item in norm_types:
            if t_item not in covered_types:
                allowed_for_single = sorted(
                    rid for rid, info in rules_index.items() if info.get("alert_type") == t_item
                )
                raise ValueError(
                    f"snapshot.alert.types 中的触发类型『{t_item}』在 rule_ids 中缺少对应的规则编号支撑；"
                    f"请为每个触发类型提供至少 1 条归属该类型的规则 ID。当前『{t_item}』下所有合法的 rule_id 列表: {allowed_for_single}"
                )

        # 门禁 4：若该预警没有引用任何当天的消息，完全由历史消息支撑，必须匹配既有已超期未闭环事项
        if today_all and not (set(ids) & today_all):
            items_bucket = current_state.get("items", {})
            matched = False
            if isinstance(items_bucket, dict):
                for open_item in items_bucket.values():
                    if isinstance(open_item, dict):
                        ev_ids = set(open_item.get("evidence_message_ids", []))
                        due_date = extract_iso_date(open_item.get("due_at"))
                        is_overdue = (
                            open_item.get("status") in {"open", "in_progress"}
                            and due_date is not None
                            and due_date < day
                        )
                        if (set(ids) & ev_ids) and is_overdue:
                            matched = True
                            break
            if not matched:
                raise ValueError(
                    "snapshot.alert 属于完全由历史消息构成的风险，但未匹配到当前任何明确超期的未闭环承诺事项！"
                )

    # 4. 校验 temperature_change 与门禁 2（温度铁律：变动必须由当天事实触发）
    change = exact_keys(
        snapshot["temperature_change"],
        {"delta", "reason", "message_ids"},
        "snapshot.temperature_change",
    )
    delta = change["delta"]
    if isinstance(delta, bool) or not isinstance(delta, int) or not -100 <= delta <= 100:
        raise ValueError("snapshot.temperature_change.delta 必须是 -100..100 的整数")
    if not isinstance(change["reason"], str):
        raise ValueError("snapshot.temperature_change.reason 必须是字符串")

    ids = evidence_ids(
        change["message_ids"],
        "snapshot.temperature_change.message_ids",
        available_evidence_ids,
        required=delta != 0,
    )
    # 门禁 2：delta != 0 时，不能完全依赖工具召回的历史消息，必须至少包含一条当天的证据
    if delta != 0 and today_all:
        if not (set(ids) & today_all):
            raise ValueError(
                "snapshot.temperature_change 的 delta 非零，但未包含任何当天的聊天证据！"
                "历史事件不得在当天无新事实时重复对温度进行加减分！"
            )

    # 5. 校验 state_changes 与门禁 3（事项履约必须引用当天证据）
    changes = result["state_changes"]
    if not isinstance(changes, list):
        raise ValueError("state_changes 必须是数组")

    # 收集当前可用及本次新增的全部 applications 实体 ID（供 items.application_id 外键强校验）
    valid_application_ids: set[str] = set()
    current_apps = current_state.get("applications", {})
    if isinstance(current_apps, dict):
        valid_application_ids.update(
            app_id for app_id in current_apps.keys() if isinstance(app_id, str) and app_id.strip()
        )
    for change_item in changes:
        if isinstance(change_item, dict) and change_item.get("collection") == "applications":
            c_id = change_item.get("id")
            if isinstance(c_id, str) and c_id.strip():
                valid_application_ids.add(c_id.strip())

    seen_keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(changes):
        path = f"state_changes[{index}]"
        item = exact_keys(raw, {"op", "collection", "id", "patch", "message_ids"}, path)
        op, collection, item_id = item["op"], item["collection"], item["id"]

        if (not isinstance(op, str) or op not in {"add", "update", "close"}
                or not isinstance(collection, str) or collection not in PATCH_ALLOWED_FIELDS):
            raise ValueError(f"{path} 的 op 或 collection 无效")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(f"{path}.id 必须是非空字符串")

        key = (collection, item_id)
        if key in seen_keys:
            raise ValueError(f"{path} 重复操作同一状态 ID: {key}")
        seen_keys.add(key)

        patch = item["patch"]
        if not isinstance(patch, dict) or not patch:
            raise ValueError(f"{path}.patch 必须是非空对象")

        normalize_state_patch(collection, patch)
        if not patch:
            raise ValueError(f"{path}.patch 必须是非空对象")

        bucket = current_state.get(collection, {})
        exists = isinstance(bucket, dict) and item_id in bucket

        def _has_non_empty_str(val: Any) -> bool:
            return isinstance(val, str) and bool(val.strip())

        def _is_minimally_complete_for_add(coll: str, p: dict[str, Any]) -> bool:
            if coll == "items":
                return (
                    _has_non_empty_str(p.get("title"))
                    and p.get("status") not in ITEM_STATUSES_CLOSED
                )
            if coll == "profile":
                return _has_non_empty_str(p.get("fact")) and _has_non_empty_str(p.get("value"))
            if coll == "applications":
                return _has_non_empty_str(p.get("project"))
            return False

        # 已存在事项的结案动作/状态双向容错归一化
        if collection == "items" and exists:
            if op in {"add", "update"} and patch.get("status") in ITEM_STATUSES_CLOSED:
                op = item["op"] = "close"
            elif op == "close" and ("status" not in patch or patch["status"] is None):
                patch["status"] = "fulfilled"

        if op == "add" and exists:
            # 幂等容错：若大模型对已有条目提议 add，程序层自动安全归一化为 update
            op = item["op"] = "update"
        if op in {"update", "close"} and not exists:
            if op == "update":
                if not _is_minimally_complete_for_add(collection, patch):
                    raise ValueError(
                        f"{path} 试图更新不存在或已结案的 {collection} ({item_id})，且补丁缺少新建所需的完整字段"
                    )
                # 容错：若大模型对新条目误标 update 且具备新建完整字段，程序层自动安全归一化为 add
                op = item["op"] = "add"
            else:
                raise ValueError(f"{path} {op} 的 ID 不存在: {item_id}")
        if op == "close" and collection != "items":
            raise ValueError(f"{path} close 只允许用于 items 集合")

        if op == "add":
            if collection == "items":
                if not _has_non_empty_str(patch.get("title")):
                    raise ValueError(f"{path} 新增 items ({item_id}) 必须包含非空字符串 title")
                if "status" not in patch or patch["status"] is None:
                    patch["status"] = "open"
            elif collection == "profile":
                if not (_has_non_empty_str(patch.get("fact")) and _has_non_empty_str(patch.get("value"))):
                    raise ValueError(f"{path} 新增 profile ({item_id}) 必须同时包含非空字符串 fact 和 value")
            elif collection == "applications":
                if not _has_non_empty_str(patch.get("project")):
                    raise ValueError(f"{path} 新增 applications ({item_id}) 必须包含非空字符串 project")

        unknown_fields = set(patch) - PATCH_ALLOWED_FIELDS[collection]
        if unknown_fields:
            raise ValueError(f"{path}.patch 包含未定义字段: {sorted(unknown_fields)}")

        if "owner" in patch:
            norm_owner = normalize_owner(patch["owner"])
            if norm_owner is None:
                patch.pop("owner", None)
            else:
                patch["owner"] = norm_owner
                if patch["owner"] not in ALLOWED_OWNERS:
                    raise ValueError(
                        f"{path}.patch.owner 无效: {patch['owner']}，必须属于 6 大业务责任人之一: {sorted(ALLOWED_OWNERS)}"
                    )

        if "due_at" in patch and patch["due_at"] is not None:
            if isinstance(patch["due_at"], str) and patch["due_at"].strip().lower() in {
                "", "待定", "未定", "无", "暂无", "null", "none",
            }:
                patch["due_at"] = None

        if collection == "applications":
            if "stage" in patch:
                patch["stage"] = normalize_application_stage(patch["stage"])
            if "status" in patch:
                patch["status"] = normalize_application_status(patch["status"])

        if collection == "items":
            if "kind" in patch:
                raw_kind = patch["kind"]
                if raw_kind is None or (isinstance(raw_kind, str) and not raw_kind.strip()):
                    patch["kind"] = "todo"
                elif isinstance(raw_kind, str) and raw_kind.strip().lower() in ITEM_KINDS:
                    patch["kind"] = raw_kind.strip().lower()
                else:
                    raise ValueError(
                        f"{path}.patch.kind 无效: {raw_kind}，必须属于 {sorted(ITEM_KINDS)}"
                    )
            if "status" in patch:
                allowed_status = ITEM_STATUSES_CLOSED if op == "close" else ITEM_STATUSES_ACTIVE
                if patch["status"] not in allowed_status:
                    raise ValueError(f"{path}.patch.status 无效: {patch['status']}")

            # 外键与格式强校验：防止大模型内省思维链泄露污染 application_id
            if "application_id" in patch:
                app_id = patch["application_id"]
                if app_id is not None:
                    if not isinstance(app_id, str):
                        raise ValueError(f"{path}.patch.application_id 必须是字符串或 null")
                    app_id_clean = app_id.strip()
                    if not app_id_clean:
                        # 空字符串安全规整为 None
                        patch["application_id"] = None
                    else:
                        # 1. 格式防御：严禁包含空白字符、换行或超长文本（彻底阻断模型思维链/内省草稿外泄）
                        if any(c.isspace() for c in app_id_clean) or len(app_id_clean) > 64:
                            raise ValueError(
                                f"{path}.patch.application_id 格式非法（检测到空白字符或超长，疑似思维链泄露）: {repr(app_id[:60])}"
                            )
                        # 2. 外键强约束：引用的 application_id 必须存在于已知或本次新增的申请实体中
                        if app_id_clean not in valid_application_ids:
                            raise ValueError(
                                f"{path}.patch.application_id 外键悬空: 申请实体 '{app_id_clean}' 不存在！"
                                f"当前可用申请列表: {sorted(valid_application_ids)}"
                            )
                        patch["application_id"] = app_id_clean

        if op == "close" and patch.get("status") not in ITEM_STATUSES_CLOSED:
            raise ValueError(f"{path} close 必须将 items.status 设为 fulfilled 或 cancelled")

        evidence_required = not (
            op == "update" and collection == "items" and patch.get("status") == "needs_review"
        )
        ch_ids = evidence_ids(
            item["message_ids"],
            f"{path}.message_ids",
            available_evidence_ids,
            required=evidence_required,
        )

        # 门禁 3：close 且为 fulfilled 时，必须包含当天的履约消息
        if op == "close" and patch.get("status") == "fulfilled" and today_all:
            if not (set(ch_ids) & today_all):
                raise ValueError(
                    f"{path} close fulfilled 事项必须引用当天的履约事实证据，不能仅引用历史承诺 ID！"
                )

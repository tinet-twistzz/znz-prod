"""飞书风险预警机器人（配置驱动解耦版）：
通过独立配置文件 `feishu_card_config.yaml` 将「主系统 (agent/) 内部契约」与「飞书推送字段、卡片排版、回送字段名及按钮选项」彻底解耦。
支持热读取 `feishu_card_config.yaml`：修改 YAML 中的字段映射、抬头、按钮文字或回送字段名无需修改任何 Python 代码，亦无需重启长连接进程。
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import lark_oapi as lark
import yaml
from lark_oapi.api.bitable.v1 import (
    AppTableField,
    AppTableRecord,
    Condition,
    CreateAppTableFieldRequest,
    CreateAppTableRecordRequest,
    DeleteAppTableRecordRequest,
    FilterInfo,
    ListAppTableFieldRequest,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    UpdateAppTableFieldRequest,
    UpdateAppTableRecordRequest,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ListChatRequest,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackCard,
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from db_adapter import DEFAULT_DB_PATH, DEFAULT_YAML_PATH, connect_db, init_production_db
from rule_patrol.patrol_engine import DEFAULT_CONFIG_PATH, load_patrol_config, trigger_rule_rework_hook

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CARD_CONFIG_PATH = Path(os.environ.get("FEISHU_CARD_CONFIG_PATH", PROJECT_DIR / "feishu_card_config.yaml"))
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

ROLE_CN_MAP: dict[str, str] = {
    "advisor": "售后主导师",
    "writer": "文案导师",
    "planner": "规划师",
    "sales": "销售顾问",
    "student": "学生",
    "parent": "家长",
}


def load_card_config(config_path: Path | str = DEFAULT_CARD_CONFIG_PATH) -> dict[str, Any]:
    """实时加载飞书卡片与字段映射配置文件（热加载，无需重启进程）。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"未找到飞书卡片配置文件: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("feishu_card_config.yaml 顶层必须是字典")
    return raw


def load_feishu_credentials() -> tuple[str, str]:
    """从环境变量、.env.local 或 .env 加载 FEISHU_APP_ID 与 FEISHU_APP_SECRET。"""
    for env_file in (
        PROJECT_DIR / ".env.local",
        PROJECT_DIR / ".env",
        PROJECT_DIR.parent.parent / ".env.local",
        PROJECT_DIR.parent.parent / ".env",
        Path.home() / "Documents" / "默认项目" / "znz" / ".env.local",
        Path.home() / "Documents" / "默认项目" / "znz" / ".env",
    ):
        if env_file.exists():
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("未配置 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
    return app_id, app_secret


def init_feishu_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """初始化单一生产数据库中的飞书预警反馈表及规则账本表。"""
    init_production_db(db_path)


def _format_field_value(raw_val: Any, fmt: str | None = None, for_card: bool = False) -> str:
    """按配置格式化单个字段显示值。"""
    if raw_val is None:
        return ""
    if fmt == "role_cn":
        s = str(raw_val).strip()
        return ROLE_CN_MAP.get(s, s)
    if fmt == "feishu_at":
        s = str(raw_val).strip()
        if not s or not for_card or s.startswith("<at "):
            return s
        if s.startswith(("ou_", "on_")):
            return f'<at id="{s}"></at>'
        if "@" in s:
            return f'<at email="{s}"></at>'
        return s
    if isinstance(raw_val, list):
        return " / ".join(str(x) for x in raw_val if x)
    return str(raw_val)


def resolve_supervisor(
    alert_data: dict[str, Any],
    receive_id: str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> str:
    """根据显式传入的 supervisor 或飞书群 chat_supervisors 映射解析对应主管标识。"""
    explicit = str(alert_data.get("supervisor") or "").strip()
    if explicit:
        return explicit
    cfg = load_card_config(config_path)
    chat_map = cfg.get("chat_supervisors") or {}
    if not isinstance(chat_map, dict):
        return ""
    for candidate in (
        receive_id,
        alert_data.get("receive_id"),
        alert_data.get("feishu_receive_id"),
        alert_data.get("chat_id"),
        alert_data.get("group_id"),
    ):
        key = str(candidate or "").strip()
        if key and key in chat_map and chat_map[key]:
            return str(chat_map[key]).strip()
    return ""


def map_internal_to_external_payload(
    internal_alert: dict[str, Any],
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    receive_id: str | None = None,
) -> dict[str, Any]:
    """根据 feishu_card_config.yaml 将主系统内部字段映射为对外推送的单层扁平字典。"""
    cfg = load_card_config(config_path)
    resolved_sup = resolve_supervisor(internal_alert, receive_id=receive_id, config_path=config_path)
    enriched = {**internal_alert, **({"supervisor": resolved_sup} if resolved_sup else {})}
    out: dict[str, Any] = {}
    for field_spec in cfg.get("push_fields", []):
        if not isinstance(field_spec, dict):
            continue
        source_key = str(field_spec.get("source_key") or "").strip()
        external_key = str(field_spec.get("external_key") or source_key or field_spec.get("label") or "").strip()
        default_val = field_spec.get("default_value", "")
        fmt = field_spec.get("format")
        raw_val = enriched.get(source_key, default_val) if source_key else default_val
        if external_key:
            out[external_key] = _format_field_value(raw_val, fmt, for_card=False) if fmt else raw_val
    return out


def build_alert_card(
    alert_data: dict[str, Any],
    supervisor_verdict: str | None = None,
    evaluated_at: str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    receive_id: str | None = None,
) -> dict[str, Any]:
    """完全基于 feishu_card_config.yaml 动态组装飞书交互卡片。"""
    cfg = load_card_config(config_path)
    header_cfg = cfg.get("card_header") or {}
    cb_cfg = cfg.get("callback") or {}

    resolved_sup = resolve_supervisor(alert_data, receive_id=receive_id, config_path=config_path)
    enriched_data = {**alert_data, **({"supervisor": resolved_sup} if resolved_sup else {})}

    customer_id = str(enriched_data.get("customer_id") or "")
    analysis_date = str(enriched_data.get("analysis_date") or "")
    alert_id = str(enriched_data.get("alert_id") or f"{customer_id}_{analysis_date}")

    lines: list[str] = []
    for field_spec in cfg.get("push_fields", []):
        if not isinstance(field_spec, dict):
            continue
        label = str(field_spec.get("label") or "").strip()
        source_key = str(field_spec.get("source_key") or "").strip()
        external_key = str(field_spec.get("external_key") or "").strip()
        default_val = field_spec.get("default_value", "")
        fmt = field_spec.get("format")

        if source_key and source_key in enriched_data:
            raw_val = enriched_data.get(source_key)
        elif external_key and external_key in enriched_data:
            raw_val = enriched_data.get(external_key)
        else:
            raw_val = default_val
        val_str = _format_field_value(raw_val, fmt, for_card=True)
        lines.append(f"**{label}**：{val_str}")

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(lines),
            },
        },
        {"tag": "hr"},
    ]

    raw_rids = alert_data.get("rule_ids")
    if isinstance(raw_rids, str):
        try:
            rule_ids_list = [str(x) for x in json.loads(raw_rids) if x]
        except json.JSONDecodeError:
            rule_ids_list = []
    elif isinstance(raw_rids, list):
        rule_ids_list = [str(x) for x in raw_rids if x]
    else:
        rule_ids_list = []

    verdict_field_name = str(cb_cfg.get("verdict_field_name") or "supervisor_verdict").strip()
    verdict_label = str(cb_cfg.get("label") or "意见").strip()
    time_label = str(cb_cfg.get("time_label") or "评估时间").strip()
    options_cfg = cb_cfg.get("options") or []
    header_template = str(header_cfg.get("pending_color") or "red")

    if supervisor_verdict:
        show_rids = bool(cb_cfg.get("show_rule_ids_after_eval", True))
        rids_str = f"\n**关联规则标签**：`{', '.join(rule_ids_list)}`" if (show_rids and rule_ids_list) else ""
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{verdict_label}**：**{supervisor_verdict}**{rids_str}\n"
                        f"**{time_label}**：{evaluated_at or datetime.now(SHANGHAI_TZ).isoformat(timespec='seconds')}"
                    ),
                },
            }
        )
    else:
        base_ctx: dict[str, Any] = {
            "alert_id": alert_id,
            "customer_id": customer_id,
            "customer_name": str(enriched_data.get("customer_name") or ""),
            "group_id": str(enriched_data.get("group_id") or ""),
            "analysis_date": analysis_date,
            "types": _parse_json_or_list(enriched_data.get("types") or enriched_data.get("type")),
            "owner": str(enriched_data.get("owner") or "advisor"),
            "details": str(enriched_data.get("details") or ""),
            "rule_ids": rule_ids_list,
        }
        if resolved_sup:
            base_ctx["supervisor"] = resolved_sup
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{verdict_label}**："},
            }
        )
        buttons: list[dict[str, Any]] = []
        for opt in options_cfg:
            if not isinstance(opt, dict):
                continue
            btn_text = str(opt.get("text") or opt.get("value") or "").strip()
            btn_val = str(opt.get("value") or btn_text).strip()
            btn_style = str(opt.get("style") or "default").strip()
            if btn_text:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn_text},
                        "type": btn_style,
                        "value": {**base_ctx, verdict_field_name: btn_val},
                    }
                )
        if buttons:
            elements.append({"tag": "action", "actions": buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_template,
            "title": {
                "tag": "plain_text",
                "content": str(header_cfg.get("title") or "群服务预警"),
            },
        },
        "elements": elements,
    }


def _verdict_count_delta(
    verdict: str | None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> int:
    """根据 feishu_card_config.yaml 判定回调 value 对规则 count 的加减影响（发起预警 +1，暂不发起 0，误报 -1）。"""
    if not verdict:
        return 0
    cfg = load_card_config(config_path)
    options_cfg = (cfg.get("callback") or {}).get("options") or []
    for opt in options_cfg:
        if isinstance(opt, dict) and str(opt.get("value")) == verdict:
            if "delta" in opt and opt["delta"] is not None:
                return int(opt["delta"])
            eff = str(opt.get("effect") or "").strip()
            if eff in {"useful", "helpful"}:
                return 1
            if eff == "harmful":
                return -1
            if eff == "neutral":
                return 0
    if verdict == "发起预警":
        return 1
    if verdict == "误报":
        return -1
    return 0


def _get_patrol_thresholds(patrol_config_path: Path | str = DEFAULT_CONFIG_PATH) -> tuple[int, int]:
    """读取规则 count 的上限（默认 5）与 Hook 触发阈值（默认 -3）。"""
    try:
        pcfg = load_patrol_config(patrol_config_path)
        t_cfg = pcfg.get("thresholds") or {}
        return int(t_cfg.get("max_count", 5)), int(t_cfg.get("trigger_count", -3))
    except Exception:
        return 5, -3


def _revert_verdict_scores(
    con: Any,
    alert_id: str,
    prev_verdict: str | None,
    rids: list[str],
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    max_count: int = 5,
) -> None:
    """回退指定 alert_id 在 playbook_rules_ledger 中对 count 的已累计影响与误报来源关联。"""
    prev_delta = _verdict_count_delta(prev_verdict, config_path)
    if not rids:
        return
    for rid in rids:
        if prev_delta != 0:
            con.execute(
                "UPDATE playbook_rules_ledger SET count = LEAST(?, COALESCE(count, 0) - ?) WHERE rule_id = ?",
                [max_count, prev_delta, rid],
            )
        if prev_delta < 0:
            src_row = con.execute("SELECT source_cust_ids FROM playbook_rules_ledger WHERE rule_id = ?", [rid]).fetchone()
            if src_row and src_row[0]:
                try:
                    src_list = [x for x in json.loads(src_row[0]) if x != alert_id]
                    con.execute(
                        "UPDATE playbook_rules_ledger SET source_cust_ids = ? WHERE rule_id = ?",
                        [json.dumps(src_list, ensure_ascii=False), rid],
                    )
                except Exception:
                    pass


_BITABLE_SCHEMA_READY: set[tuple[str, str]] = set()


def _parse_json_or_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            return [raw.strip()]
    return []


def _get_bitable_target(config_path: Path | str = DEFAULT_CARD_CONFIG_PATH) -> tuple[bool, str, str, str]:
    cfg = load_card_config(config_path)
    b_cfg = cfg.get("bitable") or {}
    enabled = bool(b_cfg.get("enabled", True))
    app_token = str(os.environ.get("FEISHU_BITABLE_APP_TOKEN") or b_cfg.get("app_token") or "").strip()
    table_id = str(os.environ.get("FEISHU_BITABLE_TABLE_ID") or b_cfg.get("table_id") or "").strip()
    pending_verdict = str(b_cfg.get("pending_verdict") or "待审批").strip()
    return enabled and bool(app_token and table_id), app_token, table_id, pending_verdict


def ensure_bitable_schema(
    client: lark.Client,
    app_token: str,
    table_id: str,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> None:
    """确保多维表格具备完整的标准表头列（幂等检查与自动创建）。"""
    cache_key = (app_token, table_id)
    if cache_key in _BITABLE_SCHEMA_READY:
        return
    assert client.bitable is not None
    resp = client.bitable.v1.app_table_field.list(
        ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).build()
    )
    if not resp.success() or not resp.data:
        return
    items = resp.data.items or []
    existing_names = {str(f.field_name): str(f.field_id) for f in items if f.field_name}
    if items and "预警ID" not in existing_names:
        primary_fid = str(items[0].field_id)
        client.bitable.v1.app_table_field.update(
            UpdateAppTableFieldRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .field_id(primary_fid)
            .request_body(AppTableField.builder().field_name("预警ID").type(1).build())
            .build()
        )
        existing_names["预警ID"] = primary_fid

    cfg = load_card_config(config_path)
    cb_cfg = cfg.get("callback") or {}
    verdict_label = str(cb_cfg.get("label") or "意见").strip()
    time_label = str(cb_cfg.get("time_label") or "评估时间").strip()

    desired_cols: list[tuple[str, int]] = [
        ("客户名称", 1),
        ("客户id", 1),
        ("群id", 1),
        ("分析日期", 1),
        ("风险类型", 3),
        ("责任人", 3),
        ("主管", 1),
        ("预警细节", 1),
        (verdict_label, 3),
        (time_label, 1),
        ("关联规则标签", 1),
    ]
    for col_name, col_type in desired_cols:
        if col_name not in existing_names:
            client.bitable.v1.app_table_field.create(
                CreateAppTableFieldRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .request_body(AppTableField.builder().field_name(col_name).type(col_type).build())
                .build()
            )
    _BITABLE_SCHEMA_READY.add(cache_key)


def _build_bitable_fields(
    record_data: dict[str, Any],
    pending_verdict: str = "待审批",
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> dict[str, Any]:
    cfg = load_card_config(config_path)
    cb_cfg = cfg.get("callback") or {}
    verdict_label = str(cb_cfg.get("label") or "意见").strip()
    time_label = str(cb_cfg.get("time_label") or "评估时间").strip()

    cid = str(record_data.get("customer_id") or "")
    adate = str(record_data.get("analysis_date") or "")
    alert_id = str(record_data.get("alert_id") or f"{cid}_{adate}")
    types_list = _parse_json_or_list(record_data.get("types") or record_data.get("type"))
    rids_list = _parse_json_or_list(record_data.get("rule_ids"))
    owner_raw = str(record_data.get("owner") or "advisor").strip()
    verdict_val = str(record_data.get("supervisor_verdict") or "").strip() or pending_verdict
    sup_val = resolve_supervisor(record_data, config_path=config_path)

    return {
        "预警ID": alert_id,
        "客户名称": str(record_data.get("customer_name") or ""),
        "客户id": cid,
        "群id": str(record_data.get("group_id") or ""),
        "分析日期": adate,
        "风险类型": " / ".join(types_list),
        "责任人": ROLE_CN_MAP.get(owner_raw, owner_raw),
        "主管": sup_val,
        "预警细节": str(record_data.get("details") or ""),
        verdict_label: verdict_val,
        time_label: str(record_data.get("evaluated_at") or ""),
        "关联规则标签": " / ".join(rids_list),
    }


def _find_bitable_record_ids(
    client: lark.Client,
    app_token: str,
    table_id: str,
    alert_id: str,
) -> list[str]:
    assert client.bitable is not None
    filter_info = (
        FilterInfo.builder()
        .conjunction("and")
        .conditions([Condition.builder().field_name("预警ID").operator("is").value([alert_id]).build()])
        .build()
    )
    body = SearchAppTableRecordRequestBody.builder().filter(filter_info).build()
    req = (
        SearchAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(body)
        .build()
    )
    resp = client.bitable.v1.app_table_record.search(req)
    if not resp.success() or not resp.data or not resp.data.items:
        return []
    return [str(r.record_id) for r in resp.data.items if getattr(r, "record_id", None)]


def sync_alert_to_bitable(
    record_data: dict[str, Any],
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> str | None:
    """将单条预警或更新后的审批结果幂等同步至飞书多维表格（存在则原位更新，不存在则新增）。"""
    try:
        enabled, app_token, table_id, pending_verdict = _get_bitable_target(config_path)
        if not enabled:
            return None
        app_id, app_secret = load_feishu_credentials()
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        assert client.bitable is not None
        ensure_bitable_schema(client, app_token, table_id, config_path=config_path)
        fields = _build_bitable_fields(record_data, pending_verdict=pending_verdict, config_path=config_path)
        alert_id = str(fields["预警ID"])
        existing_ids = _find_bitable_record_ids(client, app_token, table_id, alert_id)
        if existing_ids:
            rec_id = existing_ids[0]
            resp = client.bitable.v1.app_table_record.update(
                UpdateAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .record_id(rec_id)
                .request_body(AppTableRecord.builder().fields(fields).build())
                .build()
            )
            return rec_id if resp.success() else None
        resp = client.bitable.v1.app_table_record.create(
            CreateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .request_body(AppTableRecord.builder().fields(fields).build())
            .build()
        )
        if resp.success() and resp.data and resp.data.record:
            return str(resp.data.record.record_id)
    except Exception as exc:
        print(f"[Feishu Bitable] 同步多维表格异常 (已忽略): {exc}", flush=True)
    return None


def delete_alert_from_bitable(
    alert_id: str,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> int:
    """当同客户同日重跑变为无预警时，同步移除多维表格中的旧预警记录。"""
    try:
        enabled, app_token, table_id, _ = _get_bitable_target(config_path)
        if not enabled or not alert_id:
            return 0
        app_id, app_secret = load_feishu_credentials()
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        assert client.bitable is not None
        rec_ids = _find_bitable_record_ids(client, app_token, table_id, alert_id)
        deleted = 0
        for rid in rec_ids:
            resp = client.bitable.v1.app_table_record.delete(
                DeleteAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .record_id(rid)
                .build()
            )
            if resp.success():
                deleted += 1
        return deleted
    except Exception as exc:
        print(f"[Feishu Bitable] 删除多维表格记录异常 (已忽略): {exc}", flush=True)
        return 0


def sync_all_db_alerts_to_bitable(
    db_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> list[str]:
    """将数据库 feishu_alert_feedback 表中的所有预警记录全量幂等同步至飞书多维表格。"""
    init_production_db(db_path)
    cols = [
        "alert_id", "customer_id", "customer_name", "group_id", "evaluated_at",
        "analysis_date", "types", "owner", "message_ids", "rule_ids", "supervisor_verdict", "details",
    ]
    with connect_db(db_path, read_only=True) as con:
        rows = con.execute(
            """
            SELECT alert_id, customer_id, customer_name, group_id, evaluated_at,
                   analysis_date, types, owner, message_ids, rule_ids, supervisor_verdict, details
            FROM feishu_alert_feedback
            ORDER BY analysis_date ASC, alert_id ASC
            """
        ).fetchall()
    synced_ids: list[str] = []
    for r in rows:
        rec = dict(zip(cols, r))
        rid = sync_alert_to_bitable(rec, config_path=config_path)
        if rid:
            synced_ids.append(rid)
    return synced_ids


def clear_alert_feedback(
    alert_id: str,
    db_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    patrol_config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    """当同客户同日重跑变为无预警时，回退该日已评估计分并清除旧预警记录。"""
    init_production_db(db_path)
    max_count, _ = _get_patrol_thresholds(patrol_config_path)
    with connect_db(db_path) as con:
        prev_row = con.execute(
            "SELECT supervisor_verdict, rule_ids FROM feishu_alert_feedback WHERE alert_id = ?",
            [alert_id],
        ).fetchone()
        if not prev_row:
            return
        try:
            old_rids = [str(x).strip() for x in (json.loads(prev_row[1]) if prev_row[1] else []) if str(x).strip()]
        except Exception:
            old_rids = []
        _revert_verdict_scores(con, alert_id, prev_row[0], old_rids, config_path=config_path, max_count=max_count)
        con.execute("DELETE FROM feishu_alert_feedback WHERE alert_id = ?", [alert_id])
    delete_alert_from_bitable(alert_id, config_path=config_path)


def save_alert_to_db(
    alert_data: dict[str, Any],
    feishu_message_id: str = "",
    db_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    patrol_config_path: Path | str = DEFAULT_CONFIG_PATH,
    sync_bitable: bool = True,
) -> str:
    """将待评估的扁平预警记录存入 feishu_alert_feedback 表，并可选同步至多维表格。"""
    customer_id = str(alert_data.get("customer_id") or "")
    analysis_date = str(alert_data.get("analysis_date") or "")
    alert_id = str(alert_data.get("alert_id") or f"{customer_id}_{analysis_date}")
    types_val = alert_data.get("types") or ([alert_data["type"]] if alert_data.get("type") else [])
    msg_ids_val = alert_data.get("message_ids") or []
    rule_ids_val = alert_data.get("rule_ids") or []
    eff_verdict = alert_data.get("supervisor_verdict")
    eff_eval_at = alert_data.get("evaluated_at")

    try:
        init_production_db(db_path)
        max_count, _ = _get_patrol_thresholds(patrol_config_path)
        with connect_db(db_path) as con:
            existing = con.execute(
                "SELECT supervisor_verdict, evaluated_at, rule_ids FROM feishu_alert_feedback WHERE alert_id = ?",
                [alert_id],
            ).fetchone()

            if existing and existing[0] and eff_verdict is None:
                try:
                    old_rids = [str(x).strip() for x in (json.loads(existing[2]) if existing[2] else []) if str(x).strip()]
                except Exception:
                    old_rids = []
                new_rids = [str(x).strip() for x in (rule_ids_val if isinstance(rule_ids_val, list) else json.loads(rule_ids_val or "[]")) if str(x).strip()]
                if set(old_rids) == set(new_rids):
                    eff_verdict = existing[0]
                    eff_eval_at = existing[1]
                else:
                    _revert_verdict_scores(con, alert_id, str(existing[0]), old_rids, config_path=config_path, max_count=max_count)

            con.execute(
                """
                INSERT INTO feishu_alert_feedback (
                    alert_id, customer_id, customer_name, group_id,
                    evaluated_at, analysis_date, types, owner,
                    message_ids, rule_ids, supervisor_verdict, details
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (alert_id) DO UPDATE SET
                    customer_id = EXCLUDED.customer_id,
                    customer_name = EXCLUDED.customer_name,
                    group_id = EXCLUDED.group_id,
                    evaluated_at = EXCLUDED.evaluated_at,
                    analysis_date = EXCLUDED.analysis_date,
                    types = EXCLUDED.types,
                    owner = EXCLUDED.owner,
                    message_ids = EXCLUDED.message_ids,
                    rule_ids = EXCLUDED.rule_ids,
                    supervisor_verdict = EXCLUDED.supervisor_verdict,
                    details = EXCLUDED.details
                """,
                [
                    alert_id,
                    customer_id,
                    str(alert_data.get("customer_name") or ""),
                    str(alert_data.get("group_id") or ""),
                    eff_eval_at,
                    analysis_date,
                    json.dumps(types_val, ensure_ascii=False) if not isinstance(types_val, str) else types_val,
                    str(alert_data.get("owner") or "advisor"),
                    json.dumps(msg_ids_val, ensure_ascii=False) if not isinstance(msg_ids_val, str) else msg_ids_val,
                    json.dumps(rule_ids_val, ensure_ascii=False) if not isinstance(rule_ids_val, str) else rule_ids_val,
                    eff_verdict,
                    str(alert_data.get("details") or ""),
                ],
            )
    except Exception as exc:
        print(f"[Feishu DB] 数据库写入暂不可用，继续同步多维表格: {exc}", flush=True)

    if sync_bitable:
        sync_alert_to_bitable(
            {
                **alert_data,
                "alert_id": alert_id,
                "customer_id": customer_id,
                "analysis_date": analysis_date,
                "types": types_val,
                "rule_ids": rule_ids_val,
                "supervisor_verdict": eff_verdict,
                "evaluated_at": eff_eval_at,
            },
            config_path=config_path,
        )
    return alert_id


def update_supervisor_verdict(
    alert_id: str,
    supervisor_verdict: str,
    rule_ids_hint: list[str] | None = None,
    db_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
    patrol_config_path: Path | str = DEFAULT_CONFIG_PATH,
    yaml_path: Path | str = DEFAULT_YAML_PATH,
    run_hook_async: bool = True,
) -> dict[str, Any] | None:
    """更新指定预警的评估结果，修改规则 count（发起预警+1，暂不发起0，误报-1），并在到达阈值时触发 Hook。"""
    init_production_db(db_path)
    max_count, trigger_count = _get_patrol_thresholds(patrol_config_path)
    evaluated_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    triggered_rids: list[str] = []

    with connect_db(db_path) as con:
        prev_row = con.execute(
            "SELECT supervisor_verdict, rule_ids FROM feishu_alert_feedback WHERE alert_id = ?",
            [alert_id],
        ).fetchone()
        prev_verdict = prev_row[0] if prev_row else None
        stored_rids_str = prev_row[1] if prev_row else "[]"
        try:
            stored_rids = json.loads(stored_rids_str) if stored_rids_str else []
        except json.JSONDecodeError:
            stored_rids = []

        effective_rids = [str(x).strip() for x in (stored_rids or rule_ids_hint or []) if str(x).strip()]
        if effective_rids and not stored_rids:
            con.execute(
                "UPDATE feishu_alert_feedback SET rule_ids = ? WHERE alert_id = ?",
                [json.dumps(effective_rids, ensure_ascii=False), alert_id],
            )

        con.execute(
            "UPDATE feishu_alert_feedback SET supervisor_verdict = ?, evaluated_at = ? WHERE alert_id = ?",
            [supervisor_verdict, evaluated_at, alert_id],
        )

        curr_delta = _verdict_count_delta(supervisor_verdict, config_path)
        if effective_rids and prev_verdict != supervisor_verdict:
            _revert_verdict_scores(con, alert_id, prev_verdict, effective_rids, config_path=config_path, max_count=max_count)
            for rid in effective_rids:
                if curr_delta > 0:
                    con.execute(
                        "UPDATE playbook_rules_ledger SET count = LEAST(?, COALESCE(count, 0) + ?) WHERE rule_id = ?",
                        [max_count, curr_delta, rid],
                    )
                elif curr_delta < 0:
                    con.execute(
                        "UPDATE playbook_rules_ledger SET count = COALESCE(count, 0) + ? WHERE rule_id = ?",
                        [curr_delta, rid],
                    )
                    src_row = con.execute(
                        "SELECT source_cust_ids, COALESCE(count, 0) FROM playbook_rules_ledger WHERE rule_id = ?",
                        [rid],
                    ).fetchone()
                    src_list = []
                    new_cnt = 0
                    if src_row:
                        if src_row[0]:
                            try:
                                src_list = list(json.loads(src_row[0]))
                            except Exception:
                                src_list = []
                        new_cnt = int(src_row[1] or 0)
                    if alert_id not in src_list:
                        src_list.append(alert_id)
                    con.execute(
                        "UPDATE playbook_rules_ledger SET source_cust_ids = ? WHERE rule_id = ?",
                        [json.dumps(src_list, ensure_ascii=False), rid],
                    )
                    if new_cnt <= trigger_count and rid not in triggered_rids:
                        triggered_rids.append(rid)

        row = con.execute(
            """
            SELECT alert_id, customer_id, customer_name, group_id, evaluated_at,
                   analysis_date, types, owner, message_ids, rule_ids, supervisor_verdict, details
            FROM feishu_alert_feedback
            WHERE alert_id = ?
            """,
            [alert_id],
        ).fetchone()

    if not row:
        return None
    cols = [
        "alert_id", "customer_id", "customer_name", "group_id", "evaluated_at",
        "analysis_date", "types", "owner", "message_ids", "rule_ids", "supervisor_verdict", "details",
    ]
    out_record = dict(zip(cols, row))
    sync_alert_to_bitable(out_record, config_path=config_path)

    if triggered_rids:
        hook_reports = trigger_rule_rework_hook(
            triggered_rids,
            db_path=db_path,
            yaml_path=yaml_path,
            config_path=patrol_config_path,
            async_mode=run_hook_async,
        )
        if not run_hook_async:
            out_record["hook_reports"] = hook_reports

    return out_record


def send_alert_to_feishu(
    alert_data: dict[str, Any],
    receive_id: str,
    receive_id_type: str = "chat_id",
    db_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_CARD_CONFIG_PATH,
) -> dict[str, Any]:
    """调用飞书发送消息 API 推送交互式预警卡片，并落库到 feishu_alert_feedback。"""
    app_id, app_secret = load_feishu_credentials()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    assert client.im is not None

    resolved_sup = resolve_supervisor(alert_data, receive_id=receive_id, config_path=config_path)
    enriched_alert = {**alert_data, **({"supervisor": resolved_sup} if resolved_sup else {})}
    card_dict = build_alert_card(enriched_alert, config_path=config_path, receive_id=receive_id)
    req = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(json.dumps(card_dict, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        raise RuntimeError(f"飞书卡片发送失败: code={resp.code}, msg={resp.msg}")

    feishu_msg_id = getattr(resp.data, "message_id", "") or ""
    alert_id = save_alert_to_db(enriched_alert, feishu_message_id=feishu_msg_id, db_path=db_path, config_path=config_path)
    return {
        "alert_id": alert_id,
        "feishu_message_id": feishu_msg_id,
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "external_payload": map_internal_to_external_payload(enriched_alert, config_path=config_path, receive_id=receive_id),
    }


def handle_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理飞书长连接收到的卡片交互事件 (card.action.trigger)，按配置提取回送字段并落库。"""
    cfg = load_card_config()
    verdict_field_name = str((cfg.get("callback") or {}).get("verdict_field_name") or "supervisor_verdict").strip()

    event = data.event
    action = event.action if event else None
    raw_value: dict[str, Any] = {}
    if action and isinstance(action.value, dict):
        raw_value = action.value
    elif action and isinstance(action.value, str):
        try:
            raw_value = json.loads(action.value)
        except json.JSONDecodeError:
            raw_value = {}

    verdict = ""
    if action and action.input_value and str(action.input_value).strip():
        verdict = str(action.input_value).strip()
    elif action and isinstance(action.form_value, dict) and action.form_value.get(verdict_field_name):
        verdict = str(action.form_value[verdict_field_name]).strip()
    elif raw_value.get(verdict_field_name):
        verdict = str(raw_value[verdict_field_name]).strip()
    elif raw_value.get("supervisor_verdict"):
        verdict = str(raw_value["supervisor_verdict"]).strip()

    alert_id = str(raw_value.get("alert_id") or "")
    if not alert_id and raw_value.get("customer_id") and raw_value.get("analysis_date"):
        alert_id = f"{raw_value['customer_id']}_{raw_value['analysis_date']}"

    rule_ids_hint = raw_value.get("rule_ids") if isinstance(raw_value.get("rule_ids"), list) else None
    print(
        f"[Feishu Callback] 收到主管评估: alert_id={alert_id}, {verdict_field_name}={verdict}, rule_ids={rule_ids_hint}",
        flush=True,
    )

    resp = P2CardActionTriggerResponse()
    if not verdict:
        resp.toast = CallBackToast({"type": "warning", "content": f"未检测到有效的评估结果 {verdict_field_name}"})
        return resp

    updated_record: dict[str, Any] | None = None
    if alert_id:
        try:
            updated_record = update_supervisor_verdict(alert_id, verdict, rule_ids_hint=rule_ids_hint)
        except Exception as exc:
            print(f"[Feishu Callback] 数据库更新暂不可用，使用按钮上下文回写多维表格: {exc}", flush=True)

    open_chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "").strip()
    if not updated_record and alert_id:
        evaluated_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        updated_record = {
            "alert_id": alert_id,
            "customer_id": str(raw_value.get("customer_id") or ""),
            "customer_name": str(raw_value.get("customer_name") or ""),
            "group_id": str(raw_value.get("group_id") or ""),
            "analysis_date": str(raw_value.get("analysis_date") or ""),
            "types": raw_value.get("types") or [],
            "owner": str(raw_value.get("owner") or "student"),
            "details": str(raw_value.get("details") or ""),
            "rule_ids": rule_ids_hint or [],
            "supervisor_verdict": verdict,
            "evaluated_at": evaluated_at,
        }

    resp.toast = CallBackToast({"type": "success", "content": f"已记录评估: {verdict}"})
    if updated_record:
        sup_hint = str(raw_value.get("supervisor") or "").strip() or resolve_supervisor(
            updated_record, receive_id=open_chat_id
        )
        if sup_hint:
            updated_record["supervisor"] = sup_hint
        sync_alert_to_bitable(updated_record)
        new_card = build_alert_card(
            updated_record,
            supervisor_verdict=updated_record.get("supervisor_verdict"),
            evaluated_at=updated_record.get("evaluated_at"),
            receive_id=open_chat_id,
        )
        resp.card = CallBackCard({"type": "raw", "data": new_card})
    return resp


def list_bot_chats() -> list[dict[str, str]]:
    """查询当前机器人所在的飞书群列表（获取 chat_id）。"""
    app_id, app_secret = load_feishu_credentials()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    assert client.im is not None
    req = ListChatRequest.builder().page_size(50).build()
    resp = client.im.v1.chat.list(req)
    if not resp.success():
        raise RuntimeError(f"获取飞书群列表失败: code={resp.code}, msg={resp.msg}")
    items = getattr(resp.data, "items", None) or []
    return [
        {
            "chat_id": getattr(item, "chat_id", ""),
            "name": getattr(item, "name", ""),
            "description": getattr(item, "description", ""),
        }
        for item in items
    ]


def start_ws_listener() -> None:
    """启动飞书 WebSocket 长连接客户端，实时接收卡片回调事件。"""
    app_id, app_secret = load_feishu_credentials()
    try:
        init_feishu_table()
    except Exception as exc:
        print(f"[Feishu WS] 数据库暂不可用，将以飞书多维表格直连模式启动监听: {exc}", flush=True)
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(handle_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    print(f"[Feishu WS] 正在启动飞书长连接监听 (app_id={app_id}) ...", flush=True)
    ws_client.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书风险预警卡片推送与长连接回调接收工具")
    parser.add_argument("--listen", action="store_true", help="启动飞书 WebSocket 长连接监听回调")
    parser.add_argument("--list-chats", action="store_true", help="列出当前机器人所在的飞书群 chat_id")
    parser.add_argument("--sync-bitable", action="store_true", help="将数据库中已有的预警记录全量同步至飞书多维表格")
    args = parser.parse_args()

    if args.list_chats:
        chats = list_bot_chats()
        print(json.dumps(chats, ensure_ascii=False, indent=2))
        return

    if args.sync_bitable:
        synced = sync_all_db_alerts_to_bitable()
        print(json.dumps({"synced_count": len(synced), "record_ids": synced}, ensure_ascii=False, indent=2))
        return

    if args.listen:
        start_ws_listener()
        return

    parser.print_help()


if __name__ == "__main__":
    main()

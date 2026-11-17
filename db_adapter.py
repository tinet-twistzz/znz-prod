"""单一生产数据库接口层（PostgreSQL 版）：全系统（api_server、feishu_bot、rule_patrol）访问数据库的唯一通道。
基于 psycopg (PostgreSQL) 维护 5 张核心表，通过环境变量 DB_URL / DATABASE_URL 零成本直连企业 PostgreSQL。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import re
import threading
from typing import Any, Iterator

import psycopg
import yaml

from agent.config import MULTI_LABEL
from agent.contracts import DEFAULT_ALERT_OWNER_BY_TYPE
from agent.input_builder import build_rolling_input

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH: str = (
    os.getenv("DB_URL")
    or os.getenv("DATABASE_URL")
    or "postgresql://postgres:postgres@localhost:5432/znz"
)
DEFAULT_YAML_PATH = Path(os.getenv("ALERT_DETAILS_YAML_PATH", PROJECT_DIR / "agent" / "alert_details.yaml"))
ALERT_TYPE_ORDER: tuple[str, ...] = tuple(DEFAULT_ALERT_OWNER_BY_TYPE.keys())
_DB_LOCK = threading.RLock()
_INITIALIZED_DBS: set[str] = set()


def resolve_db_url(db_path: Path | str | None = None) -> str:
    raw = str(db_path or os.getenv("DB_URL") or os.getenv("DATABASE_URL") or DEFAULT_DB_PATH).strip()
    return re.sub(r"^(postgres(?:ql)?):/([^/])", r"\1://\2", raw)


class _PgConnectionWrapper:
    """轻量包装 psycopg.Connection，兼容原有 con.execute / con.executemany 调用方式。"""

    def __init__(self, con: psycopg.Connection[Any]) -> None:
        self._con = con

    def execute(self, sql: str, params: Any = None) -> psycopg.Cursor[Any]:
        norm_sql = sql.replace("ORDER BY rowid", "ORDER BY ctid")
        if params is not None:
            norm_sql = norm_sql.replace("?", "%s")
            return self._con.execute(norm_sql, params)
        return self._con.execute(norm_sql)

    def executemany(self, sql: str, params_seq: Any) -> None:
        norm_sql = sql.replace("?", "%s")
        with self._con.cursor() as cur:
            cur.executemany(norm_sql, params_seq)

    def close(self) -> None:
        self._con.close()


@contextmanager
def connect_db(db_path: Path | str | None = None, read_only: bool = False) -> Iterator[_PgConnectionWrapper]:
    dsn = resolve_db_url(db_path)
    with _DB_LOCK:
        raw_con = psycopg.connect(dsn, autocommit=True)
        con = _PgConnectionWrapper(raw_con)
        try:
            yield con
        finally:
            con.close()


def seed_playbook_from_yaml(db_path: Path | str | None = None, yaml_path: Path | str | None = None) -> int:
    ypath = Path(yaml_path or DEFAULT_YAML_PATH)
    items = yaml.safe_load(ypath.read_text(encoding="utf-8"))
    with connect_db(db_path) as con:
        existing = {
            str(r[0]): (int(r[1] or 0), str(r[2] or "[]"))
            for r in con.execute(
                "SELECT rule_id, count, source_cust_ids FROM playbook_rules_ledger"
            ).fetchall()
        }
        rule_rows = []
        for item in items:
            atype = item["type"].strip()
            for raw_ln in item["details"].strip().splitlines():
                m = re.match(r"^-\s*\[([^\[\]]+)\]\s*(.+)$", raw_ln.strip())
                if m:
                    rid, content = m.group(1).strip(), m.group(2).strip()
                    cnt, sids = existing.get(rid, (0, "[]"))
                    rule_rows.append((atype, rid, content, cnt, sids))
        con.execute("DELETE FROM playbook_rules_ledger")
        con.executemany(
            """
            INSERT INTO playbook_rules_ledger (alert_type, rule_id, content, count, source_cust_ids)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (rule_id) DO UPDATE SET
                alert_type = EXCLUDED.alert_type,
                content = EXCLUDED.content,
                count = EXCLUDED.count,
                source_cust_ids = EXCLUDED.source_cust_ids
            """,
            rule_rows,
        )
    return len(rule_rows)


def render_yaml_from_db(db_path: Path | str | None = None, out_yaml_path: Path | str | None = None) -> str:
    ypath = Path(out_yaml_path or DEFAULT_YAML_PATH)
    ref_path = ypath if ypath.exists() else DEFAULT_YAML_PATH
    order_map: dict[str, int] = {}
    if ref_path.exists():
        order_map = {
            rid.strip(): idx
            for idx, rid in enumerate(re.findall(r"^\s*-\s*\[([^\[\]]+)\]", ref_path.read_text(encoding="utf-8"), re.M))
        }
    with connect_db(db_path, read_only=True) as con:
        rule_rows = con.execute(
            "SELECT rule_id, alert_type, content FROM playbook_rules_ledger ORDER BY ctid"
        ).fetchall()
    rule_rows = sorted(rule_rows, key=lambda r: order_map.get(str(r[0]).strip(), 999999))
    by_type: dict[str, list[str]] = {t: [] for t in ALERT_TYPE_ORDER}
    for rid, atype, content in rule_rows:
        atype_str = str(atype)
        by_type.setdefault(atype_str, [])
        flat = " ".join(ln.strip() for ln in str(content).strip().splitlines() if ln.strip())
        by_type[atype_str].append(flat if flat.startswith("- [") else f"- [{rid}] {flat}")
    rendered = "\n\n".join(
        f"- type: {json.dumps(atype, ensure_ascii=False)}\n  details: |\n"
        + "\n".join(f"    {ln}" for ln in rules)
        for atype, rules in by_type.items()
        if rules
    ) + "\n"
    ypath.write_text(rendered, encoding="utf-8")
    return rendered


def init_production_db(db_path: Path | str | None = None, yaml_path: Path | str | None = None) -> None:
    target = resolve_db_url(db_path)
    if target in _INITIALIZED_DBS:
        return
    ypath = Path(yaml_path or DEFAULT_YAML_PATH)
    with connect_db(target) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages_store (
                id_cust VARCHAR(64) NOT NULL,
                cname_cust VARCHAR(128),
                group_id VARCHAR(128),
                analysis_date VARCHAR(10) NOT NULL,
                messages_json TEXT,
                PRIMARY KEY (id_cust, analysis_date)
            );
            CREATE TABLE IF NOT EXISTS eval_slice_runs (
                id_cust VARCHAR(64) NOT NULL,
                cname_cust VARCHAR(128),
                analysis_date VARCHAR(10) NOT NULL,
                temperature INTEGER,
                profile_json TEXT,
                items_json TEXT,
                applications_json TEXT,
                snapshot_json TEXT,
                state_changes_json TEXT,
                model_name VARCHAR(64),
                input_tokens INTEGER,
                output_tokens INTEGER,
                PRIMARY KEY (id_cust, analysis_date)
            );
            CREATE TABLE IF NOT EXISTS feishu_alert_feedback (
                alert_id VARCHAR(128) PRIMARY KEY,
                customer_id VARCHAR(64),
                customer_name VARCHAR(128),
                group_id VARCHAR(128),
                evaluated_at VARCHAR(32),
                analysis_date VARCHAR(10),
                types TEXT,
                owner VARCHAR(32),
                message_ids TEXT,
                rule_ids TEXT,
                supervisor_verdict VARCHAR(32),
                details TEXT
            );
            CREATE TABLE IF NOT EXISTS playbook_rules_ledger (
                alert_type VARCHAR(64),
                rule_id VARCHAR(64) PRIMARY KEY,
                content TEXT,
                count INTEGER,
                source_cust_ids TEXT
            );
            CREATE TABLE IF NOT EXISTS patrol_audit_log (
                patrol_id VARCHAR(64) PRIMARY KEY,
                rule_id VARCHAR(64),
                alert_type VARCHAR(64),
                attempts_used INTEGER,
                old_content TEXT,
                new_content TEXT,
                diagnosis TEXT,
                rework_history_json TEXT,
                final_status VARCHAR(32),
                created_at VARCHAR(32)
            );
            """
        )
        cnt = con.execute("SELECT COUNT(*) FROM playbook_rules_ledger").fetchone()
        need_seed = not cnt or int(cnt[0] or 0) == 0
    if need_seed and ypath.exists():
        seed_playbook_from_yaml(target, ypath)
    elif not need_seed:
        render_yaml_from_db(target, ypath)
    _INITIALIZED_DBS.add(target)


# ==============================================================================
# 预留生产数据库标准数据接口
# ==============================================================================

def save_daily_slice_run(
    cid: str,
    cname: str,
    group_id: str,
    day: str,
    messages: list[dict[str, Any]],
    state_before: dict[str, Any],
    prior_snapshots: list[dict[str, Any]],
    result: dict[str, Any],
    metrics: dict[str, Any],
    state_after: dict[str, Any],
    db_path: Path | str | None = None,
    multi_label: bool = MULTI_LABEL,
) -> None:
    """保存线上单日推理上下文与结果，供后续教师 Agent 沙盒回测直接读取。"""
    init_production_db(db_path)
    snap = result.get("snapshot") or {}
    state_changes = result.get("state_changes") or []
    with connect_db(db_path) as con:
        con.execute(
            """
            INSERT INTO chat_messages_store (id_cust, cname_cust, group_id, analysis_date, messages_json)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id_cust, analysis_date) DO UPDATE SET
                cname_cust = EXCLUDED.cname_cust,
                group_id = EXCLUDED.group_id,
                messages_json = EXCLUDED.messages_json
            """,
            [str(cid), str(cname or ""), str(group_id or ""), str(day), json.dumps(messages, ensure_ascii=False)],
        )
        con.execute(
            """
            INSERT INTO eval_slice_runs (
                id_cust, cname_cust, analysis_date, temperature,
                profile_json, items_json, applications_json,
                snapshot_json, state_changes_json,
                model_name, input_tokens, output_tokens
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id_cust, analysis_date) DO UPDATE SET
                cname_cust = EXCLUDED.cname_cust,
                temperature = EXCLUDED.temperature,
                profile_json = EXCLUDED.profile_json,
                items_json = EXCLUDED.items_json,
                applications_json = EXCLUDED.applications_json,
                snapshot_json = EXCLUDED.snapshot_json,
                state_changes_json = EXCLUDED.state_changes_json,
                model_name = EXCLUDED.model_name,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens
            """,
            [
                str(cid),
                str(cname or ""),
                str(day),
                int(state_after.get("temperature", 65)),
                json.dumps(state_after.get("profile") or {}, ensure_ascii=False),
                json.dumps(state_after.get("items") or {}, ensure_ascii=False),
                json.dumps(state_after.get("applications") or {}, ensure_ascii=False),
                json.dumps(snap, ensure_ascii=False),
                json.dumps(state_changes, ensure_ascii=False),
                str(metrics.get("model_name") or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")),
                int(metrics.get("input_tokens") or 0),
                int(metrics.get("output_tokens") or 0),
            ],
        )


def load_customer_payload(cid: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    """为指定客户组装标准 daily_chat.v1 格式的 payload。"""
    with connect_db(db_path, read_only=True) as con:
        rows = con.execute(
            "SELECT group_id, analysis_date, messages_json FROM chat_messages_store WHERE id_cust = %s ORDER BY analysis_date",
            [str(cid)],
        ).fetchall()
    if not rows:
        return None
    days_payload: dict[str, list[dict[str, Any]]] = {}
    group_id = str(rows[-1][0] or "")
    for gid, adate, m_json in rows:
        days_payload[str(adate)] = json.loads(m_json) if m_json else []
    return {
        "schema_version": "daily_chat.v1",
        "customer_id": str(cid),
        "group_id": group_id,
        "day_count": len(days_payload),
        "message_count": sum(len(v) for v in days_payload.values()),
        "days": days_payload,
    }


def _reconstruct_state_and_priors(
    con: _PgConnectionWrapper,
    cid: str,
    adate: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snap_rows = con.execute(
        """
        SELECT snapshot_json
        FROM eval_slice_runs
        WHERE id_cust = ? AND analysis_date < ?
        ORDER BY analysis_date ASC
        """,
        [str(cid), str(adate)],
    ).fetchall()
    priors = [json.loads(r[0]) for r in snap_rows if r and r[0]]
    if snap_rows:
        last = con.execute(
            """
            SELECT analysis_date, temperature, profile_json, items_json, applications_json
            FROM eval_slice_runs
            WHERE id_cust = ? AND analysis_date < ?
            ORDER BY analysis_date DESC
            LIMIT 1
            """,
            [str(cid), str(adate)],
        ).fetchone()
        state_before_obj = {
            "schema_version": "long_term_state.v1",
            "state_revision": len(snap_rows),
            "as_of_date": str(last[0]),
            "temperature": int(last[1] if last[1] is not None else 65),
            "profile": json.loads(last[2] or "{}"),
            "items": json.loads(last[3] or "{}"),
            "applications": json.loads(last[4] or "{}"),
        }
    else:
        state_before_obj = {
            "schema_version": "long_term_state.v1",
            "state_revision": 0,
            "as_of_date": None,
            "temperature": 65,
            "profile": {},
            "items": {},
            "applications": {},
        }
    return state_before_obj, priors


def load_customer_context_before_day(
    cid: str,
    adate: str,
    db_path: Path | str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[dict[str, Any]]]:
    """按单客户预加载 adate 之前的历史聊天记录、最新长期状态与历史快照列表。"""
    if not cid or not adate:
        return (
            {},
            {
                "schema_version": "long_term_state.v1",
                "state_revision": 0,
                "as_of_date": None,
                "temperature": 65,
                "profile": {},
                "items": {},
                "applications": {},
            },
            [],
        )
    init_production_db(db_path)
    with connect_db(db_path, read_only=True) as con:
        msg_rows = con.execute(
            """
            SELECT analysis_date, messages_json
            FROM chat_messages_store
            WHERE id_cust = ? AND analysis_date < ?
            ORDER BY analysis_date ASC
            """,
            [str(cid), str(adate)],
        ).fetchall()
        past_days: dict[str, list[dict[str, Any]]] = {
            str(r[0]): (json.loads(r[1]) if r[1] else []) for r in msg_rows if r and r[0]
        }
        state_before_obj, priors = _reconstruct_state_and_priors(con, str(cid), str(adate))
    return past_days, state_before_obj, priors


def load_harmful_cases_for_rule(
    rule_id: str,
    alert_type: str,
    source_cust_ids: str | list[str],
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """当规则 count 达到阈值触发 Hook 时，从单一数据库中直接提取引起该误报的完整上下文。"""
    src_ids = (
        [str(x).strip() for x in json.loads(source_cust_ids or "[]") if str(x).strip()]
        if isinstance(source_cust_ids, str)
        else [str(x).strip() for x in (source_cust_ids or []) if str(x).strip()]
    )
    cases: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    payload_cache: dict[str, dict[str, Any] | None] = {}
    with connect_db(db_path, read_only=True) as con:
        if src_ids:
            placeholders = ", ".join("%s" for _ in src_ids)
            sql = f"""
                SELECT alert_id, customer_id, customer_name, analysis_date, types, details,
                       message_ids, rule_ids, supervisor_verdict
                FROM feishu_alert_feedback
                WHERE supervisor_verdict = '误报'
                  AND (strpos(COALESCE(rule_ids, ''), %s) > 0 OR alert_id IN ({placeholders}))
            """
            params: list[Any] = [rule_id, *src_ids]
        else:
            sql = """
                SELECT alert_id, customer_id, customer_name, analysis_date, types, details,
                       message_ids, rule_ids, supervisor_verdict
                FROM feishu_alert_feedback
                WHERE supervisor_verdict = '误报'
                  AND strpos(COALESCE(rule_ids, ''), %s) > 0
            """
            params = [rule_id]
        fb_rows = con.execute(sql, params).fetchall()
        for aid, cid, cname, adate, types_str, det, mids_str, rids_str, verdict in fb_rows:
            if verdict != "误报":
                continue
            rids = [str(x).strip() for x in (json.loads(rids_str) if rids_str else []) if str(x).strip()]
            if not (rule_id in rids or (not rids and str(aid) in src_ids)):
                continue
            key = (str(cid), str(adate))
            if key in seen_keys:
                continue
            s_row = con.execute(
                """
                SELECT snapshot_json, state_changes_json
                FROM eval_slice_runs
                WHERE id_cust = %s AND analysis_date = %s
                ORDER BY ctid DESC LIMIT 1
                """,
                [str(cid), str(adate)],
            ).fetchone()
            snap_obj = json.loads(s_row[0]) if s_row and s_row[0] else {}
            state_changes_obj = json.loads(s_row[1]) if s_row and s_row[1] else []
            snap_alert = (snap_obj.get("alert") or {}) if isinstance(snap_obj, dict) else {}
            snap_rids = (
                [str(x).strip() for x in (snap_alert.get("rule_ids") or []) if str(x).strip()]
                if isinstance(snap_alert, dict)
                else []
            )
            if not rids and snap_rids and rule_id not in snap_rids:
                continue
            seen_keys.add(key)
            c_row = con.execute(
                "SELECT messages_json FROM chat_messages_store WHERE id_cust = ? AND analysis_date = ?",
                [str(cid), str(adate)],
            ).fetchone()
            day_msgs = json.loads(c_row[0]) if c_row and c_row[0] else []

            state_before_obj, priors = (
                _reconstruct_state_and_priors(con, str(cid), str(adate))
                if s_row
                else (None, [])
            )
            sb_str = json.dumps(state_before_obj, ensure_ascii=False) if state_before_obj else None
            priors_str = json.dumps(priors, ensure_ascii=False)

            fb_types = [str(x).strip() for x in (json.loads(types_str) if types_str else []) if str(x).strip()]
            snap_types = (
                [str(x).strip() for x in (snap_alert.get("types") or []) if str(x).strip()]
                if isinstance(snap_alert, dict)
                else []
            )
            pred_types = fb_types or snap_types or ([str(alert_type)] if alert_type else [])
            primary_pred_type = pred_types[0] if pred_types else str(alert_type or "")
            is_multi_label = len(fb_types) > 1 or len(snap_types) > 1
            snap_mids = (
                [str(x).strip() for x in (snap_alert.get("message_ids") or []) if str(x).strip()]
                if isinstance(snap_alert, dict)
                else []
            )
            snap_summary = str(snap_obj.get("summary") or "") if isinstance(snap_obj, dict) else ""
            cid_str = str(cid)
            if cid_str not in payload_cache:
                payload_cache[cid_str] = load_customer_payload(cid_str, db_path=db_path)
            payload = payload_cache[cid_str]
            ev_ids = set(json.loads(mids_str or "[]") or snap_mids)
            all_cand_msgs = (
                [m for d, ms in sorted(payload["days"].items()) if d <= str(adate) for m in ms]
                if payload and isinstance(payload.get("days"), dict)
                else day_msgs
            )
            cited_msgs = [m for m in all_cand_msgs if isinstance(m, dict) and m.get("message_id") in ev_ids]
            rolling_in = None
            if payload and str(adate) in payload.get("days", {}) and state_before_obj:
                rolling_in = build_rolling_input(
                    str(adate), payload, priors[-1] if priors else None, state_before_obj, multi_label=is_multi_label
                )
            cases.append(
                {
                    "alert_id": str(aid),
                    "cid": str(cid),
                    "cname": str(cname or ""),
                    "day": str(adate),
                    "predicted_type": primary_pred_type,
                    "predicted_types": pred_types,
                    "predicted_details": str(det or (snap_alert.get("details") if isinstance(snap_alert, dict) else "") or ""),
                    "multi_label": is_multi_label,
                    "day_summary": snap_summary,
                    "cited_messages": cited_msgs,
                    "day_full_chat_messages": day_msgs,
                    "state_before": sb_str,
                    "prior_snapshots": priors_str,
                    "initial_snapshot_output": {"snapshot": snap_obj, "state_changes": state_changes_obj},
                    "queried_alert_types": list(ALERT_TYPE_ORDER) if pred_types else [],
                    "recalled_evidence_ids": sorted(ev_ids),
                    "initial_student_rolling_input": rolling_in,
                }
            )
    return cases


def load_random_verification_slices(
    exclude_keys: set[tuple[str, str]],
    limit: int = 3,
    seed: int = 42,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """从单一数据库的线上切片记录中随机抽取未被设置为误报的回归验证样本。"""
    candidates = []
    seen: set[tuple[str, str]] = set()
    with connect_db(db_path, read_only=True) as con:
        rows = con.execute(
            """
            SELECT s.id_cust, s.analysis_date, s.snapshot_json
            FROM eval_slice_runs s
            LEFT JOIN feishu_alert_feedback f
              ON f.customer_id = s.id_cust AND f.analysis_date = s.analysis_date
            WHERE COALESCE(f.supervisor_verdict, '') != '误报'
            ORDER BY s.analysis_date DESC, s.id_cust DESC
            LIMIT 200
            """
        ).fetchall()
        for r in rows:
            key = (str(r[0]), str(r[1]))
            if key in exclude_keys or key in seen:
                continue
            seen.add(key)
            snap_obj = json.loads(r[2]) if len(r) > 2 and r[2] else {}
            snap_alert = (snap_obj.get("alert") or {}) if isinstance(snap_obj, dict) else {}
            raw_types = snap_alert.get("types") if isinstance(snap_alert, dict) else None
            baseline_types = (
                [str(x).strip() for x in raw_types if str(x).strip()]
                if isinstance(raw_types, list) and raw_types
                else []
            )
            baseline_pred = baseline_types[0] if baseline_types else None
            baseline_details = str(snap_alert.get("details") or "") if isinstance(snap_alert, dict) else ""
            state_before_obj, priors = _reconstruct_state_and_priors(con, str(r[0]), str(r[1]))
            candidates.append(
                {
                    "cid": str(r[0]),
                    "day": str(r[1]),
                    "baseline_pred": baseline_pred,
                    "baseline_types": baseline_types,
                    "baseline_details": baseline_details,
                    "state_before": json.dumps(state_before_obj, ensure_ascii=False),
                    "prior_snapshots": json.dumps(priors, ensure_ascii=False),
                    "multi_label": len(baseline_types) > 1,
                }
            )
    if not candidates or limit <= 0:
        return []
    return random.Random(seed).sample(candidates, min(limit, len(candidates)))

"""纯生产环境 HTTP 服务入口：
提供单日聊天切片风险推理落库 (/api/v1/analyze)、教师模型规则 Hook 触发 (/api/v1/patrol/run) 与健康检查 (/api/health)，
并支持同进程挂载飞书 WebSocket 长连接。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import heapq
import itertools
import os
from pathlib import Path
import re
import threading
from typing import Any

from flask import Flask, jsonify, request

from agent import run_single_slice
from agent.config import MULTI_LABEL
from db_adapter import (
    DEFAULT_DB_PATH,
    connect_db,
    init_production_db,
    load_customer_context_before_day,
    resolve_db_url,
    save_daily_slice_run,
)
from feishu_bot import (
    clear_alert_feedback,
    map_internal_to_external_payload,
    save_alert_to_db,
    send_alert_to_feishu,
)
from rule_patrol.patrol_engine import run_patrol_once

app = Flask(__name__)
app.json.ensure_ascii = False

ANALYZE_MAX_WORKERS = int(os.getenv("ANALYZE_MAX_WORKERS", "20"))
ANALYZE_EXECUTOR = ThreadPoolExecutor(max_workers=ANALYZE_MAX_WORKERS, thread_name_prefix="analyze-worker")
# shortcut: single-process memory queue (lost on restart), move to DB/Redis queue when scaling >1 replica
_QUEUE_LOCK = threading.Lock()
_CUSTOMER_PENDING: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
_ACTIVE_CUSTOMERS: set[str] = set()
_TASK_SEQ = itertools.count()


def _enqueue_customer_job(**job_kwargs: Any) -> None:
    cid = str(job_kwargs["cid"])
    day = str(job_kwargs["day"])
    with _QUEUE_LOCK:
        if cid in _ACTIVE_CUSTOMERS:
            heapq.heappush(_CUSTOMER_PENDING.setdefault(cid, []), (day, next(_TASK_SEQ), job_kwargs))
            return
        _ACTIVE_CUSTOMERS.add(cid)
    try:
        ANALYZE_EXECUTOR.submit(_run_customer_job, cid, job_kwargs)
    except Exception:
        with _QUEUE_LOCK:
            _ACTIVE_CUSTOMERS.discard(cid)
            _CUSTOMER_PENDING.pop(cid, None)
        raise


def _run_customer_job(cid: str, job_kwargs: dict[str, Any]) -> None:
    try:
        _run_analyze_job(**job_kwargs)
    finally:
        with _QUEUE_LOCK:
            pending = _CUSTOMER_PENDING.get(cid)
            if pending:
                _, _, next_kwargs = heapq.heappop(pending)
                if not pending:
                    _CUSTOMER_PENDING.pop(cid, None)
            else:
                next_kwargs = None
                _ACTIVE_CUSTOMERS.discard(cid)
        if next_kwargs is not None:
            try:
                ANALYZE_EXECUTOR.submit(_run_customer_job, cid, next_kwargs)
            except Exception:
                with _QUEUE_LOCK:
                    _ACTIVE_CUSTOMERS.discard(cid)
                    _CUSTOMER_PENDING.pop(cid, None)


def _run_analyze_job(
    cid: str,
    cname: str,
    group_id: str,
    day: str,
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
    previous_snapshot: dict[str, Any] | None,
    raw_state: dict[str, Any] | None,
    raw_priors: list[dict[str, Any]] | None,
    enable_tools: bool,
    multi_label: bool,
    supervisor: str,
    receive_id: str,
    receive_id_type: str,
    db_path: str,
) -> None:
    """后台线程池执行单切片推理、落库与飞书推送任务。"""
    try:
        db_past_days, db_state_before, db_priors = load_customer_context_before_day(cid, day, db_path=db_path)

        merged_days: dict[str, Any] = dict(db_past_days)
        merged_days.update(payload.get("days") or {})
        full_payload = {**payload, "days": merged_days}

        current_state: dict[str, Any] = dict(raw_state) if isinstance(raw_state, dict) else db_state_before
        for coll in ("profile", "applications", "items"):
            if current_state.get(coll) is None:
                current_state[coll] = {}

        prior_snapshots: list[dict[str, Any]] = list(raw_priors) if isinstance(raw_priors, list) else db_priors
        if previous_snapshot and not prior_snapshots:
            prior_snapshots = [previous_snapshot]

        slice_out = run_single_slice(
            cid=cid,
            day=day,
            payload=full_payload,
            state_before=current_state,
            prior_snapshots=prior_snapshots,
            multi_label=multi_label,
            enable_tools=enable_tools,
            previous_snapshot=previous_snapshot,
        )

        result = slice_out["result"]
        metrics = slice_out["metrics"]
        state_after = slice_out["state_after"]
        save_daily_slice_run(
            cid=cid,
            cname=cname,
            group_id=group_id,
            day=day,
            messages=messages,
            state_before=current_state,
            prior_snapshots=prior_snapshots,
            result=result,
            metrics=metrics,
            state_after=state_after,
            db_path=db_path,
            multi_label=multi_label,
        )

        snap = result["snapshot"]
        alert_obj = snap.get("alert") if isinstance(snap.get("alert"), dict) else None
        if alert_obj and alert_obj.get("types"):
            flat_alert = {
                "alert_id": f"{cid}_{day}",
                "customer_id": cid,
                "customer_name": cname,
                "group_id": group_id,
                "analysis_date": day,
                "types": alert_obj.get("types") or [],
                "owner": alert_obj.get("owner") or "advisor",
                "supervisor": supervisor,
                "details": alert_obj.get("details") or "",
                "message_ids": alert_obj.get("message_ids") or [],
                "rule_ids": alert_obj.get("rule_ids") or [],
            }
            map_internal_to_external_payload(flat_alert, receive_id=receive_id)
            if receive_id:
                send_alert_to_feishu(
                    flat_alert, receive_id=receive_id, receive_id_type=receive_id_type, db_path=db_path
                )
            else:
                save_alert_to_db(flat_alert, db_path=db_path)
        else:
            clear_alert_feedback(f"{cid}_{day}", db_path=db_path)
        print(f"[Analyze Worker] 完成: customer_id={cid}, day={day}", flush=True)
    except Exception as exc:
        print(f"[Analyze Worker] 失败: customer_id={cid}, day={day}, error={exc}", flush=True)


@app.after_request
def set_utf8_content_type(response):
    if response.mimetype == "application/json":
        response.content_type = "application/json; charset=utf-8"
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 400 <= code < 600:
        return jsonify({"error": getattr(exc, "description", str(exc))}), code
    return jsonify({"error": str(exc)}), 500


@app.get("/api/health")
def health_check():
    """返回服务状态、数据库连接状态与当前生效规则数。"""
    db_path = resolve_db_url()
    init_production_db(db_path)
    with connect_db(db_path, read_only=True) as con:
        active_rules = int(
            (con.execute("SELECT COUNT(*) FROM playbook_rules_ledger").fetchone() or [0])[0]
        )
    return jsonify({
        "status": "ok",
        "db_connected": True,
        "db_path": re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", str(db_path)),
        "active_rules_count": active_rules,
    })


@app.post("/api/v1/analyze")
def analyze_daily_slice():
    """生产单日推理异步接口：同步校验入参格式后投入 20 线程后台队列执行推理、落库与飞书推送。"""
    body = request.get_json(silent=True) or {}
    day = str(body.get("day") or body.get("analysis_date") or "").strip()
    payload = body.get("payload")
    if not day or not isinstance(payload, dict) or day not in (payload.get("days") or {}):
        return jsonify({"error": "必须提供合法的 day 与包含 payload.days[day] 的 daily_chat.v1 数据"}), 400

    db_path = resolve_db_url()
    raw_prev = body.get("previous_snapshot")
    if raw_prev is not None and not isinstance(raw_prev, dict):
        return jsonify({"error": "previous_snapshot 必须是对象或 null"}), 400
    previous_snapshot = raw_prev

    raw_state = body.get("current_state")
    if raw_state is not None and not isinstance(raw_state, dict):
        return jsonify({"error": "current_state 必须是对象或 null"}), 400
    if isinstance(raw_state, dict):
        for coll in ("profile", "applications", "items"):
            if raw_state.get(coll) is not None and not isinstance(raw_state.get(coll), dict):
                return jsonify({"error": f"current_state.{coll} 必须是对象"}), 400

    raw_priors = body.get("prior_snapshots")
    if raw_priors is not None and (not isinstance(raw_priors, list) or any(not isinstance(s, dict) for s in raw_priors)):
        return jsonify({"error": "prior_snapshots 必须是对象数组"}), 400

    messages = payload["days"][day]
    if not isinstance(messages, list) or not messages or any(
        not isinstance(m, dict) or not all(k in m and m[k] is not None and str(m[k]).strip() != "" for k in ("message_id", "sent_at", "role", "text"))
        for m in messages
    ):
        return jsonify({"error": "payload.days[day] 必须是非空数组，且每条消息必须包含非空的 message_id、sent_at、role、text"}), 400

    cid = str(payload.get("customer_id") or "")
    cname = str(body.get("customer_name") or payload.get("customer_name") or "")
    group_id = str(payload.get("group_id") or "")
    enable_tools = bool(body.get("enable_tools", True))
    multi_label = bool(body["multi_label"]) if "multi_label" in body else MULTI_LABEL
    supervisor = str(body.get("supervisor") or payload.get("supervisor") or "").strip()
    receive_id = str(body.get("feishu_receive_id") or os.environ.get("FEISHU_DEFAULT_RECEIVE_ID") or "").strip()
    receive_id_type = str(body.get("feishu_receive_id_type") or "chat_id").strip()

    _enqueue_customer_job(
        cid=cid,
        cname=cname,
        group_id=group_id,
        day=day,
        payload=payload,
        messages=messages,
        previous_snapshot=previous_snapshot,
        raw_state=raw_state,
        raw_priors=raw_priors,
        enable_tools=enable_tools,
        multi_label=multi_label,
        supervisor=supervisor,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        db_path=db_path,
    )

    return jsonify({
        "status": "queued",
        "customer_id": cid,
        "analysis_date": day,
    }), 202


@app.post("/api/v1/patrol/run")
def trigger_patrol():
    """手动触发一次教师 Agent 规则巡查接口。"""
    db_path = resolve_db_url()
    report = run_patrol_once(db_path=db_path)
    return jsonify(report)


if __name__ == "__main__":
    init_production_db()
    if os.environ.get("ENABLE_FEISHU_WS") == "1":
        from feishu_bot import start_ws_listener
        threading.Thread(target=start_ws_listener, daemon=True, name="feishu-ws").start()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )

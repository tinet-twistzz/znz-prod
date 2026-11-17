"""规则回调 Hook 触发、教师模型（gemini-4-argon, thinking_level=high）审查修改与最多 5 轮沙盒重试引擎。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
import yaml

from agent import run_single_slice
from agent.config import DEFAULT_TIMEOUT, is_transient_model_error, load_agent_env
from agent.runner import parse_json, response_text
from db_adapter import (
    ALERT_TYPE_ORDER,
    DEFAULT_DB_PATH,
    DEFAULT_YAML_PATH,
    connect_db,
    init_production_db,
    load_customer_payload,
    load_harmful_cases_for_rule,
    load_random_verification_slices,
    render_yaml_from_db,
)

PATROL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PATROL_DIR.parent
DEFAULT_CONFIG_PATH = Path(os.getenv("PATROL_CONFIG_PATH", PATROL_DIR / "config.yaml"))
SANDBOX_SCRATCH_DIR = Path(os.getenv("SANDBOX_SCRATCH_DIR", Path(tempfile.gettempdir()) / "znz_sandbox"))
SHANGHAI = ZoneInfo("Asia/Shanghai")
_HOOK_LOCK = threading.Lock()
_ACTIVE_HOOK_RULES: set[str] = set()


def init_patrol_table(db_path: Path | str | None = None) -> None:
    """初始化单一生产数据库所需全部表并在账本为空时从 YAML 灌入。"""
    init_production_db(db_path)


def load_patrol_config(config_path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """从 rule_patrol/config.yaml 读取 count 阈值、重试次数、教师模型配置与提示词。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"未找到巡查配置文件: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("rule_patrol/config.yaml 顶层必须是字典")
    return raw


def _render_rules_yaml_str(
    active_rule_rows: list[tuple[Any, ...]],
    target_rule_id: str | None = None,
    candidate_content: str | None = None,
) -> str:
    """根据生效规则渲染完整 YAML 字符串（若指定 target_rule_id 则仅替换该条规则内容）。"""
    by_type: dict[str, list[str]] = {t: [] for t in ALERT_TYPE_ORDER}

    for rid, atype, content in active_rule_rows:
        rid_str, atype_str, content_str = str(rid), str(atype), str(content)
        by_type.setdefault(atype_str, [])
        if target_rule_id and rid_str == target_rule_id and candidate_content is not None:
            content_str = candidate_content
        flat = " ".join(ln.strip() for ln in content_str.strip().splitlines() if ln.strip())
        line = flat if flat.startswith("- [") else f"- [{rid_str}] {flat}"
        by_type[atype_str].append(line)

    blocks = []
    for atype, rules in by_type.items():
        if not rules:
            continue
        indented = "\n".join(f"    {ln}" for ln in rules)
        blocks.append(f"- type: {json.dumps(atype, ensure_ascii=False)}\n  details: |\n{indented}")
    return "\n\n".join(blocks) + "\n"


def _render_sandbox_yaml(
    active_rule_rows: list[tuple[Any, ...]],
    target_rule_id: str,
    candidate_content: str,
) -> Path:
    """在独立沙盒目录 (SANDBOX_SCRATCH_DIR) 下生成候选 YAML 文件，100% 不触碰正式 DB 与正式 YAML。"""
    SANDBOX_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_yaml = SANDBOX_SCRATCH_DIR / f"sandbox_{target_rule_id}_{uuid.uuid4().hex[:8]}.yaml"
    rendered = _render_rules_yaml_str(active_rule_rows, target_rule_id, candidate_content)
    sandbox_yaml.write_text(rendered, encoding="utf-8")
    return sandbox_yaml


def verify_candidate_yaml(
    db_path: Path | str | None,
    cand_yaml_path: Path,
    rule_id: str,
    alert_type: str,
    harmful_cases: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """在沙盒 YAML 上运行子模型，返回原误报案例与随机非误报抽检案例的【修改前 vs 修改后】对比结果。"""
    v_cfg = config.get("verification") or {}
    eval_model = str(os.getenv("GEMINI_MODEL") or v_cfg.get("eval_model") or "gemini-3.5-flash").strip()
    random_count = int(v_cfg.get("random_sample_count", 3))
    seed = int(v_cfg.get("random_seed", 42))

    runnable_harmful = [c for c in harmful_cases if c.get("state_before")]
    exclude_keys = {(c["cid"], c["day"]) for c in runnable_harmful}
    random_samples = load_random_verification_slices(exclude_keys, limit=random_count, seed=seed, db_path=db_path)

    needed_cids = sorted({c["cid"] for c in runnable_harmful} | {s["cid"] for s in random_samples})
    samples_map = {}
    for cid in needed_cids:
        pl = load_customer_payload(cid, db_path=db_path)
        if pl:
            samples_map[cid] = pl

    runnable_harmful = [c for c in runnable_harmful if c["cid"] in samples_map and c["day"] in samples_map[c["cid"]].get("days", {})]
    random_samples = [s for s in random_samples if s["cid"] in samples_map and s["day"] in samples_map[s["cid"]].get("days", {})]

    def _run_item(item: dict[str, Any]) -> dict[str, Any]:
        try:
            return run_single_slice(
                item["cid"],
                item["day"],
                samples_map[item["cid"]],
                json.loads(item["state_before"]),
                json.loads(item["prior_snapshots"]),
                model_name=eval_model,
                yaml_path=cand_yaml_path,
                multi_label=bool(item.get("multi_label", False)),
            )
        except Exception as exc:
            return {
                "cid": item["cid"],
                "day": item["day"],
                "error": str(exc),
                "predicted_alert_type": "__ERROR__",
                "predicted_alert_types": ["__ERROR__"],
                "predicted_alert_details": f"沙盒推理异常: {exc}",
                "snapshot": {"alert": None},
            }

    all_tasks = runnable_harmful + random_samples
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(all_tasks)))) as ex:
        results = list(ex.map(_run_item, all_tasks)) if all_tasks else []

    harmful_results = results[: len(runnable_harmful)]
    random_results = results[len(runnable_harmful) :]

    harmful_passed = True
    harmful_details = []
    for case, res in zip(runnable_harmful, harmful_results):
        snap_alert = (res.get("snapshot") or {}).get("alert") or {}
        triggered_rids = snap_alert.get("rule_ids") or []
        pred_type = res.get("predicted_alert_type")
        pred_types = res.get("predicted_alert_types") or snap_alert.get("types") or ([pred_type] if pred_type else [])
        pred_details = res.get("predicted_alert_details") or snap_alert.get("details") or ""
        msg_ids = snap_alert.get("message_ids") or []
        still_triggered = (
            (rule_id in triggered_rids)
            or (pred_type == alert_type)
            or (alert_type in pred_types)
            or bool(res.get("error"))
        )
        if still_triggered:
            harmful_passed = False
        harmful_details.append(
            {
                "cid": case["cid"],
                "day": case["day"],
                "before_pred_type": case.get("predicted_type"),
                "before_pred_types": case.get("predicted_types") or ([case["predicted_type"]] if case.get("predicted_type") else []),
                "before_alert_details": case.get("predicted_details"),
                "after_pred_type": pred_type,
                "after_pred_types": pred_types,
                "after_alert_details": pred_details,
                "after_rule_ids": triggered_rids,
                "after_message_ids": msg_ids,
                "passed": not still_triggered,
            }
        )

    random_passed = True
    random_details = []
    for sample, res in zip(random_samples, random_results):
        snap_alert = (res.get("snapshot") or {}).get("alert") or {}
        after_pred = res.get("predicted_alert_type")
        raw_after_types = res.get("predicted_alert_types") or snap_alert.get("types") or ([after_pred] if after_pred else [])
        after_types = [str(x).strip() for x in raw_after_types if str(x).strip()]
        raw_base_types = sample.get("baseline_types")
        if raw_base_types is None:
            raw_base_types = [sample["baseline_pred"]] if sample.get("baseline_pred") else []
        baseline_types = [str(x).strip() for x in raw_base_types if str(x).strip()]
        after_details = res.get("predicted_alert_details") or snap_alert.get("details") or ""
        unchanged = (
            not bool(res.get("error"))
            and after_pred == sample["baseline_pred"]
            and set(after_types) == set(baseline_types)
        )
        if not unchanged:
            random_passed = False
        random_details.append(
            {
                "cid": sample["cid"],
                "day": sample["day"],
                "baseline_pred": sample["baseline_pred"],
                "baseline_types": baseline_types,
                "baseline_details": sample["baseline_details"],
                "after_pred": after_pred,
                "after_types": after_types,
                "after_alert_details": after_details,
                "after_rule_ids": snap_alert.get("rule_ids") or [],
                "passed": unchanged,
            }
        )

    return {
        "harmful_cases_checked": len(runnable_harmful),
        "harmful_cases_passed": harmful_passed,
        "harmful_details": harmful_details,
        "random_cases_checked": len(random_samples),
        "random_cases_passed": random_passed,
        "random_details": random_details,
        "all_passed": harmful_passed and random_passed,
    }


def _format_rework_error_history(rework_history: list[dict[str, Any]]) -> str:
    """将前序重试未通过的候选规则与具体错误过程格式化为结构化 JSON 供教师模型下一轮修正参考。"""
    if not rework_history:
        return "无（当前为第 1 次修改尝试）"
    failed_records = []
    for item in rework_history:
        failed_harmful = [d for d in item.get("harmful_details", []) if not d.get("passed")]
        failed_random = [d for d in item.get("random_details", []) if not d.get("passed")]
        failed_records.append(
            {
                "attempt": item["attempt"],
                "proposed_content": item["proposed_content"],
                "diagnosis": item["diagnosis"],
                "harmful_cases_still_falsely_triggered": failed_harmful,
                "random_cases_regressed": failed_random,
            }
        )
    return json.dumps(failed_records, ensure_ascii=False, indent=2)


def invoke_teacher_review(
    config: dict[str, Any],
    alert_type: str,
    rule_id: str,
    rule_content: str,
    rule_count: int,
    full_rules_yaml: str,
    harmful_cases: list[dict[str, Any]],
    attempt_no: int = 1,
    rework_history: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """调用教师模型（gemini-4-argon, 默认 thinking_level='high'）修改目标规则条例。"""
    t_cfg = config.get("teacher_model") or {}
    thresh_cfg = config.get("thresholds") or {}
    max_rework = max(1, int(thresh_cfg.get("max_rework_attempts", 5)))
    model_name = str(os.getenv("TEACHER_MODEL") or t_cfg.get("model_name") or "gemini-4-argon").strip()
    thinking_level = str(t_cfg.get("thinking_level") or "high").strip()
    temperature = float(t_cfg.get("temperature", 0.2))
    sys_prompt = (
        str(t_cfg.get("system_prompt") or "")
        .strip()
        .replace("{max_rework_attempts}", str(max_rework))
        .replace("{rule_id}", str(rule_id))
    )
    user_tpl = str(t_cfg.get("user_prompt_template") or "").strip()

    cases_for_prompt = [
        {
            "customer_id": c["cid"],
            "date": c["day"],
            "initial_student_rolling_input": c.get("initial_student_rolling_input")
            or {
                "day_summary": c.get("day_summary", ""),
                "messages": (c.get("day_full_chat_messages") or [])[:50],
            },
            "initial_student_tool_queries": {
                "queried_alert_types": c.get("queried_alert_types") or [],
                "recalled_evidence_message_ids": c.get("recalled_evidence_ids") or [],
            },
            "initial_student_false_positive_output": {
                "predicted_type": c.get("predicted_type"),
                "predicted_details": c.get("predicted_details"),
                "cited_messages": c.get("cited_messages") or [],
            },
        }
        for c in harmful_cases
    ]

    fmt_kwargs = {
        "attempt_no": attempt_no,
        "max_rework_attempts": max_rework,
        "alert_type": alert_type,
        "rule_id": rule_id,
        "rule_content": rule_content,
        "rule_count": rule_count,
        "full_rules_yaml": full_rules_yaml,
        "harmful_cases_json": json.dumps(cases_for_prompt, ensure_ascii=False, indent=2),
        "rework_error_history": _format_rework_error_history(rework_history or []),
    }
    user_prompt = user_tpl.format(**fmt_kwargs)

    api_key, _ = load_agent_env()
    llm_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "model": model_name,
        "temperature": temperature,
        "request_timeout": DEFAULT_TIMEOUT,
        "retries": 0,
    }
    if "gemini-3" in model_name or "gemini-4" in model_name:
        llm_kwargs["thinking_level"] = thinking_level
    llm = ChatGoogleGenerativeAI(**llm_kwargs)

    resp: Any = None
    for retry_idx in range(3):
        try:
            resp = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_prompt)])
            break
        except Exception as exc:
            if retry_idx >= 2 or not is_transient_model_error(exc):
                raise
            import time
            time.sleep(2 * (retry_idx + 1))
    raw_text = response_text(resp)

    try:
        parsed = parse_json(raw_text)
    except Exception:
        parsed = {}

    raw_new = str(parsed.get("new_content") or "").strip()
    clean_new = re.sub(r"^-\s*\[[^\[\]]+\]\s*", "", raw_new).strip()
    diagnosis = str(parsed.get("diagnosis") or "").strip()
    return {
        "new_content": clean_new,
        "diagnosis": diagnosis,
    }


def rework_rule_by_hook(
    rule_id: str,
    db_path: Path | str | None = None,
    yaml_path: Path | str = DEFAULT_YAML_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any] | None:
    """对单条 count 达到触发阈值的规则执行教师修改与最多 max_rework_attempts (默认 5) 次沙盒重试。"""
    with _HOOK_LOCK:
        if rule_id in _ACTIVE_HOOK_RULES:
            return None
        _ACTIVE_HOOK_RULES.add(rule_id)

    try:
        config = load_patrol_config(config_path)
        init_patrol_table(db_path)

        t_cfg = config.get("thresholds") or {}
        max_rework = max(1, int(t_cfg.get("max_rework_attempts", 5)))
        reset_count = int(t_cfg.get("reset_count", 0))

        with connect_db(db_path, read_only=True) as con:
            rule_row = con.execute(
                """
                SELECT rule_id, alert_type, content, COALESCE(count, 0), COALESCE(source_cust_ids, '[]')
                FROM playbook_rules_ledger
                WHERE rule_id = ?
                """,
                [str(rule_id)],
            ).fetchone()
            if not rule_row:
                return None
            active_rule_rows = con.execute(
                "SELECT rule_id, alert_type, content FROM playbook_rules_ledger ORDER BY rowid"
            ).fetchall()

        rid, atype, old_content, trig_cnt, src_cids_str = (
            str(rule_row[0]),
            str(rule_row[1]),
            str(rule_row[2]),
            int(rule_row[3]),
            str(rule_row[4]),
        )
        full_rules_yaml = _render_rules_yaml_str(active_rule_rows)
        harmful_cases = load_harmful_cases_for_rule(rid, atype, src_cids_str, db_path=db_path)

        rework_history: list[dict[str, Any]] = []
        applied_rec: dict[str, Any] | None = None

        for attempt in range(1, max_rework + 1):
            review_out = invoke_teacher_review(
                config=config,
                alert_type=atype,
                rule_id=rid,
                rule_content=old_content,
                rule_count=trig_cnt,
                full_rules_yaml=full_rules_yaml,
                harmful_cases=harmful_cases,
                attempt_no=attempt,
                rework_history=rework_history,
            )
            cand_content = review_out["new_content"] or old_content
            diagnosis = review_out["diagnosis"]

            sandbox_yaml = _render_sandbox_yaml(active_rule_rows, rid, cand_content)
            try:
                ver_res = verify_candidate_yaml(db_path, sandbox_yaml, rid, atype, harmful_cases, config)
            finally:
                sandbox_yaml.unlink(missing_ok=True)

            rec = {
                "attempt": attempt,
                "proposed_content": cand_content,
                "diagnosis": diagnosis,
                "all_passed": ver_res["all_passed"],
                "harmful_details": ver_res["harmful_details"],
                "random_details": ver_res["random_details"],
                "verification": ver_res,
            }
            rework_history.append(rec)

            if ver_res["all_passed"]:
                applied_rec = rec
                break

        if applied_rec is not None:
            new_content = applied_rec["proposed_content"]
            diagnosis = applied_rec["diagnosis"]
            ver_res = applied_rec["verification"]
            final_status = "APPLIED"
            with connect_db(db_path) as con:
                con.execute(
                    "UPDATE playbook_rules_ledger SET content = ?, count = ?, source_cust_ids = '[]' WHERE rule_id = ?",
                    [new_content, reset_count, rid],
                )
            render_yaml_from_db(db_path, yaml_path)
        else:
            last_rec = rework_history[-1] if rework_history else None
            new_content = last_rec["proposed_content"] if last_rec else ""
            diagnosis = last_rec["diagnosis"] if last_rec else ""
            ver_res = last_rec["verification"] if last_rec else {
                "harmful_cases_checked": 0,
                "harmful_cases_passed": False,
                "random_cases_checked": 0,
                "random_cases_passed": False,
                "all_passed": False,
            }
            final_status = "REJECTED_ROLLED_BACK"
            with connect_db(db_path) as con:
                con.execute(
                    "UPDATE playbook_rules_ledger SET count = ?, source_cust_ids = '[]' WHERE rule_id = ?",
                    [reset_count, rid],
                )

        clean_history = [
            {
                "attempt": h["attempt"],
                "proposed_content": h["proposed_content"],
                "diagnosis": h["diagnosis"],
                "all_passed": h["all_passed"],
                "harmful_details": h["harmful_details"],
                "random_details": h["random_details"],
            }
            for h in rework_history
        ]

        patrol_id = f"hook_{rid}_{uuid.uuid4().hex[:6]}"
        now_str = datetime.now(SHANGHAI).isoformat(timespec="seconds")

        with connect_db(db_path) as con:
            con.execute(
                """
                INSERT INTO patrol_audit_log (
                    patrol_id, rule_id, alert_type, attempts_used,
                    old_content, new_content, diagnosis,
                    rework_history_json, final_status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (patrol_id) DO UPDATE SET
                    rule_id = EXCLUDED.rule_id,
                    alert_type = EXCLUDED.alert_type,
                    attempts_used = EXCLUDED.attempts_used,
                    old_content = EXCLUDED.old_content,
                    new_content = EXCLUDED.new_content,
                    diagnosis = EXCLUDED.diagnosis,
                    rework_history_json = EXCLUDED.rework_history_json,
                    final_status = EXCLUDED.final_status,
                    created_at = EXCLUDED.created_at
                """,
                [
                    patrol_id,
                    rid,
                    atype,
                    len(rework_history),
                    old_content,
                    new_content,
                    diagnosis,
                    json.dumps(clean_history, ensure_ascii=False),
                    final_status,
                    now_str,
                ],
            )

        return {
            "patrol_id": patrol_id,
            "rule_id": rid,
            "alert_type": atype,
            "trigger_count": trig_cnt,
            "attempts_used": len(rework_history),
            "rework_history": clean_history,
            "old_content": old_content,
            "new_content": new_content,
            "diagnosis": diagnosis,
            "verification": ver_res,
            "final_status": final_status,
        }
    finally:
        with _HOOK_LOCK:
            _ACTIVE_HOOK_RULES.discard(rule_id)


def trigger_rule_rework_hook(
    rule_ids: list[str],
    db_path: Path | str | None = None,
    yaml_path: Path | str = DEFAULT_YAML_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    async_mode: bool = True,
) -> list[dict[str, Any]]:
    """当规则 count 达到触发阈值（默认 -3）时启动 Hook 执行规则修改与沙盒重试。"""
    targets = [str(r).strip() for r in rule_ids if str(r).strip()]
    if not targets:
        return []

    def _worker() -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for rid in targets:
            try:
                rep = rework_rule_by_hook(rid, db_path=db_path, yaml_path=yaml_path, config_path=config_path)
                if rep:
                    reports.append(rep)
            except Exception as exc:
                print(f"[Rule Hook] 规则 {rid} 修改异常: {exc}", flush=True)
        return reports

    if async_mode:
        threading.Thread(target=_worker, daemon=True, name=f"rule-hook-{'-'.join(targets)}").start()
        return []
    return _worker()


def run_patrol_once(
    db_path: Path | str | None = None,
    yaml_path: Path | str = DEFAULT_YAML_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """扫描所有 count <= trigger_count 的生效规则并同步执行教师修改 Hook。"""
    config = load_patrol_config(config_path)
    init_patrol_table(db_path)
    trig_thresh = int((config.get("thresholds") or {}).get("trigger_count", -3))

    with connect_db(db_path, read_only=True) as con:
        active_rules = con.execute(
            "SELECT rule_id, COALESCE(count, 0) FROM playbook_rules_ledger ORDER BY rowid"
        ).fetchall()

    triggered_ids = [str(r[0]) for r in active_rules if int(r[1]) <= trig_thresh]
    actions_report = trigger_rule_rework_hook(
        triggered_ids, db_path=db_path, yaml_path=yaml_path, config_path=config_path, async_mode=False
    )
    return {
        "checked_active_rules": len(active_rules),
        "triggered_actions_count": len(actions_report),
        "actions": actions_report,
    }

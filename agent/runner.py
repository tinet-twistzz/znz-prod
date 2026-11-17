"""Agent 核心编排、调用驱动、容错重试与指标统计。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    MODEL_OVERRIDE_VAR,
    MULTI_LABEL,
    is_transient_model_error,
    load_agent_env,
)
from .input_builder import build_rolling_input
from .prompts import JSON_REPAIR_PROMPT, get_system_prompt
from .reducer import apply_state_changes
from .schemas.result import ModelProposalOutput
from .tools import build_customer_tools, collect_message_ids
from .tools.alert_tools import YAML_PATH_OVERRIDE_VAR
from .validator import validate_model_output


def parse_json(text: str) -> dict[str, Any]:
    """解析模型返回的 JSON，支持自动剥离 Markdown 代码块。"""
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("模型响应必须是 JSON 对象")
    return parsed


def response_text(response: Any) -> str:
    """提取消息文本内容（兼容纯字符串与结构化 Blocks）。"""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return str(content)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _inject_alert_detected_at(result: dict[str, Any]) -> None:
    """当 snapshot.alert 为非空字典时，由程序确定性注入当前 UTC+8 识别时间（覆盖模型可能自造的值）。"""
    if not isinstance(result, dict):
        return
    snap = result.get("snapshot")
    if isinstance(snap, dict):
        snap.setdefault("alert", None)
        alert_obj = snap.get("alert")
        if isinstance(alert_obj, dict) and (alert_obj.get("types") or alert_obj.get("type")):
            alert_obj["detected_at"] = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def build_rolling_human_content(
    request: dict[str, Any],
    last_validation_error: str | None = None,
    multi_label: bool = MULTI_LABEL,
) -> str:
    """构造滚动分析的 HumanMessage 文本内容，支持多标签风险识别提示与确定性门禁校验修正提示。"""
    base_json = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    parts = [base_json]
    if multi_label or bool(request.get("multi_label")):
        parts.append(
            "【多标签风险识别模式 (multi_label=true)】今日允许输出多个风险标签（数目不限）："
            "若今日群聊同时触发多个不同类别的风险，请在 snapshot.alert.types 中列出全部触发的风险类别名称，"
            "并在 rule_ids 中为每个触发类别提供至少 1 个归属该类别的合法规则 ID。"
        )
    if last_validation_error and last_validation_error.strip():
        parts.append(
            f"【系统校验修正提示】你上一次输出未通过确定性门禁校验，"
            f"具体错误原因：{last_validation_error.strip()}。"
            "请严格修正上述问题后重新输出，切勿重复同一错误。"
        )
    return "\n\n".join(parts)


def invoke_rolling_with_metrics(
    day: str,
    payload: dict[str, Any],
    previous_snapshot: dict[str, Any] | None,
    current_state: dict[str, Any],
    prior_snapshots: list[dict[str, Any]],
    evidence_ids: set[str] | None = None,
    enable_tools: bool = True,
    last_validation_error: str | None = None,
    queried_alert_types: set[str] | None = None,
    multi_label: bool = MULTI_LABEL,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """核心流水线驱动：带正交检索工具、受控 ReAct 熔断与语法自愈的滚动分析。"""
    api_key, model_name = load_agent_env()
    llm = ChatGoogleGenerativeAI(
        api_key=api_key,
        model=model_name,
        temperature=DEFAULT_TEMPERATURE,
        max_output_tokens=DEFAULT_MAX_TOKENS,
        request_timeout=DEFAULT_TIMEOUT,
        retries=0,
    )

    tools = build_customer_tools(payload, prior_snapshots, day) if enable_tools else []
    system_prompt_text = get_system_prompt()

    # 创建 LangChain Agent，通过 Pydantic 模型约束结构化输出
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt_text,
        response_format=ModelProposalOutput,
    )

    request = build_rolling_input(day, payload, previous_snapshot, current_state, multi_label=multi_label)
    human_content = build_rolling_human_content(request, last_validation_error, multi_label=multi_label)
    started = time.perf_counter()
    model_retry_count = 0
    response: dict[str, Any] = {}

    # 瞬时故障有限退避重试（最多 2 次尝试）
    for attempt in range(2):
        try:
            # 硬性注入 recursion_limit=10 阻断死循环（安全承载最多 3 轮工具调用）
            response = agent.invoke(
                {"messages": [HumanMessage(content=human_content)]},
                config={"recursion_limit": 10},
            )
            break
        except Exception as exc:
            if attempt > 0 or not is_transient_model_error(exc):
                raise
            model_retry_count += 1
            time.sleep(2)

    elapsed_seconds = time.perf_counter() - started
    messages = response.get("messages", [])
    structured_result = response.get("structured_response")
    final_messages = [
        message for message in messages
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None)
    ]

    if structured_result is None and not final_messages:
        raise ValueError("LangChain Agent 没有返回最终分析内容")

    final_content = response_text(final_messages[-1]) if final_messages else ""
    repair_response = None

    if structured_result is not None:
        if hasattr(structured_result, "model_dump"):
            result = structured_result.model_dump(exclude_none=True)
        elif isinstance(structured_result, dict):
            result = structured_result
        else:
            result = dict(structured_result)
    else:
        try:
            result = parse_json(final_content)
        except json.JSONDecodeError as parse_error:
            # 单次 JSON 语法修复自愈流水线
            repair_started = time.perf_counter()
            repair_response = llm.invoke([
                SystemMessage(content=JSON_REPAIR_PROMPT),
                HumanMessage(content=(
                    f"JSON 解析错误：{parse_error.msg}，位置 {parse_error.pos}，"
                    f"行 {parse_error.lineno} 列 {parse_error.colno}。\n"
                    f"错误附近内容：{final_content[max(0, parse_error.pos - 100):parse_error.pos + 100]}\n"
                    f"待修复的完整文本如下：\n{final_content}"
                )),
            ])
            elapsed_seconds += time.perf_counter() - repair_started
            try:
                result = parse_json(response_text(repair_response))
            except json.JSONDecodeError as repair_error:
                raise ValueError(
                    f"{day} 的模型 JSON 在一次语法修复后仍无法解析；"
                    f"原错误位置 {parse_error.pos}，修复后错误位置 {repair_error.pos}。"
                ) from repair_error

    _inject_alert_detected_at(result)

    # 收集全部合法证据 ID 池
    if evidence_ids is not None:
        evidence_ids.update(message["message_id"] for message in payload.get("days", {}).get(day, []))
        evidence_ids.update(collect_message_ids(current_state))
        for message in messages:
            if isinstance(message, ToolMessage):
                try:
                    evidence_ids.update(collect_message_ids(json.loads(response_text(message))))
                except (json.JSONDecodeError, TypeError):
                    pass

    # 统计 Token、工具调用次数与本轮查询过的预警类型集合
    input_tokens = output_tokens = total_tokens = 0
    usage_found = False
    tool_calls = 0
    collected_alert_types: set[str] = set()
    measured_messages = [*messages, *([repair_response] if repair_response is not None else [])]

    for message in measured_messages:
        if isinstance(message, AIMessage):
            msg_tool_calls = getattr(message, "tool_calls", None) or []
            tool_calls += len(msg_tool_calls)
            for tc in msg_tool_calls:
                if isinstance(tc, dict) and tc.get("name") == "get_alert_type_details":
                    # 一旦调用 get_alert_type_details 工具，该工具会一次性返回全部合法预警类型的 details 供全景横向比对
                    from .contracts import ALLOWED_ALERT_TYPES
                    from .tools.alert_tools import load_alert_yaml_entries
                    collected_alert_types.update(ALLOWED_ALERT_TYPES)
                    collected_alert_types.update(item["type"] for item in load_alert_yaml_entries())
            usage = getattr(message, "usage_metadata", None) or {}
            if not usage:
                usage = (getattr(message, "response_metadata", {}) or {}).get("token_usage", {})
            in_count = usage.get("input_tokens", usage.get("prompt_tokens"))
            out_count = usage.get("output_tokens", usage.get("completion_tokens"))
            total_count = usage.get("total_tokens")
            if in_count is not None or out_count is not None or total_count is not None:
                usage_found = True
                input_tokens += int(in_count or 0)
                output_tokens += int(out_count or 0)
                total_tokens += int(total_count if total_count is not None else (in_count or 0) + (out_count or 0))

    if queried_alert_types is not None:
        queried_alert_types.update(collected_alert_types)

    metrics = {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "input_tokens": input_tokens if usage_found else None,
        "output_tokens": output_tokens if usage_found else None,
        "total_tokens": total_tokens if usage_found else None,
        "unclassified_tokens": total_tokens - input_tokens - output_tokens if usage_found else None,
        "tool_calls": tool_calls,
        "queried_alert_types": sorted(collected_alert_types),
        "json_repair_calls": int(repair_response is not None),
        "model_retry_count": model_retry_count,
    }

    return result, metrics


def run_single_slice(
    cid: str,
    day: str,
    payload: dict[str, Any],
    state_before: dict[str, Any],
    prior_snapshots: list[dict[str, Any]],
    model_name: str | None = None,
    yaml_path: str | Path | None = None,
    multi_label: bool = MULTI_LABEL,
    enable_tools: bool = True,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一单切片生产与沙盒推理入口（内置最多 3 次门禁自愈重试与状态机演进）。"""
    messages = payload["days"][day]
    current_ids = {m["message_id"] for m in messages if isinstance(m, dict) and m.get("message_id")}
    today_cust_ids = {
        m["message_id"]
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("student", "parent") and m.get("message_id")
    }
    prev_snap = previous_snapshot if previous_snapshot is not None else (prior_snapshots[-1] if prior_snapshots else None)
    feedback: str | None = None
    last_err: Exception | None = None
    result: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    visible_ids: set[str] = set()

    for attempt in range(1, 4):
        visible_ids, queried_types = set(), set()
        tok_m = MODEL_OVERRIDE_VAR.set(model_name)
        tok_y = YAML_PATH_OVERRIDE_VAR.set(yaml_path)
        try:
            result, metrics = invoke_rolling_with_metrics(
                day,
                payload,
                prev_snap,
                state_before,
                prior_snapshots,
                visible_ids,
                enable_tools=enable_tools,
                last_validation_error=feedback,
                queried_alert_types=queried_types,
                multi_label=multi_label,
            )
            validate_model_output(
                result,
                day,
                state_before,
                visible_ids,
                current_message_ids=current_ids,
                today_customer_message_ids=today_cust_ids,
                queried_alert_types=queried_types if enable_tools else None,
                multi_label=multi_label,
            )
            last_err = None
            break
        except (ValueError, StructuredOutputValidationError) as exc:
            last_err, feedback = exc, str(exc)
        except Exception as exc:
            if not is_transient_model_error(exc):
                raise
            last_err = exc
        finally:
            MODEL_OVERRIDE_VAR.reset(tok_m)
            YAML_PATH_OVERRIDE_VAR.reset(tok_y)
        if attempt < 3:
            time.sleep(1.5 * attempt)

    if last_err is not None or result is None or metrics is None:
        raise RuntimeError(f"run_single_slice failed ({cid}@{day}): {last_err}")

    snap = result["snapshot"]
    as_of = max((m["sent_at"] for m in messages if isinstance(m, dict) and m.get("sent_at")), default=f"{day}T23:59:59+08:00")
    state_after = apply_state_changes(state_before, result, day, as_of_time=as_of, previous_snapshot=prev_snap)
    alert_obj = snap.get("alert") if isinstance(snap.get("alert"), dict) else None
    pred_types = list(alert_obj.get("types") or []) if alert_obj else []
    tc = snap.get("temperature_change") or {}
    return {
        "cid": str(cid),
        "day": day,
        "predicted_alert_type": pred_types[0] if pred_types else None,
        "predicted_alert_types": pred_types,
        "owner": alert_obj.get("owner") if alert_obj else None,
        "detected_at": alert_obj.get("detected_at") if alert_obj else None,
        "predicted_alert_details": alert_obj.get("details") if alert_obj else None,
        "queried_alert_types": metrics.get("queried_alert_types") or [],
        "evidence_message_ids": sorted(visible_ids),
        "temp_delta": int(tc.get("delta") or 0),
        "temp_after": int(state_after["temperature"]),
        "input_tokens": int(metrics.get("input_tokens") or 0),
        "output_tokens": int(metrics.get("output_tokens") or 0),
        "snapshot": snap,
        "state_changes": result.get("state_changes", []),
        "state_after": state_after,
        "result": result,
        "metrics": metrics,
    }

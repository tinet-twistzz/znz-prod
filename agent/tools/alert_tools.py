"""风控预警类型详细判定细则（L2）按需检索工具（BaseTool 子类化实现）。

单文件唯一数据源：agent/alert_details.yaml 维护 L2 完整判定细则与边界（type + details）；
L1 轻量简介（description）维护于 SYSTEM_PROMPT.md。
怀疑可能触发预警时，调用 get_alert_type_details 工具一次性返回全集供横向比对。
"""

from __future__ import annotations

from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
from typing import Any

import yaml
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

_DEFAULT_YAML_PATH = Path(__file__).resolve().parent.parent / "alert_details.yaml"
YAML_PATH_OVERRIDE_VAR: ContextVar[Path | str | None] = ContextVar("YAML_PATH_OVERRIDE_VAR", default=None)
RULE_LINE_RE = re.compile(r"^\s*-\s*\[([^\[\]]+)\]\s*(.+)$")
_YAML_CACHE: dict[tuple[str, int], tuple[list[dict[str, str]], dict[str, dict[str, str]]]] = {}


def _load_yaml_bundle(
    yaml_path: Path | str | None = None,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """按文件 mtime_ns 一次性解析并缓存 YAML 列表与规则索引。"""
    raw_path = yaml_path or YAML_PATH_OVERRIDE_VAR.get() or os.getenv("ALERT_DETAILS_YAML_PATH") or _DEFAULT_YAML_PATH
    target_path = Path(raw_path).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"未找到预警详情配置文件: {target_path}")
    cache_key = (str(target_path), target_path.stat().st_mtime_ns)
    if cache_key in _YAML_CACHE:
        return _YAML_CACHE[cache_key]

    raw_items = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError("alert_details.yaml 顶层必须是列表")

    entries: list[dict[str, str]] = []
    rules_index: dict[str, dict[str, str]] = {}
    for item in raw_items:
        if not (isinstance(item, dict) and isinstance(item.get("type"), str)):
            continue
        t = item["type"].strip()
        if not t:
            continue
        details = str(item.get("details") or "").strip()
        entries.append({"type": t, "details": details})
        for raw_line in details.splitlines():
            rule_match = RULE_LINE_RE.match(raw_line)
            if rule_match:
                rule_id = rule_match.group(1).strip()
                content = rule_match.group(2).strip()
                if rule_id and content:
                    rules_index[rule_id] = {
                        "rule_id": rule_id,
                        "alert_type": t,
                        "content": content,
                    }

    if len(_YAML_CACHE) > 32:
        _YAML_CACHE.clear()
    _YAML_CACHE[cache_key] = (entries, rules_index)
    return entries, rules_index


def load_alert_yaml_entries(yaml_path: Path | str | None = None) -> list[dict[str, str]]:
    """从 agent/alert_details.yaml 实时读取预警配置列表（按文件 mtime_ns 缓存，热修改 YAML 立即生效）。"""
    return _load_yaml_bundle(yaml_path)[0]


def load_alert_rules_index(yaml_path: Path | str | None = None) -> dict[str, dict[str, str]]:
    """从 agent/alert_details.yaml 解析提取全部带显式编号 `- [RULE_ID] CONTENT` 的单条规则索引。"""
    return _load_yaml_bundle(yaml_path)[1]


class GetAlertTypeDetailsInput(BaseModel):
    """预警细则检索工具入参定义（兼容可选候选列表，调用即返回完整 YAML 细则以支持全景排他比对）。"""

    alert_types: list[str] = Field(
        default_factory=list,
        description="可选：初步怀疑的候选预警类型名称列表（如 ['顾问专业性', '文书问题']）；留空或传入任意候选均返回全部预警类型的完整 details 与排他边界供横向核验。",
    )


class GetAlertTypeDetailsTool(BaseTool):
    """渐进式披露风控预警细则检索工具（生产级 BaseTool 实现）。"""

    name: str = "get_alert_type_details"
    description: str = (
        "当通过今日的聊天记录，快照等信息结合往日长期，初步判断可能触发某一种或几种预警类型时，"
        "必须先调用本工具获取对应类型的 details 进行比对核实，严禁未调用本工具直接输出触发预警结论。"
    )
    args_schema: Any = GetAlertTypeDetailsInput

    def _run(self, alert_types: list[str] | None = None) -> str:
        entries = load_alert_yaml_entries()
        prio = list(dict.fromkeys(
            t.strip() for t in (alert_types or []) if isinstance(t, str) and t.strip()
        ))
        rank = {t: i for i, t in enumerate(prio)}
        ordered = sorted(entries, key=lambda e: (0, rank[e["type"]]) if e["type"] in rank else (1, 0))
        return json.dumps(
            [{"type": e["type"], "details": e["details"]} for e in ordered],
            ensure_ascii=False,
        )

"""契约定义与核心枚举常量。"""

from __future__ import annotations

import re
from typing import Any

# 允许的角色常量（6 大核心业务角色 + bot）
ALLOWED_ROLES: set[str] = {
    "student", "parent", "sales", "advisor", "writer", "planner", "bot"
}

# 核心业务责任人枚举（状态机与未闭环事项白名单）
ALLOWED_OWNERS: set[str] = {
    "advisor", "sales", "writer", "planner", "student", "parent"
}

DEFAULT_ALERT_OWNER_BY_TYPE: dict[str, str] = {
    "顾问专业性": "advisor",
    "费用相关": "advisor",
    "入学风险": "advisor",
    "语言不达标": "student",
    "拖延失联": "student",
    "申请策略": "advisor",
    "文书问题": "writer",
    "其他": "advisor",
    "录取风险": "advisor",
    "定校相关": "advisor",
    "难度客户": "advisor",
}

# 允许的 11 大标准风控预警类型名称集合
ALLOWED_ALERT_TYPES: set[str] = set(DEFAULT_ALERT_OWNER_BY_TYPE)

# 状态变更合规字段白名单
PATCH_ALLOWED_FIELDS: dict[str, set[str]] = {
    "profile": {"fact", "value"},
    "applications": {
        "project", "stage", "status", "current_status", "next_step", "owner",
        "title", "due_at", "progress",
    },
    "items": {"title", "owner", "due_at", "status", "application_id", "progress", "kind"},
}

APPLICATION_STAGES: set[str] = {
    "consultation", "school_selection", "material_collection", "document_drafting",
    "application_submission", "school_review", "offer", "visa", "pre_departure", "other",
}

APPLICATION_STATUSES: set[str] = {
    "planned", "active", "submitted", "offer", "rejected", "withdrawn",
}

# 申请阶段与状态的中英文双向对齐与容错归一化映射
STAGE_SYNONYMS: dict[str, str] = {
    "consultation": "consultation",
    "school_selection": "school_selection",
    "material_collection": "material_collection",
    "document_drafting": "document_drafting",
    "application_submission": "application_submission",
    "school_review": "school_review",
    "offer": "offer",
    "visa": "visa",
    "pre_departure": "pre_departure",
    "other": "other",
    # 中文语义映射
    "咨询": "consultation",
    "前期咨询": "consultation",
    "选校": "school_selection",
    "选校方案": "school_selection",
    "选校阶段": "school_selection",
    "材料": "material_collection",
    "材料收集": "material_collection",
    "材料准备": "material_collection",
    "准备": "material_collection",
    "准备中": "material_collection",
    "筹备": "material_collection",
    "提前批准备": "material_collection",
    "提前批筹备": "material_collection",
    "文书": "document_drafting",
    "文书准备": "document_drafting",
    "文书准备阶段": "document_drafting",
    "文书起草": "document_drafting",
    "文书修改": "document_drafting",
    "文书写作": "document_drafting",
    "网申": "application_submission",
    "网申递交": "application_submission",
    "网申提交": "application_submission",
    "网申已提交": "application_submission",
    "网申已递交": "application_submission",
    "网申填报中": "application_submission",
    "网申填写中": "application_submission",
    "网申填写完成": "application_submission",
    "网申准备": "application_submission",
    "网申中": "application_submission",
    "网申阶段": "application_submission",
    "递交": "application_submission",
    "已递交": "application_submission",
    "审核": "school_review",
    "学校审核": "school_review",
    "学校审理": "school_review",
    "审理中": "school_review",
    "录取": "offer",
    "已获录取": "offer",
    "有条件录取": "offer",
    "无条件录取": "offer",
    "获得录取": "offer",
    "签证": "visa",
    "签证办理": "visa",
    "行前": "pre_departure",
    "行前准备": "pre_departure",
}

STATUS_SYNONYMS: dict[str, str] = {
    "planned": "planned",
    "active": "active",
    "submitted": "submitted",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    # 中文及容错映射
    "规划中": "planned",
    "进行中": "active",
    "已递交": "submitted",
    "已提交": "submitted",
    "已录取": "offer",
    "已获录取": "offer",
    "录取": "offer",
    "fulfilled": "offer",
    "拒信": "rejected",
    "已拒信": "rejected",
    "撤回": "withdrawn",
    "已撤回": "withdrawn",
}


def normalize_application_stage(raw: str | None) -> str:
    """将大模型提议的中文/非标阶段归一化为标准的 10 大申请阶段英文枚举。"""
    if not raw or not isinstance(raw, str):
        return "other"
    key = raw.strip().lower()
    if key in STAGE_SYNONYMS:
        return STAGE_SYNONYMS[key]
    for syn, target in STAGE_SYNONYMS.items():
        if syn in key:
            return target
    return "other"


def normalize_application_status(raw: str | None) -> str:
    """将大模型提议的中文/非标状态归一化为标准的 6 大申请状态英文枚举。"""
    if not raw or not isinstance(raw, str):
        return "active"
    key = raw.strip().lower()
    if key in STATUS_SYNONYMS:
        return STATUS_SYNONYMS[key]
    for syn, target in STATUS_SYNONYMS.items():
        if syn in key:
            return target
    return "active"


# 6 大核心业务责任人中文同义词映射
OWNER_SYNONYMS: dict[str, str] = {
    # advisor
    "advisor": "advisor",
    "售后": "advisor",
    "主导师": "advisor",
    "服务导师": "advisor",
    "申请导师": "advisor",
    "顾问": "advisor",
    "后期": "advisor",
    "班主任": "advisor",
    # sales
    "sales": "sales",
    "销售": "sales",
    "前期顾问": "sales",
    "咨询顾问": "sales",
    "签约顾问": "sales",
    "前期": "sales",
    # writer
    "writer": "writer",
    "文案": "writer",
    "文书": "writer",
    "文书导师": "writer",
    "文书老师": "writer",
    "文书顾问": "writer",
    # planner
    "planner": "planner",
    "规划师": "planner",
    "规划导师": "planner",
    "规划老师": "planner",
    "背景提升": "planner",
    # student
    "student": "student",
    "学生": "student",
    "同学": "student",
    "本人": "student",
    "申请人": "student",
    # parent
    "parent": "parent",
    "家长": "parent",
    "父母": "parent",
    "妈妈": "parent",
    "爸爸": "parent",
    "母亲": "parent",
    "父亲": "parent",
}

_ISO_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def normalize_owner(raw: str | None) -> str | None:
    """将大模型提议的责任人称谓归一化为 6 大核心业务角色英文枚举。"""
    if raw is None or not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    key = stripped.lower()
    if key in OWNER_SYNONYMS:
        return OWNER_SYNONYMS[key]
    for syn, target in sorted(OWNER_SYNONYMS.items(), key=lambda item: len(item[0]), reverse=True):
        if syn in key:
            return target
    return stripped


def extract_iso_date(raw: Any) -> str | None:
    """从任意输入中提取标准 YYYY-MM-DD 日期字符串；若无合法带年份日期则返回 None。"""
    if not isinstance(raw, str):
        return None
    match = _ISO_DATE_RE.search(raw.strip())
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _has_non_empty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def normalize_state_patch(collection: str, patch: dict[str, Any]) -> None:
    """针对 StatePatch 扁平 Schema 引发的跨集合同义/冗余字段进行确定性原地规整。

    仅处理 StatePatch 已知跨集合字段的同义映射与无害剥离；
    任何不在 StatePatch 定义范围内的野生/幻觉字段保留原样，交由下游白名单严格拦截。
    """
    if not isinstance(patch, dict) or collection not in PATCH_ALLOWED_FIELDS:
        return

    allowed = PATCH_ALLOWED_FIELDS[collection]

    # a) 清理跨集合无效 None 值及除 due_at / application_id 外的空占位符
    for k in list(patch.keys()):
        if patch[k] is None and (k not in allowed or k not in {"due_at", "application_id"}):
            patch.pop(k, None)

    # b) profile 集合同义映射与无害跨集合字段剥离
    if collection == "profile":
        if not _has_non_empty_str(patch.get("fact")) and _has_non_empty_str(patch.get("title")):
            patch["fact"] = patch.pop("title")
        if not _has_non_empty_str(patch.get("value")):
            for cand in ("content", "current_status", "progress"):
                if _has_non_empty_str(patch.get(cand)):
                    patch["value"] = patch.pop(cand)
                    break
        if _has_non_empty_str(patch.get("fact")) or _has_non_empty_str(patch.get("value")):
            for redundant in (
                "status", "owner", "title", "content", "current_status",
                "next_step", "progress", "due_at", "application_id",
                "project", "stage", "kind",
            ):
                patch.pop(redundant, None)

    # c) items 集合同义映射與冗余剥离（统一收敛至 title）
    elif collection == "items":
        legacy_content = patch.pop("content", None)
        if not _has_non_empty_str(patch.get("title")) and _has_non_empty_str(legacy_content):
            patch["title"] = legacy_content

        cs = patch.pop("current_status", None)
        ns = patch.pop("next_step", None)
        if not _has_non_empty_str(patch.get("progress")):
            cs_str = cs.strip() if isinstance(cs, str) and cs.strip() else ""
            ns_str = ns.strip() if isinstance(ns, str) and ns.strip() else ""
            if cs_str and ns_str and cs_str != ns_str:
                patch["progress"] = f"{cs_str}；下一步：{ns_str}"
            elif cs_str or ns_str:
                patch["progress"] = cs_str or ns_str

        if not _has_non_empty_str(patch.get("title")):
            for cand in ("project", "value", "fact"):
                if _has_non_empty_str(patch.get(cand)):
                    patch["title"] = patch[cand]
                    break
        for redundant in ("project", "stage", "fact", "value"):
            patch.pop(redundant, None)

    # d) applications 集合同义映射与无害跨集合字段剥离
    elif collection == "applications":
        legacy_content = patch.pop("content", None)
        if not _has_non_empty_str(patch.get("project")) and _has_non_empty_str(patch.get("title")):
            patch["project"] = patch["title"]
        if not _has_non_empty_str(patch.get("current_status")):
            if _has_non_empty_str(patch.get("progress")):
                patch["current_status"] = patch["progress"]
            elif _has_non_empty_str(legacy_content):
                patch["current_status"] = legacy_content
        for redundant in ("application_id", "kind", "fact", "value"):
            patch.pop(redundant, None)

ITEM_KINDS: set[str] = {
    "commitment", "todo", "question",
}

ITEM_STATUSES_ACTIVE: set[str] = {
    "open", "in_progress", "needs_review",
}

ITEM_STATUSES_CLOSED: set[str] = {
    "fulfilled", "cancelled",
}

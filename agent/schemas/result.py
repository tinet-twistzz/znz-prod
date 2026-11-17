"""分析结果与长期状态数据模型定义（基于 mvp.v1 数据规范与真实运行结果文件）。"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts import ALLOWED_OWNERS, DEFAULT_ALERT_OWNER_BY_TYPE, normalize_owner


# ==========================================
# 1. 单日分析快照（Daily Snapshot）相关模型
# ==========================================

class AlertItem(BaseModel):
    """当日触发的风控预警记录。"""
    model_config = ConfigDict(extra="ignore")

    types: list[str] = Field(
        default_factory=list,
        description="识别到的预警类型列表（单标签模式下仅含 1 个类型；多标签模式下可含多个合法预警类型），必须属于输入 alert_types 定义的合法类型",
    )
    owner: str = Field(
        default="advisor",
        description="该风险的责任人角色（必须属于 advisor=售后, writer=文案, planner=规划师, sales=销售, student=学生, parent=家长 之一）",
    )
    details: str = Field(description="简明可核验的预警事实、客户诉求与情绪描述")
    message_ids: list[str] = Field(description="支撑该预警的原始消息 ID 列表")
    rule_ids: list[str] = Field(
        default_factory=list,
        description="支撑该预警的细则规则编号列表（如 ['文书-预兆-01']），必须引用 get_alert_type_details 返回的对应类别下的真实规则 ID",
    )
    detected_at: Optional[str] = Field(
        default=None,
        description="系统识别出该风险的时间（UTC+8，RFC 3339 格式，由程序自动生成注入）",
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_type_and_types(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_type = data.pop("type", None)
            raw_types = data.get("types")
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
                data["types"] = norm_types
            raw_owner = data.get("owner")
            if isinstance(raw_owner, str) and raw_owner.strip():
                norm_o = normalize_owner(raw_owner)
                if norm_o in ALLOWED_OWNERS:
                    data["owner"] = norm_o
            elif not raw_owner:
                p_type = norm_types[0] if norm_types else ""
                data["owner"] = DEFAULT_ALERT_OWNER_BY_TYPE.get(str(p_type), "advisor")
        return data


class TemperatureChange(BaseModel):
    """当日服务温度建议变化。"""
    model_config = ConfigDict(extra="ignore")

    delta: int = Field(ge=-100, le=100, description="温度变化幅度，区间 [-100, 100]，无变化为 0")
    reason: str = Field(description="温度变化原因说明")
    message_ids: list[str] = Field(default_factory=list, description="非零变化必须引用的证据消息 ID")


class DailySnapshot(BaseModel):
    """当日不可变服务快照（业务结论）。"""
    model_config = ConfigDict(extra="ignore")

    analysis_date: str = Field(description="分析日期，格式 YYYY-MM-DD")
    summary: str = Field(description="当日服务事实总结摘要")
    alert: Optional[AlertItem] = Field(default=None, description="当日触发的风控预警；未触发时为 null")
    temperature_change: TemperatureChange = Field(description="当日温度建议变化")


# ==========================================
# 2. 状态变更提案（State Changes）模型
# ==========================================

StateOp = Literal["add", "update", "close"]
StateCollection = Literal["profile", "applications", "items"]


class StatePatch(BaseModel):
    """状态变更字段补丁（跨 profile、applications、items 集合的受控合法字段）。"""
    model_config = ConfigDict(extra="ignore")

    # 通用/共享字段
    owner: Optional[str] = Field(default=None, description="负责角色（advisor=售后, sales=销售, writer=文案, planner=规划师, student=学生, parent=家长）")
    status: Optional[str] = Field(default=None, description="状态枚举（应用阶段状态 或 待办事项状态 open/in_progress/fulfilled/cancelled）")

    # profile 字段
    fact: Optional[str] = Field(default=None, description="画像事实名称")
    value: Optional[str] = Field(default=None, description="画像具体内容")

    # applications 字段
    project: Optional[str] = Field(default=None, description="申请项目全称")
    stage: Optional[str] = Field(default=None, description="申请阶段")
    current_status: Optional[str] = Field(default=None, description="当前进展说明")
    next_step: Optional[str] = Field(default=None, description="下一步行动")

    # items (待办事项) 字段
    kind: Optional[str] = Field(default=None, description="待办事项分类（commitment/todo/question）")
    title: Optional[str] = Field(default=None, description="待办事项的描述")
    due_at: Optional[str] = Field(default=None, description="截止时间")
    progress: Optional[str] = Field(default=None, description="最新跟进进度")
    application_id: Optional[str] = Field(default=None, description="关联申请项目 ID")

    @model_validator(mode="before")
    @classmethod
    def _sync_title_content(cls, data: Any) -> Any:
        if isinstance(data, dict):
            legacy_c = data.pop("content", None)
            if not data.get("title") and legacy_c is not None:
                data["title"] = legacy_c
        return data


class StateChange(BaseModel):
    """模型提出的单项长期状态变更事件。"""
    model_config = ConfigDict(extra="ignore")

    op: StateOp = Field(description="操作类型：add (新增), update (更新), close (关闭)")
    collection: StateCollection = Field(description="目标集合：profile (画像), applications (申请), items (事项)")
    id: str = Field(description="状态对象的稳定 ID")
    patch: StatePatch = Field(description="实际变化的字段补丁键值对")
    message_ids: list[str] = Field(default_factory=list, description="支撑该变更的证据消息 ID 列表")


class ModelProposalOutput(BaseModel):
    """大模型分析返回的顶层原始 JSON 契约。"""
    model_config = ConfigDict(extra="ignore")

    snapshot: DailySnapshot = Field(description="当日服务快照")
    state_changes: list[StateChange] = Field(default_factory=list, description="长期状态变更提议列表")

"""agent 统一对外门面与向下兼容导出。"""

from .config import is_transient_model_error
from .contracts import ALLOWED_ALERT_TYPES
from .input_builder import (
    build_rolling_input,
    compact_chat_message,
    compact_evidence_ids,
    compact_previous_snapshot,
)
from .prompts import PROMPT_VERSION, get_system_prompt
from .reducer import apply_state_changes
from .runner import build_rolling_human_content, invoke_rolling_with_metrics, run_single_slice
from .tools.alert_tools import load_alert_rules_index
from .tools.base import collect_message_ids
from .validator import validate_model_output

__all__ = [
    "PROMPT_VERSION",
    "ALLOWED_ALERT_TYPES",
    "is_transient_model_error",
    "compact_chat_message",
    "compact_evidence_ids",
    "compact_previous_snapshot",
    "build_rolling_input",
    "build_rolling_human_content",
    "collect_message_ids",
    "load_alert_rules_index",
    "validate_model_output",
    "apply_state_changes",
    "invoke_rolling_with_metrics",
    "run_single_slice",
]

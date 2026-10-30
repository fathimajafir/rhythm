"""The agent layer: model / tools / orchestration, per *Introduction to Agents*.

Capability level: **Level 2 — strategic problem-solver** (planning, memory, tools,
context engineering). Deliberately not Level 3: a single accountable loop with a
human clinician backstop is the right shape until the stepped-wedge pilot proves
the loop works.
"""
from .agent import Agent, MAX_STEPS, MAX_TOOL_CALLS, MAX_WALL_SECONDS
from .contracts import (
    AgentResult,
    Decision,
    Mission,
    Observation,
    Scene,
    Step,
    ToolCall,
)
from .evaluate import EvalCase, EvalReport, run_eval
from .model import CallbackModel, Model, ScriptedPlanner
from .policy import (
    ADMIN,
    PolicyError,
    Principal,
    READ,
    SAFETY,
    WRITE,
    authorize,
    visible_tools,
)
from .registry import Registry, Runtime, ToolContext, ToolError, ToolSpec
from .tools import build_registry
from .trace import Trace

__all__ = [
    "Agent",
    "AgentResult",
    "CallbackModel",
    "Decision",
    "EvalCase",
    "EvalReport",
    "Mission",
    "Model",
    "Observation",
    "Principal",
    "READ",
    "SAFETY",
    "WRITE",
    "Registry",
    "Runtime",
    "Scene",
    "ScriptedPlanner",
    "Step",
    "ToolCall",
    "ToolContext",
    "ToolError",
    "ToolSpec",
    "Trace",
    "authorize",
    "build_registry",
    "run_eval",
    "visible_tools",
]

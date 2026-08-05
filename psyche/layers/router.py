"""Message classifier + layer plan selector.

v1: pure rules + keyword heuristics. No embeddings, no classifier model.
Returns (message_class, layer_plan) where layer_plan is an ordered list
of layer names to run, subject to short-circuit on confidence.
"""

from __future__ import annotations

import re

_CODE_HINTS = re.compile(
    r"\b(function|class|def |import |const |let |var |return|error|exception|"
    r"traceback|stack trace|compile|build|test|lint|bug|crash|segfault|"
    r"null pointer|undefined|typescript|python|javascript|c\+\+|rust)\b",
    re.I,
)
_MATH_HINTS = re.compile(
    r"\b(calculate|compute|sum|product|derivative|integral|equation|solve|"
    r"matrix|vector|probability|statistics|percent|percentage)\b|"
    r"\d+\s*[\+\-\*/\^]\s*\d+",
    re.I,
)
_FACTUAL_HINTS = re.compile(
    r"\b(what is|who is|when did|where is|how many|how much|what year|"
    r"define|definition of|history of|capital of)\b",
    re.I,
)
_MULTI_STEP_HINTS = re.compile(
    r"\b(step by step|walk me through|explain how|design|architect|plan|"
    r"strategy|approach|pros and cons|trade\-?offs?)\b",
    re.I,
)
_GREETING_HINTS = re.compile(
    r"^\s*(hi|hello|hey|yo|sup|good morning|good afternoon|good evening|"
    r"how are you|thanks|thank you|ty|cool|nice|ok|okay|got it)\b[\s\.\!\?]*$",
    re.I,
)
_OPINION_HINTS = re.compile(
    r"\b(what do you think|your opinion|do you prefer|would you|your take|"
    r"thoughts on)\b",
    re.I,
)


def classify(message: str) -> str:
    """Return the coarse class of the message."""
    m = message.strip()
    if _GREETING_HINTS.match(m):
        return "greeting"
    if _CODE_HINTS.search(m):
        return "code"
    if _MATH_HINTS.search(m):
        return "math"
    if _MULTI_STEP_HINTS.search(m):
        return "multi_step"
    if _FACTUAL_HINTS.search(m):
        return "factual"
    if _OPINION_HINTS.search(m):
        return "opinion"
    return "chitchat"


def plan_layers(
    message_class: str,
    layers_enabled: dict[str, bool],
    force_deliberate: list[str],
    reflex_only: list[str],
) -> list[str]:
    """Decide which layers to run, in order.

    Every plan starts with reflex. Subsequent layers depend on class and
    what's enabled. Short-circuit on confidence happens in pipeline.run().
    """
    plan = ["reflex"]

    if message_class in reflex_only:
        return plan  # reflex only, deliberate skipped unless confidence low

    if layers_enabled.get("deliberate"):
        plan.append("deliberate")

    if layers_enabled.get("critic"):
        plan.append("critic")

    if layers_enabled.get("recall") and message_class in ("factual", "code"):
        plan.append("recall")

    return plan


def forces_deliberate(message_class: str, force_list: list[str]) -> bool:
    return message_class in force_list

"""
Mine Claude Code conversation JSONL files for error->recovery patterns,
user correction patterns, and successful multi-step sequences.

Samples the 50 largest files from each directory (longer = more patterns).
"""

import json
import os
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

DIRS = [
    Path.home() / ".claude/projects/c--Users-jruss-Documents-GitHub-Hekate",
    Path.home() / ".claude/projects/C--Hekate-orchestration",
]

OUTPUT = Path(r"c:\Users\jruss\Documents\GitHub\Hekate\orchestration\backend\services\learning\conversation_patterns.json")

# Negative signal words for user corrections
NEGATIVE_SIGNALS = re.compile(
    r"\b(no[,.\s]|don'?t|wrong|not that|stop|instead|shouldn'?t|"
    r"that'?s not|wasn'?t|isn'?t|never|undo|revert|actually[,]|"
    r"wait[,.\s]|hold on|I said|I meant|not what I|you broke|"
    r"why did you|that was wrong|incorrect)\b",
    re.IGNORECASE,
)

CORRECTION_CATEGORIES = {
    "wrong_file": re.compile(r"wrong file|not that file|different file|other file|that file", re.I),
    "wrong_approach": re.compile(r"wrong approach|not how|different way|shouldn't have|don't.*that way|why did you|that's not what|not what i", re.I),
    "unnecessary_change": re.compile(r"unnecessary|didn't need|don't change|leave it|undo|revert|put it back|shouldn't have changed", re.I),
    "missing_context": re.compile(r"look at|check first|read.*first|you missed|didn't read|didn't check|should have read|need to read|there's no reason|we don't even have|why isn't", re.I),
    "wrong_tool": re.compile(r"use.*instead|try.*instead|wrong tool|don't use", re.I),
    "scope_creep": re.compile(r"only.*asked|just.*do|too much|scope|more than|i only|i just", re.I),
    "wrong_content": re.compile(r"wrong.*content|not.*correct|incorrect|typo|spelling", re.I),
    "misunderstanding": re.compile(r"i meant|i said|no,?\s+i|that's not|what i want|i was asking|i'm asking|i need", re.I),
    "premature_action": re.compile(r"wait|hold on|not yet|before that|first|stop|don't.*yet", re.I),
    "incomplete": re.compile(r"also need|what about|you forgot|missing|still need|isn't complete|not complete|not done", re.I),
}


def get_largest_files(directory, n=50, min_size=100_000):
    """Get n largest JSONL files above min_size."""
    if not directory.exists():
        return []
    files = []
    for f in directory.glob("*.jsonl"):
        sz = f.stat().st_size
        if sz >= min_size:
            files.append((sz, f))
    files.sort(reverse=True)
    return [f for _, f in files[:n]]


def parse_conversation(filepath):
    """Parse JSONL into ordered list of (type, data) messages."""
    messages = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = obj.get("type")
            if msg_type in ("user", "assistant"):
                messages.append(obj)
    return messages


def extract_tool_uses(assistant_msg):
    """Extract tool_use blocks from assistant message content."""
    tools = []
    content = assistant_msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return tools
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append({
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    return tools


def extract_tool_results(user_msg):
    """Extract tool_result blocks from user message content."""
    results = []
    content = user_msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return results
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            results.append({
                "tool_use_id": block.get("tool_use_id", ""),
                "is_error": block.get("is_error", False),
                "content": _extract_text(block.get("content", "")),
            })
    return results


def extract_user_text(user_msg):
    """Extract text content from user message."""
    content = user_msg.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
    return " ".join(texts)


def extract_assistant_text(assistant_msg):
    """Extract text content from assistant message."""
    content = assistant_msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return " ".join(texts)


def _extract_text(content):
    """Extract text from content which may be string, list, or other."""
    if isinstance(content, str):
        return content[:200]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)[:200]
    return str(content)[:200]


def classify_recovery(prev_tool_name, next_tools, error_content, next_assistant_msg=None):
    """Classify the recovery strategy after an error."""
    if not next_tools:
        # No tools in response -- check if assistant text suggests asking user
        if next_assistant_msg:
            text = extract_assistant_text(next_assistant_msg).lower()
            ask_signals = ["could you", "can you", "please ", "would you", "do you want", "shall i", "?"]
            if any(s in text for s in ask_signals):
                return "ask_user", None
        return "explain_and_continue", None

    next_tool = next_tools[0]
    next_name = next_tool["name"]

    # Check if it's a diagnostic tool first (reading/searching before retrying)
    diag_tools = {"Read", "Grep", "Glob"}
    if next_name in diag_tools and prev_tool_name not in diag_tools:
        return "diagnose_first", next_name

    # Bash after non-Bash error could be diagnostic (ls, cat, etc.) or retry
    if next_name == "Bash" and prev_tool_name != "Bash":
        inp = next_tool.get("input", {})
        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
        if any(c in cmd for c in ["ls ", "cat ", "head ", "tail ", "which ", "type ", "echo ", "pwd"]):
            return "diagnose_first", next_name
        return "fallback_tool", next_name

    if next_name == prev_tool_name:
        return "retry_modified", next_name

    return "fallback_tool", next_name


def categorize_correction(user_text):
    """Categorize a user correction."""
    for cat, pattern in CORRECTION_CATEGORIES.items():
        if pattern.search(user_text):
            return cat
    return "other"


def summarize_assistant_action(assistant_msg):
    """Brief summary of what the assistant did."""
    tools = extract_tool_uses(assistant_msg)
    text = extract_assistant_text(assistant_msg)
    if tools:
        tool_names = [t["name"] for t in tools]
        return f"Used tools: {', '.join(tool_names)}"
    if text:
        return text[:150]
    return "unknown action"


def analyze_conversation(messages):
    """Analyze a single conversation for patterns."""
    error_recoveries = []
    user_corrections = []
    tool_sequence = []
    has_errors = False
    has_corrections = False
    first_user_text = ""
    error_count = 0

    # Build a map of tool_use_id -> tool_name from assistant messages
    tool_id_to_name = {}

    for i, msg in enumerate(messages):
        if msg["type"] == "assistant":
            for tu in extract_tool_uses(msg):
                tool_id_to_name[tu["id"]] = tu["name"]
                tool_sequence.append(tu["name"])

    # Now scan for error->recovery patterns
    for i, msg in enumerate(messages):
        if msg["type"] == "user":
            # Capture first user text
            ut = extract_user_text(msg)
            if ut and not first_user_text:
                first_user_text = ut[:200]

            # Check for tool errors
            results = extract_tool_results(msg)
            for r in results:
                if r["is_error"]:
                    error_count += 1
                    has_errors = True
                    error_tool = tool_id_to_name.get(r["tool_use_id"], "unknown")
                    error_snippet = r["content"][:100]

                    # Find next assistant message
                    next_assistant = None
                    for j in range(i + 1, len(messages)):
                        if messages[j]["type"] == "assistant":
                            next_assistant = messages[j]
                            break

                    if next_assistant:
                        next_tools = extract_tool_uses(next_assistant)
                        strategy, next_tool = classify_recovery(
                            error_tool, next_tools, r["content"], next_assistant
                        )
                        # Check if the recovery succeeded (no error in next tool_result)
                        succeeded = None
                        for k in range(i + 2, min(i + 5, len(messages))):
                            if messages[k]["type"] == "user":
                                next_results = extract_tool_results(messages[k])
                                if next_results:
                                    succeeded = not any(nr["is_error"] for nr in next_results)
                                break

                        error_recoveries.append({
                            "tool": error_tool,
                            "error_snippet": error_snippet,
                            "recovery_strategy": strategy,
                            "next_tool": next_tool,
                            "succeeded": succeeded,
                        })

            # Check for user corrections (only if there's actual text, not just tool results)
            if ut and NEGATIVE_SIGNALS.search(ut):
                # Only count as correction if previous message was assistant
                if i > 0 and messages[i - 1]["type"] == "assistant":
                    has_corrections = True
                    prev_action = summarize_assistant_action(messages[i - 1])
                    category = categorize_correction(ut)
                    user_corrections.append({
                        "what_wrong": prev_action[:200],
                        "what_wanted": ut[:200],
                        "category": category,
                    })

    # Successful sequences: no errors, no corrections
    successful_seq = None
    if not has_errors and not has_corrections and tool_sequence and first_user_text:
        # Infer task type from first user message
        task_type = infer_task_type(first_user_text)
        # Deduplicate consecutive same tools
        deduped = []
        for t in tool_sequence:
            if not deduped or deduped[-1] != t:
                deduped.append(t)
        if len(deduped) <= 30:  # Skip absurdly long sequences
            successful_seq = {
                "task_type": task_type,
                "tool_sequence": deduped,
            }

    return error_recoveries, user_corrections, successful_seq, error_count


def infer_task_type(text):
    """Infer task type from user message."""
    text_lower = text.lower()
    if any(w in text_lower for w in ["fix", "bug", "error", "broken", "crash"]):
        return "bug_fix"
    if any(w in text_lower for w in ["add", "create", "implement", "new", "build"]):
        return "feature"
    if any(w in text_lower for w in ["refactor", "rename", "move", "restructure", "clean"]):
        return "refactor"
    if any(w in text_lower for w in ["test", "spec", "assert"]):
        return "testing"
    if any(w in text_lower for w in ["deploy", "release", "publish"]):
        return "deployment"
    if any(w in text_lower for w in ["look", "find", "search", "where", "what", "how", "check", "read", "show"]):
        return "investigation"
    if any(w in text_lower for w in ["update", "change", "modify", "edit"]):
        return "modification"
    if any(w in text_lower for w in ["doc", "comment", "readme"]):
        return "documentation"
    return "other"


def main():
    all_error_recoveries = []
    all_user_corrections = []
    all_successful_sequences = []
    total_conversations = 0
    total_errors = 0

    for d in DIRS:
        files = get_largest_files(d, n=50, min_size=50_000)
        print(f"Processing {len(files)} files from {d.name}", file=sys.stderr)

        for filepath in files:
            total_conversations += 1
            try:
                messages = parse_conversation(filepath)
            except Exception as e:
                print(f"  SKIP {filepath.name}: {e}", file=sys.stderr)
                continue

            if len(messages) < 3:
                continue

            err_rec, usr_cor, succ_seq, err_count = analyze_conversation(messages)
            all_error_recoveries.extend(err_rec)
            all_user_corrections.extend(usr_cor)
            if succ_seq:
                all_successful_sequences.append(succ_seq)
            total_errors += err_count

            if (total_conversations % 10) == 0:
                print(f"  Processed {total_conversations} conversations...", file=sys.stderr)

    # Compute statistics
    recovery_succeeded = [e for e in all_error_recoveries if e["succeeded"] is True]
    recovery_failed = [e for e in all_error_recoveries if e["succeeded"] is False]
    recovery_unknown = [e for e in all_error_recoveries if e["succeeded"] is None]

    strategy_counts = Counter(e["recovery_strategy"] for e in all_error_recoveries)
    correction_counts = Counter(c["category"] for c in all_user_corrections)
    error_tool_counts = Counter(e["tool"] for e in all_error_recoveries)
    task_type_counts = Counter(s["task_type"] for s in all_successful_sequences)

    # Strategy success rates
    strategy_success = {}
    for strat in strategy_counts:
        total = sum(1 for e in all_error_recoveries if e["recovery_strategy"] == strat)
        succeeded = sum(1 for e in all_error_recoveries if e["recovery_strategy"] == strat and e["succeeded"] is True)
        failed = sum(1 for e in all_error_recoveries if e["recovery_strategy"] == strat and e["succeeded"] is False)
        strategy_success[strat] = {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "unknown": total - succeeded - failed,
            "success_rate": round(succeeded / max(succeeded + failed, 1), 3),
        }

    result = {
        "error_recovery_patterns": all_error_recoveries,
        "user_corrections": all_user_corrections,
        "successful_sequences": all_successful_sequences,
        "statistics": {
            "total_conversations_analyzed": total_conversations,
            "total_errors": total_errors,
            "total_error_recovery_patterns": len(all_error_recoveries),
            "total_user_corrections": len(all_user_corrections),
            "total_successful_sequences": len(all_successful_sequences),
            "recovery_success_rate": round(
                len(recovery_succeeded) / max(len(recovery_succeeded) + len(recovery_failed), 1), 3
            ),
            "recovery_by_strategy": strategy_success,
            "errors_by_tool": dict(error_tool_counts.most_common(20)),
            "common_corrections": dict(correction_counts.most_common(10)),
            "successful_task_types": dict(task_type_counts.most_common(10)),
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Analyzed {total_conversations} conversations.", file=sys.stderr)
    print(f"  Error recovery patterns: {len(all_error_recoveries)}", file=sys.stderr)
    print(f"  User corrections: {len(all_user_corrections)}", file=sys.stderr)
    print(f"  Successful sequences: {len(all_successful_sequences)}", file=sys.stderr)
    print(f"  Output: {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

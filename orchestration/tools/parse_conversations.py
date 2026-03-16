import argparse
import glob
import os
import json
from collections import defaultdict

# Entry types that carry actual conversation messages
_MESSAGE_TYPES = {"user", "assistant"}

# Entry types to skip entirely (system noise, metadata, snapshots)
_SKIP_TYPES = {
    "queue-operation", "last-prompt", "file-history-snapshot",
    "system", "progress", "pr-link",
}

# Content block types that represent pure text output
_TEXT_BLOCK_TYPES = {"text"}


def find_conversation_files(claude_dir, project=None):
    """Finds Claude conversation (.jsonl) files."""
    base_path = os.path.expanduser(claude_dir)
    if project:
        search_path = os.path.join(base_path, "projects", project, "*.jsonl")
    else:
        search_path = os.path.join(base_path, "projects", "*", "*.jsonl")

    return glob.glob(search_path, recursive=True)


def _extract_text_content(content):
    """Extract only text from a message's content field.

    Returns the combined text string, or None if no text was found
    (e.g. the message was purely tool_result / tool_use blocks).
    """
    if isinstance(content, str):
        return content if content.strip() else None

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in _TEXT_BLOCK_TYPES:
                text = block.get("text", "")
                if text.strip():
                    texts.append(text)
            elif isinstance(block, str):
                if block.strip():
                    texts.append(block)
        return "\n".join(texts) if texts else None

    return None


def parse_jsonl_stream(filepath):
    """Stream-parse a single .jsonl file, yielding extracted messages.

    Yields dicts: {role, content, timestamp, session_id, cwd}
    Skips system events, queue operations, tool-only messages, and
    file-history-snapshot entries.
    """
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")

            # Fast reject non-message entry types
            if entry_type not in _MESSAGE_TYPES:
                continue

            message = entry.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            if role not in ("user", "assistant"):
                continue

            text = _extract_text_content(message.get("content", ""))
            if text is None:
                continue

            yield {
                "role": role,
                "content": text,
                "timestamp": entry.get("timestamp"),
                "session_id": entry.get("sessionId"),
                "cwd": entry.get("cwd"),
            }


def group_by_session(conversation_files):
    """Parse all files and group messages by session_id.

    Returns a dict: session_id -> {project, messages, timestamps}.
    """
    sessions = defaultdict(lambda: {"project": None, "messages": [], "timestamps": []})

    for filepath in conversation_files:
        # Derive project name from parent directory of the .jsonl file
        file_project = os.path.basename(os.path.dirname(filepath))

        for msg in parse_jsonl_stream(filepath):
            sid = msg["session_id"]
            if sid is None:
                continue

            session = sessions[sid]

            # Use cwd-derived project, fall back to file path parent
            if session["project"] is None:
                session["project"] = msg.get("cwd") or file_project

            if msg["timestamp"]:
                session["timestamps"].append(msg["timestamp"])

            session["messages"].append({
                "role": msg["role"],
                "content": msg["content"],
            })

    return sessions


def format_session(session_id, session_data):
    """Build the output structure for a single session."""
    ts = sorted(session_data["timestamps"]) if session_data["timestamps"] else []
    return {
        "session_id": session_id,
        "project": session_data["project"],
        "first_message": ts[0] if ts else None,
        "last_message": ts[-1] if ts else None,
        "message_count": len(session_data["messages"]),
        "messages": session_data["messages"],
    }


def write_sessions(sessions, output_dir):
    """Write each session as a JSON file to output_dir.

    Returns the number of files written.
    """
    os.makedirs(output_dir, exist_ok=True)
    count = 0
    for session_id, data in sessions.items():
        if not data["messages"]:
            continue
        output = format_session(session_id, data)
        filename = f"{session_id}.json"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        count += 1
    return count


def main():
    """Main function to parse Claude Code conversation histories."""
    parser = argparse.ArgumentParser(
        description="Parse and extract Claude Code conversation histories from .jsonl files."
    )
    parser.add_argument(
        "--project", type=str, help="The name of a specific project to parse."
    )
    parser.add_argument(
        "--output",
        type=str,
        default="./parsed_conversations",
        help="The directory to save the output JSON files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't actually write any files, just print the names of the files that would be processed.",
    )
    parser.add_argument(
        "--claude_dir", type=str, default="~/.claude", help=argparse.SUPPRESS
    )

    args = parser.parse_args()

    conversation_files = find_conversation_files(args.claude_dir, args.project)

    if not conversation_files:
        print("No conversation files found.")
        return

    if args.dry_run:
        print("--- Dry Run ---")
        print(f"Found {len(conversation_files)} conversation files to process:")
        for f in conversation_files:
            print(f"- {f}")
        print("--- End Dry Run ---")
        return

    print(f"Parsing {len(conversation_files)} files...")
    sessions = group_by_session(conversation_files)

    written = write_sessions(sessions, args.output)
    print(f"Wrote {written} session files to {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()

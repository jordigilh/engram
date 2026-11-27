#!/usr/bin/env python3
"""Generate the transcript serializer golden fixture (issue #245).

This is developer tooling, not a pytest test. It is a line-for-line port of the
pure helpers in `src/engram/flows/configured.py` and
`src/engram/correction_gate.py` (regex mode), so the Rust implementation can be
compared against the documented Python contract without importing CocoIndex.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

CORRECTION_PATTERNS = [
    re.compile(r"\bno[,.]?\s+that'?s\s+(not|wrong|incorrect)", re.I),
    re.compile(r"\bdon'?t\s+do\s+that", re.I),
    re.compile(r"\bI\s+(said|meant)\s+", re.I),
    re.compile(r"\bwrong\s+(file|path|dir|approach|method|function|model|endpoint)", re.I),
    re.compile(r"\bthat\s+broke", re.I),
    re.compile(r"\bundo\s+(that|this|it)", re.I),
    re.compile(r"\bthat'?s\s+not\s+what\s+I", re.I),
    re.compile(r"\byou\s+(shouldn'?t|should\s+not)\s+have", re.I),
    re.compile(r"\bdo\s+not\s+use\b", re.I),
    re.compile(r"\bwe\s+don'?t\s+use\b", re.I),
    re.compile(r"\b(you'?re|you\s+are)\s+(still\s+)?not\s+(following|aligned)\b", re.I),
    re.compile(r"\bnot\s+following\s+(the\s+)?(project'?s?\s+)?(methodology|convention|AGENTS\.md|CLAUDE\.md)\b", re.I),
    re.compile(r"\byou\s+keep\s+making\s+the\s+same\s+mistake\b", re.I),
    re.compile(r"\bmistak(?:e|ing)\b.{0,40}\bfor\b", re.I),
]
INSTRUCTION_PATTERNS = [
    re.compile(r"\balways\s+(use|follow|run|start\s+with|ensure)", re.I),
    re.compile(r"\bnever\s+(skip|push|commit|deploy|use)", re.I),
    re.compile(r"\bmandatory\b", re.I),
    re.compile(r"\bour\s+(workflow|process|methodology|convention|standard)", re.I),
    re.compile(r"\bthe\s+rule\s+is\b", re.I),
    re.compile(r"\bfor\s+this\s+(project|repo|team)\s+we\b", re.I),
    re.compile(r"\bwe\s+(always|never|require|must)\b", re.I),
    re.compile(r"\bbefore\s+(implementing|proceeding|starting\s+any)", re.I),
]
BOILERPLATE_PREFIXES = (
    "Briefly inform the user about the task result and perform any follow-up actions",
    "The beginning of the above subagent result is already visible to the user.",
)
BOILERPLATE_TAG_RE = re.compile(
    r"^\s*<(system_reminder|attached_files|system_notification|mcp_server_catalog|user_info)\b"
)


def is_system_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if any(stripped.startswith(p) for p in BOILERPLATE_PREFIXES):
        return True
    return bool(BOILERPLATE_TAG_RE.match(stripped))


def is_correction(text: str) -> bool:
    if not text or len(text) > 2000 or is_system_boilerplate(text):
        return False
    return any(pattern.search(text) for pattern in CORRECTION_PATTERNS)


def is_instruction(text: str) -> bool:
    if not text or len(text) < 20 or len(text) > 2000:
        return False
    return any(pattern.search(text) for pattern in INSTRUCTION_PATTERNS)


def transcript_user_text(message: dict) -> str:
    content = message.get("message", {}).get("content", [])
    if isinstance(content, str):
        raw = content
    else:
        texts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text", ""))
            if text.startswith("<external_links>"):
                continue
            match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
            texts.append(match.group(1) if match else text)
        raw = "\n".join(texts)
    return raw.strip() if not is_system_boilerplate(raw) else ""


def transcript_assistant_text(message: dict) -> str:
    content = message.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def learning_windows(messages: list[dict], start_index: int = 0, window: int = 2) -> list[dict]:
    parsed = []
    for raw_index, message in enumerate(messages):
        role = message.get("role", "")
        if role == "user":
            text = transcript_user_text(message)
            if text:
                parsed.append({"role": role, "text": text, "correction": is_correction(text),
                               "instruction": is_instruction(text), "raw_index": raw_index})
        elif role == "assistant":
            text = transcript_assistant_text(message)
            if text:
                parsed.append({"role": role, "text": text[:400], "correction": False,
                               "instruction": False, "raw_index": raw_index})
    windows = []
    for signal, item in enumerate(parsed):
        if not (item["correction"] or item["instruction"]) or item["raw_index"] < start_index:
            continue
        lines = []
        for index in range(max(0, signal - window), min(len(parsed), signal + window + 1)):
            context = parsed[index]
            marker = ""
            if index == signal:
                marker = "[CORRECTION] " if context["correction"] else "[INSTRUCTION] "
            lines.append(f"{marker}{context['role'].title()}: {context['text'][:300]}")
        text = "\n\n".join(lines)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        windows.append({
            "text": text,
            "document_id": f"transcript-session-w{digest}",
            "signal_raw_index": item["raw_index"],
        })
    return windows


def main() -> None:
    root = Path(__file__).resolve().parent
    lines = (root / "session.jsonl").read_text().splitlines()
    messages = []
    for line in lines:
        try:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    messages.append(value)
        except json.JSONDecodeError:
            continue
    payload = {
        "fixture": "session.jsonl",
        "message_count": len(messages),
        "cases": [
            {"start_index": start, "windows": learning_windows(messages, start)}
            for start in (0, 6, 9)
        ],
    }
    (root / "goldens.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

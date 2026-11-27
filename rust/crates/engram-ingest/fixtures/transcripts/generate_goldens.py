#!/usr/bin/env python3
"""Generate the transcript serializer golden fixture (issue #245).

This is developer tooling, not a pytest test. It is a line-for-line port of the
pure helpers in `src/engram/flows/configured.py` and
`src/engram/correction_gate.py` (regex mode), so the Rust implementation can be
compared against the documented Python contract without importing CocoIndex.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CONFIGURED = REPO_ROOT / "src" / "engram" / "flows" / "configured.py"
CORRECTION_GATE = REPO_ROOT / "src" / "engram" / "correction_gate.py"


def _compiled_patterns(path: Path, name: str) -> list[re.Pattern[str]]:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            patterns = []
            for element in node.value.elts:
                source = element.args[0].value
                patterns.append(re.compile(source, re.I))
            return patterns
    raise RuntimeError(f"{name} not found in {path}")


def _string_sequence(path: Path, name: str) -> tuple[str, ...]:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return tuple(element.value for element in node.value.elts)
    raise RuntimeError(f"{name} not found in {path}")


def _compiled_regex(path: Path, name: str) -> re.Pattern[str]:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return re.compile(node.value.args[0].value)
    raise RuntimeError(f"{name} not found in {path}")


CORRECTION_PATTERNS = _compiled_patterns(CONFIGURED, "CORRECTION_PATTERNS")
INSTRUCTION_PATTERNS = _compiled_patterns(CONFIGURED, "INSTRUCTION_PATTERNS")
BOILERPLATE_PREFIXES = _string_sequence(CORRECTION_GATE, "_BOILERPLATE_PREFIXES")
BOILERPLATE_TAG_RE = _compiled_regex(CORRECTION_GATE, "_BOILERPLATE_TAG_RE")


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

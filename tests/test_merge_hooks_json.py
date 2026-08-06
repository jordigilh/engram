"""Tests for hooks/_merge_hooks_json.py -- the JSON-merge logic behind
hooks/install.sh. The risky part isn't the hook scripts themselves, it's
making this idempotent and non-destructive to a target repo's *existing*
.cursor/hooks.json (e.g. kubernaut already has other hooks registered)."""
from __future__ import annotations

import json

import _merge_hooks_json as m

DETECTOR = "/Users/jgil/.hindsight/hooks/detect-plan-kickoff.sh"
ENFORCER = "/Users/jgil/.hindsight/venv/bin/python3 /Users/jgil/.hindsight/hooks/post-plan-hindsight-check.py"
REMINDER = "/Users/jgil/.hindsight/venv/bin/python3 /Users/jgil/.hindsight/hooks/post-plan-checklist-reminder.py"


class TestMerge:
    def test_empty_config_gets_both_hooks(self):
        result = m.merge({}, DETECTOR, ENFORCER)

        assert result["version"] == 1
        assert result["hooks"]["beforeSubmitPrompt"] == [{"command": DETECTOR, "timeout": m.DETECTOR_TIMEOUT_S}]
        assert result["hooks"]["preToolUse"] == [
            {"matcher": m.MATCHER, "command": ENFORCER, "timeout": m.ENFORCER_TIMEOUT_S}
        ]

    def test_preserves_unrelated_existing_hooks(self):
        existing = {
            "version": 1,
            "hooks": {
                "afterMCPExecution": [{"command": "/some/other/hook.sh", "timeout": 5}],
            },
        }

        result = m.merge(existing, DETECTOR, ENFORCER)

        assert result["hooks"]["afterMCPExecution"] == [{"command": "/some/other/hook.sh", "timeout": 5}]
        assert len(result["hooks"]["beforeSubmitPrompt"]) == 1
        assert len(result["hooks"]["preToolUse"]) == 1

    def test_preserves_existing_entries_in_same_event(self):
        existing = {
            "version": 1,
            "hooks": {
                "preToolUse": [{"matcher": "SomeOtherTool", "command": "/some/other/enforcer.py", "timeout": 10}],
            },
        }

        result = m.merge(existing, DETECTOR, ENFORCER)

        pre_tool = result["hooks"]["preToolUse"]
        assert len(pre_tool) == 2
        assert {"matcher": "SomeOtherTool", "command": "/some/other/enforcer.py", "timeout": 10} in pre_tool
        assert {"matcher": m.MATCHER, "command": ENFORCER, "timeout": m.ENFORCER_TIMEOUT_S} in pre_tool

    def test_is_idempotent_on_repeat_merge(self):
        result = m.merge({}, DETECTOR, ENFORCER)
        result = m.merge(result, DETECTOR, ENFORCER)
        result = m.merge(result, DETECTOR, ENFORCER)

        assert len(result["hooks"]["beforeSubmitPrompt"]) == 1
        assert len(result["hooks"]["preToolUse"]) == 1

    def test_different_enforcer_command_is_added_alongside_not_replacing(self):
        """Different Python interpreter path (e.g. re-run after moving the
        venv) should add a new entry rather than silently vanish -- stale
        entries are a separate cleanup concern, not this function's job."""
        result = m.merge({}, DETECTOR, ENFORCER)
        result = m.merge(result, DETECTOR, "/different/python3 /different/path.py")

        assert len(result["hooks"]["preToolUse"]) == 2

    def test_reminder_cmd_adds_posttooluse_hook(self):
        result = m.merge({}, DETECTOR, ENFORCER, REMINDER)

        assert result["hooks"]["postToolUse"] == [
            {"matcher": m.MATCHER, "command": REMINDER, "timeout": m.REMINDER_TIMEOUT_S}
        ]

    def test_no_reminder_cmd_omits_posttooluse_entirely(self):
        """The kubernaut case: install.sh passes no reminder command at
        all, and hooks.json must not gain a postToolUse key just for this
        feature -- not even an empty list."""
        result = m.merge({}, DETECTOR, ENFORCER, None)

        assert "postToolUse" not in result["hooks"]

    def test_reminder_merge_is_idempotent(self):
        result = m.merge({}, DETECTOR, ENFORCER, REMINDER)
        result = m.merge(result, DETECTOR, ENFORCER, REMINDER)

        assert len(result["hooks"]["postToolUse"]) == 1

    def test_reminder_preserves_existing_posttooluse_entries(self):
        existing = {
            "version": 1,
            "hooks": {"postToolUse": [{"matcher": "TodoWrite", "command": "/some/other.sh", "timeout": 5}]},
        }

        result = m.merge(existing, DETECTOR, ENFORCER, REMINDER)

        post = result["hooks"]["postToolUse"]
        assert len(post) == 2
        assert {"matcher": "TodoWrite", "command": "/some/other.sh", "timeout": 5} in post


class TestMainCli:
    def test_writes_merged_config_to_file(self, tmp_path):
        import sys

        hooks_path = tmp_path / "hooks.json"
        argv = ["prog", str(hooks_path), DETECTOR, ENFORCER]
        old_argv = sys.argv
        sys.argv = argv
        try:
            assert m.main() == 0
        finally:
            sys.argv = old_argv

        data = json.loads(hooks_path.read_text())
        assert data["hooks"]["beforeSubmitPrompt"][0]["command"] == DETECTOR

    def test_invalid_existing_json_refuses_to_overwrite(self, tmp_path, capsys):
        import sys

        hooks_path = tmp_path / "hooks.json"
        hooks_path.write_text("{not valid json")
        original_content = hooks_path.read_text()

        sys.argv = ["prog", str(hooks_path), DETECTOR, ENFORCER]
        assert m.main() == 1
        assert hooks_path.read_text() == original_content
        assert "not valid JSON" in capsys.readouterr().err

    def test_wrong_arg_count_returns_error(self):
        import sys

        sys.argv = ["prog", "only-one-arg"]
        assert m.main() == 1

    def test_four_args_with_reminder_writes_all_three_hooks(self, tmp_path):
        import sys

        hooks_path = tmp_path / "hooks.json"
        sys.argv = ["prog", str(hooks_path), DETECTOR, ENFORCER, REMINDER]
        assert m.main() == 0

        data = json.loads(hooks_path.read_text())
        assert data["hooks"]["postToolUse"][0]["command"] == REMINDER

    def test_four_args_with_empty_reminder_omits_posttooluse(self, tmp_path):
        """Mirrors how install.sh invokes this for kubernaut repos: it still
        passes 4 positional args, but the 4th is an empty string."""
        import sys

        hooks_path = tmp_path / "hooks.json"
        sys.argv = ["prog", str(hooks_path), DETECTOR, ENFORCER, ""]
        assert m.main() == 0

        data = json.loads(hooks_path.read_text())
        assert "postToolUse" not in data["hooks"]

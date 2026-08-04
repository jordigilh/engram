"""Tests for hooks/_hindsight_check_worker.py -- the child process
post-plan-hindsight-check.py runs under a hard subprocess timeout to
isolate the real recall()+resolve() network/LLM call. See
docs/findings/2026-08.md for why this needed its own credentials-fixup
logic (config.env's GOOGLE_APPLICATION_CREDENTIALS points at a
container-only path)."""
from __future__ import annotations

import json
from io import StringIO

import _hindsight_check_worker as worker


class TestLoadConfigEnv:
    def test_loads_key_value_pairs(self, monkeypatch, tmp_path):
        (tmp_path / ".hindsight").mkdir()
        config = tmp_path / ".hindsight" / "config.env"
        config.write_text("FOO=bar\nBAZ=qux\n")
        monkeypatch.setattr(worker.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        worker.load_config_env()

        import os
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_does_not_overwrite_existing_env(self, monkeypatch, tmp_path):
        (tmp_path / ".hindsight").mkdir()
        config = tmp_path / ".hindsight" / "config.env"
        config.write_text("FOO=from_file\n")
        monkeypatch.setattr(worker.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("FOO", "already_set")

        worker.load_config_env()

        import os
        assert os.environ["FOO"] == "already_set"

    def test_skips_comments_and_blank_lines(self, monkeypatch, tmp_path):
        (tmp_path / ".hindsight").mkdir()
        config = tmp_path / ".hindsight" / "config.env"
        config.write_text("# a comment\n\nGOOD=value\n")
        monkeypatch.setattr(worker.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("GOOD", raising=False)

        worker.load_config_env()

        import os
        assert os.environ["GOOD"] == "value"

    def test_missing_file_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(worker.Path, "home", staticmethod(lambda: tmp_path))
        worker.load_config_env()  # should not raise


class TestFixCredentialsPath:
    def test_overrides_when_configured_path_missing_and_local_adc_exists(self, monkeypatch, tmp_path):
        local_adc = tmp_path / "adc.json"
        local_adc.write_text("{}")
        monkeypatch.setattr("os.path.expanduser", lambda p: str(local_adc) if "gcloud" in p else p)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/keys/adc.json")  # container-only, doesn't exist here

        worker.fix_credentials_path()

        import os
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(local_adc)

    def test_leaves_configured_path_untouched_when_it_exists(self, monkeypatch, tmp_path):
        real_creds = tmp_path / "real_creds.json"
        real_creds.write_text("{}")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(real_creds))

        worker.fix_credentials_path()

        import os
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(real_creds)

    def test_noop_when_neither_configured_nor_local_adc_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "nonexistent-adc.json") if "gcloud" in p else p)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/keys/adc.json")

        worker.fix_credentials_path()

        import os
        # Left as-is: no local fallback available, worker will fail downstream
        # (caught by main()'s try/except -> action="retain", fails open).
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/tmp/keys/adc.json"


class TestMain:
    def test_empty_overview_returns_retain_without_calling_resolve(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"overview": "", "project": "kubernaut"})))
        assert worker.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "retain"

    def test_calls_resolve_and_prints_its_result(self, monkeypatch, capsys):
        from types import SimpleNamespace

        monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"overview": "Do the thing.", "project": "kubernaut"})))
        monkeypatch.setattr(worker, "load_config_env", lambda: None)
        monkeypatch.setattr(worker, "fix_credentials_path", lambda: None)

        fake_module = SimpleNamespace(
            resolve=lambda bank_id, statement, project=None: SimpleNamespace(
                action="auto_resolved", confidence=0.95, explanation="conflicts with X",
            )
        )
        import sys
        monkeypatch.setitem(sys.modules, "contradiction_resolution", fake_module)

        assert worker.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"action": "auto_resolved", "confidence": 0.95, "explanation": "conflicts with X"}

    def test_internal_exception_degrades_to_retain(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"overview": "Do the thing.", "project": None})))

        def boom():
            raise RuntimeError("credentials broken")

        monkeypatch.setattr(worker, "load_config_env", boom)

        assert worker.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "retain"
        assert "worker error" in out["explanation"]

    def test_malformed_stdin_degrades_to_retain(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", StringIO("not json"))
        assert worker.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "retain"

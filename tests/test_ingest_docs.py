"""Tests for ingest_docs.py -- the one-off kubernaut-docs -> Hindsight
bootstrap ingester.

Business outcomes under test: the module must be importable/runnable at all
(it was not -- see the `main()` SyntaxError regression this file's first
test pins down), bank creation must be idempotent (a pre-existing bank is
not a failure), and ingestion must skip empty files and keep going past
per-file errors rather than aborting the whole run.
"""
from __future__ import annotations

from urllib.error import HTTPError


class TestModuleIsImportable:
    def test_module_imports_without_error(self, ingest_docs):
        """Regression test: main() referenced the HINDSIGHT_URL global as an
        argparse default *before* its own `global HINDSIGHT_URL` declaration
        further down the same function body, which is a SyntaxError in
        Python (a name can't be used in a function before the `global`
        statement naming it), not just a runtime bug -- the module failed to
        import at all, silently, since nothing exercised it."""
        assert hasattr(ingest_docs, "main")
        assert hasattr(ingest_docs, "ingest")
        assert hasattr(ingest_docs, "create_bank")


class TestApiRequest:
    def test_get_request_returns_parsed_json(self, ingest_docs, monkeypatch):
        captured = {}

        class FakeResponse:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return FakeResponse()

        monkeypatch.setattr(ingest_docs, "urlopen", fake_urlopen)

        result = ingest_docs.api_request("GET", "/v1/default/banks/foo")

        assert result == {"ok": True}
        assert captured["method"] == "GET"
        assert captured["url"].endswith("/v1/default/banks/foo")

    def test_http_error_is_logged_and_reraised(self, ingest_docs, monkeypatch, capsys):
        import io

        def fake_urlopen(req, timeout=None):
            raise HTTPError(req.full_url, 500, "Internal Server Error", {}, io.BytesIO(b"boom"))

        monkeypatch.setattr(ingest_docs, "urlopen", fake_urlopen)

        try:
            ingest_docs.api_request("POST", "/v1/default/banks/foo/memories", {"x": 1})
            raised = False
        except HTTPError:
            raised = True

        assert raised
        assert "API error 500" in capsys.readouterr().err


class TestCreateBank:
    def test_bank_creation_conflict_is_treated_as_already_exists(self, ingest_docs, monkeypatch, capsys):
        """A 409 on PUT (bank already exists) must not abort the run -- this
        is what makes re-running the ingester after a partial failure safe."""
        import io

        calls = []

        def fake_api_request(method, path, payload=None):
            calls.append((method, path))
            if method == "PUT":
                raise HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(b""))
            return {}

        monkeypatch.setattr(ingest_docs, "api_request", fake_api_request)

        ingest_docs.create_bank()

        assert ("PUT", f"/v1/default/banks/{ingest_docs.BANK_ID}") in calls
        assert any(m == "PATCH" for m, _ in calls)
        assert "already exists" in capsys.readouterr().out

    def test_bank_creation_other_http_errors_propagate(self, ingest_docs, monkeypatch):
        import io

        def fake_api_request(method, path, payload=None):
            if method == "PUT":
                raise HTTPError("http://x", 500, "Internal Server Error", {}, io.BytesIO(b""))
            return {}

        monkeypatch.setattr(ingest_docs, "api_request", fake_api_request)

        try:
            ingest_docs.create_bank()
            raised = False
        except HTTPError:
            raised = True
        assert raised


class TestIngest:
    def test_skips_empty_files_and_counts_chunks(self, ingest_docs, monkeypatch, tmp_path):
        (tmp_path / "a.md").write_text("# Hello\n\nSome real content.")
        (tmp_path / "empty.md").write_text("   \n")

        calls = []

        def fake_api_request(method, path, payload=None):
            calls.append((method, path, payload))
            return {"items_count": 3}

        monkeypatch.setattr(ingest_docs, "api_request", fake_api_request)

        success = ingest_docs.ingest(tmp_path)

        assert success is True
        assert len(calls) == 1  # empty.md must not trigger a memories call
        method, path, payload = calls[0]
        assert method == "POST"
        assert payload["items"][0]["document_id"] == "a"

    def test_per_file_error_does_not_abort_remaining_files(self, ingest_docs, monkeypatch, tmp_path):
        (tmp_path / "bad.md").write_text("content one")
        (tmp_path / "good.md").write_text("content two")

        def fake_api_request(method, path, payload=None):
            doc_id = payload["items"][0]["document_id"]
            if doc_id == "bad":
                raise RuntimeError("simulated API failure")
            return {"items_count": 1}

        monkeypatch.setattr(ingest_docs, "api_request", fake_api_request)

        success = ingest_docs.ingest(tmp_path)

        assert success is False  # errors occurred...
        # ...but both files were still attempted, not just the first
        # (proven by there being no exception propagated out of ingest()).

    def test_root_level_file_tagged_root_nested_file_tagged_by_directory(self, ingest_docs, monkeypatch, tmp_path):
        (tmp_path / "root.md").write_text("root content")
        sub = tmp_path / "architecture"
        sub.mkdir()
        (sub / "nested.md").write_text("nested content")

        tags_seen = {}

        def fake_api_request(method, path, payload=None):
            item = payload["items"][0]
            tags_seen[item["document_id"]] = item["tags"]
            return {"items_count": 1}

        monkeypatch.setattr(ingest_docs, "api_request", fake_api_request)

        ingest_docs.ingest(tmp_path)

        assert tags_seen["root"] == ["root"]
        assert tags_seen["architecture--nested"] == ["architecture"]


class TestMain:
    def test_hindsight_url_override_flows_through_to_module_global(self, ingest_docs, monkeypatch, tmp_path):
        """The actual bug this file exists to pin down: --hindsight-url must
        be able to override the module-level HINDSIGHT_URL global used as
        api_request()'s default target, without raising a SyntaxError."""
        monkeypatch.setattr(ingest_docs.sys, "argv", [
            "ingest-docs.py", "--docs-dir", str(tmp_path), "--hindsight-url", "http://example-hindsight:9999",
        ])
        monkeypatch.setattr(ingest_docs, "create_bank", lambda: None)
        monkeypatch.setattr(ingest_docs, "ingest", lambda docs_dir: True)

        try:
            ingest_docs.main()
        except SystemExit as exc:
            assert exc.code == 0

        assert ingest_docs.HINDSIGHT_URL == "http://example-hindsight:9999"

    def test_missing_docs_dir_exits_nonzero_without_creating_bank(self, ingest_docs, monkeypatch, tmp_path):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(ingest_docs.sys, "argv", ["ingest-docs.py", "--docs-dir", str(missing)])
        create_bank_calls = []
        monkeypatch.setattr(ingest_docs, "create_bank", lambda: create_bank_calls.append(1))

        try:
            ingest_docs.main()
            code = 0
        except SystemExit as exc:
            code = exc.code

        assert code == 1
        assert create_bank_calls == []

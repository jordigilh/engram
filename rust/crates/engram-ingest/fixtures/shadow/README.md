# Shadow fixture

`tree/` is a frozen Markdown corpus. `expected.jsonl` is the canonical operation
list produced by the **real Python flow**:

- CocoIndex `localfs.walk_dir(..., PatternFilePathMatcher(["**/*.md"], []))`
  supplies the file set and ordering;
- `configured._retain_document` produces the operations, with
  `hindsight_retain` replaced by a capture function;
- timestamps are `null` because git does not preserve mtimes.

Regenerate with the deployed flow interpreter:

```bash
PYTHONPATH=src ~/.engram/venv/bin/python \
    rust/crates/engram-ingest/fixtures/shadow/generate_expected.py
```

`expected.meta.json` records the Python/CocoIndex versions, source commit, tree,
source tag, and operation count. Rust `tests/shadow.rs` compares the library and
the `engram-ingest-shadow` binary byte-for-byte against `expected.jsonl`.

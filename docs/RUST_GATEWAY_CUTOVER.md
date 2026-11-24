# Rust gateway cutover and rollback (issue #224)

Target: replace the Python `engram-gateway` on `127.0.0.1:8898` with
`engram-mcp`, keeping Python installed and runnable for rollback.

## Preconditions

- `docs/RUST_GATEWAY_SHADOW_RUN.md` shows 22/22 catalog matches and
  byte-identical representative calls.
- Rust suite, clippy, fmt, and MSRV 1.88 are green on `main`.
- Backup the current plist and binary:
  `cp ~/Library/LaunchAgents/io.vectorize.engram-gateway-native.plist ~/Library/LaunchAgents/io.vectorize.engram-gateway-native.plist.bak-$(date +%Y%m%d-%H%M%S)`

## Install (no traffic change)

```sh
mkdir -p ~/.engram/bin
cargo build --release -p engram-mcp --locked
install -m 0755 rust/target/release/engram-mcp ~/.engram/bin/engram-mcp
sed "s#__HOME__#$HOME#g" launchd/io.vectorize.engram-mcp.plist \
  > ~/Library/LaunchAgents/io.vectorize.engram-mcp.plist
plutil -lint ~/Library/LaunchAgents/io.vectorize.engram-mcp.plist
```

Do not bootstrap the new agent while the Python one holds `:8898`.

## Switch

```sh
launchctl bootout gui/$(id -u)/io.vectorize.engram-gateway-native
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.vectorize.engram-mcp.plist
launchctl print gui/$(id -u)/io.vectorize.engram-mcp | head -20
```

`KeepAlive` restarts the Rust gateway on failure. `SuccessfulExit` is false so
a clean stop stays stopped.

## Verify

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8898/mcp/engram
# POST tools/list for every project; expect the counts in
# docs/RUST_GATEWAY_SHADOW_RUN.md
tail -n 5 ~/.engram/logs/gateway-calls.jsonl
tail -n 20 ~/.engram/logs/engram-mcp-stderr.log
```

Check one `docs_recall` and one `zvec_grep_search` call through the client
before declaring success.

## Rollback

```sh
launchctl bootout gui/$(id -u)/io.vectorize.engram-mcp
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.vectorize.engram-gateway-native.plist
launchctl print gui/$(id -u)/io.vectorize.engram-gateway-native | head -10
```

Python reads the same `native-instances.toml`; no config change is needed.
The OAuth-free local routes recover without re-authentication.

## Soak checks

- `gateway-calls.jsonl` keeps growing with `project`/`backend`/`tool` rows.
- No repeated `mcp connect failed` or `stdio initialize failed` in the Rust
  stderr log.
- One full session per family (kubernaut, praxis, dcm, engram) exercises
  recall, code search, and Serena.
- Keep both plists installed for at least one week.

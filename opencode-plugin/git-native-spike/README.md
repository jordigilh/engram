# Native Git metadata spike

This is a proof of concept for replacing the OpenCode plugin's `execFile("git", ...)`
calls with an in-process Rust implementation.

- [`gix`](https://crates.io/crates/gix) reads repository state directly.
- [`napi-rs`](https://napi.rs/) exposes that implementation to the Bun/Node-API
  plugin without introducing a helper executable.
- No production plugin code imports this package yet.

## Local probe

From this directory:

```sh
cargo check
bun run build
bun run probe -- "$PWD/../.."
```

The probe reports whether the path is a non-bare Git worktree, its branch,
`HEAD` object ID, and configured fetch URLs. `gix::discover` searches parent
directories, matching the current `git -C <path> rev-parse` behavior.

## Integration gate

Before replacing the current adapter, compare this result with canonical Git on
fixtures covering:

1. the main checkout;
2. a linked `git worktree` whose `.git` is a file;
3. detached `HEAD`;
4. an unborn repository;
5. a non-Git aggregate directory;
6. a bare repository;
7. SHA-256 and unusual remote/config layouts where available.

The production package would need `napi-rs` platform artifacts for macOS
(`arm64`, `x64`), Linux (`x64`, `arm64`), and Windows (`x64`, `arm64`), plus a
Bun runtime smoke test. Until that matrix is green, the existing Git adapter
remains the safe fallback.

The local ARM64 spike was validated against the current checkout, a linked
worktree (both attached and detached), an unborn repository, a bare repository,
a non-Git directory, and a SHA-256 repository. The native results matched the
corresponding canonical Git probes for those cases.

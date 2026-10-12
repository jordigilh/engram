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

The repository CI workflow `.github/workflows/native-git-spike.yml` builds and
smoke-tests native artifacts on macOS ARM64/x64, Linux x64, and Windows x64,
and performs cross-target Rust checks for Linux ARM64 and Windows ARM64. It
also runs the Bun probe and Rust unit tests on every host lane. Publishing and
production-plugin wiring remain intentionally deferred until the ARM64 cross
targets have a real linker/package lane; the existing Git CLI adapter remains
the safe fallback until then.

The local ARM64 spike was validated against the current checkout, a linked
worktree (both attached and detached), an unborn repository, a bare repository,
a non-Git directory, and a SHA-256 repository. The native results matched the
corresponding canonical Git probes for those cases.

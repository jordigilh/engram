use std::path::PathBuf;

use engram_ingest::shadow::{plan_tree, to_jsonl, ShadowOptions};

fn main() {
    if let Err(error) = run() {
        eprintln!("engram-ingest-shadow: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut root: Option<PathBuf> = None;
    let mut source_tag: Option<String> = None;
    let mut out: Option<PathBuf> = None;
    let mut timestamps = true;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--root" => root = Some(PathBuf::from(args.next().ok_or("--root requires a value")?)),
            "--source-tag" => {
                source_tag = Some(args.next().ok_or("--source-tag requires a value")?)
            }
            "--out" => out = Some(PathBuf::from(args.next().ok_or("--out requires a value")?)),
            "--no-timestamps" => timestamps = false,
            "-h" | "--help" => {
                eprintln!(
                    "usage: engram-ingest-shadow --root <dir> --source-tag <tag> [--out <file>] [--no-timestamps]"
                );
                return Ok(());
            }
            other => return Err(format!("unknown argument {other}").into()),
        }
    }
    let root = root.ok_or("--root is required")?;
    let source_tag = source_tag.ok_or("--source-tag is required")?;
    let operations = plan_tree(
        &root,
        &ShadowOptions {
            source_tag,
            timestamps,
        },
    )?;
    let jsonl = to_jsonl(&operations)?;
    match out {
        Some(path) => std::fs::write(path, jsonl)?,
        None => print!("{jsonl}"),
    }
    Ok(())
}

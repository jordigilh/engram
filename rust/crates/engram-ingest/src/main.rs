//! `engram-ingest` process entrypoint (issue #242).

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use clap::{Parser, Subcommand};
use engram_ingest::config::RuntimeConfig;
use engram_ingest::runtime::{Runtime, RuntimeError, DEFAULT_DRAIN_TIMEOUT};
use engram_ingest::signals;
use engram_ingest::worker::Shutdown;

#[derive(Debug, Parser)]
#[command(
    name = "engram-ingest",
    version,
    about = "Native Engram docs ingestion runtime (scan, reconcile, worker supervisor)"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Run the resident docs ingestion loop.
    Run(RunArgs),
}

#[derive(Debug, Parser)]
struct RunArgs {
    /// Path to projects.toml. Defaults to ENGRAM_PROJECTS_CONFIG or
    /// ~/.engram/projects.toml.
    #[arg(long)]
    config: Option<PathBuf>,
    /// Project to ingest. Repeat for multiple projects.
    #[arg(long = "project", required = true)]
    projects: Vec<String>,
    /// Scan once, drain ready jobs, then exit.
    #[arg(long)]
    once: bool,
    /// Seconds between bounded reconciliation scans.
    #[arg(long)]
    scan_interval: Option<u64>,
    /// Worker slots per bank (1..=8).
    #[arg(long)]
    concurrency: Option<usize>,
    /// Maximum seconds to wait for the one-shot drain.
    #[arg(long, default_value_t = DEFAULT_DRAIN_TIMEOUT.as_secs())]
    drain_timeout: u64,
    /// Allow an empty existing root to delete every active document.
    #[arg(long)]
    allow_empty_root: bool,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("engram-ingest: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    match cli.command {
        Command::Run(args) => {
            let mut config = RuntimeConfig::load(args.config.as_deref(), &args.projects)?;
            if let Some(seconds) = args.scan_interval {
                config = config.with_scan_interval(Duration::from_secs(seconds))?;
            }
            if let Some(concurrency) = args.concurrency {
                config = config.with_concurrency(concurrency)?;
            }
            if args.allow_empty_root {
                config = config.with_allow_empty_root(true);
            }

            let runtime = Runtime::new(config)?;
            let shutdown = Shutdown::default();
            let _signals = signals::install(shutdown.clone())?;
            let handle = match runtime.start(shutdown) {
                Ok(handle) => handle,
                Err(RuntimeError::Cancelled) => return Ok(()),
                Err(error) => return Err(error.into()),
            };
            for outcome in handle.prepared().outcomes.iter() {
                eprintln!("engram-ingest: startup {}", outcome.label());
            }
            eprintln!(
                "engram-ingest: serving {} bank(s) from {} source(s)",
                handle.bank_ids().len(),
                handle.prepared().outcomes.len()
            );
            let incomplete: Vec<String> = handle
                .prepared()
                .outcomes
                .iter()
                .filter_map(|outcome| match outcome {
                    engram_ingest::runtime::SourceOutcome::Guarded { project, tag, .. } => {
                        Some(format!("{project}/{tag} (guarded)"))
                    }
                    engram_ingest::runtime::SourceOutcome::Skipped {
                        project,
                        tag,
                        reason,
                        ..
                    } => Some(format!("{project}/{tag} (skipped: {reason})")),
                    engram_ingest::runtime::SourceOutcome::Deferred { project, tag, .. } => {
                        Some(format!("{project}/{tag} (deferred)"))
                    }
                    engram_ingest::runtime::SourceOutcome::Transcripts {
                        project,
                        tag,
                        files_skipped,
                        discovery_incomplete,
                        ..
                    } if *files_skipped > 0 || *discovery_incomplete => {
                        let mut reasons = Vec::new();
                        if *files_skipped > 0 {
                            reasons.push(format!("{files_skipped} transcript file(s) skipped"));
                        }
                        if *discovery_incomplete {
                            reasons.push("transcript discovery incomplete".to_string());
                        }
                        Some(format!("{project}/{tag} ({})", reasons.join("; ")))
                    }
                    _ => None,
                })
                .collect();

            if args.once {
                handle.drain(Duration::from_secs(args.drain_timeout))?;
                if !incomplete.is_empty() {
                    return Err(std::io::Error::other(format!(
                        "{} source(s) were not fully applied ({}); fix the root or \
                         configuration, or pass --allow-empty-root if an empty root is a \
                         genuine deletion",
                        incomplete.len(),
                        incomplete.join(", ")
                    ))
                    .into());
                }
            } else {
                handle.run()?;
            }
            Ok(())
        }
    }
}

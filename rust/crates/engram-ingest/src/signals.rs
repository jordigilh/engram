//! SIGINT/SIGTERM handling for the resident runtime (issue #242).

use std::io;
use std::thread::{self, JoinHandle};

use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;

use crate::worker::Shutdown;

#[derive(Debug, thiserror::Error)]
pub enum SignalError {
    #[error("cannot register shutdown signals: {0}")]
    Register(#[from] io::Error),
    #[error("cannot start signal thread: {0}")]
    Thread(io::Error),
}

/// Keeps the signal thread alive for the process lifetime. Dropping the guard
/// does not join: the thread is blocked in `Signals::forever` and the process
/// is already exiting when the guard is dropped.
pub struct SignalGuard {
    _handle: JoinHandle<()>,
}

/// Cancel `shutdown` on the first SIGINT or SIGTERM.
pub fn install(shutdown: Shutdown) -> Result<SignalGuard, SignalError> {
    let mut signals = Signals::new([SIGINT, SIGTERM])?;
    let handle = thread::Builder::new()
        .name("engram-ingest-signals".to_string())
        .spawn(move || {
            if signals.forever().next().is_some() {
                shutdown.cancel();
            }
        })
        .map_err(SignalError::Thread)?;
    Ok(SignalGuard { _handle: handle })
}

//! Job lease and retry state machine for the ingestion ledger.
//!
//! The Python flow retries a retain three times (1s, 2s) and then swallows the
//! failure. The native ledger keeps the same retry budget but records a dead
//! job instead of silently advancing, and leases prevent two workers from
//! processing the same job.

use std::sync::atomic::{AtomicU64, Ordering};

pub const MAX_ATTEMPTS: u32 = 3;
pub const RETRY_BASE_MS: i64 = 1_000;

static TOKEN_FALLBACK: AtomicU64 = AtomicU64::new(0);

/// Opaque per-claim token. A reused owner string must not be able to complete
/// or extend a lease created by an earlier claim.
pub fn new_claim_token() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::getrandom(&mut bytes).is_ok() {
        return bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let counter = TOKEN_FALLBACK.fetch_add(1, Ordering::Relaxed);
    format!("{nanos:032x}{counter:016x}")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    Pending,
    Leased,
    Succeeded,
    Dead,
}

impl JobState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Leased => "leased",
            Self::Succeeded => "succeeded",
            Self::Dead => "dead",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "pending" => Some(Self::Pending),
            "leased" => Some(Self::Leased),
            "succeeded" => Some(Self::Succeeded),
            "dead" => Some(Self::Dead),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lease {
    pub owner: String,
    pub token: String,
    pub expires_at_ms: i64,
    pub heartbeat_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Job {
    pub id: i64,
    pub state: JobState,
    pub attempts: u32,
    pub max_attempts: u32,
    pub run_after_ms: i64,
    pub lease: Option<Lease>,
    pub last_error: Option<String>,
    pub last_error_class: Option<String>,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum JobError {
    #[error("job is not pending")]
    NotPending,
    #[error("job is not leased")]
    NotLeased,
    #[error("job is leased by another owner")]
    LeaseHeld,
    #[error("job lease has expired")]
    LeaseExpired,
    #[error("job lease token does not match the current claim")]
    TokenMismatch,
    #[error("job is dead and cannot be retried")]
    Dead,
}

impl Job {
    pub fn pending(id: i64) -> Self {
        Self {
            id,
            state: JobState::Pending,
            attempts: 0,
            max_attempts: MAX_ATTEMPTS,
            run_after_ms: 0,
            lease: None,
            last_error: None,
            last_error_class: None,
        }
    }

    pub fn claim(&mut self, owner: &str, now_ms: i64, ttl_ms: i64) -> Result<Lease, JobError> {
        match self.state {
            JobState::Dead => return Err(JobError::Dead),
            JobState::Pending if now_ms >= self.run_after_ms => {}
            JobState::Pending => return Err(JobError::NotPending),
            JobState::Leased => return Err(JobError::LeaseHeld),
            JobState::Succeeded => return Err(JobError::NotPending),
        }
        let lease = Lease {
            owner: owner.to_string(),
            token: new_claim_token(),
            expires_at_ms: now_ms + ttl_ms,
            heartbeat_at_ms: now_ms,
        };
        self.state = JobState::Leased;
        self.lease = Some(lease.clone());
        Ok(lease)
    }

    pub fn heartbeat(&mut self, token: &str, now_ms: i64, ttl_ms: i64) -> Result<Lease, JobError> {
        let lease = self.active_lease(token, now_ms)?;
        let renewed = Lease {
            owner: lease.owner.clone(),
            token: lease.token.clone(),
            expires_at_ms: now_ms + ttl_ms,
            heartbeat_at_ms: now_ms,
        };
        self.lease = Some(renewed.clone());
        Ok(renewed)
    }

    pub fn complete(&mut self, token: &str, now_ms: i64) -> Result<(), JobError> {
        self.active_lease(token, now_ms)?;
        self.state = JobState::Succeeded;
        self.lease = None;
        Ok(())
    }

    pub fn fail(
        &mut self,
        token: &str,
        now_ms: i64,
        error_class: &str,
        error: &str,
    ) -> Result<(), JobError> {
        self.active_lease(token, now_ms)?;
        self.last_error = Some(error.to_string());
        self.last_error_class = Some(error_class.to_string());
        self.retry_or_die(now_ms);
        Ok(())
    }

    /// Called at startup: an expired lease means the worker died mid-job.
    /// Returns true when the job was recovered.
    pub fn recover_expired(&mut self, now_ms: i64) -> bool {
        let expired = self
            .lease
            .as_ref()
            .is_some_and(|lease| lease.expires_at_ms <= now_ms);
        if self.state != JobState::Leased || !expired {
            return false;
        }
        self.last_error = Some("lease expired".to_string());
        self.last_error_class = Some("transient".to_string());
        self.retry_or_die(now_ms);
        true
    }

    fn active_lease(&self, token: &str, now_ms: i64) -> Result<&Lease, JobError> {
        if self.state != JobState::Leased {
            return Err(JobError::NotLeased);
        }
        let lease = self.lease.as_ref().ok_or(JobError::NotLeased)?;
        if lease.expires_at_ms <= now_ms {
            return Err(JobError::LeaseExpired);
        }
        if lease.token != token {
            return Err(JobError::TokenMismatch);
        }
        Ok(lease)
    }

    fn retry_or_die(&mut self, now_ms: i64) {
        self.attempts += 1;
        self.lease = None;
        if self.attempts >= self.max_attempts {
            self.state = JobState::Dead;
            return;
        }
        self.state = JobState::Pending;
        let shift = (self.attempts - 1).min(31);
        self.run_after_ms = now_ms + RETRY_BASE_MS.saturating_mul(1_i64 << shift);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claim_heartbeat_complete() {
        let mut job = Job::pending(1);
        let lease = job.claim("worker-a", 1_000, 5_000).unwrap();
        assert_eq!(lease.expires_at_ms, 6_000);
        assert_eq!(job.state, JobState::Leased);
        job.heartbeat(&lease.token, 3_000, 5_000).unwrap();
        assert_eq!(job.lease.as_ref().unwrap().expires_at_ms, 8_000);
        job.complete(&lease.token, 4_000).unwrap();
        assert_eq!(job.state, JobState::Succeeded);
        assert!(job.lease.is_none());
    }

    #[test]
    fn double_claim_is_rejected() {
        let mut job = Job::pending(1);
        job.claim("worker-a", 0, 1_000).unwrap();
        assert_eq!(job.claim("worker-b", 1, 1_000), Err(JobError::LeaseHeld));
    }

    #[test]
    fn expired_lease_recovers_with_backoff_then_dies() {
        let mut job = Job::pending(1);
        job.claim("worker-a", 0, 1_000).unwrap();
        assert!(job.recover_expired(1_000));
        assert_eq!(job.state, JobState::Pending);
        assert_eq!(job.attempts, 1);
        assert_eq!(job.run_after_ms, 2_000);

        job.claim("worker-b", 2_000, 1_000).unwrap();
        assert!(job.recover_expired(3_000));
        assert_eq!(job.attempts, 2);
        assert_eq!(job.run_after_ms, 5_000);

        job.claim("worker-c", 5_000, 1_000).unwrap();
        assert!(job.recover_expired(6_000));
        assert_eq!(job.attempts, 3);
        assert_eq!(job.state, JobState::Dead);
        assert_eq!(job.claim("worker-d", 9_000, 1_000), Err(JobError::Dead));
    }

    #[test]
    fn explicit_failures_use_one_then_two_second_backoff() {
        let mut job = Job::pending(1);
        let lease = job.claim("worker-a", 0, 10_000).unwrap();
        job.fail(&lease.token, 0, "transient", "timeout").unwrap();
        assert_eq!(job.run_after_ms, 1_000);
        let lease = job.claim("worker-a", 1_000, 10_000).unwrap();
        job.fail(&lease.token, 1_000, "transient", "timeout")
            .unwrap();
        assert_eq!(job.run_after_ms, 3_000);
        let lease = job.claim("worker-a", 3_000, 10_000).unwrap();
        job.fail(&lease.token, 3_000, "transient", "timeout")
            .unwrap();
        assert_eq!(job.state, JobState::Dead);
    }

    #[test]
    fn stale_token_cannot_complete_or_heartbeat() {
        let mut job = Job::pending(1);
        let lease = job.claim("worker-a", 0, 10_000).unwrap();
        job.recover_expired(10_000);
        let replacement = job.claim("worker-a", 11_000, 10_000).unwrap();
        assert_ne!(lease.token, replacement.token);
        assert_eq!(
            job.heartbeat(&lease.token, 12_000, 10_000),
            Err(JobError::TokenMismatch)
        );
        assert_eq!(
            job.complete(&lease.token, 12_000),
            Err(JobError::TokenMismatch)
        );
    }
}

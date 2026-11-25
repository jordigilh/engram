//! Hindsight sink for ledger jobs (issue #214, Hindsight-parity slice).
//!
//! One HTTP attempt per ledger job: the durable job ledger owns retries and
//! backoff, so a crash between the HTTP call and the job acknowledgement does
//! not multiply attempts inside the client. A retain is accepted only on a 2xx;
//! a delete treats 404 as already-absent success, matching
//! `contradiction_resolution.delete_document`.

use std::collections::BTreeMap;
use std::time::Duration;

use chrono::{DateTime, Duration as LedgerDuration, Utc};
use postgres::Client;
use reqwest::blocking::Client as HttpClient;
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::Value;

use crate::store::{
    claim_job, complete_job, fail_job, lease_is_active, recover_expired_leases_in_bank, ClaimedJob,
    FailDisposition, RecoveryReport, StoreError,
};

#[derive(Debug, Clone)]
pub struct HindsightSink {
    base_url: reqwest::Url,
    http: HttpClient,
    retain_timeout: Duration,
    delete_timeout: Duration,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetainRequest {
    pub bank_id: String,
    pub document_id: String,
    pub content: String,
    pub timestamp: Option<String>,
    pub metadata: BTreeMap<String, String>,
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeleteOutcome {
    Deleted,
    AlreadyAbsent,
}

#[derive(Debug, thiserror::Error)]
pub enum SinkError {
    #[error("Hindsight request failed transiently: {0}")]
    Transient(String),
    #[error("Hindsight rejected the request permanently: {0}")]
    Permanent(String),
    #[error("ledger job payload is invalid: {0}")]
    Payload(String),
    #[error("lease TTL is too short for the sink request timeout: {0}")]
    InvalidLease(String),
    #[error(transparent)]
    Ledger(#[from] StoreError),
}

impl SinkError {
    pub fn is_transient(&self) -> bool {
        matches!(self, Self::Transient(_))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SinkOutcome {
    Retained,
    Deleted(DeleteOutcome),
    /// The sink call failed and `fail_job` scheduled a ledger retry.
    Retried(String),
    /// The sink call failed and exhausted the ledger retry budget.
    Dead(String),
    /// The sink call succeeded but the ledger lease was lost before the ack.
    /// At-least-once delivery may send the document again; the stable
    /// `document_id` makes the repeat converge.
    LeaseLost,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessedJob {
    pub job_id: i64,
    pub kind: String,
    pub outcome: SinkOutcome,
}

/// One worker step: the processed job (if any), any recovery it performed, and
/// a recovery error that arrived after a job outcome was already committed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProcessStep {
    pub job: Option<ProcessedJob>,
    pub recovery: RecoveryReport,
    pub recovery_error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RetainPayload {
    document_id: String,
    content: String,
    #[serde(default)]
    metadata: BTreeMap<String, String>,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    timestamp: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DeletePayload {
    document_id: String,
}

impl HindsightSink {
    pub fn new(base_url: impl Into<String>) -> Result<Self, SinkError> {
        let base_url = reqwest::Url::parse(&base_url.into())
            .map_err(|error| SinkError::Permanent(error.to_string()))?;
        if !matches!(base_url.scheme(), "http" | "https") {
            return Err(SinkError::Permanent(format!(
                "unsupported base URL scheme {}",
                base_url.scheme()
            )));
        }
        if !base_url.username().is_empty() || base_url.password().is_some() {
            return Err(SinkError::Permanent(
                "base URL must not contain credentials".to_string(),
            ));
        }
        let http = HttpClient::builder()
            // A redirected retain POST becomes a GET and a 200 login page would
            // otherwise be acknowledged as a successful retain.
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| SinkError::Permanent(format!("build HTTP client: {error}")))?;
        Ok(Self {
            base_url,
            http,
            retain_timeout: Duration::from_secs(60),
            delete_timeout: Duration::from_secs(10),
        })
    }

    pub fn max_request_timeout(&self) -> Duration {
        self.retain_timeout.max(self.delete_timeout)
    }

    /// Minimum safe lease: two request windows, so claim, HTTP, and ack fit.
    pub fn minimum_ttl(&self) -> LedgerDuration {
        LedgerDuration::milliseconds(self.max_request_timeout().as_millis() as i64 * 2)
    }

    fn endpoint(&self, segments: &[&str]) -> Result<reqwest::Url, SinkError> {
        let mut url = self.base_url.clone();
        {
            let mut path = url.path_segments_mut().map_err(|_| {
                SinkError::Permanent("base URL cannot be a cannot-be-a-base URL".into())
            })?;
            path.pop_if_empty();
            for segment in segments {
                path.push(segment);
            }
        }
        Ok(url)
    }

    pub fn with_timeouts(mut self, retain_timeout: Duration, delete_timeout: Duration) -> Self {
        self.retain_timeout = retain_timeout;
        self.delete_timeout = delete_timeout;
        self
    }

    pub fn health(&self) -> Result<(), SinkError> {
        let response = self
            .http
            .get(self.endpoint(&["health"])?)
            .timeout(Duration::from_secs(5))
            .send()
            .map_err(|error| classify_transport(&error))?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(classify_status(response.status(), "health"))
        }
    }

    pub fn retain(&self, request: &RetainRequest) -> Result<Value, SinkError> {
        let mut item = serde_json::json!({
            "content": request.content,
            "document_id": request.document_id,
        });
        if let Some(timestamp) = &request.timestamp {
            item["timestamp"] = Value::String(timestamp.clone());
        }
        if !request.metadata.is_empty() {
            item["metadata"] = serde_json::to_value(&request.metadata)
                .map_err(|error| SinkError::Payload(error.to_string()))?;
        }
        if !request.tags.is_empty() {
            item["tags"] = serde_json::to_value(&request.tags)
                .map_err(|error| SinkError::Payload(error.to_string()))?;
        }
        let response = self
            .http
            .post(self.endpoint(&["v1", "default", "banks", &request.bank_id, "memories"])?)
            .json(&serde_json::json!({ "items": [item] }))
            .timeout(self.retain_timeout)
            .send()
            .map_err(|error| classify_transport(&error))?;
        let status = response.status();
        if !status.is_success() {
            return Err(classify_status(status, "retain"));
        }
        Ok(response.json().unwrap_or(Value::Null))
    }

    pub fn delete_document(
        &self,
        bank_id: &str,
        document_id: &str,
    ) -> Result<DeleteOutcome, SinkError> {
        let response = self
            .http
            .delete(self.endpoint(&[
                "v1",
                "default",
                "banks",
                bank_id,
                "documents",
                document_id,
            ])?)
            .timeout(self.delete_timeout)
            .send()
            .map_err(|error| classify_transport(&error))?;
        let status = response.status();
        if status.is_success() {
            return Ok(DeleteOutcome::Deleted);
        }
        if status == StatusCode::NOT_FOUND {
            return Ok(DeleteOutcome::AlreadyAbsent);
        }
        Err(classify_status(status, "delete"))
    }

    pub fn dispatch(&self, bank_id: &str, job: &ClaimedJob) -> Result<SinkOutcome, SinkError> {
        match job.kind.as_str() {
            "retain" => {
                let payload: RetainPayload = serde_json::from_value(job.payload.clone())
                    .map_err(|error| SinkError::Payload(error.to_string()))?;
                self.retain(&RetainRequest {
                    bank_id: bank_id.to_string(),
                    document_id: payload.document_id,
                    content: payload.content,
                    timestamp: payload.timestamp,
                    metadata: payload.metadata,
                    tags: payload.tags,
                })?;
                Ok(SinkOutcome::Retained)
            }
            "delete" => {
                let payload: DeletePayload = serde_json::from_value(job.payload.clone())
                    .map_err(|error| SinkError::Payload(error.to_string()))?;
                Ok(SinkOutcome::Deleted(
                    self.delete_document(bank_id, &payload.document_id)?,
                ))
            }
            other => Err(SinkError::Payload(format!("unknown job kind {other}"))),
        }
    }
}

fn classify_transport(error: &reqwest::Error) -> SinkError {
    if error.is_timeout() || error.is_connect() || error.is_request() {
        SinkError::Transient(error.to_string())
    } else {
        SinkError::Permanent(error.to_string())
    }
}

fn classify_status(status: StatusCode, operation: &str) -> SinkError {
    if status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
        SinkError::Transient(format!("{operation} returned {status}"))
    } else {
        SinkError::Permanent(format!("{operation} returned {status}"))
    }
}

/// Claim one job, call Hindsight, and acknowledge or schedule a ledger retry.
///
/// Returns `None` when no job is claimable. Sink and payload errors are
/// recorded via `fail_job`; a lease lost to recovery returns `LeaseLost`
/// without aborting the worker loop. Only ledger failures abort the call.
///
/// `now` is the claim instant. Acknowledgement and recovery use a fresh clock
/// after the HTTP call; the two-window TTL guard keeps that safe, so callers
/// should pass an instant consistent with the ledger clock.
pub fn process_one(
    db: &mut Client,
    sink: &HindsightSink,
    bank_id: &str,
    owner: &str,
    ttl: LedgerDuration,
    now: DateTime<Utc>,
) -> Result<Option<ProcessedJob>, SinkError> {
    Ok(process_one_reporting(db, sink, bank_id, owner, ttl, now, true)?.job)
}

/// Worker-facing variant that reports recovery done during this step.
///
/// `recovery_due` lets the worker gate recovery to a periodic cadence instead
/// of opening a write transaction on every poll.
pub fn process_one_reporting(
    db: &mut Client,
    sink: &HindsightSink,
    bank_id: &str,
    owner: &str,
    ttl: LedgerDuration,
    now: DateTime<Utc>,
    recovery_due: bool,
) -> Result<ProcessStep, SinkError> {
    if ttl < sink.minimum_ttl() {
        return Err(SinkError::InvalidLease(format!(
            "ttl {ttl} must be at least {} (twice the request timeout)",
            sink.minimum_ttl()
        )));
    }

    let mut recovery = RecoveryReport::default();
    let mut recovered = false;
    let Some(job) = claim_job(db, bank_id, owner, ttl, now)? else {
        // An expired lease can block a document's FIFO queue even when no
        // unleased job is claimable. Recover on the caller's cadence.
        if recovery_due {
            recovery = recover_expired_leases_in_bank(db, bank_id, Utc::now())?;
        }
        return Ok(ProcessStep {
            job: None,
            recovery,
            recovery_error: None,
        });
    };

    debug_assert_eq!(job.bank_id, bank_id);
    // A worker paused between claim and dispatch must not mutate Hindsight
    // after another worker recovered the job. The TTL guard keeps normal
    // in-flight requests safe; this check closes the claim-to-dispatch window.
    if !lease_is_active(db, job.job_id, &job.token, Utc::now())? {
        recovery = recover_expired_leases_in_bank(db, bank_id, Utc::now())?;
        return Ok(ProcessStep {
            job: Some(ProcessedJob {
                job_id: job.job_id,
                kind: job.kind,
                outcome: SinkOutcome::LeaseLost,
            }),
            recovery,
            recovery_error: None,
        });
    }

    let outcome = match sink.dispatch(&job.bank_id, &job) {
        Ok(outcome) => match complete_job(db, job.job_id, &job.token, Utc::now()) {
            Ok(()) => outcome,
            Err(StoreError::LeaseNotHeld) => {
                recovery = recover_expired_leases_in_bank(db, bank_id, Utc::now())?;
                recovered = true;
                SinkOutcome::LeaseLost
            }
            Err(error) => return Err(SinkError::Ledger(error)),
        },
        Err(error) => {
            let (class, message) = match &error {
                SinkError::Transient(message) => ("transient", message.clone()),
                SinkError::Permanent(message) => ("permanent", message.clone()),
                SinkError::Payload(message) => ("permanent", message.clone()),
                SinkError::InvalidLease(message) => ("permanent", message.clone()),
                SinkError::Ledger(_) => return Err(error),
            };
            match fail_job(db, job.job_id, &job.token, Utc::now(), class, &message) {
                Ok(FailDisposition::RetryScheduled) => SinkOutcome::Retried(message),
                Ok(FailDisposition::Dead) => SinkOutcome::Dead(message),
                Err(StoreError::LeaseNotHeld) => {
                    recovery = recover_expired_leases_in_bank(db, bank_id, Utc::now())?;
                    recovered = true;
                    SinkOutcome::LeaseLost
                }
                Err(error) => return Err(SinkError::Ledger(error)),
            }
        }
    };

    let processed = ProcessedJob {
        job_id: job.job_id,
        kind: job.kind,
        outcome,
    };
    let mut recovery_error = None;
    if recovery_due && !recovered {
        match recover_expired_leases_in_bank(db, bank_id, Utc::now()) {
            Ok(report) => recovery = report,
            // The job outcome is already committed. Keep it observable and let
            // the worker record/reconnect for the unfinished recovery.
            Err(error) => recovery_error = Some(error.to_string()),
        }
    }
    Ok(ProcessStep {
        job: Some(processed),
        recovery,
        recovery_error,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transient_classification() {
        assert!(classify_status(StatusCode::BAD_GATEWAY, "retain").is_transient());
        assert!(classify_status(StatusCode::TOO_MANY_REQUESTS, "retain").is_transient());
        assert!(!classify_status(StatusCode::BAD_REQUEST, "retain").is_transient());
    }

    #[test]
    fn payload_shape_is_parsed() {
        let value = serde_json::json!({
            "document_id": "repo--a",
            "content": "body",
            "metadata": {"source": "cocoindex"},
            "tags": ["root", "repo"],
            "timestamp": "2026-11-24T10:15:30"
        });
        let payload: RetainPayload = serde_json::from_value(value).unwrap();
        assert_eq!(payload.document_id, "repo--a");
        assert_eq!(payload.metadata["source"], "cocoindex");
        assert_eq!(payload.tags, vec!["root", "repo"]);
    }
}

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
    claim_job, complete_job, fail_job, recover_expired_leases, ClaimedJob, StoreError,
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
    /// The sink call failed and `fail_job` scheduled a ledger retry (or death).
    Failed(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessedJob {
    pub job_id: i64,
    pub kind: String,
    pub outcome: SinkOutcome,
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
/// recorded via `fail_job`; a lease lost to recovery returns `Failed` without
/// aborting the worker loop. Only ledger failures abort the call.
///
/// `now` is the claim instant. Acknowledgement uses a fresh clock after the
/// HTTP call; the two-window TTL guard keeps that safe, so callers should pass
/// an instant consistent with the ledger clock.
pub fn process_one(
    db: &mut Client,
    sink: &HindsightSink,
    bank_id: &str,
    owner: &str,
    ttl: LedgerDuration,
    now: DateTime<Utc>,
) -> Result<Option<ProcessedJob>, SinkError> {
    if ttl < sink.minimum_ttl() {
        return Err(SinkError::InvalidLease(format!(
            "ttl {ttl} must be at least {} (twice the request timeout)",
            sink.minimum_ttl()
        )));
    }

    // Reclaim crashed-worker leases before claiming; otherwise an expired
    // lease blocks every later job for the same document.
    recover_expired_leases(db, now)?;
    let Some(job) = claim_job(db, owner, ttl, now)? else {
        return Ok(None);
    };
    match sink.dispatch(bank_id, &job) {
        Ok(outcome) => match complete_job(db, job.job_id, &job.token, Utc::now()) {
            Ok(()) => Ok(Some(ProcessedJob {
                job_id: job.job_id,
                kind: job.kind,
                outcome,
            })),
            Err(StoreError::LeaseNotHeld) => {
                recover_expired_leases(db, Utc::now())?;
                Ok(Some(ProcessedJob {
                    job_id: job.job_id,
                    kind: job.kind,
                    outcome: SinkOutcome::Failed(
                        "lease lost; job recovered by another worker".to_string(),
                    ),
                }))
            }
            Err(error) => Err(SinkError::Ledger(error)),
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
                Ok(()) => Ok(Some(ProcessedJob {
                    job_id: job.job_id,
                    kind: job.kind,
                    outcome: SinkOutcome::Failed(message),
                })),
                Err(StoreError::LeaseNotHeld) => {
                    recover_expired_leases(db, Utc::now())?;
                    Ok(Some(ProcessedJob {
                        job_id: job.job_id,
                        kind: job.kind,
                        outcome: SinkOutcome::Failed(
                            "lease lost; job recovered by another worker".to_string(),
                        ),
                    }))
                }
                Err(error) => Err(SinkError::Ledger(error)),
            }
        }
    }
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

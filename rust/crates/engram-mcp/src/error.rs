//! Typed busy/outcome envelopes shared with the Python multiplex contract.
//!
//! Reference: `docs/SERENA_PER_WORKSPACE_INVALIDATION.md` section 3.4.

use serde_json::{json, Value};

pub const JSONRPC_TYPED_ERROR_CODE: i64 = -32002;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    WorkspaceFlushing,
    WorkspaceUnbound,
    WorkspaceAuthTimeout,
    OutcomeUnknown,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::WorkspaceFlushing => "workspace_flushing",
            ErrorKind::WorkspaceUnbound => "workspace_unbound",
            ErrorKind::WorkspaceAuthTimeout => "workspace_auth_timeout",
            ErrorKind::OutcomeUnknown => "outcome_unknown",
        }
    }

    pub fn retryable(self) -> bool {
        matches!(
            self,
            ErrorKind::WorkspaceFlushing | ErrorKind::WorkspaceAuthTimeout
        )
    }

    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::WorkspaceUnbound | ErrorKind::OutcomeUnknown => 409,
            ErrorKind::WorkspaceFlushing | ErrorKind::WorkspaceAuthTimeout => 503,
        }
    }

    pub fn sends_retry_after(self) -> bool {
        matches!(
            self,
            ErrorKind::WorkspaceFlushing | ErrorKind::WorkspaceAuthTimeout
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypedError {
    pub kind: ErrorKind,
    pub workspace: Option<String>,
    pub epoch: Option<u64>,
    pub retry_after_ms: Option<u64>,
}

impl TypedError {
    pub fn new(kind: ErrorKind) -> Self {
        Self {
            kind,
            workspace: None,
            epoch: None,
            retry_after_ms: None,
        }
    }

    pub fn flushing() -> Self {
        Self::new(ErrorKind::WorkspaceFlushing).with_retry_after_ms(1_000)
    }

    pub fn unbound() -> Self {
        Self::new(ErrorKind::WorkspaceUnbound)
    }

    pub fn outcome_unknown(workspace: Option<String>) -> Self {
        let mut error = Self::new(ErrorKind::OutcomeUnknown);
        error.workspace = workspace;
        error
    }

    pub fn in_workspace(mut self, workspace: impl Into<String>) -> Self {
        self.workspace = Some(workspace.into());
        self
    }

    pub fn with_epoch(mut self, epoch: u64) -> Self {
        self.epoch = Some(epoch);
        self
    }

    pub fn with_retry_after_ms(mut self, retry_after_ms: u64) -> Self {
        self.retry_after_ms = Some(retry_after_ms);
        self
    }

    pub fn message(&self) -> String {
        let mut message = self.kind.as_str().to_string();
        if let Some(workspace) = &self.workspace {
            message.push_str(": ");
            message.push_str(workspace);
        }
        if let Some(retry_after_ms) = self.retry_after_ms {
            message.push_str(&format!(
                "; retry in {}s",
                std::cmp::max(1, retry_after_ms / 1000)
            ));
        }
        message
    }

    pub fn payload(&self, message_id: Value) -> Value {
        let mut data = serde_json::Map::new();
        data.insert("kind".into(), json!(self.kind.as_str()));
        data.insert("retryable".into(), json!(self.kind.retryable()));
        if let Some(workspace) = &self.workspace {
            data.insert("workspace".into(), json!(workspace));
        }
        if let Some(epoch) = self.epoch {
            data.insert("epoch".into(), json!(epoch));
        }
        if let Some(retry_after_ms) = self.retry_after_ms {
            data.insert("retry_after_ms".into(), json!(retry_after_ms));
        }
        json!({
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {
                "code": JSONRPC_TYPED_ERROR_CODE,
                "message": self.message(),
                "data": data,
            }
        })
    }

    pub fn headers(&self) -> Vec<(&'static str, String)> {
        let mut headers = vec![
            ("X-Engram-Error", self.kind.as_str().to_string()),
            ("Content-Type", "application/json".to_string()),
        ];
        if self.kind.sends_retry_after() {
            if let Some(retry_after_ms) = self.retry_after_ms {
                headers.push((
                    "Retry-After",
                    std::cmp::max(1, retry_after_ms / 1000).to_string(),
                ));
            }
        }
        headers
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flushing_payload_matches_the_python_contract() {
        let busy = TypedError::flushing()
            .in_workspace("/ws")
            .with_epoch(3)
            .with_retry_after_ms(1500);
        let payload = busy.payload(json!(7));
        assert_eq!(payload["error"]["code"], -32002);
        assert_eq!(payload["error"]["data"]["kind"], "workspace_flushing");
        assert_eq!(payload["error"]["data"]["workspace"], "/ws");
        assert_eq!(payload["error"]["data"]["epoch"], 3);
        assert_eq!(payload["error"]["data"]["retryable"], true);
        assert_eq!(
            payload["error"]["message"],
            "workspace_flushing: /ws; retry in 1s"
        );
        let headers = busy.headers();
        assert!(headers.contains(&("X-Engram-Error", "workspace_flushing".into())));
        assert!(headers.contains(&("Retry-After", "1".into())));
    }

    #[test]
    fn unbound_and_outcome_unknown_are_409_and_not_retried() {
        assert_eq!(ErrorKind::WorkspaceUnbound.http_status(), 409);
        assert_eq!(ErrorKind::OutcomeUnknown.http_status(), 409);
        assert!(!ErrorKind::OutcomeUnknown.retryable());
        assert!(TypedError::outcome_unknown(None)
            .headers()
            .iter()
            .all(|(name, _)| *name != "Retry-After"));
    }

    #[test]
    fn auth_timeout_is_retryable_503_without_a_budget() {
        let error = TypedError::new(ErrorKind::WorkspaceAuthTimeout);
        assert_eq!(error.message(), "workspace_auth_timeout");
        assert_eq!(error.kind.http_status(), 503);
        assert!(error
            .headers()
            .iter()
            .all(|(name, _)| *name != "Retry-After"));
    }
}

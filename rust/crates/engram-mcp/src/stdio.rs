//! Long-lived stdio MCP backend adapter built on the official `rmcp` client.

use std::collections::{BTreeMap, HashSet};
use std::process::Stdio;
use std::sync::{Arc, Mutex as StdMutex};

use async_trait::async_trait;
use rmcp::model::{CallToolRequestParams, PaginatedRequestParams, Tool};
use rmcp::service::{RoleClient, RunningService, ServiceError, ServiceExt};
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::backend::{is_write_tool, BackendAdapter, BackendError};

const MAX_TOOL_PAGES: usize = 100;
const SAFE_ENV: [&str; 6] = ["PATH", "HOME", "LOGNAME", "SHELL", "TERM", "USER"];

type ClientService = RunningService<RoleClient, ()>;

struct Session {
    service: ClientService,
    child: tokio::process::Child,
}

struct StartupChild {
    child: Option<tokio::process::Child>,
    queue: Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>,
}

impl Drop for StartupChild {
    fn drop(&mut self) {
        if let Some(child) = self.child.take() {
            queue_child_cleanup(child, &self.queue);
        }
    }
}

/// One long-lived stdio MCP child, serialized like the Python adapter.
pub struct StdioSubprocessAdapter {
    command: String,
    args: Vec<String>,
    env: BTreeMap<String, String>,
    // The child is leased out while a call is in flight. A dropped future
    // leaves the slot empty, so the next caller spawns a fresh child instead
    // of overlapping the cancelled request on the same one.
    service: Arc<StdMutex<Option<Session>>>,
    retiring: Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>,
    call_lock: Mutex<()>,
}

struct ServiceLease {
    service: Option<Session>,
    slot: Arc<StdMutex<Option<Session>>>,
    retiring: Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>,
}

struct RetireHandleGuard {
    handle: Option<tokio::task::JoinHandle<()>>,
    queue: Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>,
}

impl Drop for RetireHandleGuard {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            // Cancellation during retirement must leave the cleanup waitable.
            self.queue
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(handle);
        }
    }
}

fn queue_cleanup(session: Session, queue: &Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>) {
    let handle = tokio::spawn(async move {
        cleanup_session(session).await;
    });
    queue
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .push(handle);
}

fn queue_child_cleanup(
    child: tokio::process::Child,
    queue: &Arc<StdMutex<Vec<tokio::task::JoinHandle<()>>>>,
) {
    let handle = tokio::spawn(async move {
        cleanup_child(child).await;
    });
    queue
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .push(handle);
}

async fn cleanup_child(mut child: tokio::process::Child) {
    if tokio::time::timeout(std::time::Duration::from_secs(5), child.wait())
        .await
        .is_err()
    {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
}

impl ServiceLease {
    fn session(&mut self) -> &mut Session {
        self.service.as_mut().expect("leased service")
    }

    fn restore(mut self) {
        if let Some(service) = self.service.take() {
            *self
                .slot
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(service);
        }
    }

    fn retire(mut self) {
        if let Some(service) = self.service.take() {
            queue_cleanup(service, &self.retiring);
        }
    }
}

impl Drop for ServiceLease {
    fn drop(&mut self) {
        if let Some(service) = self.service.take() {
            queue_cleanup(service, &self.retiring);
        }
    }
}

impl StdioSubprocessAdapter {
    pub fn new(command: String, args: Vec<String>, env: BTreeMap<String, String>) -> Self {
        Self {
            command,
            args,
            env,
            service: Arc::new(StdMutex::new(None)),
            retiring: Arc::new(StdMutex::new(Vec::new())),
            call_lock: Mutex::new(()),
        }
    }

    async fn spawn(&self) -> Result<Session, BackendError> {
        let mut command = Command::new(&self.command);
        command.args(&self.args);
        // Python's stdio client starts children with a deliberately small
        // default environment; do not leak the gateway's full env (secrets).
        command.env_clear();
        for key in SAFE_ENV {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        command.envs(&self.env);
        command.stdin(Stdio::piped()).stdout(Stdio::piped());
        let child = command.spawn().map_err(|error| {
            BackendError::transient(format!(
                "cannot spawn stdio backend {:?}: {error}",
                self.command
            ))
        })?;
        // If initialization is cancelled or fails, the guard queues waitable
        // cleanup; Tokio does not kill children on drop by default.
        let mut startup = StartupChild {
            child: Some(child),
            queue: Arc::clone(&self.retiring),
        };
        let child = startup.child.as_mut().expect("startup child");
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| BackendError::transient("stdio child did not expose stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| BackendError::transient("stdio child did not expose stdout"))?;
        let transport = tokio::io::join(stdout, stdin);
        let service = ().serve(transport).await.map_err(|error| {
            BackendError::transient(format!("stdio initialize failed: {error}"))
        })?;
        Ok(Session {
            service,
            child: startup.child.take().expect("startup child"),
        })
    }

    async fn ensure_service(&self) -> Result<(), BackendError> {
        if self
            .service
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .is_some()
        {
            return Ok(());
        }
        let service = self.spawn().await?;
        *self
            .service
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(service);
        Ok(())
    }

    fn take_lease(&self) -> ServiceLease {
        let service = self
            .service
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
            .expect("service ensured");
        ServiceLease {
            service: Some(service),
            slot: Arc::clone(&self.service),
            retiring: Arc::clone(&self.retiring),
        }
    }

    async fn wait_for_retired(&self) {
        loop {
            let handle = self
                .retiring
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .pop();
            let Some(handle) = handle else {
                return;
            };
            let mut guard = RetireHandleGuard {
                handle: Some(handle),
                queue: Arc::clone(&self.retiring),
            };
            if let Some(handle) = guard.handle.as_mut() {
                // Awaiting `&mut JoinHandle` is cancellation-safe: dropping
                // this future re-queues the same handle for the next caller.
                let _ = handle.await;
            }
            let _ = guard.handle.take();
        }
    }
}

#[async_trait]
impl BackendAdapter for StdioSubprocessAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        let _call = self.call_lock.lock().await;
        self.wait_for_retired().await;
        self.ensure_service().await?;
        let mut lease = self.take_lease();
        let first = list_tools_pages(lease.session()).await;
        match first {
            Ok(tools) => {
                lease.restore();
                Ok(normalize_tools(tools))
            }
            Err(error) if is_definitive(&error) => {
                lease.restore();
                Err(error)
            }
            Err(error) => {
                let first_error = error.message;
                lease.retire();
                self.wait_for_retired().await;
                if let Err(restart) = self.ensure_service().await {
                    return Err(BackendError::transient(format!(
                        "stdio tools/list restart failed: {restart} (first: {first_error})"
                    )));
                }
                let mut retry = self.take_lease();
                let result = list_tools_pages(retry.session()).await;
                match result {
                    Ok(tools) => {
                        retry.restore();
                        Ok(normalize_tools(tools))
                    }
                    Err(error) => {
                        retry.restore();
                        Err(BackendError::permanent(format!(
                            "stdio tools/list failed after retry: {} (first: {first_error})",
                            error.message
                        )))
                    }
                }
            }
        }
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
        let params: CallToolRequestParams = serde_json::from_value(
            json!({"name": name, "arguments": arguments}),
        )
        .map_err(|error| BackendError::permanent(format!("invalid stdio tool request: {error}")))?;
        let _call = self.call_lock.lock().await;
        self.wait_for_retired().await;
        self.ensure_service().await?;
        let mut lease = self.take_lease();
        let first = lease
            .session()
            .service
            .peer()
            .call_tool(params.clone())
            .await;
        let result = match first {
            Ok(result) => {
                lease.restore();
                result
            }
            Err(error) if service_error_is_definitive(&error) => {
                lease.restore();
                return Err(BackendError::permanent(format!(
                    "stdio tools/call rejected: {error}"
                )));
            }
            Err(error) if is_write_tool(name) => {
                // Recover the child for later calls, but never replay the
                // write itself; let the gateway surface outcome_unknown.
                lease.retire();
                self.wait_for_retired().await;
                let recovery = match self.ensure_service().await {
                    Ok(()) => String::new(),
                    Err(restart_error) => format!(" (restart failed: {restart_error})"),
                };
                return Err(BackendError::transient(format!(
                    "stdio tools/call failed for write {name:?}: {error}{recovery}"
                ))
                .uncertain());
            }
            Err(error) => {
                let first_error = format!("{error}");
                lease.retire();
                self.wait_for_retired().await;
                if let Err(restart) = self.ensure_service().await {
                    return Err(BackendError::transient(format!(
                        "stdio tools/call restart failed: {restart} (first: {first_error})"
                    )));
                }
                let mut retry = self.take_lease();
                let result = retry.session().service.peer().call_tool(params).await;
                match result {
                    Ok(result) => {
                        retry.restore();
                        result
                    }
                    Err(error) => {
                        retry.restore();
                        return Err(BackendError::permanent(format!(
                            "stdio tools/call failed after retry: {error} (first: {first_error})"
                        )));
                    }
                }
            }
        };
        let value = serde_json::to_value(result).map_err(|error| {
            BackendError::permanent(format!("cannot serialize stdio tool result: {error}"))
        })?;
        Ok(normalize_call_result(value))
    }
}

async fn cleanup_session(session: Session) {
    let _ = session.service.cancel().await;
    cleanup_child(session.child).await;
}

fn is_definitive(error: &BackendError) -> bool {
    error.typed.is_none() && !error.transient && !error.uncertain
}

fn service_error_is_definitive(error: &ServiceError) -> bool {
    matches!(error, ServiceError::McpError(_))
}

async fn list_tools_pages(session: &mut Session) -> Result<Vec<Tool>, BackendError> {
    let mut tools = Vec::new();
    let mut cursor: Option<String> = None;
    let mut seen = HashSet::new();
    for _ in 0..MAX_TOOL_PAGES {
        let params = match &cursor {
            Some(cursor) => Some(
                serde_json::from_value::<PaginatedRequestParams>(json!({"cursor": cursor}))
                    .map_err(|error| {
                        BackendError::permanent(format!("invalid tools/list cursor: {error}"))
                    })?,
            ),
            None => None,
        };
        let result = session
            .service
            .peer()
            .list_tools(params)
            .await
            .map_err(|error| {
                if service_error_is_definitive(&error) {
                    BackendError::permanent(format!("stdio tools/list rejected: {error}"))
                } else {
                    BackendError::transient(format!("stdio tools/list failed: {error}"))
                }
            })?;
        tools.extend(result.tools);
        let Some(next) = result.next_cursor.filter(|cursor| !cursor.is_empty()) else {
            return Ok(tools);
        };
        if !seen.insert(next.clone()) {
            return Err(BackendError::permanent(
                "stdio tools/list returned a repeated pagination cursor",
            ));
        }
        cursor = Some(next);
    }
    Err(BackendError::permanent(
        "stdio tools/list exceeded the pagination limit",
    ))
}

fn normalize_tools(tools: Vec<Tool>) -> Vec<Value> {
    tools
        .into_iter()
        .filter_map(|tool| serde_json::to_value(tool).ok())
        .map(|tool| {
            let description = tool
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let input_schema = tool
                .get("inputSchema")
                .cloned()
                .or_else(|| tool.get("input_schema").cloned())
                .unwrap_or_else(|| json!({"type": "object"}));
            json!({
                "name": tool.get("name").cloned().unwrap_or(Value::Null),
                "description": description,
                "inputSchema": input_schema,
            })
        })
        .collect()
}

fn normalize_call_result(result: Value) -> Value {
    let content = result.get("content").cloned().unwrap_or_else(|| json!([]));
    let is_error = result
        .get("isError")
        .or_else(|| result.get("is_error"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    json!({
        "content": strip_nulls(content),
        "isError": is_error,
    })
}

fn strip_nulls(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .filter(|(_, value)| !value.is_null())
                .map(|(key, value)| (key, strip_nulls(value)))
                .collect(),
        ),
        // Pydantic's exclude_none drops None-valued object fields, not None
        // elements inside arrays.
        Value::Array(items) => Value::Array(items.into_iter().map(strip_nulls).collect()),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_tools_and_strips_optional_fields() {
        let tools = normalize_tools(vec![]);
        assert!(tools.is_empty());

        let result = normalize_call_result(json!({
            "content": [
                {"type": "text", "text": "ok", "annotations": null,
                 "nested": {"meta": null, "value": 1}}
            ],
            "structuredContent": {"ignored": true},
            "is_error": true
        }));
        assert_eq!(result["isError"], true);
        assert_eq!(result["content"][0]["text"], "ok");
        assert!(result["content"][0].get("annotations").is_none());
        assert!(result["content"][0]["nested"].get("meta").is_none());
        assert_eq!(result["content"][0]["nested"]["value"], 1);
        assert!(result.get("structuredContent").is_none());
    }

    #[test]
    fn strips_nested_null_arrays() {
        let value = strip_nulls(json!({"a": [null, {"b": null, "c": 1}], "d": null}));
        assert_eq!(value, json!({"a": [null, {"c": 1}]}));
    }
}

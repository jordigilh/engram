//! Long-lived stdio MCP backend adapter built on the official `rmcp` client.

use std::collections::{BTreeMap, HashSet};

use async_trait::async_trait;
use rmcp::model::{CallToolRequestParams, PaginatedRequestParams, Tool};
use rmcp::service::{RoleClient, RunningService, ServiceError, ServiceExt};
use rmcp::transport::TokioChildProcess;
use serde_json::{json, Value};
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::backend::{is_write_tool, BackendAdapter, BackendError};

const MAX_TOOL_PAGES: usize = 100;
const SAFE_ENV: [&str; 6] = ["PATH", "HOME", "LOGNAME", "SHELL", "TERM", "USER"];

type ClientService = RunningService<RoleClient, ()>;

/// One long-lived stdio MCP child, serialized like the Python adapter.
pub struct StdioSubprocessAdapter {
    command: String,
    args: Vec<String>,
    env: BTreeMap<String, String>,
    service: Mutex<Option<ClientService>>,
}

impl StdioSubprocessAdapter {
    pub fn new(command: String, args: Vec<String>, env: BTreeMap<String, String>) -> Self {
        Self {
            command,
            args,
            env,
            service: Mutex::new(None),
        }
    }

    async fn spawn(&self) -> Result<ClientService, BackendError> {
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
        let transport = TokioChildProcess::new(command).map_err(|error| {
            BackendError::transient(format!(
                "cannot spawn stdio backend {:?}: {error}",
                self.command
            ))
        })?;
        ().serve(transport)
            .await
            .map_err(|error| BackendError::transient(format!("stdio initialize failed: {error}")))
    }

    async fn restart(&self, service: &mut Option<ClientService>) -> Result<(), BackendError> {
        if let Some(service) = service.take() {
            // rmcp's documented graceful path: closes stdin, waits, then
            // TERM/KILL. A bare drop is SIGKILL-only and can orphan children.
            let _ = service.cancel().await;
        }
        *service = Some(self.spawn().await?);
        Ok(())
    }

    async fn ensure(&self, service: &mut Option<ClientService>) -> Result<(), BackendError> {
        if service.is_none() {
            *service = Some(self.spawn().await?);
        }
        Ok(())
    }
}

#[async_trait]
impl BackendAdapter for StdioSubprocessAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        let mut guard = self.service.lock().await;
        self.ensure(&mut guard).await?;
        let first = list_tools_pages(guard.as_ref().expect("service initialized")).await;
        let tools = match first {
            Ok(tools) => tools,
            Err(error) if is_definitive(&error) => return Err(error),
            Err(error) => {
                let first_error = error.message;
                if let Err(restart) = self.restart(&mut guard).await {
                    return Err(BackendError::transient(format!(
                        "stdio tools/list restart failed: {restart} (first: {first_error})"
                    )));
                }
                list_tools_pages(guard.as_ref().expect("restarted service"))
                    .await
                    .map_err(|error| {
                        BackendError::permanent(format!(
                            "stdio tools/list failed after retry: {} (first: {first_error})",
                            error.message
                        ))
                    })?
            }
        };
        Ok(normalize_tools(tools))
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
        let params: CallToolRequestParams = serde_json::from_value(
            json!({"name": name, "arguments": arguments}),
        )
        .map_err(|error| BackendError::permanent(format!("invalid stdio tool request: {error}")))?;
        let mut guard = self.service.lock().await;
        self.ensure(&mut guard).await?;
        let first = guard
            .as_ref()
            .expect("service initialized")
            .peer()
            .call_tool(params.clone())
            .await;
        let result = match first {
            Ok(result) => result,
            Err(error) if service_error_is_definitive(&error) => {
                return Err(BackendError::permanent(format!(
                    "stdio tools/call rejected: {error}"
                )));
            }
            Err(error) if is_write_tool(name) => {
                // Recover the child for later calls, but never replay the
                // write itself; let the gateway surface outcome_unknown.
                let restart = self.restart(&mut guard).await;
                let recovery = match restart {
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
                if let Err(restart) = self.restart(&mut guard).await {
                    return Err(BackendError::transient(format!(
                        "stdio tools/call restart failed: {restart} (first: {first_error})"
                    )));
                }
                guard
                    .as_ref()
                    .expect("restarted service")
                    .peer()
                    .call_tool(params)
                    .await
                    .map_err(|error| {
                        BackendError::permanent(format!(
                            "stdio tools/call failed after retry: {error} (first: {first_error})"
                        ))
                    })?
            }
        };
        let value = serde_json::to_value(result).map_err(|error| {
            BackendError::permanent(format!("cannot serialize stdio tool result: {error}"))
        })?;
        Ok(normalize_call_result(value))
    }
}

fn is_definitive(error: &BackendError) -> bool {
    error.typed.is_none() && !error.transient && !error.uncertain
}

fn service_error_is_definitive(error: &ServiceError) -> bool {
    matches!(error, ServiceError::McpError(_))
}

async fn list_tools_pages(service: &ClientService) -> Result<Vec<Tool>, BackendError> {
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
        let result = service.peer().list_tools(params).await.map_err(|error| {
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

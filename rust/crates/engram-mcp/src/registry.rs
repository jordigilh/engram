//! Deployment-local instance registry loading and validation.
//!
//! Mirrors `load_instance_registry`/`_parse_runtime_backend` from the Python
//! gateway. Table iteration order is preserved so collision qualification and
//! tool order match the registry.

use std::collections::BTreeMap;
use std::path::Path;

use toml::Value;
use url::Url;

pub const DEFAULT_FORWARD_TIMEOUT_S: f64 = 60.0;
pub const MAX_FORWARD_TIMEOUT_S: f64 = 600.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendKind {
    Http,
    ShadowHttp,
    Stdio,
}

impl BackendKind {
    pub fn as_str(self) -> &'static str {
        match self {
            BackendKind::Http => "http",
            BackendKind::ShadowHttp => "shadow_http",
            BackendKind::Stdio => "stdio",
        }
    }
}

#[derive(Debug, Clone)]
pub struct BackendSpec {
    pub kind: BackendKind,
    pub url: Option<String>,
    pub headers: BTreeMap<String, String>,
    pub timeout_seconds: f64,
    pub workspace_routing: bool,
    pub allow_insecure_http: bool,
    pub scope_tags: Vec<String>,
    pub allow_unscoped: bool,
    pub mental_model_ids: Vec<String>,
    pub command: Option<String>,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Default)]
pub struct ProjectSpec {
    pub backends: Vec<(String, BackendSpec)>,
}

impl ProjectSpec {
    pub fn backend(&self, key: &str) -> Option<&BackendSpec> {
        self.backends
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, spec)| spec)
    }
}

#[derive(Debug, Clone, Default)]
pub struct Registry {
    pub projects: Vec<(String, ProjectSpec)>,
}

impl Registry {
    pub fn load(path: &Path) -> Result<Self, String> {
        let contents = std::fs::read_to_string(path)
            .map_err(|error| format!("Cannot read instance config {}: {error}", path.display()))?;
        Self::parse_str(&contents)
    }

    pub fn parse_str(contents: &str) -> Result<Self, String> {
        let document: Value = toml::from_str(contents)
            .map_err(|error| format!("Invalid instance config: {error}"))?;
        let instances = document
            .get("instances")
            .and_then(Value::as_table)
            .ok_or_else(|| {
                "Instance config must contain a non-empty [instances] table".to_string()
            })?;
        if instances.is_empty() {
            return Err("Instance config must contain a non-empty [instances] table".into());
        }

        let mut projects = Vec::new();
        for (name, settings) in instances {
            validate_route_name(name)?;
            let settings = settings
                .as_table()
                .ok_or_else(|| format!("Instance {name:?} must be a TOML table"))?;
            let endpoint = settings.get("endpoint");
            let backends = settings.get("backends");
            if endpoint.is_some() && backends.is_some() {
                return Err(format!(
                    "Instance {name:?} cannot define both endpoint and backends"
                ));
            }
            if let Some(endpoint) = endpoint {
                let url = endpoint
                    .as_str()
                    .ok_or_else(|| format!("Instance {name:?} endpoint must be a string"))?;
                let timeout_seconds = parse_timeout(name, "host", settings.get("timeout_seconds"))?
                    .unwrap_or(DEFAULT_FORWARD_TIMEOUT_S);
                let spec = validate_http(name, "host", url, timeout_seconds, false)?;
                projects.push((
                    name.clone(),
                    ProjectSpec {
                        backends: vec![("host".into(), spec)],
                    },
                ));
                continue;
            }

            let backends = backends
                .and_then(Value::as_table)
                .ok_or_else(|| format!("Instance {name:?} requires a non-empty backends table"))?;
            if backends.is_empty() {
                return Err(format!(
                    "Instance {name:?} requires a non-empty backends table"
                ));
            }
            let mut parsed = Vec::new();
            for (backend, settings) in backends {
                if backend.is_empty() || backend.contains('/') {
                    return Err(format!(
                        "Invalid backend key for instance {name:?}: {backend:?}"
                    ));
                }
                parsed.push((backend.clone(), parse_backend(name, backend, settings)?));
            }
            projects.push((name.clone(), ProjectSpec { backends: parsed }));
        }
        Ok(Self { projects })
    }

    pub fn get(&self, name: &str) -> Option<&ProjectSpec> {
        self.projects
            .iter()
            .find(|(project, _)| project == name)
            .map(|(_, spec)| spec)
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.projects.iter().map(|(name, _)| name.as_str())
    }
}

fn validate_route_name(name: &str) -> Result<(), String> {
    if name.is_empty() || name.contains('/') || matches!(name, "." | "..") {
        return Err(format!("Invalid instance route: {name:?}"));
    }
    Ok(())
}

fn parse_backend(project: &str, backend: &str, settings: &Value) -> Result<BackendSpec, String> {
    let settings = settings
        .as_table()
        .ok_or_else(|| format!("Backend {project:?}/{backend:?} must be a TOML table"))?;
    let kind = match settings.get("kind").and_then(Value::as_str) {
        Some("http") => BackendKind::Http,
        Some("shadow_http") => BackendKind::ShadowHttp,
        Some("stdio") => BackendKind::Stdio,
        _ => {
            return Err(format!(
                "Backend {project:?}/{backend:?} kind must be 'http', 'shadow_http', or 'stdio'"
            ))
        }
    };
    let workspace_routing = match settings.get("workspace_routing") {
        Some(value) => value.as_bool().ok_or_else(|| {
            format!("Backend {project:?}/{backend:?} workspace_routing must be a boolean")
        })?,
        None => backend == "serena",
    };
    if backend == "hindsight" && kind != BackendKind::Http {
        return Err("Backend 'hindsight' must use an HTTP Hindsight bank endpoint".into());
    }
    if backend == "serena" && workspace_routing && kind != BackendKind::Http {
        return Err(format!(
            "Backend {project:?}/{backend:?} workspace_routing=true requires an HTTP Serena multiplex endpoint"
        ));
    }

    let timeout_seconds = parse_timeout(project, backend, settings.get("timeout_seconds"))?
        .unwrap_or(DEFAULT_FORWARD_TIMEOUT_S);

    match kind {
        BackendKind::Http | BackendKind::ShadowHttp => {
            let url = settings
                .get("url")
                .and_then(Value::as_str)
                .filter(|url| !url.is_empty())
                .ok_or_else(|| {
                    format!("Backend {project:?}/{backend:?} requires a non-empty url")
                })?;
            let allow_insecure_http =
                optional_bool(project, backend, settings, "allow_insecure_http")?.unwrap_or(false);
            let mut spec =
                validate_http(project, backend, url, timeout_seconds, allow_insecure_http)?;
            spec.kind = kind;
            spec.workspace_routing = workspace_routing;
            if kind == BackendKind::ShadowHttp {
                let shadow = settings
                    .get("shadow_url")
                    .and_then(Value::as_str)
                    .filter(|url| !url.is_empty())
                    .ok_or_else(|| {
                        format!("Backend {project:?}/{backend:?} requires a non-empty shadow_url")
                    })?;
                validate_url(project, backend, shadow)?;
            }
            spec.headers = parse_headers(project, backend, settings.get("headers"))?;
            if backend == "hindsight" {
                spec.scope_tags = parse_scope_list(project, backend, settings.get("scope_tags"))?;
                spec.allow_unscoped =
                    optional_bool(project, backend, settings, "allow_unscoped")?.unwrap_or(false);
                spec.mental_model_ids =
                    parse_scope_list(project, backend, settings.get("mental_model_ids"))?;
                if spec.scope_tags.is_empty() && !spec.allow_unscoped {
                    return Err(format!(
                        "Backend {project:?}/{backend:?} requires scope_tags or explicit allow_unscoped=true"
                    ));
                }
            }
            Ok(spec)
        }
        BackendKind::Stdio => {
            let command = settings
                .get("command")
                .and_then(Value::as_str)
                .filter(|command| !command.is_empty())
                .ok_or_else(|| {
                    format!("Backend {project:?}/{backend:?} requires a non-empty command")
                })?;
            let args = parse_string_list(project, backend, settings.get("args"))?;
            let env = parse_headers(project, backend, settings.get("env"))?;
            Ok(BackendSpec {
                kind,
                url: None,
                headers: BTreeMap::new(),
                timeout_seconds,
                workspace_routing,
                allow_insecure_http: false,
                scope_tags: Vec::new(),
                allow_unscoped: false,
                mental_model_ids: Vec::new(),
                command: Some(command.to_string()),
                args,
                env,
            })
        }
    }
}

fn validate_http(
    project: &str,
    backend: &str,
    url: &str,
    timeout_seconds: f64,
    allow_insecure_http: bool,
) -> Result<BackendSpec, String> {
    let parsed = validate_url(project, backend, url)?;
    if backend == "hindsight" {
        let path = parsed.path().trim_end_matches('/');
        if path != "/mcp/shared-memory" && path != "/mcp/hindsight" {
            return Err(format!(
                "Backend {project:?}/{backend:?} must point to /mcp/shared-memory/ or /mcp/hindsight/"
            ));
        }
        let loopback = parsed
            .host_str()
            .map(|host| {
                matches!(
                    host.trim_matches(|character| character == '[' || character == ']'),
                    "localhost" | "127.0.0.1" | "::1"
                )
            })
            .unwrap_or(false);
        if parsed.scheme() == "http" && !loopback && !allow_insecure_http {
            return Err(format!(
                "Backend {project:?}/{backend:?} must use HTTPS for non-loopback Hindsight URLs or explicitly set allow_insecure_http=true"
            ));
        }
    }
    Ok(BackendSpec {
        kind: BackendKind::Http,
        url: Some(url.to_string()),
        headers: BTreeMap::new(),
        timeout_seconds,
        workspace_routing: false,
        allow_insecure_http,
        scope_tags: Vec::new(),
        allow_unscoped: false,
        mental_model_ids: Vec::new(),
        command: None,
        args: Vec::new(),
        env: BTreeMap::new(),
    })
}

fn validate_url(project: &str, backend: &str, value: &str) -> Result<Url, String> {
    let parsed = Url::parse(value)
        .map_err(|_| format!("Backend {project:?}/{backend:?} url must be an HTTP(S) URL"))?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err(format!(
            "Backend {project:?}/{backend:?} url must be an HTTP(S) URL"
        ));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(format!(
            "Backend {project:?}/{backend:?} url must not contain credentials"
        ));
    }
    Ok(parsed)
}

fn parse_timeout(
    project: &str,
    backend: &str,
    value: Option<&Value>,
) -> Result<Option<f64>, String> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Some(timeout) = value
        .as_float()
        .or_else(|| value.as_integer().map(|value| value as f64))
    else {
        return Err(format!(
            "Backend {project:?}/{backend:?} timeout_seconds must be a number"
        ));
    };
    if !timeout.is_finite() || !(1.0..=MAX_FORWARD_TIMEOUT_S).contains(&timeout) {
        return Err(format!(
            "Backend {project:?}/{backend:?} timeout_seconds must be between 1 and {MAX_FORWARD_TIMEOUT_S}"
        ));
    }
    Ok(Some(timeout))
}

fn parse_headers(
    project: &str,
    backend: &str,
    value: Option<&Value>,
) -> Result<BTreeMap<String, String>, String> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let table = value.as_table().ok_or_else(|| {
        format!("Backend {project:?}/{backend:?} headers must be a string-to-string table")
    })?;
    let mut headers = BTreeMap::new();
    for (key, value) in table {
        let value = value.as_str().ok_or_else(|| {
            format!("Backend {project:?}/{backend:?} headers must be a string-to-string table")
        })?;
        headers.insert(key.clone(), value.to_string());
    }
    Ok(headers)
}

fn optional_bool(
    project: &str,
    backend: &str,
    settings: &toml::Table,
    key: &str,
) -> Result<Option<bool>, String> {
    match settings.get(key) {
        Some(value) => value
            .as_bool()
            .map(Some)
            .ok_or_else(|| format!("Backend {project:?}/{backend:?} {key} must be a boolean")),
        None => Ok(None),
    }
}

fn parse_string_list(
    project: &str,
    backend: &str,
    value: Option<&Value>,
) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    if let Some(single) = value.as_str() {
        return Ok(vec![single.to_string()]);
    }
    let array = value.as_array().ok_or_else(|| {
        format!("Backend {project:?}/{backend:?} value must be a string or string array")
    })?;
    let mut values = Vec::new();
    for item in array {
        let item = item.as_str().ok_or_else(|| {
            format!("Backend {project:?}/{backend:?} value must be a string or string array")
        })?;
        values.push(item.to_string());
    }
    Ok(values)
}

/// Scope/model-id lists are trimmed and must not contain blanks. Arguments and
/// environment values keep their exact bytes.
fn parse_scope_list(
    project: &str,
    backend: &str,
    value: Option<&Value>,
) -> Result<Vec<String>, String> {
    let values = parse_string_list(project, backend, value)?;
    let mut scoped = Vec::new();
    for value in values {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return Err(format!(
                "Backend {project:?}/{backend:?} scope values must be non-empty strings"
            ));
        }
        scoped.push(trimmed.to_string());
    }
    Ok(scoped)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn repo_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
    }

    fn parse(contents: &str) -> Result<Registry, String> {
        Registry::parse_str(contents)
    }

    #[test]
    fn runtime_example_parses_with_expected_backends() {
        let path = repo_root().join("docs/runtime-instances.toml.example");
        let registry = Registry::load(&path).expect("runtime example parses");
        let project = registry.get("my-project").expect("my-project");
        let keys: Vec<&str> = project
            .backends
            .iter()
            .map(|(key, _)| key.as_str())
            .collect();
        assert_eq!(keys, vec!["docs", "issues", "hindsight", "code", "serena"]);
        assert_eq!(project.backend("code").unwrap().timeout_seconds, 180.0);
        assert!(project.backend("serena").unwrap().workspace_routing);
        assert_eq!(
            project.backend("hindsight").unwrap().scope_tags,
            vec!["my-project"]
        );
    }

    #[test]
    fn native_example_parses_stdio_serena_for_config_compatibility() {
        let path = repo_root().join("docs/native-instances.toml.example");
        let registry = Registry::load(&path).expect("native example parses");
        let serena = registry
            .get("my-project")
            .and_then(|project| project.backend("serena"))
            .expect("serena");
        assert_eq!(serena.kind, BackendKind::Stdio);
        assert!(!serena.workspace_routing);
        assert_eq!(
            serena.args.last().map(String::as_str),
            Some("/path/to/my-project")
        );
    }

    #[test]
    fn preserves_backend_order() {
        let registry = parse(
            r#"
[instances.demo.backends.zeta]
kind = "http"
url = "http://127.0.0.1:1/mcp"
[instances.demo.backends.alpha]
kind = "http"
url = "http://127.0.0.1:2/mcp"
"#,
        )
        .unwrap();
        let keys: Vec<&str> = registry.projects[0]
            .1
            .backends
            .iter()
            .map(|(key, _)| key.as_str())
            .collect();
        assert_eq!(keys, vec!["zeta", "alpha"]);
    }

    #[test]
    fn legacy_endpoint_uses_host_and_default_timeout() {
        let registry = parse(
            r#"
[instances.kubernaut]
endpoint = "http://127.0.0.1:9001/mcp/kubernaut"
"#,
        )
        .unwrap();
        let project = registry.get("kubernaut").unwrap();
        assert_eq!(project.backends.len(), 1);
        let (key, spec) = &project.backends[0];
        assert_eq!(key, "host");
        assert_eq!(spec.timeout_seconds, 60.0);
    }

    #[test]
    fn rejection_matrix() {
        let cases = [
            ("", "non-empty [instances]"),
            ("[instances.\"a/b\".backends.x]\nkind=\"http\"\nurl=\"http://127.0.0.1/mcp\"\n", "Invalid instance route"),
            ("[instances.demo]\nendpoint=\"http://127.0.0.1/mcp\"\n[instances.demo.backends.x]\nkind=\"http\"\nurl=\"http://127.0.0.1/mcp\"\n", "both endpoint and backends"),
            ("[instances.demo.backends.x]\nkind=\"grpc\"\n", "kind must be"),
            ("[instances.demo.backends.x]\nkind=\"http\"\nurl=\"ftp://example.com/x\"\n", "HTTP(S) URL"),
            ("[instances.demo.backends.x]\nkind=\"http\"\nurl=\"http://user:pass@example.com/x\"\n", "must not contain credentials"),
            ("[instances.demo.backends.hindsight]\nkind=\"http\"\nurl=\"http://127.0.0.1:8888/mcp/other\"\nallow_unscoped=true\n", "/mcp/shared-memory"),
            ("[instances.demo.backends.hindsight]\nkind=\"http\"\nurl=\"http://example.com/mcp/hindsight\"\nallow_unscoped=true\n", "must use HTTPS"),
            ("[instances.demo.backends.hindsight]\nkind=\"http\"\nurl=\"http://127.0.0.1:8888/mcp/hindsight\"\nallow_unscoped=false\n", "requires scope_tags"),
            ("[instances.demo.backends.hindsight]\nkind=\"stdio\"\ncommand=\"/opt/hindsight\"\n", "must use an HTTP Hindsight bank endpoint"),
            ("[instances.demo.backends.hindsight]\nkind=\"http\"\nurl=\"http://127.0.0.1:8888/mcp/hindsight\"\nscope_tags=[\"  \"]\n", "scope values must be non-empty"),
            ("[instances.demo.backends.hindsight]\nkind=\"http\"\nurl=\"http://127.0.0.1:8888/mcp/hindsight\"\nmental_model_ids=[\"\"]\nallow_unscoped=true\n", "scope values must be non-empty"),
            ("[instances.demo.backends.serena]\nkind=\"stdio\"\ncommand=\"/opt/serena\"\nworkspace_routing=true\n", "workspace_routing=true requires an HTTP Serena"),
            ("[instances.demo.backends.docs]\nkind=\"http\"\nurl=\"http://127.0.0.1/mcp\"\nheaders={ Authorization = 1 }\n", "headers must be a string-to-string table"),
            ("[instances.demo.backends.docs]\nkind=\"http\"\nurl=\"http://127.0.0.1/mcp\"\ntimeout_seconds=0\n", "timeout_seconds must be between"),
        ];
        for (contents, expected) in cases {
            let error = parse(contents).expect_err(contents);
            assert!(
                error.contains(expected),
                "expected {expected:?} in {error:?} for {contents:?}"
            );
        }
    }
}

//! Deployment configuration for the native docs ingestion runtime (issue #242).
//!
//! The runtime consumes the deployment-local `projects.toml` subset that the
//! Python CocoIndex flow already uses: `[defaults]` plus
//! `[projects.<name>]`/`[[projects.<name>.sources]]`. Only docs sources are
//! loaded; code, issue, and transcript configuration belongs to other slices.
//!
//! Parsing mirrors `src/engram/project_config.py` where it affects persisted
//! identity: environment overrides, `~`/environment expansion, roots resolved
//! against the config file, effective branch-qualified source tags, the
//! `-docs` bank fallback, and the source-tag/bank identifier shapes.

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

pub const DEFAULT_HINDSIGHT_URL: &str = "http://localhost:8888";
pub const DEFAULT_PG_DSN: &str = "postgresql://hindsight:hindsight@localhost:5432/hindsight";
pub const DEFAULT_CONFIG_PATH: &str = "~/.engram/projects.toml";
pub const DEFAULT_SCAN_INTERVAL_SECS: u64 = 300;
pub const MIN_SCAN_INTERVAL_SECS: u64 = 1;
pub const MAX_SCAN_INTERVAL_SECS: u64 = 86_400;
pub const DEFAULT_CONCURRENCY: usize = 1;
pub const MAX_CONCURRENCY: usize = 8;

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("cannot read project config {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid project config {path}: {source}")]
    Parse {
        path: PathBuf,
        #[source]
        source: toml::de::Error,
    },
    #[error("invalid runtime configuration: {0}")]
    Invalid(String),
    #[error("project {0:?} is not defined in the project config")]
    UnknownProject(String),
}

/// One configured docs root and the bank its memories belong to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocsSource {
    pub project: String,
    /// Effective tag after applying `branch` (`repo@release-v1.2`, ...).
    pub tag: String,
    pub root: PathBuf,
    pub bank_id: String,
    pub docs_include: Vec<String>,
    pub docs_exclude: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfig {
    pub config_path: PathBuf,
    pub hindsight_url: String,
    pub database_url: String,
    pub scan_interval: Duration,
    pub concurrency: usize,
    pub allow_empty_root: bool,
    pub sources: Vec<DocsSource>,
}

impl RuntimeConfig {
    /// Load the runtime subset for the selected projects.
    ///
    /// `config_path` is optional; when absent `ENGRAM_PROJECTS_CONFIG` and then
    /// `~/.engram/projects.toml` are used.
    pub fn load(config_path: Option<&Path>, projects: &[String]) -> Result<Self, ConfigError> {
        Self::load_with_env(config_path, projects, &|name| std::env::var(name).ok())
    }

    /// Testable loader with an injected environment lookup.
    pub fn load_with_env(
        config_path: Option<&Path>,
        projects: &[String],
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Self, ConfigError> {
        if projects.is_empty() {
            return Err(ConfigError::Invalid(
                "select at least one project with --project".to_string(),
            ));
        }
        let path = resolve_config_path(config_path, env)?;
        let raw = std::fs::read_to_string(&path).map_err(|source| ConfigError::Read {
            path: path.clone(),
            source,
        })?;
        let document: ConfigDocument =
            toml::from_str(&raw).map_err(|source| ConfigError::Parse {
                path: path.clone(),
                source,
            })?;

        let base = path.parent().unwrap_or_else(|| Path::new("."));
        let env_hindsight = env("HINDSIGHT_URL").filter(|value| !value.trim().is_empty());
        let env_database = env("COCOINDEX_PG_URL").filter(|value| !value.trim().is_empty());
        let mut sources = Vec::new();
        let mut hindsight_url: Option<String> = None;
        let mut database_url: Option<String> = None;
        let mut seen_projects = HashSet::new();
        let mut seen_bank_tags: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();

        for project in projects {
            if !seen_projects.insert(project.clone()) {
                continue;
            }
            validate_project_name(project)?;
            let table = document
                .projects
                .get(project)
                .ok_or_else(|| ConfigError::UnknownProject(project.clone()))?;

            let project_hindsight = table
                .hindsight_url
                .clone()
                .or_else(|| document.defaults.hindsight_url.clone())
                .unwrap_or_else(|| DEFAULT_HINDSIGHT_URL.to_string());
            let project_dsn = table
                .pg_dsn
                .clone()
                .or_else(|| document.defaults.pg_dsn.clone())
                .unwrap_or_else(|| DEFAULT_PG_DSN.to_string());
            if env_hindsight.is_none() {
                merge_runtime_value(
                    &mut hindsight_url,
                    project_hindsight,
                    "hindsight_url",
                    project,
                )?;
            }
            if env_database.is_none() {
                merge_runtime_value(&mut database_url, project_dsn, "pg_dsn", project)?;
            }
            let mut seen_tags = HashSet::new();
            let mut project_sources = Vec::new();
            for (index, source) in table.sources.iter().enumerate() {
                let tag = source
                    .tag
                    .as_deref()
                    .or(source.name.as_deref())
                    .ok_or_else(|| {
                        ConfigError::Invalid(format!(
                            "project {project:?} source {index} must define tag or name"
                        ))
                    })?;
                validate_source_tag(tag, project, index)?;
                if source.docs_include.is_empty() && source.code_include.is_empty() {
                    return Err(ConfigError::Invalid(format!(
                        "project {project:?} source {tag:?} must configure docs or code patterns"
                    )));
                }
                if source.docs_include.is_empty() {
                    // Code-only sources are owned by zvec-grep, not this runtime.
                    continue;
                }
                let format = source
                    .document_format
                    .as_deref()
                    .or(source.docs_format.as_deref())
                    .unwrap_or("markdown")
                    .trim()
                    .to_ascii_lowercase();
                match format.as_str() {
                    "markdown" | "md" => {}
                    "pdf" => {
                        return Err(ConfigError::Invalid(format!(
                            "project {project:?} source {tag:?} uses document_format = \"pdf\"; \
                             PDF extraction is not implemented in the Rust runtime yet"
                        )))
                    }
                    other => {
                        return Err(ConfigError::Invalid(format!(
                            "project {project:?} source {tag:?} document_format must be \
                             'markdown' or 'pdf', got {other:?}"
                        )))
                    }
                }
                let root = source
                    .root
                    .as_deref()
                    .map(str::trim)
                    .filter(|root| !root.is_empty())
                    .ok_or_else(|| {
                        ConfigError::Invalid(format!(
                            "project {project:?} source {tag:?} root must be a non-empty string"
                        ))
                    })?;
                crate::scan::validate_patterns(&source.docs_include, &source.docs_exclude)
                    .map_err(|error| {
                        ConfigError::Invalid(format!(
                            "project {project:?} source {tag:?} has invalid docs patterns: {error}"
                        ))
                    })?;
                let effective_tag = effective_source_tag(tag, source.branch.as_deref());
                if !seen_tags.insert(effective_tag.clone()) {
                    return Err(ConfigError::Invalid(format!(
                        "project {project:?} has duplicate source tag {effective_tag:?}"
                    )));
                }
                let bank_id = resolve_docs_bank(project, table)?;
                let identity = format!("{project}/{effective_tag}");
                let same_bank = seen_bank_tags.entry(bank_id.clone()).or_default();
                if let Some((existing_tag, previous)) = same_bank
                    .iter()
                    .find(|(existing_tag, _)| tags_ambiguous(existing_tag, &effective_tag))
                {
                    return Err(ConfigError::Invalid(format!(
                        "sources {previous:?} and {identity:?} have ambiguous tags \
                         {existing_tag:?} and {effective_tag:?} in bank {bank_id:?}; both '--' \
                         and path separators are used in Hindsight document ids, so they can \
                         collide"
                    )));
                }
                same_bank.push((effective_tag.clone(), identity));
                project_sources.push(DocsSource {
                    project: project.clone(),
                    tag: effective_tag,
                    root: resolve_root(root, base, env)?,
                    bank_id,
                    docs_include: source.docs_include.clone(),
                    docs_exclude: source.docs_exclude.clone(),
                });
            }
            if project_sources.is_empty() {
                return Err(ConfigError::Invalid(format!(
                    "project {project:?} has no docs sources with docs_include patterns"
                )));
            }
            sources.extend(project_sources);
        }

        let scan_interval_secs = document
            .defaults
            .scan_interval_seconds
            .unwrap_or(DEFAULT_SCAN_INTERVAL_SECS);
        if !(MIN_SCAN_INTERVAL_SECS..=MAX_SCAN_INTERVAL_SECS).contains(&scan_interval_secs) {
            return Err(ConfigError::Invalid(format!(
                "scan_interval_seconds must be between {MIN_SCAN_INTERVAL_SECS} and \
                 {MAX_SCAN_INTERVAL_SECS}"
            )));
        }
        let concurrency = document.defaults.concurrency.unwrap_or(DEFAULT_CONCURRENCY);
        if concurrency == 0 || concurrency > MAX_CONCURRENCY {
            return Err(ConfigError::Invalid(format!(
                "concurrency must be between 1 and {MAX_CONCURRENCY}"
            )));
        }

        Ok(Self {
            config_path: path,
            hindsight_url: env_hindsight
                .unwrap_or_else(|| hindsight_url.unwrap_or_else(|| DEFAULT_HINDSIGHT_URL.into())),
            database_url: env_database
                .unwrap_or_else(|| database_url.unwrap_or_else(|| DEFAULT_PG_DSN.into())),
            scan_interval: Duration::from_secs(scan_interval_secs),
            concurrency,
            allow_empty_root: document.defaults.allow_empty_root.unwrap_or(false),
            sources,
        })
    }

    pub fn with_scan_interval(mut self, interval: Duration) -> Result<Self, ConfigError> {
        let seconds = interval.as_secs();
        if !(MIN_SCAN_INTERVAL_SECS..=MAX_SCAN_INTERVAL_SECS).contains(&seconds) {
            return Err(ConfigError::Invalid(format!(
                "scan interval must be between {MIN_SCAN_INTERVAL_SECS} and \
                 {MAX_SCAN_INTERVAL_SECS} seconds"
            )));
        }
        self.scan_interval = interval;
        Ok(self)
    }

    pub fn with_concurrency(mut self, concurrency: usize) -> Result<Self, ConfigError> {
        if concurrency == 0 || concurrency > MAX_CONCURRENCY {
            return Err(ConfigError::Invalid(format!(
                "concurrency must be between 1 and {MAX_CONCURRENCY}"
            )));
        }
        self.concurrency = concurrency;
        Ok(self)
    }

    pub fn with_allow_empty_root(mut self, allow: bool) -> Self {
        self.allow_empty_root = allow;
        self
    }

    /// Distinct bank ids in first-seen order for the worker supervisor.
    pub fn bank_ids(&self) -> Vec<String> {
        let mut banks = Vec::new();
        for source in &self.sources {
            if !banks.contains(&source.bank_id) {
                banks.push(source.bank_id.clone());
            }
        }
        banks
    }
}

/// `document_base_id` joins the tag and the path with `--`, and path
/// separators become `--` too. A tag that extends another with one or more
/// hyphens can therefore produce the same id for a different path
/// (repo + -intro vs repo- + intro, or repo + docs/intro vs repo--docs + intro).
fn tags_ambiguous(left: &str, right: &str) -> bool {
    if left == right {
        return true;
    }
    let (shorter, longer) = if left.len() < right.len() {
        (left, right)
    } else {
        (right, left)
    };
    longer
        .strip_prefix(shorter)
        .is_some_and(|suffix| !suffix.is_empty() && (suffix.starts_with("--") || suffix == "-"))
}

fn merge_runtime_value(
    slot: &mut Option<String>,
    value: String,
    field: &str,
    project: &str,
) -> Result<(), ConfigError> {
    match slot {
        Some(existing) if *existing != value => Err(ConfigError::Invalid(format!(
            "selected projects disagree on {field} (project {project:?}); set it once in \
             [defaults] or run one project at a time"
        ))),
        Some(_) => Ok(()),
        None => {
            *slot = Some(value);
            Ok(())
        }
    }
}

/// `effective_source_tag` from `project_config.py`: a branch-qualified source
/// must not collide with the same checkout's main-branch document ids.
pub fn effective_source_tag(tag: &str, branch: Option<&str>) -> String {
    let branch = branch.unwrap_or("").trim();
    if branch.is_empty() || branch == "main" || branch == "default" {
        return tag.to_string();
    }
    if let Some(release) = branch.strip_prefix("release/") {
        return format!("{tag}@release-{release}");
    }
    if is_version_branch(branch) {
        return format!("{tag}@release-{branch}");
    }
    let safe: String = branch
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-') {
                character
            } else {
                '-'
            }
        })
        .collect();
    let safe = safe.trim_matches('-');
    format!(
        "{tag}@branch-{}",
        if safe.is_empty() { "unknown" } else { safe }
    )
}

fn is_version_branch(branch: &str) -> bool {
    let mut characters = branch.chars();
    matches!(characters.next(), Some('v'))
        && characters
            .as_str()
            .split_once('.')
            .is_some_and(|(major, minor)| {
                !major.is_empty()
                    && !minor.is_empty()
                    && major.chars().all(|character| character.is_ascii_digit())
                    && minor.chars().all(|character| character.is_ascii_digit())
            })
}

fn resolve_docs_bank(project: &str, table: &ProjectTable) -> Result<String, ConfigError> {
    if let Some(explicit) = table.docs_bank.as_deref() {
        let explicit = explicit.trim();
        validate_bank(explicit, project)?;
        return Ok(explicit.to_string());
    }
    let matches: Vec<&String> = table
        .banks
        .iter()
        .filter(|bank| bank.ends_with("-docs"))
        .collect();
    match matches.as_slice() {
        [] => Err(ConfigError::Invalid(format!(
            "project {project:?} has docs sources but no docs_bank and no banks entry ending in -docs"
        ))),
        [bank] => {
            validate_bank(bank, project)?;
            Ok((*bank).clone())
        }
        _ => Err(ConfigError::Invalid(format!(
            "project {project:?} has multiple -docs banks; set docs_bank explicitly"
        ))),
    }
}

fn validate_project_name(project: &str) -> Result<(), ConfigError> {
    let mut characters = project.chars();
    let valid_start = characters
        .next()
        .is_some_and(|character| character.is_ascii_alphanumeric());
    let valid_rest = characters
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-'));
    if !valid_start || !valid_rest {
        return Err(ConfigError::Invalid(format!(
            "invalid project name {project:?}"
        )));
    }
    Ok(())
}

fn validate_source_tag(tag: &str, project: &str, index: usize) -> Result<(), ConfigError> {
    let mut characters = tag.chars();
    let valid_start = characters
        .next()
        .is_some_and(|character| character.is_ascii_alphanumeric());
    let valid_rest = characters.all(|character| {
        character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '@' | '-')
    });
    if !valid_start || !valid_rest {
        return Err(ConfigError::Invalid(format!(
            "project {project:?} source {index} tag must match \
             ^[A-Za-z0-9][A-Za-z0-9._@-]*$, got {tag:?}"
        )));
    }
    Ok(())
}

fn validate_bank(bank: &str, project: &str) -> Result<(), ConfigError> {
    let mut characters = bank.chars();
    let valid_start = characters
        .next()
        .is_some_and(|character| character.is_ascii_alphanumeric());
    let valid_rest = characters
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-'));
    let valid = !bank.is_empty() && bank.len() <= 128 && valid_start && valid_rest;
    if !valid {
        return Err(ConfigError::Invalid(format!(
            "project {project:?} docs bank {bank:?} is not path-segment safe"
        )));
    }
    Ok(())
}

fn resolve_config_path(
    config_path: Option<&Path>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<PathBuf, ConfigError> {
    let expanded = match config_path {
        Some(path) => match path.to_str() {
            Some(text) => expand_path(text, env),
            None => path.to_path_buf(),
        },
        None => {
            let selected =
                env("ENGRAM_PROJECTS_CONFIG").unwrap_or_else(|| DEFAULT_CONFIG_PATH.to_string());
            expand_path(&selected, env)
        }
    };
    Ok(if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir()
            .map_err(|source| ConfigError::Read {
                path: expanded.clone(),
                source,
            })?
            .join(expanded)
    })
}

fn resolve_root(
    raw: &str,
    config_dir: &Path,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<PathBuf, ConfigError> {
    let expanded = expand_path(raw.trim(), env);
    if expanded.as_os_str().is_empty() {
        return Err(ConfigError::Invalid(
            "a source root expands to an empty path".to_string(),
        ));
    }
    Ok(if expanded.is_absolute() {
        expanded
    } else {
        config_dir.join(expanded)
    })
}

fn expand_path(raw: &str, env: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    let expanded_vars = expand_vars(raw, env);
    if expanded_vars == "~" {
        if let Some(home) = env("HOME") {
            return PathBuf::from(home);
        }
    }
    if let Some(rest) = expanded_vars.strip_prefix("~/") {
        if let Some(home) = env("HOME") {
            return PathBuf::from(home).join(rest.trim_start_matches('/'));
        }
    }
    PathBuf::from(expanded_vars)
}

fn expand_vars(raw: &str, env: &dyn Fn(&str) -> Option<String>) -> String {
    let mut output = String::with_capacity(raw.len());
    let mut characters = raw.char_indices().peekable();
    while let Some((_, character)) = characters.next() {
        if character != '$' {
            output.push(character);
            continue;
        }
        let Some((_, next)) = characters.peek().copied() else {
            output.push('$');
            continue;
        };
        if next == '{' {
            characters.next();
            let mut name = String::new();
            let mut closed = false;
            for (_, value) in characters.by_ref() {
                if value == '}' {
                    closed = true;
                    break;
                }
                name.push(value);
            }
            if closed {
                if let Some(value) = env(&name) {
                    output.push_str(&value);
                } else {
                    output.push_str("${");
                    output.push_str(&name);
                    output.push('}');
                }
                continue;
            }
            output.push_str("${");
            output.push_str(&name);
            continue;
        }
        if next.is_ascii_alphabetic() || next == '_' {
            let mut name = String::new();
            while let Some((_, value)) = characters.peek().copied() {
                if value.is_ascii_alphanumeric() || value == '_' {
                    name.push(value);
                    characters.next();
                } else {
                    break;
                }
            }
            if let Some(value) = env(&name) {
                output.push_str(&value);
            } else {
                output.push('$');
                output.push_str(&name);
            }
            continue;
        }
        output.push('$');
    }
    output
}

#[derive(Debug, Default, Deserialize)]
struct ConfigDocument {
    #[serde(default)]
    defaults: DefaultsTable,
    #[serde(default)]
    projects: BTreeMap<String, ProjectTable>,
}

#[derive(Debug, Default, Deserialize)]
struct DefaultsTable {
    hindsight_url: Option<String>,
    pg_dsn: Option<String>,
    scan_interval_seconds: Option<u64>,
    concurrency: Option<usize>,
    allow_empty_root: Option<bool>,
}

#[derive(Debug, Default, Deserialize)]
struct ProjectTable {
    #[serde(default)]
    docs_bank: Option<String>,
    #[serde(default, deserialize_with = "one_or_many")]
    banks: Vec<String>,
    hindsight_url: Option<String>,
    pg_dsn: Option<String>,
    #[serde(default)]
    sources: Vec<SourceTable>,
}

#[derive(Debug, Default, Deserialize)]
struct SourceTable {
    tag: Option<String>,
    name: Option<String>,
    root: Option<String>,
    branch: Option<String>,
    #[serde(default, deserialize_with = "one_or_many")]
    docs_include: Vec<String>,
    #[serde(default, deserialize_with = "one_or_many")]
    docs_exclude: Vec<String>,
    #[serde(default, deserialize_with = "one_or_many")]
    code_include: Vec<String>,
    document_format: Option<String>,
    docs_format: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum StringOrVec {
    One(String),
    Many(Vec<String>),
}

fn one_or_many<'de, D>(deserializer: D) -> Result<Vec<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = Option::<StringOrVec>::deserialize(deserializer)?;
    let values = match value {
        None => Vec::new(),
        Some(StringOrVec::One(value)) => {
            if value.trim().is_empty() {
                Vec::new()
            } else {
                vec![value.trim().to_string()]
            }
        }
        Some(StringOrVec::Many(values)) => values
            .into_iter()
            .map(|value| value.trim().to_string())
            .collect(),
    };
    if values.iter().any(|value| value.is_empty()) {
        return Err(serde::de::Error::custom(
            "pattern and bank arrays must contain non-empty strings",
        ));
    }
    Ok(values)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::ops::Deref;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    struct TempDir(PathBuf);

    impl Deref for TempDir {
        type Target = Path;

        fn deref(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn temp_dir() -> TempDir {
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "engram-ingest-config-{}-{unique}",
            std::process::id()
        ));
        std::fs::create_dir(&path).expect("temp dir");
        TempDir(path)
    }

    fn env_map(values: &[(&str, &str)]) -> HashMap<String, String> {
        values
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect()
    }

    fn write_config(body: &str) -> (TempDir, PathBuf) {
        let dir = temp_dir();
        let path = dir.join("projects.toml");
        std::fs::write(&path, body).expect("config");
        (dir, path)
    }

    fn load(
        path: &Path,
        projects: &[&str],
        env: &HashMap<String, String>,
    ) -> Result<RuntimeConfig, ConfigError> {
        let projects: Vec<String> = projects.iter().map(|value| value.to_string()).collect();
        let lookup = |key: &str| env.get(key).cloned();
        RuntimeConfig::load_with_env(Some(path), &projects, &lookup)
    }

    fn valid_config() -> &'static str {
        r#"
[defaults]
hindsight_url = "http://sink:8888"
pg_dsn = "postgresql://db/engram"
scan_interval_seconds = 60
concurrency = 2

[projects.example]
docs_bank = "example-docs"

[[projects.example.sources]]
tag = "repo"
root = "checkout"
docs_include = ["**/*.md"]
docs_exclude = ["**/vendor/**"]
"#
    }

    #[test]
    fn loads_docs_sources_and_resolves_relative_roots() {
        let (dir, path) = write_config(valid_config());
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        assert_eq!(config.hindsight_url, "http://sink:8888");
        assert_eq!(config.database_url, "postgresql://db/engram");
        assert_eq!(config.scan_interval, Duration::from_secs(60));
        assert_eq!(config.concurrency, 2);
        assert_eq!(config.sources.len(), 1);
        assert_eq!(config.sources[0].tag, "repo");
        assert_eq!(config.sources[0].root, dir.join("checkout"));
        assert_eq!(config.sources[0].bank_id, "example-docs");
        assert_eq!(config.bank_ids(), vec!["example-docs".to_string()]);
    }

    #[test]
    fn accepts_tag_name_and_string_patterns() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
name = "repo"
root = "/tmp/repo"
docs_include = "**/*.md"
docs_exclude = "**/vendor/**"
"#,
        );
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        assert_eq!(config.sources[0].tag, "repo");
        assert_eq!(config.sources[0].docs_include, vec!["**/*.md"]);
        assert_eq!(config.sources[0].docs_exclude, vec!["**/vendor/**"]);
    }

    #[test]
    fn falls_back_to_unique_docs_bank() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
banks = ["example-issues", "example-docs"]
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        assert_eq!(config.sources[0].bank_id, "example-docs");
    }

    #[test]
    fn rejects_ambiguous_or_missing_docs_bank() {
        let (_dir, ambiguous) = write_config(
            r#"
[projects.example]
banks = ["a-docs", "b-docs"]
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&ambiguous, &["example"], &HashMap::new()).expect_err("ambiguous");
        assert!(error.to_string().contains("multiple -docs banks"));

        let (_dir, missing) = write_config(
            r#"
[projects.example]
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&missing, &["example"], &HashMap::new()).expect_err("missing");
        assert!(error.to_string().contains("no docs_bank"));
    }

    #[test]
    fn environment_overrides_defaults() {
        let (_dir, path) = write_config(valid_config());
        let env = env_map(&[
            ("HINDSIGHT_URL", "http://env-sink:9999"),
            ("COCOINDEX_PG_URL", "postgresql://env/db"),
        ]);
        let config = load(&path, &["example"], &env).expect("load");
        assert_eq!(config.hindsight_url, "http://env-sink:9999");
        assert_eq!(config.database_url, "postgresql://env/db");
    }

    #[test]
    fn uses_config_from_environment_when_no_flag() {
        let (_dir, path) = write_config(valid_config());
        let env = env_map(&[("ENGRAM_PROJECTS_CONFIG", path.to_str().expect("utf8"))]);
        let lookup = |key: &str| env.get(key).cloned();
        let config =
            RuntimeConfig::load_with_env(None, &["example".to_string()], &lookup).expect("load");
        assert_eq!(config.config_path, path);
    }

    #[test]
    fn expands_tilde_and_variables_in_roots() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "$WORKSPACE/~/docs"
docs_include = ["**/*.md"]
"#,
        );
        let env = env_map(&[("WORKSPACE", "/workspace")]);
        let config = load(&path, &["example"], &env).expect("load");
        assert_eq!(config.sources[0].root, PathBuf::from("/workspace/~/docs"));

        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "~/docs"
docs_include = ["**/*.md"]
"#,
        );
        let env = env_map(&[("HOME", "/home/operator")]);
        let config = load(&path, &["example"], &env).expect("load");
        assert_eq!(config.sources[0].root, PathBuf::from("/home/operator/docs"));
    }

    #[test]
    fn applies_effective_branch_tags() {
        assert_eq!(effective_source_tag("repo", None), "repo");
        assert_eq!(effective_source_tag("repo", Some("main")), "repo");
        assert_eq!(
            effective_source_tag("repo", Some("release/v1.2")),
            "repo@release-v1.2"
        );
        assert_eq!(
            effective_source_tag("repo", Some("v2.0")),
            "repo@release-v2.0"
        );
        assert_eq!(
            effective_source_tag("repo", Some("feature/foo bar")),
            "repo@branch-feature-foo-bar"
        );
        assert_eq!(
            effective_source_tag("repo", Some("///")),
            "repo@branch-unknown"
        );
        assert!(!is_version_branch("v1.2.3"));
        assert!(!is_version_branch("version1.2"));
    }

    #[test]
    fn branch_sources_get_distinct_effective_tags() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
[[projects.example.sources]]
tag = "repo"
branch = "release/v1.2"
root = "/tmp/repo-v1.2"
docs_include = ["**/*.md"]
"#,
        );
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        assert_eq!(config.sources[0].tag, "repo");
        assert_eq!(config.sources[1].tag, "repo@release-v1.2");
    }

    #[test]
    fn rejects_duplicate_effective_tags() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[[projects.example.sources]]
tag = "repo"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&path, &["example"], &HashMap::new()).expect_err("duplicate");
        assert!(error.to_string().contains("duplicate source tag"));
    }

    #[test]
    fn rejects_pdf_and_unknown_formats() {
        let (_dir, pdf) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.pdf"]
document_format = "pdf"
"#,
        );
        let error = load(&pdf, &["example"], &HashMap::new()).expect_err("pdf");
        assert!(error.to_string().contains("PDF extraction"));

        let (_dir, other) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
document_format = "html"
"#,
        );
        let error = load(&other, &["example"], &HashMap::new()).expect_err("format");
        assert!(error.to_string().contains("document_format"));
    }

    #[test]
    fn rejects_unknown_or_empty_project_selection() {
        let (_dir, path) = write_config(valid_config());
        let error = load(&path, &["missing"], &HashMap::new()).expect_err("unknown");
        assert!(matches!(error, ConfigError::UnknownProject(_)));
        let error = load(&path, &[], &HashMap::new()).expect_err("empty");
        assert!(error.to_string().contains("--project"));
    }

    #[test]
    fn rejects_project_without_docs_sources() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
code_include = ["**/*.rs"]
"#,
        );
        let error = load(&path, &["example"], &HashMap::new()).expect_err("no docs");
        assert!(error.to_string().contains("no docs sources"));
    }

    #[test]
    fn rejects_source_without_any_patterns() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
"#,
        );
        let error = load(&path, &["example"], &HashMap::new()).expect_err("no patterns");
        assert!(error.to_string().contains("docs or code patterns"));
    }

    #[test]
    fn rejects_invalid_tags_banks_and_intervals() {
        let (_dir, tag) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "-bad"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&tag, &["example"], &HashMap::new()).expect_err("tag");
        assert!(error.to_string().contains("tag must match"));

        let (_dir, bank) = write_config(
            r#"
[projects.example]
docs_bank = "../escape"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&bank, &["example"], &HashMap::new()).expect_err("bank");
        assert!(error.to_string().contains("not path-segment safe"));

        let (_dir, interval) = write_config(
            r#"
[defaults]
scan_interval_seconds = 0
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&interval, &["example"], &HashMap::new()).expect_err("interval");
        assert!(error.to_string().contains("scan_interval_seconds"));

        let (_dir, concurrency) = write_config(
            r#"
[defaults]
concurrency = 9
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&concurrency, &["example"], &HashMap::new()).expect_err("concurrency");
        assert!(error.to_string().contains("concurrency"));
    }

    #[test]
    fn rejects_conflicting_project_runtime_values() {
        let (_dir, path) = write_config(
            r#"
[projects.a]
hindsight_url = "http://a"
docs_bank = "a-docs"
[[projects.a.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
hindsight_url = "http://b"
docs_bank = "b-docs"
[[projects.b.sources]]
tag = "repo"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&path, &["a", "b"], &HashMap::new()).expect_err("conflict");
        assert!(error.to_string().contains("disagree on hindsight_url"));
    }

    #[test]
    fn rejects_empty_or_whitespace_root() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "   "
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&path, &["example"], &HashMap::new()).expect_err("root");
        assert!(error
            .to_string()
            .contains("root must be a non-empty string"));
    }

    #[test]
    fn rejects_invalid_docs_patterns_at_load_time() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["["]
"#,
        );
        let error = load(&path, &["example"], &HashMap::new()).expect_err("pattern");
        assert!(error.to_string().contains("invalid docs patterns"));
    }

    #[test]
    fn reads_allow_empty_root() {
        let (_dir, path) = write_config(
            r#"
[defaults]
allow_empty_root = true
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "/tmp/repo"
docs_include = ["**/*.md"]
"#,
        );
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        assert!(config.allow_empty_root);
        let config = config.with_allow_empty_root(false);
        assert!(!config.allow_empty_root);
    }

    #[test]
    fn environment_override_skips_project_conflict() {
        let (_dir, path) = write_config(
            r#"
[projects.a]
hindsight_url = "http://a"
docs_bank = "shared-docs"
[[projects.a.sources]]
tag = "a"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
hindsight_url = "http://b"
docs_bank = "shared-docs"
[[projects.b.sources]]
tag = "b"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let env = env_map(&[("HINDSIGHT_URL", "http://override")]);
        let config = load(&path, &["a", "b"], &env).expect("override resolves conflict");
        assert_eq!(config.hindsight_url, "http://override");
    }

    #[test]
    fn rejects_bank_and_tag_collisions_across_projects() {
        let (_dir, path) = write_config(
            r#"
[projects.a]
docs_bank = "shared-docs"
[[projects.a.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
docs_bank = "shared-docs"
[[projects.b.sources]]
tag = "repo"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&path, &["a", "b"], &HashMap::new()).expect_err("collision");
        assert!(error.to_string().contains("ambiguous tags"));
    }

    #[test]
    fn rejects_root_that_expands_to_empty() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "$EMPTY"
docs_include = ["**/*.md"]
"#,
        );
        let env = env_map(&[("EMPTY", "")]);
        let error = load(&path, &["example"], &env).expect_err("empty expansion");
        assert!(error.to_string().contains("empty path"));
    }

    #[test]
    fn tilde_with_repeated_separator_stays_under_home() {
        let (_dir, path) = write_config(
            r#"
[projects.example]
docs_bank = "example-docs"
[[projects.example.sources]]
tag = "repo"
root = "~//docs"
docs_include = ["**/*.md"]
"#,
        );
        let env = env_map(&[("HOME", "/home/operator")]);
        let config = load(&path, &["example"], &env).expect("load");
        assert_eq!(config.sources[0].root, PathBuf::from("/home/operator/docs"));
    }

    #[test]
    fn rejects_ambiguous_tags_in_one_bank() {
        let (_dir, path) = write_config(
            r#"
[projects.a]
docs_bank = "shared-docs"
[[projects.a.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
docs_bank = "shared-docs"
[[projects.b.sources]]
tag = "repo--docs"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&path, &["a", "b"], &HashMap::new()).expect_err("ambiguous tags");
        assert!(error.to_string().contains("ambiguous tags"));
        assert!(error.to_string().contains("collide"));

        let (_dir, hyphen) = write_config(
            r#"
[projects.a]
docs_bank = "shared-docs"
[[projects.a.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
docs_bank = "shared-docs"
[[projects.b.sources]]
tag = "repo-"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let error = load(&hyphen, &["a", "b"], &HashMap::new()).expect_err("hyphen suffix");
        assert!(error.to_string().contains("ambiguous tags"));
    }

    #[test]
    fn allows_non_ambiguous_tag_prefixes_in_one_bank() {
        let (_dir, path) = write_config(
            r#"
[projects.a]
docs_bank = "shared-docs"
[[projects.a.sources]]
tag = "repo"
root = "/tmp/a"
docs_include = ["**/*.md"]
[projects.b]
docs_bank = "shared-docs"
[[projects.b.sources]]
tag = "repo2"
root = "/tmp/b"
docs_include = ["**/*.md"]
"#,
        );
        let config = load(&path, &["a", "b"], &HashMap::new()).expect("distinct tags");
        assert_eq!(config.sources.len(), 2);
    }

    #[test]
    fn constructor_overrides_validate_bounds() {
        let (_dir, path) = write_config(valid_config());
        let config = load(&path, &["example"], &HashMap::new()).expect("load");
        let config = config
            .with_scan_interval(Duration::from_secs(5))
            .expect("interval")
            .with_concurrency(4)
            .expect("concurrency");
        assert_eq!(config.scan_interval, Duration::from_secs(5));
        assert_eq!(config.concurrency, 4);
        assert!(config.clone().with_scan_interval(Duration::ZERO).is_err());
        assert!(config.with_concurrency(0).is_err());
    }

    #[test]
    fn reports_read_and_parse_errors() {
        let dir = temp_dir();
        let missing = dir.join("missing.toml");
        let error = RuntimeConfig::load(Some(&missing), &["example".to_string()])
            .expect_err("missing file");
        assert!(matches!(error, ConfigError::Read { .. }));

        let bad = dir.join("bad.toml");
        std::fs::write(&bad, "not = [valid").expect("write");
        let error = RuntimeConfig::load(Some(&bad), &["example".to_string()]).expect_err("parse");
        assert!(matches!(error, ConfigError::Parse { .. }));
    }
}

//! Transcript JSONL parsing and learning-window extraction (issue #245).
//!
//! This is the deterministic half of the native replacement for
//! `configured.process_transcript`: it turns an append-oriented JSONL file into
//! the same stable windows the Python flow emits. All I/O lives in
//! [`crate::transcript_ingest`]; the functions here are pure so the serializer
//! can be locked down by unit and parity fixtures.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;
use sha1::{Digest, Sha1};

/// Python `_learning_windows` default: signal plus two parsed messages each way.
pub const WINDOW_CONTEXT: usize = 2;
/// Python truncates every window line to 300 characters.
pub const WINDOW_LINE_CHARS: usize = 300;
/// Python truncates assistant text to 400 before window assembly.
pub const ASSISTANT_TEXT_CHARS: usize = 400;
pub const MAX_SIGNAL_TEXT_CHARS: usize = 2_000;
pub const MIN_INSTRUCTION_TEXT_CHARS: usize = 20;
/// Refuse to read an unbounded transcript file into memory.
pub const MAX_TRANSCRIPT_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Debug, thiserror::Error)]
pub enum TranscriptError {
    #[error("transcript root does not exist or is not a directory: {0}")]
    MissingRoot(PathBuf),
    #[error("cannot read transcript directory {path}: {source}")]
    ReadDir {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("transcript file is not valid UTF-8")]
    InvalidUtf8,
    #[error("transcript file exceeds the {MAX_TRANSCRIPT_BYTES}-byte limit")]
    TooLarge,
}

/// Gate that decides which user messages are correction/instruction signals.
/// Production wires the Python Haiku classifier here; the default is the
/// documented regex fallback so the contract stays deterministic and offline.
pub trait SignalDetector {
    fn is_correction(&self, text: &str) -> bool;
    fn is_instruction(&self, text: &str) -> bool;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct RegexSignalDetector;

impl SignalDetector for RegexSignalDetector {
    fn is_correction(&self, text: &str) -> bool {
        is_correction(text)
    }

    fn is_instruction(&self, text: &str) -> bool {
        is_instruction(text)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptFileRef {
    /// Root-relative POSIX path used as the ledger document path.
    pub relative_path: String,
    /// Absolute path used for reading.
    pub path: PathBuf,
    /// Python `Path(relative_path).stem`.
    pub transcript_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptDiscovery {
    pub files: Vec<TranscriptFileRef>,
    /// False when an entry or subtree could not be inspected. The caller must
    /// not treat absent paths as removed in that case.
    pub complete: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LearningWindow {
    pub text: String,
    /// Index of the signal in the parsed-message list (Python `raw_index`).
    pub signal_raw_index: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedMessage {
    role: String,
    text: String,
    correction: bool,
    instruction: bool,
    raw_index: usize,
}

pub fn is_system_boilerplate(text: &str) -> bool {
    const PREFIXES: [&str; 2] = [
        "Briefly inform the user about the task result and perform any follow-up actions",
        "The beginning of the above subagent result is already visible to the user.",
    ];
    let stripped = text.trim();
    if PREFIXES.iter().any(|prefix| stripped.starts_with(prefix)) {
        return true;
    }
    boilerplate_tag_regex().is_match(stripped)
}

fn boilerplate_tag_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(
            r"^\s*<(system_reminder|attached_files|system_notification|mcp_server_catalog|user_info)\b",
        )
        .expect("boilerplate regex")
    })
}

/// Python `CORRECTION_PATTERNS` from `configured.py`, kept verbatim. The
/// native path uses this deterministic gate; an external classifier can be
/// injected by callers that have one.
pub fn is_correction(text: &str) -> bool {
    let length = text.chars().count();
    if text.is_empty() || length > MAX_SIGNAL_TEXT_CHARS {
        return false;
    }
    if is_system_boilerplate(text) {
        return false;
    }
    correction_patterns()
        .iter()
        .any(|pattern| pattern.is_match(text))
}

/// Python `INSTRUCTION_PATTERNS` from `configured.py`.
pub fn is_instruction(text: &str) -> bool {
    let length = text.chars().count();
    if text.is_empty() || !(MIN_INSTRUCTION_TEXT_CHARS..=MAX_SIGNAL_TEXT_CHARS).contains(&length) {
        return false;
    }
    instruction_patterns()
        .iter()
        .any(|pattern| pattern.is_match(text))
}

fn correction_patterns() -> &'static [Regex] {
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        compile_all(&[
            r"\bno[,.]?\s+that'?s\s+(not|wrong|incorrect)",
            r"\bdon'?t\s+do\s+that",
            r"\bI\s+(said|meant)\s+",
            r"\bwrong\s+(file|path|dir|approach|method|function|model|endpoint)",
            r"\bthat\s+broke",
            r"\bundo\s+(that|this|it)",
            r"\bthat'?s\s+not\s+what\s+I",
            r"\byou\s+(shouldn'?t|should\s+not)\s+have",
            r"\bdo\s+not\s+use\b",
            r"\bwe\s+don'?t\s+use\b",
            r"\b(you'?re|you\s+are)\s+(still\s+)?not\s+(following|aligned)\b",
            r"\bnot\s+following\s+(the\s+)?(project'?s?\s+)?(methodology|convention|AGENTS\.md|CLAUDE\.md)\b",
            r"\byou\s+keep\s+making\s+the\s+same\s+mistake\b",
            r"\bmistak(?:e|ing)\b.{0,40}\bfor\b",
        ])
    })
}

fn instruction_patterns() -> &'static [Regex] {
    static PATTERNS: OnceLock<Vec<Regex>> = OnceLock::new();
    PATTERNS.get_or_init(|| {
        compile_all(&[
            r"\balways\s+(use|follow|run|start\s+with|ensure)",
            r"\bnever\s+(skip|push|commit|deploy|use)",
            r"\bmandatory\b",
            r"\bour\s+(workflow|process|methodology|convention|standard)",
            r"\bthe\s+rule\s+is\b",
            r"\bfor\s+this\s+(project|repo|team)\s+we\b",
            r"\bwe\s+(always|never|require|must)\b",
            r"\bbefore\s+(implementing|proceeding|starting\s+any)",
        ])
    })
}

fn compile_all(patterns: &[&str]) -> Vec<Regex> {
    patterns
        .iter()
        .map(|pattern| Regex::new(&format!("(?i){pattern}")).expect("correction pattern"))
        .collect()
}

fn user_query_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r"(?s)<user_query>\s*(.*?)\s*</user_query>").expect("query regex")
    })
}

/// Parse every blank/malformed line as absent, matching Python's per-line
/// `json.JSONDecodeError` skip. Invalid UTF-8 rejects the whole file like
/// Python `read_text()`; non-object JSON values are skipped defensively.
pub fn parse_messages(content: &[u8]) -> Result<Vec<Value>, TranscriptError> {
    let text = std::str::from_utf8(content).map_err(|_| TranscriptError::InvalidUtf8)?;
    Ok(text
        .lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(Value::is_object)
        .collect())
}

/// Hash of the parsed prefix a watermark covers. A changed prefix means the
/// file was rewritten or rotated in place, so the scan must restart from zero.
pub fn prefix_sha256(messages: &[Value], count: usize) -> crate::hashing::ContentHash {
    let mut bytes = Vec::new();
    for message in messages.iter().take(count) {
        let encoded = serde_json::to_vec(message).unwrap_or_default();
        bytes.extend_from_slice(&(encoded.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&encoded);
    }
    crate::hashing::ContentHash::of(&bytes)
}

pub fn transcript_id_for_path(relative_path: &str) -> String {
    Path::new(relative_path)
        .file_stem()
        .map(|stem| stem.to_string_lossy().to_string())
        .unwrap_or_default()
}

pub fn window_document_id(transcript_id: &str, window_text: &str) -> String {
    let digest = Sha1::digest(window_text.as_bytes());
    let hex = format!("{digest:x}");
    format!("transcript-{transcript_id}-w{}", &hex[..16])
}

/// Build the windows whose signal index is at or past `start_index`.
///
/// Context still comes from the complete parsed list so a signal at the
/// watermark boundary keeps its before-context, exactly like
/// `_learning_windows(messages, start_index=...)`.
pub fn learning_windows(messages: &[Value], start_index: usize) -> Vec<LearningWindow> {
    learning_windows_with_detector(messages, start_index, &RegexSignalDetector)
}

/// Like [`learning_windows`], but with an injectable correction/instruction
/// detector (production wires the Haiku classifier).
pub fn learning_windows_with_detector(
    messages: &[Value],
    start_index: usize,
    detector: &dyn SignalDetector,
) -> Vec<LearningWindow> {
    let parsed: Vec<ParsedMessage> = messages
        .iter()
        .enumerate()
        .filter_map(|(raw_index, message)| parse_message(raw_index, message, detector))
        .collect();
    let mut windows = Vec::new();
    for (signal, item) in parsed.iter().enumerate() {
        if !(item.correction || item.instruction) || item.raw_index < start_index {
            continue;
        }
        let start = signal.saturating_sub(WINDOW_CONTEXT);
        let end = (signal + WINDOW_CONTEXT + 1).min(parsed.len());
        let mut lines = Vec::with_capacity(end - start);
        for (index, context) in parsed.iter().enumerate().take(end).skip(start) {
            let marker = if index == signal {
                if context.correction {
                    "[CORRECTION] "
                } else {
                    "[INSTRUCTION] "
                }
            } else {
                ""
            };
            lines.push(format!(
                "{marker}{}: {}",
                title_case(&context.role),
                take_chars(&context.text, WINDOW_LINE_CHARS)
            ));
        }
        windows.push(LearningWindow {
            text: lines.join("\n\n"),
            signal_raw_index: item.raw_index,
        });
    }
    windows
}

fn parse_message(
    raw_index: usize,
    message: &Value,
    detector: &dyn SignalDetector,
) -> Option<ParsedMessage> {
    let role = message.get("role").and_then(Value::as_str).unwrap_or("");
    match role {
        "user" => {
            let text = transcript_user_text(message);
            if text.is_empty() {
                return None;
            }
            Some(ParsedMessage {
                role: role.to_string(),
                correction: detector.is_correction(&text),
                instruction: detector.is_instruction(&text),
                text,
                raw_index,
            })
        }
        "assistant" => {
            let text = transcript_assistant_text(message);
            if text.is_empty() {
                return None;
            }
            Some(ParsedMessage {
                role: role.to_string(),
                text: take_chars(&text, ASSISTANT_TEXT_CHARS),
                correction: false,
                instruction: false,
                raw_index,
            })
        }
        _ => None,
    }
}

fn transcript_user_text(message: &Value) -> String {
    let content = message
        .get("message")
        .and_then(|value| value.get("content"))
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));
    let raw = match content {
        Value::String(text) => text,
        Value::Array(blocks) => {
            let mut texts = Vec::new();
            for block in blocks {
                if block.get("type").and_then(Value::as_str) != Some("text") {
                    continue;
                }
                let text = python_str(block.get("text"));
                if text.starts_with("<external_links>") {
                    continue;
                }
                let extracted = user_query_regex()
                    .captures(&text)
                    .and_then(|captures| captures.get(1))
                    .map(|capture| capture.as_str().to_string())
                    .unwrap_or(text);
                texts.push(extracted);
            }
            texts.join("\n")
        }
        other => python_str(Some(&other)),
    };
    let stripped = raw.trim().to_string();
    if is_system_boilerplate(&stripped) {
        String::new()
    } else {
        stripped
    }
}

fn transcript_assistant_text(message: &Value) -> String {
    let content = message
        .get("message")
        .and_then(|value| value.get("content"))
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));
    match content {
        Value::String(text) => text,
        Value::Array(blocks) => {
            let texts: Vec<String> = blocks
                .iter()
                .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
                .map(|block| python_str(block.get("text")))
                .collect();
            texts.join("\n").trim().to_string()
        }
        other => python_str(Some(&other)),
    }
}

fn python_str(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(other) => other.to_string(),
    }
}

fn title_case(role: &str) -> String {
    let mut chars = role.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

fn take_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// Discover `{prefix}*/agent-transcripts/**/*.jsonl` files under `root`.
pub fn discover_transcripts(
    root: &Path,
    workspace_prefixes: &[String],
) -> Result<TranscriptDiscovery, TranscriptError> {
    if !root.is_dir() {
        return Err(TranscriptError::MissingRoot(root.to_path_buf()));
    }
    let mut files = Vec::new();
    let mut ancestors = std::collections::BTreeSet::new();
    let mut complete = true;
    let canonical_root = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    walk(
        root,
        root,
        workspace_prefixes,
        &mut files,
        &mut ancestors,
        &canonical_root,
        WalkStage::Root,
        &mut complete,
    )?;
    files.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
    Ok(TranscriptDiscovery { files, complete })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WalkStage {
    /// The configured root: only selected `{prefix}*` workspaces.
    Root,
    /// A selected workspace: only its `agent-transcripts` directory.
    Workspace,
    /// Inside `agent-transcripts`: every descendant `.jsonl` file.
    Transcripts,
}

#[allow(clippy::too_many_arguments)]
fn walk(
    root: &Path,
    directory: &Path,
    prefixes: &[String],
    files: &mut Vec<TranscriptFileRef>,
    ancestors: &mut std::collections::BTreeSet<PathBuf>,
    canonical_root: &Path,
    stage: WalkStage,
    complete: &mut bool,
) -> Result<(), TranscriptError> {
    let canonical = fs::canonicalize(directory).unwrap_or_else(|_| directory.to_path_buf());
    if !canonical.starts_with(canonical_root) || !ancestors.insert(canonical.clone()) {
        return Ok(());
    }
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(source) if stage == WalkStage::Root => {
            return Err(TranscriptError::ReadDir {
                path: directory.to_path_buf(),
                source,
            })
        }
        Err(_) => {
            *complete = false;
            ancestors.remove(&canonical);
            return Ok(());
        }
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => {
                *complete = false;
                continue;
            }
        };
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        let selected = match stage {
            WalkStage::Root => prefixes
                .iter()
                .any(|prefix| name.starts_with(prefix.as_str())),
            WalkStage::Workspace => name == "agent-transcripts",
            WalkStage::Transcripts => true,
        };
        if !selected {
            continue;
        }
        // Follow directory symlinks; a workspace root is commonly a symlink.
        let metadata = match fs::metadata(&path) {
            Ok(metadata) => metadata,
            Err(_) => {
                *complete = false;
                continue;
            }
        };
        if metadata.is_dir() {
            let next = match stage {
                WalkStage::Root => Some(WalkStage::Workspace),
                WalkStage::Workspace => Some(WalkStage::Transcripts),
                WalkStage::Transcripts => Some(WalkStage::Transcripts),
            };
            if let Some(next) = next {
                walk(
                    root,
                    &path,
                    prefixes,
                    files,
                    ancestors,
                    canonical_root,
                    next,
                    complete,
                )?;
            }
            continue;
        }
        if stage != WalkStage::Transcripts
            || !metadata.is_file()
            || !matches_transcript_path(root, &path, prefixes)
        {
            continue;
        }
        let canonical_file = match fs::canonicalize(&path) {
            Ok(canonical) => canonical,
            Err(_) => {
                *complete = false;
                continue;
            }
        };
        if !canonical_file.starts_with(canonical_root) {
            continue;
        }
        let relative_path = relative_posix(root, &path);
        files.push(TranscriptFileRef {
            transcript_id: transcript_id_for_path(&relative_path),
            relative_path,
            path: canonical_file,
        });
    }
    ancestors.remove(&canonical);
    Ok(())
}

fn matches_transcript_path(root: &Path, path: &Path, prefixes: &[String]) -> bool {
    let relative = relative_posix(root, path);
    let parts: Vec<&str> = relative.split('/').collect();
    if parts.len() < 3 || parts[1] != "agent-transcripts" {
        return false;
    }
    if !parts[parts.len() - 1].ends_with(".jsonl") {
        return false;
    }
    prefixes
        .iter()
        .any(|prefix| parts[0].starts_with(prefix.as_str()))
}

fn relative_posix(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .components()
        .map(|component| component.as_os_str().to_string_lossy().to_string())
        .collect::<Vec<_>>()
        .join("/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn user(text: &str) -> Value {
        json!({"role": "user", "message": {"content": text}})
    }

    fn assistant(text: &str) -> Value {
        json!({"role": "assistant", "message": {"content": text}})
    }

    #[test]
    fn extracts_user_query_and_drops_wrapper() {
        let message = json!({
            "role": "user",
            "message": {"content": [
                {"type": "text", "text": "<user_query>Always use tests before proceeding.</user_query>"},
                {"type": "image", "text": "ignored"}
            ]}
        });
        let text = transcript_user_text(&message);
        assert_eq!(text, "Always use tests before proceeding.");
        assert!(is_instruction(&text));
    }

    #[test]
    fn skips_external_links_and_boilerplate() {
        let links = json!({
            "role": "user",
            "message": {"content": [
                {"type": "text", "text": "<external_links>https://example.test</external_links>"},
                {"type": "text", "text": "Real question"}
            ]}
        });
        assert_eq!(transcript_user_text(&links), "Real question");
        let boilerplate = json!({
            "role": "user",
            "message": {"content": "The beginning of the above subagent result is already visible to the user."}
        });
        assert_eq!(transcript_user_text(&boilerplate), "");
    }

    #[test]
    fn builds_marked_windows_with_two_context_messages() {
        let messages = vec![
            user("first message"),
            assistant("assistant one"),
            user("always run the tests before proceeding"),
            assistant("assistant two"),
            user("fifth message"),
        ];
        let windows = learning_windows(&messages, 0);
        assert_eq!(windows.len(), 1);
        assert!(windows[0].text.starts_with("User: first message"));
        assert!(windows[0]
            .text
            .contains("[INSTRUCTION] User: always run the tests before proceeding"));
        assert!(windows[0].text.ends_with("User: fifth message"));
    }

    #[test]
    fn start_index_filters_signals_but_keeps_before_context() {
        let messages = vec![
            user("first message"),
            assistant("assistant one"),
            user("always run the tests before proceeding"),
            assistant("assistant two"),
            user("later message"),
        ];
        assert!(learning_windows(&messages, 3).is_empty());
        assert_eq!(learning_windows(&messages, 0).len(), 1);
    }

    #[test]
    fn stable_id_matches_python_sha1_contract() {
        let text = "User: always use tests\n\nAssistant: ok";
        assert_eq!(
            window_document_id("session", text),
            "transcript-session-w2dbe05c3360b3062"
        );
    }

    #[test]
    fn malformed_and_non_object_lines_are_skipped() {
        let content = b"{\"role\":\"user\"}\nnot json\n\n[1,2]\n{\"role\":\"assistant\"}\n";
        assert_eq!(parse_messages(content).unwrap().len(), 2);
        assert!(matches!(
            parse_messages(&[0xff, 0xfe]),
            Err(TranscriptError::InvalidUtf8)
        ));
    }

    struct AlwaysInstruction;

    impl SignalDetector for AlwaysInstruction {
        fn is_correction(&self, _text: &str) -> bool {
            false
        }

        fn is_instruction(&self, _text: &str) -> bool {
            true
        }
    }

    #[test]
    fn injectable_detector_controls_the_signal_gate() {
        let messages = vec![user("plain text that the regex gate ignores")];
        assert!(learning_windows(&messages, 0).is_empty());
        let windows = learning_windows_with_detector(&messages, 0, &AlwaysInstruction);
        assert_eq!(windows.len(), 1);
        assert!(windows[0].text.starts_with("[INSTRUCTION] User:"));
    }

    #[test]
    fn prefix_hash_tracks_append_and_detects_rewrite() {
        let original = vec![user("one"), assistant("two"), user("three")];
        let appended = vec![user("one"), assistant("two"), user("three"), user("four")];
        let rewritten = vec![user("one"), assistant("changed"), user("three")];
        assert_eq!(prefix_sha256(&original, 3), prefix_sha256(&appended, 3));
        assert_ne!(prefix_sha256(&original, 3), prefix_sha256(&rewritten, 3));
    }

    #[cfg(unix)]
    #[test]
    fn discovery_does_not_follow_symlinks_outside_the_root() {
        use std::os::unix::fs::symlink;

        let root =
            std::env::temp_dir().join(format!("engram-transcript-escape-{}", std::process::id()));
        let outside =
            std::env::temp_dir().join(format!("engram-transcript-outside-{}", std::process::id()));
        fs::create_dir_all(root.join("workspace-demo").join("agent-transcripts")).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("secret.jsonl"), "{}\n").unwrap();
        symlink(
            &outside,
            root.join("workspace-demo")
                .join("agent-transcripts")
                .join("escape"),
        )
        .unwrap();
        symlink(
            outside.join("secret.jsonl"),
            root.join("workspace-demo")
                .join("agent-transcripts")
                .join("secret.jsonl"),
        )
        .unwrap();
        let discovery = discover_transcripts(&root, &["workspace-".to_string()]).unwrap();
        assert!(discovery.files.is_empty(), "{discovery:?}");
        assert!(discovery.complete);
        fs::remove_dir_all(&root).ok();
        fs::remove_dir_all(&outside).ok();
    }

    #[test]
    fn discovery_requires_prefix_and_transcript_layout() {
        let root = std::env::temp_dir().join(format!("engram-transcript-{}", std::process::id()));
        let nested = root
            .join("workspace-demo")
            .join("agent-transcripts")
            .join("a");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join("session.jsonl"), "{}\n").unwrap();
        let ignored = root.join("other").join("agent-transcripts");
        fs::create_dir_all(&ignored).unwrap();
        fs::write(ignored.join("session.jsonl"), "{}\n").unwrap();
        let files = discover_transcripts(&root, &["workspace-".to_string()]).unwrap();
        assert!(files.complete);
        assert_eq!(files.files.len(), 1);
        assert_eq!(files.files[0].transcript_id, "session");
        assert_eq!(
            files.files[0].relative_path,
            "workspace-demo/agent-transcripts/a/session.jsonl"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn dangling_symlink_marks_the_scan_incomplete_without_aborting() {
        use std::os::unix::fs::symlink;

        let root =
            std::env::temp_dir().join(format!("engram-transcript-dangling-{}", std::process::id()));
        let transcripts = root.join("workspace-demo").join("agent-transcripts");
        fs::create_dir_all(&transcripts).unwrap();
        symlink(
            transcripts.join("missing"),
            transcripts.join("broken.jsonl"),
        )
        .unwrap();
        fs::write(transcripts.join("session.jsonl"), "{}\n").unwrap();
        let discovery = discover_transcripts(&root, &["workspace-".to_string()]).unwrap();
        assert!(!discovery.complete);
        assert_eq!(discovery.files.len(), 1);
        fs::remove_dir_all(&root).ok();
    }
}

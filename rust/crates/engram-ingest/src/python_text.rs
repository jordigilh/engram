//! Small ports of Python string semantics that affect stable output.
//!
//! `str.splitlines` and `str.strip` are used by the synthesis port; Rust's
//! `str::lines` and `str::trim` do not use exactly the same character sets.

/// Characters Python treats as line boundaries in `str.splitlines`.
fn is_line_boundary(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{0b}'
            | '\u{0c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// `str.splitlines()` with no `keepends`: no trailing empty entry for a
/// trailing boundary, and CRLF counts as one boundary.
pub fn splitlines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if !is_line_boundary(c) {
            current.push(c);
            continue;
        }
        lines.push(std::mem::take(&mut current));
        if c == '\r' && chars.peek() == Some(&'\n') {
            chars.next();
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

/// Python's whitespace set for `str.strip`: Unicode whitespace plus the
/// information separators U+001C..U+001F.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `str.lower()` (full Unicode lowercase mapping).
pub fn lower(text: &str) -> String {
    text.to_lowercase()
}

/// Python's `len(str)` for parity checks (code points, not bytes).
pub fn char_len(text: &str) -> usize {
    text.chars().count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_matches_python_boundaries() {
        assert_eq!(splitlines(""), Vec::<String>::new());
        assert_eq!(splitlines("a\nb"), vec!["a", "b"]);
        assert_eq!(splitlines("a\r\nb"), vec!["a", "b"]);
        assert_eq!(splitlines("a\rb"), vec!["a", "b"]);
        assert_eq!(splitlines("a\n\n"), vec!["a", ""]);
        assert_eq!(splitlines("a\u{2028}b\u{85}c"), vec!["a", "b", "c"]);
    }

    #[test]
    fn strip_covers_python_information_separators() {
        assert_eq!(strip("\u{1c} text \u{1f}"), "text");
        assert_eq!(strip("\u{2028}text\u{2029}"), "text");
    }

    #[test]
    fn char_len_counts_code_points() {
        assert_eq!(char_len("ae\u{301}"), 3);
    }
}

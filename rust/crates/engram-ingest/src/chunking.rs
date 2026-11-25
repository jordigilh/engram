//! Character-indexed ports of `src/engram/chunking.py` used by the docs flow.
//!
//! Python string offsets count code points, not bytes, so both splitters work
//! on `Vec<char>` to keep non-ASCII documents identical.

use crate::ids::stable_section_key;

/// Sliding-window split with newline preference and overlap.
///
/// This is a line-for-line port of `split_fixed_window`, including the
/// overshoot guard that prevents the almost-empty trailing chunk described in
/// the Python docstring.
pub fn split_fixed_window(text: &str, chunk_size: usize, chunk_overlap: usize) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= chunk_size {
        return vec![text.to_string()];
    }
    let mut chunks = Vec::new();
    let mut start = 0usize;
    while start < chars.len() {
        let mut end = start + chunk_size;
        if end < chars.len() {
            let break_point = (start..end).rev().find(|&index| chars[index] == '\n');
            if let Some(break_point) = break_point {
                if break_point > start + chunk_size / 2 {
                    end = break_point + 1;
                }
            }
        }
        let capped = end.min(chars.len());
        chunks.push(chars[start..capped].iter().collect());
        if end >= chars.len() {
            break;
        }
        start = end - chunk_overlap;
    }
    chunks
}

/// Split Markdown into `(key, text)` sections anchored at heading boundaries.
///
/// Mirrors `split_markdown_sections`:
/// - content before the first heading (or the whole document without headings)
///   gets `""`;
/// - the first heading is `""` only when there is no non-empty preamble;
/// - later headings are keyed by the 16-hex SHA-1 of their own heading line;
/// - oversized sections split into `{key}-part{n}`, or `part{n}` for an empty
///   key.
pub fn split_markdown_sections(
    content: &str,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Vec<(String, String)> {
    let chars: Vec<char> = content.chars().collect();
    let headings = scan_headings(&chars);
    if headings.is_empty() {
        return numbered_fixed_window(content, chunk_size, chunk_overlap);
    }

    let mut sections: Vec<(String, String)> = Vec::new();
    let preamble: String = chars[..headings[0].start].iter().collect();
    if !crate::python_text::strip(&preamble).is_empty() {
        sections.push((String::new(), preamble));
    }

    for (index, heading) in headings.iter().enumerate() {
        let end = headings
            .get(index + 1)
            .map(|next| next.start)
            .unwrap_or(chars.len());
        let body: String = chars[heading.start..end].iter().collect();
        let key = if sections.is_empty() {
            String::new()
        } else {
            stable_section_key(&heading.line)
        };
        sections.push((key, body));
    }

    let mut result = Vec::new();
    for (key, body) in sections {
        if crate::python_text::char_len(&body) <= chunk_size {
            result.push((key, body));
            continue;
        }
        for (part_index, part) in split_fixed_window(&body, chunk_size, chunk_overlap)
            .into_iter()
            .enumerate()
        {
            let part_key = if part_index == 0 {
                key.clone()
            } else if key.is_empty() {
                format!("part{part_index}")
            } else {
                format!("{key}-part{part_index}")
            };
            result.push((part_key, part));
        }
    }
    result
}

fn numbered_fixed_window(
    text: &str,
    chunk_size: usize,
    chunk_overlap: usize,
) -> Vec<(String, String)> {
    split_fixed_window(text, chunk_size, chunk_overlap)
        .into_iter()
        .enumerate()
        .map(|(index, chunk)| {
            let key = if index == 0 {
                String::new()
            } else {
                format!("chunk{index}")
            };
            (key, chunk)
        })
        .collect()
}

struct Heading {
    start: usize,
    line: String,
}

/// Equivalent of `re.compile(r"^(#{1,6})[ \t]+.*$", re.MULTILINE)`.
fn scan_headings(chars: &[char]) -> Vec<Heading> {
    let mut headings = Vec::new();
    let mut line_start = 0usize;
    while line_start <= chars.len() {
        let line_end = chars[line_start..]
            .iter()
            .position(|&c| c == '\n')
            .map(|offset| line_start + offset)
            .unwrap_or(chars.len());
        if let Some(line) = heading_line(chars, line_start, line_end) {
            headings.push(Heading {
                start: line_start,
                line,
            });
        }
        if line_end == chars.len() {
            break;
        }
        line_start = line_end + 1;
    }
    headings
}

fn heading_line(chars: &[char], start: usize, end: usize) -> Option<String> {
    let mut index = start;
    while index < end && chars[index] == '#' && index - start < 7 {
        index += 1;
    }
    let hashes = index - start;
    if hashes == 0 || hashes > 6 || index >= end {
        return None;
    }
    if chars[index] != ' ' && chars[index] != '\t' {
        return None;
    }
    Some(chars[start..end].iter().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_headingless_text_is_one_bare_chunk() {
        assert_eq!(
            split_markdown_sections("just some plain text", 800, 200),
            vec![(String::new(), "just some plain text".to_string())]
        );
    }

    #[test]
    fn long_headingless_text_uses_legacy_numbering() {
        let content = "line\n".repeat(500);
        let result = split_markdown_sections(&content, 800, 200);
        assert!(result.len() > 1);
        assert_eq!(result[0].0, "");
        assert_eq!(result[1].0, "chunk1");
    }

    #[test]
    fn single_leading_heading_is_bare_key() {
        let content = "# Hello\n\nSome content";
        assert_eq!(
            split_markdown_sections(content, 800, 200),
            vec![(String::new(), content.to_string())]
        );
    }

    #[test]
    fn headings_after_a_preamble_are_hashed_and_stable() {
        let content = "Intro\n\n# Title\n\nBody\n\n## A\n\nBody A\n";
        let result = split_markdown_sections(content, 800, 200);
        let keys: Vec<&str> = result.iter().map(|(key, _)| key.as_str()).collect();
        assert_eq!(keys[0], "");
        assert_eq!(keys[1], stable_section_key("# Title"));
        assert_eq!(keys[2], stable_section_key("## A"));
    }

    #[test]
    fn oversized_sections_are_scoped_parts() {
        let content = format!("## Big\n\n{}", "x\n".repeat(500));
        let result = split_markdown_sections(&content, 800, 200);
        assert_eq!(result[0].0, "");
        assert_eq!(result[1].0, "part1");
    }

    #[test]
    fn oversized_later_section_uses_the_heading_key() {
        let content = format!(
            "Preamble long enough to be its own section.\n\n## Big\n\n{}",
            "x\n".repeat(500)
        );
        let result = split_markdown_sections(&content, 800, 200);
        let key = stable_section_key("## Big");
        assert_eq!(result[0].0, "");
        assert_eq!(result[1].0, key);
        assert_eq!(result[2].0, format!("{key}-part1"));
    }

    #[test]
    fn heading_scanner_needs_space_and_max_six_hashes() {
        let content = "#yes\n\n####### seven\n\n#\tTabbed\n";
        let result = split_markdown_sections(content, 800, 200);
        assert_eq!(result.len(), 2);
        assert_eq!(result[1].0, stable_section_key("#\tTabbed"));
    }

    #[test]
    fn non_ascii_offsets_follow_code_points() {
        let content = format!("# Héad\n\n{}", "é".repeat(900));
        let result = split_markdown_sections(&content, 800, 200);
        assert_eq!(result.len(), 2);
        assert!(result
            .iter()
            .all(|(_, text)| crate::python_text::char_len(text) <= 800));
    }
}

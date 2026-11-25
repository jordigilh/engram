//! Deterministic extractive synthesis ported from `src/engram/synthesis.py`.
//!
//! sklearn's `TfidfVectorizer` parameters and networkx's `_pagerank_scipy`
//! iteration are reproduced explicitly so the same text yields the same
//! `key_sentences` and `keywords` as the Python docs flow.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use crate::python_text;

pub const TOP_TERMS: usize = 15;
pub const MIN_SENTENCES: usize = 5;
pub const MAX_SENTENCES: usize = 12;
pub const CHARS_PER_SENTENCE: usize = 8000;
pub const SIMILARITY_THRESHOLD: f64 = 0.1;

const PAGERANK_ALPHA: f64 = 0.85;
const PAGERANK_TOL: f64 = 1.0e-6;
const PAGERANK_MAX_ITER: usize = 100;

#[derive(Debug, Clone, PartialEq)]
pub struct Stats {
    pub n_sentences: usize,
    pub source_chars: usize,
    pub coverage_chars: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Synthesis {
    pub doc_id: String,
    pub key_sentences: Vec<String>,
    pub keywords: Vec<String>,
    pub stats: Stats,
}

/// Intermediate values exposed so parity tests can compare the maths, not only
/// the final selection.
#[derive(Debug, Clone)]
pub struct Analysis {
    pub sentences: Vec<String>,
    pub vocabulary: Vec<String>,
    /// Sparse `(term index, weight)` rows; dense matrices are quadratic in
    /// sentences and terms and are not needed for selection.
    pub tfidf: Vec<Vec<(usize, f64)>>,
    pub row_sums: Vec<f64>,
    pub ranks: Vec<f64>,
    pub keyword_scores: Vec<f64>,
    pub key_indices: Vec<usize>,
    pub keyword_indices: Vec<usize>,
    pub n_keep: usize,
    pub empty_vocabulary: bool,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SynthesisError {
    #[error("PageRank did not converge within {max_iter} iterations")]
    PageRankDidNotConverge { max_iter: usize },
}

/// Split Markdown into candidate sentences. Headings, list items, and table
/// rows stand alone; fenced code is skipped; fragments of 40 characters or
/// fewer are dropped; duplicates keep their first position.
pub fn split_sentences(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut in_fence = false;
    for line in python_text::splitlines(text) {
        let stripped = python_text::strip(&line);
        if stripped.starts_with("```") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence || stripped.is_empty() {
            continue;
        }
        lines.push(stripped.to_string());
    }

    let mut candidates = Vec::new();
    for line in lines {
        for part in split_sentence_line(&line) {
            let stripped = python_text::strip(&part);
            if python_text::char_len(stripped) > 40 {
                candidates.push(stripped.to_string());
            }
        }
    }

    let mut seen = HashSet::new();
    let mut unique = Vec::new();
    for sentence in candidates {
        if seen.insert(sentence.clone()) {
            unique.push(sentence);
        }
    }
    unique
}

/// Equivalent of `re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(`])", line)`.
fn split_sentence_line(line: &str) -> Vec<String> {
    let chars: Vec<char> = line.chars().collect();
    let mut parts = Vec::new();
    let mut start = 0usize;
    let mut index = 0usize;
    while index < chars.len() {
        if matches!(chars[index], '.' | '!' | '?') {
            let mut next = index + 1;
            while next < chars.len() && python_text::is_space(chars[next]) {
                next += 1;
            }
            if next > index + 1 && next < chars.len() && starts_sentence(chars[next]) {
                parts.push(chars[start..=index].iter().collect());
                start = next;
                index = next;
                continue;
            }
        }
        index += 1;
    }
    parts.push(chars[start..].iter().collect());
    parts
}

fn starts_sentence(c: char) -> bool {
    c.is_ascii_uppercase() || c.is_ascii_digit() || matches!(c, '"' | '\'' | '(' | '`')
}

pub fn analyze(text: &str) -> Result<Analysis, SynthesisError> {
    let sentences = split_sentences(text);
    let n = sentences.len();
    let n_keep =
        MAX_SENTENCES.min(MIN_SENTENCES.max(python_text::char_len(text) / CHARS_PER_SENTENCE));

    if n == 0 {
        return Ok(Analysis {
            sentences,
            vocabulary: Vec::new(),
            tfidf: Vec::new(),
            row_sums: Vec::new(),
            ranks: Vec::new(),
            keyword_scores: Vec::new(),
            key_indices: Vec::new(),
            keyword_indices: Vec::new(),
            n_keep,
            empty_vocabulary: true,
        });
    }

    let counts: Vec<BTreeMap<String, u32>> = sentences.iter().map(|s| token_counts(s)).collect();
    let vocabulary: Vec<String> = counts
        .iter()
        .flat_map(|map| map.keys().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let vocabulary_index: BTreeMap<&str, usize> = vocabulary
        .iter()
        .enumerate()
        .map(|(index, term)| (term.as_str(), index))
        .collect();

    if vocabulary.is_empty() {
        let key_indices: Vec<usize> = (0..n_keep.min(n)).collect();
        return Ok(Analysis {
            sentences,
            vocabulary,
            tfidf: Vec::new(),
            row_sums: Vec::new(),
            ranks: Vec::new(),
            keyword_scores: Vec::new(),
            key_indices,
            keyword_indices: Vec::new(),
            n_keep,
            empty_vocabulary: true,
        });
    }

    let document_frequency = vocabulary
        .iter()
        .map(|term| counts.iter().filter(|map| map.contains_key(term)).count() as f64)
        .collect::<Vec<_>>();
    let idf: Vec<f64> = document_frequency
        .iter()
        .map(|df| ((1.0 + n as f64) / (1.0 + df)).ln() + 1.0)
        .collect();

    let mut tfidf: Vec<Vec<(usize, f64)>> = Vec::with_capacity(n);
    for map in &counts {
        let mut row: Vec<(usize, f64)> = map
            .iter()
            .map(|(term, count)| {
                let column = vocabulary_index[term.as_str()];
                (column, f64::from(*count) * idf[column])
            })
            .collect();
        row.sort_unstable_by_key(|(column, _)| *column);
        let norm = row
            .iter()
            .map(|(_, value)| value * value)
            .sum::<f64>()
            .sqrt();
        if norm != 0.0 {
            for (_, value) in &mut row {
                *value /= norm;
            }
        }
        tfidf.push(row);
    }

    let row_sums: Vec<f64> = tfidf
        .iter()
        .map(|row| row.iter().map(|(_, value)| value).sum())
        .collect();
    let weights = similarity_weights(&tfidf);
    let edge_count = weights
        .iter()
        .enumerate()
        .map(|(row, values)| {
            values
                .iter()
                .enumerate()
                .filter(|(column, value)| column > &row && **value > 0.0)
                .count()
        })
        .sum::<usize>();

    let ranks = if edge_count == 0 {
        row_sums.clone()
    } else {
        pagerank(&weights)?
    };

    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&left, &right| {
        ranks[right]
            .partial_cmp(&ranks[left])
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(left.cmp(&right))
    });
    let mut key_indices = order[..n_keep.min(n)].to_vec();
    key_indices.sort_unstable();

    let mut keyword_scores = vec![0.0; vocabulary.len()];
    for row in &tfidf {
        for (column, value) in row {
            keyword_scores[*column] += value;
        }
    }
    let mut keyword_order: Vec<usize> = (0..vocabulary.len()).collect();
    keyword_order.sort_by(|&left, &right| {
        keyword_scores[right]
            .partial_cmp(&keyword_scores[left])
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(left.cmp(&right))
    });
    keyword_order.truncate(TOP_TERMS);

    Ok(Analysis {
        sentences,
        vocabulary,
        tfidf,
        row_sums,
        ranks,
        keyword_scores,
        key_indices,
        keyword_indices: keyword_order,
        n_keep,
        empty_vocabulary: false,
    })
}

pub fn synthesize_document(doc_id: &str, text: &str) -> Result<Synthesis, SynthesisError> {
    let analysis = analyze(text)?;
    let key_sentences: Vec<String> = analysis
        .key_indices
        .iter()
        .map(|&index| analysis.sentences[index].clone())
        .collect();
    let keywords: Vec<String> = analysis
        .keyword_indices
        .iter()
        .map(|&index| analysis.vocabulary[index].clone())
        .collect();
    Ok(Synthesis {
        doc_id: doc_id.to_string(),
        stats: Stats {
            n_sentences: analysis.sentences.len(),
            source_chars: python_text::char_len(text),
            coverage_chars: key_sentences
                .iter()
                .map(|sentence| python_text::char_len(sentence))
                .sum(),
        },
        key_sentences,
        keywords,
    })
}

/// sklearn `cosine_similarity` on L2-normalised sparse rows, diagonal zeroed.
fn similarity_weights(tfidf: &[Vec<(usize, f64)>]) -> Vec<Vec<f64>> {
    let n = tfidf.len();
    let mut weights = vec![vec![0.0; n]; n];
    for left in 0..n {
        for right in (left + 1)..n {
            let similarity = sparse_dot(&tfidf[left], &tfidf[right]);
            if similarity > SIMILARITY_THRESHOLD {
                weights[left][right] = similarity;
                weights[right][left] = similarity;
            }
        }
    }
    weights
}

fn sparse_dot(left: &[(usize, f64)], right: &[(usize, f64)]) -> f64 {
    let (mut i, mut j, mut sum) = (0usize, 0usize, 0.0);
    while i < left.len() && j < right.len() {
        match left[i].0.cmp(&right[j].0) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                sum += left[i].1 * right[j].1;
                i += 1;
                j += 1;
            }
        }
    }
    sum
}

/// networkx 3.x `_pagerank_scipy` for an undirected weighted graph.
fn pagerank(weights: &[Vec<f64>]) -> Result<Vec<f64>, SynthesisError> {
    let n = weights.len();
    let out_degree: Vec<f64> = weights.iter().map(|row| row.iter().sum()).collect();
    let dangling: Vec<usize> = (0..n).filter(|&index| out_degree[index] == 0.0).collect();
    let mut x = vec![1.0 / n as f64; n];
    let p = vec![1.0 / n as f64; n];

    for _ in 0..PAGERANK_MAX_ITER {
        let mut next = vec![0.0; n];
        for (column, value) in next.iter_mut().enumerate() {
            let mut flow = 0.0;
            for (row, degree) in out_degree.iter().enumerate() {
                if *degree != 0.0 {
                    flow += x[row] * weights[row][column] / degree;
                }
            }
            let dangling_flow: f64 = dangling.iter().map(|&row| x[row] * p[column]).sum();
            *value = PAGERANK_ALPHA * (flow + dangling_flow) + (1.0 - PAGERANK_ALPHA) * p[column];
        }
        let error: f64 = next
            .iter()
            .zip(&x)
            .map(|(new, old)| (new - old).abs())
            .sum();
        x = next;
        if error < n as f64 * PAGERANK_TOL {
            return Ok(x);
        }
    }
    Err(SynthesisError::PageRankDidNotConverge {
        max_iter: PAGERANK_MAX_ITER,
    })
}

fn token_counts(sentence: &str) -> BTreeMap<String, u32> {
    let lower = python_text::lower(sentence);
    let chars: Vec<char> = lower.chars().collect();
    let mut counts = BTreeMap::new();
    let mut index = 0usize;
    while index < chars.len() {
        if !chars[index].is_ascii_alphabetic() {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        while index < chars.len() && is_token_continuation(chars[index]) {
            index += 1;
        }
        if index - start < 3 {
            continue;
        }
        let token: String = chars[start..index].iter().collect();
        if crate::stopwords::is_stopword(&token) {
            continue;
        }
        *counts.entry(token).or_insert(0) += 1;
    }
    counts
}

fn is_token_continuation(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fences_and_short_fragments_are_skipped() {
        let text = "```\nThis code block sentence is definitely long enough to count normally.\n```\nA real sentence that is definitely longer than forty characters in total.";
        let sentences = split_sentences(text);
        assert_eq!(sentences.len(), 1);
        assert!(sentences[0].starts_with("A real sentence"));
    }

    #[test]
    fn duplicates_keep_first_position() {
        let sentence = "This sentence is long enough to survive the forty character filter.";
        let sentences = split_sentences(&format!("{sentence}\n{sentence}"));
        assert_eq!(sentences, vec![sentence]);
    }

    #[test]
    fn sentence_split_needs_uppercase_lookahead() {
        let parts = split_sentence_line("First sentence. second stays. Third starts.");
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0], "First sentence. second stays.");
        assert_eq!(parts[1], "Third starts.");
    }

    #[test]
    fn zero_edge_graph_uses_tfidf_row_mass() {
        let text = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu.\nNu xi omicron pi rho sigma tau upsilon phi chi psi omega.";
        let analysis = analyze(text).unwrap();
        assert_eq!(analysis.ranks, analysis.row_sums);
    }

    #[test]
    fn tokenizer_matches_sklearn_pattern() {
        let counts = token_counts("the quick brown fox jumps over the lazy dog");
        assert!(!counts.contains_key("the"));
        assert_eq!(counts.get("quick"), Some(&1));
        assert_eq!(counts.get("fox"), Some(&1));
        assert_eq!(counts.get("jumps"), Some(&1));
        assert!(counts.contains_key("lazy"));
    }
}

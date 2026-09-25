use std::collections::{HashMap, HashSet};

pub fn validate_workflow_parameters(
    required: &HashSet<String>,
    supplied: HashMap<String, String>,
) -> Result<HashMap<String, String>, String> {
    let missing = required
        .iter()
        .filter(|key| !supplied.contains_key(*key))
        .cloned()
        .collect::<Vec<_>>();
    let unknown = supplied
        .keys()
        .filter(|key| !required.contains(*key))
        .cloned()
        .collect::<Vec<_>>();
    if !missing.is_empty() || !unknown.is_empty() {
        return Err(format!(
            "invalid workflow parameters: missing={missing:?}, unknown={unknown:?}"
        ));
    }
    Ok(supplied)
}

use std::collections::{HashMap, HashSet};

#[derive(Clone, Default)]
pub struct WorkflowState {
    ids: HashSet<String>,
}

impl WorkflowState {
    pub fn add(&mut self, workflow_ids: &[String]) {
        self.ids
            .extend(workflow_ids.iter().filter(|id| !id.is_empty()).cloned());
    }

    pub fn contains(&self, workflow_id: &str) -> bool {
        self.ids.contains(workflow_id)
    }
}

pub fn state_from_context(context: &HashMap<String, WorkflowState>) -> WorkflowState {
    context.get("workflow_state").cloned().unwrap_or_default()
}

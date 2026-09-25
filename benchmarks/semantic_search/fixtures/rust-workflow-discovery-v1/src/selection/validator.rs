use crate::discovery::state::WorkflowState;

#[derive(Debug)]
pub struct ValidationError {
    pub workflow_id: String,
}

pub struct WorkflowValidator {
    allowed_ids: std::collections::HashSet<String>,
    workflow_state: Option<WorkflowState>,
}

impl WorkflowValidator {
    pub fn new(allowed_ids: std::collections::HashSet<String>) -> Self {
        Self {
            allowed_ids,
            workflow_state: None,
        }
    }

    pub fn set_workflow_state(&mut self, state: WorkflowState) {
        self.workflow_state = Some(state);
    }

    pub fn is_allowed(&self, workflow_id: &str) -> bool {
        self.allowed_ids.contains(workflow_id)
            && self
                .workflow_state
                .as_ref()
                .is_none_or(|state| state.contains(workflow_id))
    }

    pub fn validate(&self, workflow_id: &str) -> Result<(), ValidationError> {
        if self.is_allowed(workflow_id) {
            Ok(())
        } else {
            Err(ValidationError {
                workflow_id: workflow_id.to_owned(),
            })
        }
    }
}

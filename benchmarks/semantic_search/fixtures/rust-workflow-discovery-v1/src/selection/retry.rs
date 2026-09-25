use super::validator::{ValidationError, WorkflowValidator};

pub fn select_with_retry(
    validator: &WorkflowValidator,
    workflow_id: &str,
    corrected_id: &str,
) -> Result<String, ValidationError> {
    match validator.validate(workflow_id) {
        Ok(()) => Ok(workflow_id.to_owned()),
        Err(_) => self_correct_selection(validator, corrected_id),
    }
}

pub fn self_correct_selection(
    validator: &WorkflowValidator,
    workflow_id: &str,
) -> Result<String, ValidationError> {
    validator.validate(workflow_id)?;
    Ok(workflow_id.to_owned())
}

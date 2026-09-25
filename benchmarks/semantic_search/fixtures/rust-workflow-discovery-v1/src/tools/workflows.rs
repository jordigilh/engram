use crate::discovery::{
    catalog::{Workflow, WorkflowCatalog},
    state::WorkflowState,
};

pub struct DiscoveryResult {
    pub recommended: String,
    pub alternatives: Vec<String>,
}

pub struct WorkflowResult {
    pub workflow_id: String,
    pub parameters: std::collections::HashMap<String, String>,
}

pub fn list_workflows(catalog: &WorkflowCatalog, state: &mut WorkflowState) -> Vec<Workflow> {
    let workflows = catalog.list_workflows(&std::collections::HashMap::new());
    let workflow_ids = workflows
        .iter()
        .map(|workflow| workflow.workflow_id.clone())
        .collect::<Vec<_>>();
    record_discovered_workflows(state, &workflow_ids);
    workflows
}

pub fn record_discovered_workflows(state: &mut WorkflowState, workflow_ids: &[String]) {
    state.add(workflow_ids);
}

pub fn is_workflow_in_discovery_result(workflow_id: &str, result: &DiscoveryResult) -> bool {
    workflow_id == result.recommended || result.alternatives.iter().any(|item| item == workflow_id)
}

pub fn authorize_selection_driver(
    workflow_id: &str,
    result: &DiscoveryResult,
) -> Result<(), &'static str> {
    if is_workflow_in_discovery_result(workflow_id, result) {
        Ok(())
    } else {
        Err("workflow was not returned by discovery")
    }
}

pub fn handle_select_workflow(
    workflow_id: &str,
    result: &DiscoveryResult,
    catalog: &WorkflowCatalog,
) -> Result<WorkflowResult, &'static str> {
    authorize_selection_driver(workflow_id, result)?;
    let workflow = catalog
        .get_workflow(workflow_id)
        .ok_or("unknown workflow")?;
    Ok(apply_selected_workflow(workflow, result))
}

pub fn apply_selected_workflow(workflow: &Workflow, result: &DiscoveryResult) -> WorkflowResult {
    WorkflowResult {
        workflow_id: workflow.workflow_id.clone(),
        parameters: lookup_discovered_parameters(&workflow.workflow_id, result),
    }
}

pub fn lookup_discovered_parameters(
    workflow_id: &str,
    result: &DiscoveryResult,
) -> std::collections::HashMap<String, String> {
    let source = if workflow_id == result.recommended {
        "recommended"
    } else {
        "alternative"
    };
    std::collections::HashMap::from([("source".to_owned(), source.to_owned())])
}

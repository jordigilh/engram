use std::collections::HashMap;

#[derive(Clone)]
pub struct Workflow {
    pub workflow_id: String,
    pub parameters: HashMap<String, String>,
    pub labels: HashMap<String, String>,
}

pub struct WorkflowCatalog {
    workflows: HashMap<String, Workflow>,
}

impl WorkflowCatalog {
    pub fn new(workflows: Vec<Workflow>) -> Self {
        Self {
            workflows: workflows
                .into_iter()
                .map(|workflow| (workflow.workflow_id.clone(), workflow))
                .collect(),
        }
    }

    pub fn list_workflows(&self, filters: &HashMap<String, String>) -> Vec<Workflow> {
        self.workflows
            .values()
            .filter(|workflow| {
                filters
                    .iter()
                    .all(|(key, value)| workflow.labels.get(key) == Some(value))
            })
            .cloned()
            .collect()
    }

    pub fn list_actions(&self, filters: &HashMap<String, String>) -> Vec<Workflow> {
        self.list_workflows(filters)
    }

    pub fn get_workflow(&self, workflow_id: &str) -> Option<&Workflow> {
        self.workflows.get(workflow_id)
    }
}

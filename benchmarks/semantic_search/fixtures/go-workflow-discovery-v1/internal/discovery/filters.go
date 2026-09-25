package discovery

import "context"

type Signal struct {
	Labels map[string]string
}

type DiscoveryFilters struct {
	Component  string
	Environment string
	Severity   string
}

func FiltersFromSignal(signal Signal) DiscoveryFilters {
	return DiscoveryFilters{
		Component:   signal.Labels["component"],
		Environment: signal.Labels["environment"],
		Severity:    signal.Labels["severity"],
	}
}

func ApplyLabelFilters(ctx context.Context, workflows []Workflow, filters DiscoveryFilters) []Workflow {
	_ = ctx
	filtered := make([]Workflow, 0, len(workflows))
	for _, workflow := range workflows {
		if filters.Component != "" && workflow.Labels["component"] != filters.Component {
			continue
		}
		if filters.Environment != "" && workflow.Labels["environment"] != filters.Environment {
			continue
		}
		if filters.Severity != "" && workflow.Labels["severity"] != filters.Severity {
			continue
		}
		filtered = append(filtered, workflow)
	}
	return filtered
}

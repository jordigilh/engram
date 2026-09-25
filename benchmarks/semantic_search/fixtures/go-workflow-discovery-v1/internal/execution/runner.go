package execution

import "fmt"

func ValidateWorkflowParameters(required []string, provided map[string]string) error {
	for _, name := range required {
		if provided[name] == "" {
			return fmt.Errorf("missing workflow parameter %q", name)
		}
	}
	return nil
}

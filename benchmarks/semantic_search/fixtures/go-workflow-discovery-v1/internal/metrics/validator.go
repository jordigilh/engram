package metrics

import "unicode"

// ValidateMetricName is intentionally similar in name to selection validators
// but has no workflow or discovery behavior.
func ValidateMetricName(name string) bool {
	if name == "" {
		return false
	}
	return unicode.IsLetter(rune(name[0]))
}

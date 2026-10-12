export function validateMetricName(name: string): boolean {
  return name.length > 0 && /^[a-zA-Z][a-zA-Z0-9_]*$/.test(name);
}

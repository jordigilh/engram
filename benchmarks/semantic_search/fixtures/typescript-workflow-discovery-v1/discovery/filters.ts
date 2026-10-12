export interface Signal {
  labels: Record<string, string>;
}

export interface DiscoveryFilters {
  component: string;
  environment: string;
  severity: string;
}

export function filtersFromSignal(signal: Signal): DiscoveryFilters {
  return {
    component: signal.labels.component ?? "",
    environment: signal.labels.environment ?? "",
    severity: signal.labels.severity ?? "",
  };
}

export function applyLabelFilters<T extends { labels: Record<string, string> }>(
  workflows: readonly T[],
  filters: DiscoveryFilters,
): T[] {
  const expected = Object.entries(filters).filter(([, value]) => value.length > 0);
  return workflows.filter((workflow) =>
    expected.every(([key, value]) => workflow.labels[key] === value),
  );
}

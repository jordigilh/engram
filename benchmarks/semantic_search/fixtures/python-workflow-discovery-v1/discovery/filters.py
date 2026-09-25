from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    labels: dict[str, str]


@dataclass
class DiscoveryFilters:
    component: str = ""
    environment: str = ""
    severity: str = ""


def filters_from_signal(signal: Signal) -> DiscoveryFilters:
    return DiscoveryFilters(
        component=signal.labels.get("component", ""),
        environment=signal.labels.get("environment", ""),
        severity=signal.labels.get("severity", ""),
    )


def apply_label_filters(workflows: list[object], filters: DiscoveryFilters) -> list[object]:
    expected = {
        key: value
        for key, value in {
            "component": filters.component,
            "environment": filters.environment,
            "severity": filters.severity,
        }.items()
        if value
    }
    return [workflow for workflow in workflows if all(getattr(workflow, "labels", {}).get(k) == v for k, v in expected.items())]

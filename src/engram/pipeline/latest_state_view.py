"""Deterministic latest-state projections over layered Engram records.

This module intentionally knows nothing about Jira, GitHub, CocoIndex, or
Hindsight's storage model beyond the record envelope supplied by the caller.
View definitions provide the reusable, server-owned interpretation of that
envelope; projection itself performs no LLM calls or semantic retrieval.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


INVALID_RECORD_STATES = frozenset({"invalidated", "deleted", "superseded"})


@dataclass(frozen=True)
class FieldRule:
    """How one projected field is extracted and resolved."""

    paths: tuple[str, ...]
    source_precedence: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FieldRule":
        paths = value.get("paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("field rule paths must be a non-empty string array")
        precedence = value.get("source_precedence", [])
        if not isinstance(precedence, list) or any(not isinstance(source, str) for source in precedence):
            raise ValueError("field rule source_precedence must be a string array")
        return cls(tuple(paths), tuple(precedence))


@dataclass(frozen=True)
class ViewDefinition:
    """A named, reusable deterministic projection contract."""

    view_id: str
    entity_paths: tuple[str, ...]
    fields: dict[str, FieldRule]
    stale_after_seconds: int | None = None
    entity_key_options: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ViewDefinition":
        view_id = value.get("view_id")
        entity_paths = value.get("entity_paths")
        fields = value.get("fields")
        if not isinstance(view_id, str) or not view_id:
            raise ValueError("view definition requires a non-empty view_id")
        if not isinstance(entity_paths, list) or not entity_paths or any(
            not isinstance(path, str) or not path for path in entity_paths
        ):
            raise ValueError("view definition entity_paths must be a non-empty string array")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("view definition fields must be a non-empty object")
        stale_after_seconds = value.get("stale_after_seconds")
        if stale_after_seconds is not None and (
            not isinstance(stale_after_seconds, int) or stale_after_seconds < 0
        ):
            raise ValueError("stale_after_seconds must be a non-negative integer")
        raw_options = value.get("entity_key_options", [])
        if not isinstance(raw_options, list) or any(
            not isinstance(option, list)
            or not option
            or any(not isinstance(path, str) or not path for path in option)
            for option in raw_options
        ):
            raise ValueError("entity_key_options must be an array of non-empty string arrays")
        return cls(
            view_id=view_id,
            entity_paths=tuple(entity_paths),
            fields={name: FieldRule.from_dict(rule) for name, rule in fields.items()},
            stale_after_seconds=stale_after_seconds,
            entity_key_options=tuple(tuple(option) for option in raw_options),
        )


@dataclass(frozen=True)
class ScopeDefinition:
    """Server-owned record scope for one view route."""

    name: str
    metadata_equals: Mapping[str, Any] = field(default_factory=dict)
    required_tags: frozenset[str] = frozenset()

    def matches(self, item: Mapping[str, Any]) -> bool:
        metadata = item.get("metadata") or {}
        if any(metadata.get(key) != value for key, value in self.metadata_equals.items()):
            return False
        return self.required_tags.issubset(set(item.get("tags") or []))


@dataclass(frozen=True)
class _Observation:
    value: Any
    observed_at: datetime | None
    source: str
    memory_id: str | None
    document_id: str | None
    source_priority: int


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _lookup(item: Mapping[str, Any], path: str) -> Any:
    """Read a view path from metadata first, or from the record envelope."""
    metadata = item.get("metadata") or {}
    if path.startswith("metadata."):
        return metadata.get(path.removeprefix("metadata."))
    if path.startswith("item."):
        return item.get(path.removeprefix("item."))
    if path in metadata:
        return metadata[path]
    return item.get(path)


def _first_value(item: Mapping[str, Any], paths: Iterable[str]) -> Any:
    for path in paths:
        value = _lookup(item, path)
        if value is not None and value != "":
            return value
    return None


def _entity_key(item: Mapping[str, Any], view: ViewDefinition) -> str | None:
    if view.entity_key_options:
        for option in view.entity_key_options:
            values = [_lookup(item, path) for path in option]
            if all(value is not None and value != "" for value in values):
                return "|".join(str(value) for value in values)
    return _first_value(item, view.entity_paths)


def _observed_at(item: Mapping[str, Any]) -> datetime | None:
    return _parse_timestamp(
        _first_value(
            item,
            ("item.date", "metadata.updated_at", "item.occurred_end", "item.mentioned_at", "item.edited_at"),
        )
    )


def _value_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _is_valid(item: Mapping[str, Any]) -> bool:
    state = str(item.get("state") or "").lower()
    return state not in INVALID_RECORD_STATES


def _source_priority(source: str, precedence: tuple[str, ...]) -> int:
    if source not in precedence:
        return 0
    return len(precedence) - precedence.index(source)


def _observation(item: Mapping[str, Any], value: Any, rule: FieldRule) -> _Observation:
    metadata = item.get("metadata") or {}
    source = str(metadata.get("source") or item.get("source") or "unknown")
    return _Observation(
        value=value,
        observed_at=_observed_at(item),
        source=source,
        memory_id=item.get("id"),
        document_id=item.get("document_id"),
        source_priority=_source_priority(source, rule.source_precedence),
    )


def _latest_observations(observations: list[_Observation]) -> list[_Observation]:
    if not observations:
        return []
    latest_time = max(
        (observation.observed_at for observation in observations if observation.observed_at is not None),
        default=None,
    )
    if latest_time is None:
        latest = observations
    else:
        latest = [observation for observation in observations if observation.observed_at == latest_time]
    priority = max(observation.source_priority for observation in latest)
    return [observation for observation in latest if observation.source_priority == priority]


def _provenance(observation: _Observation) -> dict[str, Any]:
    return {
        "memory_id": observation.memory_id,
        "document_id": observation.document_id,
        "source": observation.source,
        "observed_at": _timestamp_text(observation.observed_at),
    }


def project_latest_state(
    records: Iterable[Mapping[str, Any]],
    view: ViewDefinition,
    scope: ScopeDefinition,
    *,
    as_of: str | None = None,
    include_provenance: bool = True,
) -> dict[str, Any]:
    """Materialize the latest valid state for each entity in ``scope``.

    A field is resolved independently, so a newer observation of one field
    cannot erase a still-current observation of another. Equal-time conflicting
    values remain explicit conflicts unless the view declares source precedence.
    """
    requested_as_of = _parse_timestamp(as_of) if as_of else None
    if as_of and requested_as_of is None:
        raise ValueError("as_of must be an ISO-8601 timestamp")

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    scoped_count = 0
    skipped_invalid = 0
    skipped_without_entity = 0
    missing_timestamps = 0
    timestamps: list[datetime] = []

    for item in records:
        if not _is_valid(item):
            skipped_invalid += 1
            continue
        if not scope.matches(item):
            continue
        scoped_count += 1
        observed_at = _observed_at(item)
        if observed_at is None:
            missing_timestamps += 1
        if requested_as_of is not None and observed_at is not None and observed_at > requested_as_of:
            continue
        if observed_at is not None:
            timestamps.append(observed_at)
        entity = _entity_key(item, view)
        if entity is None:
            entity = item.get("document_id")
        if entity is None or entity == "":
            skipped_without_entity += 1
            continue
        grouped[str(entity)].append(item)

    effective_as_of = requested_as_of or (max(timestamps) if timestamps else None)
    projected: list[dict[str, Any]] = []
    total_conflicts = 0
    stale_count = 0

    for entity_key in sorted(grouped):
        items = grouped[entity_key]
        state: dict[str, Any] = {}
        conflicts: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        missing_fields: list[str] = []
        field_latest_times: list[datetime] = []

        for field_name, rule in view.fields.items():
            observations = [
                _observation(item, _first_value(item, rule.paths), rule)
                for item in items
                if _first_value(item, rule.paths) is not None
            ]
            latest = _latest_observations(observations)
            if not latest:
                state[field_name] = None
                missing_fields.append(field_name)
                continue

            values = {_value_key(observation.value) for observation in latest}
            if len(values) > 1:
                state[field_name] = None
                conflict = {
                    "field": field_name,
                    "observations": [
                        {
                            "value": observation.value,
                            **(_provenance(observation) if include_provenance else {}),
                        }
                        for observation in sorted(
                            latest,
                            key=lambda observation: (observation.memory_id or "", observation.document_id or ""),
                        )
                    ],
                }
                conflicts.append(conflict)
                total_conflicts += 1
                continue

            winner = sorted(
                latest,
                key=lambda observation: (observation.memory_id or "", observation.document_id or ""),
            )[0]
            state[field_name] = winner.value
            if winner.observed_at is not None:
                field_latest_times.append(winner.observed_at)
            if include_provenance:
                provenance.extend(_provenance(observation) for observation in latest)

        stale: bool | None = None
        if view.stale_after_seconds is not None:
            if field_latest_times and effective_as_of is not None:
                stale = any(
                    (effective_as_of - observed_at).total_seconds() > view.stale_after_seconds
                    for observed_at in field_latest_times
                )
            elif not field_latest_times:
                stale = None
            else:
                stale = True
            if stale:
                stale_count += 1

        result = {
            "entity_key": entity_key,
            "state": state,
            "missing_fields": missing_fields,
            "conflicts": conflicts,
            "stale": stale,
        }
        if include_provenance:
            result["provenance"] = provenance
        projected.append(result)

    oldest = min(timestamps) if timestamps else None
    newest = max(timestamps) if timestamps else None
    return {
        "view_id": view.view_id,
        "scope": scope.name,
        "as_of": _timestamp_text(effective_as_of),
        "items": projected,
        "freshness": {
            "scoped_records": scoped_count,
            "projected_entities": len(projected),
            "skipped_invalid_records": skipped_invalid,
            "skipped_records_without_entity": skipped_without_entity,
            "missing_timestamp_records": missing_timestamps,
            "oldest_observation": _timestamp_text(oldest),
            "newest_observation": _timestamp_text(newest),
            "stale_entities": stale_count,
        },
        "conflict_count": total_conflicts,
    }

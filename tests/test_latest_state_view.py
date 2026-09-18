from __future__ import annotations

from engram.pipeline.latest_state_view import FieldRule, ScopeDefinition, ViewDefinition, project_latest_state


def _view(*, precedence: tuple[str, ...] = (), stale_after_seconds: int | None = None) -> ViewDefinition:
    return ViewDefinition(
        view_id="workstreams",
        entity_paths=("entity_key",),
        fields={"status": FieldRule(("status",), precedence)},
        stale_after_seconds=stale_after_seconds,
    )


def _record(memory_id: str, entity: str, status: str, observed_at: str, source: str = "tracker") -> dict:
    return {
        "id": memory_id,
        "document_id": f"doc-{entity}",
        "date": observed_at,
        "metadata": {"entity_key": entity, "status": status, "source": source},
        "state": "valid",
        "tags": ["project-a"],
    }


def test_latest_state_is_selected_per_field_not_by_input_order() -> None:
    result = project_latest_state(
        [
            _record("new", "aurora-1", "ready", "2026-09-16T12:00:00Z"),
            _record("old", "aurora-1", "blocked", "2026-09-16T09:00:00Z"),
        ],
        _view(),
        ScopeDefinition("project-a", required_tags=frozenset({"project-a"})),
    )

    assert result["as_of"] == "2026-09-16T12:00:00Z"
    assert result["items"][0]["state"] == {"status": "ready"}
    assert result["items"][0]["provenance"][0]["memory_id"] == "new"


def test_equal_time_conflicts_are_preserved_without_inventing_a_state() -> None:
    result = project_latest_state(
        [
            _record("a", "aurora-1", "blocked", "2026-09-16T12:00:00Z"),
            _record("b", "aurora-1", "ready", "2026-09-16T12:00:00Z"),
        ],
        _view(),
        ScopeDefinition("project-a"),
    )

    item = result["items"][0]
    assert item["state"] == {"status": None}
    assert item["conflicts"][0]["field"] == "status"
    assert result["conflict_count"] == 1


def test_declared_source_precedence_resolves_equal_time_observations() -> None:
    result = project_latest_state(
        [
            _record("tracker", "aurora-1", "ready", "2026-09-16T12:00:00Z", "tracker"),
            _record("verification", "aurora-1", "verified", "2026-09-16T12:00:00Z", "verification"),
        ],
        _view(precedence=("verification", "tracker")),
        ScopeDefinition("project-a"),
    )

    assert result["items"][0]["state"] == {"status": "verified"}
    assert result["conflict_count"] == 0


def test_scope_invalid_records_future_records_and_missing_fields_are_explicit() -> None:
    records = [
        _record("in-scope", "aurora-1", "ready", "2026-09-16T12:00:00Z"),
        {**_record("out-of-scope", "orion-1", "ready", "2026-09-16T12:00:00Z"), "tags": ["other"]},
        {**_record("invalid", "aurora-1", "failed", "2026-09-16T13:00:00Z"), "state": "invalidated"},
        _record("future", "aurora-1", "released", "2026-09-17T12:00:00Z"),
        {
            "id": "missing",
            "document_id": "doc-orion-2",
            "date": "2026-09-16T11:00:00Z",
            "metadata": {"entity_key": "orion-2"},
            "state": "valid",
            "tags": ["project-a"],
        },
    ]

    result = project_latest_state(
        records,
        _view(stale_after_seconds=3600),
        ScopeDefinition("project-a", required_tags=frozenset({"project-a"})),
        as_of="2026-09-16T12:30:00Z",
        include_provenance=False,
    )

    assert [item["entity_key"] for item in result["items"]] == ["aurora-1", "orion-2"]
    assert result["items"][0]["state"] == {"status": "ready"}
    assert result["items"][1]["state"] == {"status": None}
    assert result["items"][1]["missing_fields"] == ["status"]
    assert "provenance" not in result["items"][0]
    assert result["freshness"]["skipped_invalid_records"] == 1
    assert result["freshness"]["scoped_records"] == 3


def test_stale_entities_are_reported_from_the_view_definition() -> None:
    result = project_latest_state(
        [_record("old", "aurora-1", "ready", "2026-09-16T09:00:00Z")],
        _view(stale_after_seconds=3600),
        ScopeDefinition("project-a"),
        as_of="2026-09-16T12:00:00Z",
    )

    assert result["items"][0]["stale"] is True
    assert result["freshness"]["stale_entities"] == 1


def test_composite_entity_key_options_prevent_same_number_collisions() -> None:
    view = ViewDefinition(
        view_id="tracker-status",
        entity_paths=("entity_key",),
        fields={"status": FieldRule(("status",))},
        entity_key_options=(("repo", "kind", "number"),),
    )
    records = [
        {
            **_record("a", "unused", "open", "2026-09-16T10:00:00Z"),
            "metadata": {"repo": "org/one", "kind": "issue", "number": "7", "status": "open"},
        },
        {
            **_record("b", "unused", "closed", "2026-09-16T11:00:00Z"),
            "metadata": {"repo": "org/two", "kind": "issue", "number": "7", "status": "closed"},
        },
    ]

    result = project_latest_state(records, view, ScopeDefinition("project-a"))

    assert [item["entity_key"] for item in result["items"]] == [
        "org/one|issue|7",
        "org/two|issue|7",
    ]

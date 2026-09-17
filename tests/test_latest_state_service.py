from __future__ import annotations

import json

import pytest

from engram.pipeline import latest_state_service as service_module
from engram.pipeline.latest_state_service import HindsightMemoryListClient, LatestStateService
from engram.pipeline.latest_state_view import FieldRule, ScopeDefinition, ViewDefinition


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()
        self.requests: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


def test_hindsight_client_paginates_without_reading_record_content(monkeypatch) -> None:
    requests = []

    def fake_urlopen(request, timeout=60):
        requests.append(request.full_url)
        offset = 0 if "offset=0" in request.full_url else 2
        return FakeResponse({
            "total": 3,
            "items": [{"id": f"memory-{offset + index}", "metadata": {}} for index in range(2 if offset == 0 else 1)],
        })

    monkeypatch.setattr(service_module, "urlopen", fake_urlopen)
    client = HindsightMemoryListClient("http://localhost:8888", "project-issues", page_size=2)

    records, scan = client.list_records()

    assert [record["id"] for record in records] == ["memory-0", "memory-1", "memory-2"]
    assert len(requests) == 2
    assert "offset=2" in requests[1]
    assert scan == {"scanned_records": 3, "reported_total_records": 3, "truncated": False}


def test_hindsight_client_reports_a_bounded_scan(monkeypatch) -> None:
    def fake_urlopen(request, timeout=60):
        return FakeResponse({"total": 5, "items": [{"id": "memory"}]})

    monkeypatch.setattr(service_module, "urlopen", fake_urlopen)
    client = HindsightMemoryListClient("http://localhost:8888", "project-issues", page_size=1, max_records=2)

    records, scan = client.list_records()

    assert len(records) == 2
    assert scan["truncated"] is True


def test_service_requires_server_owned_view_and_scope() -> None:
    view = ViewDefinition("latest", ("entity_key",), {"status": FieldRule(("status",))})
    service = LatestStateService(
        client=type("Client", (), {"list_records": lambda self: ([], {})})(),
        views={"latest": view},
        scopes={"project": ScopeDefinition("project")},
    )

    with pytest.raises(ValueError, match="unknown view_id"):
        service.query(view_id="caller-defined", scope="project", as_of=None, include_provenance=True)
    with pytest.raises(ValueError, match="not permitted"):
        service.query(view_id="latest", scope="other", as_of=None, include_provenance=True)

"""MCP service for on-demand deterministic latest-state views."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from engram.pipeline.latest_state_view import (
    FieldRule,
    ScopeDefinition,
    ViewDefinition,
    project_latest_state,
)


DEFAULT_VIEW = ViewDefinition(
    view_id="latest-state",
    entity_paths=("entity_key", "entity_id"),
    fields={"status": FieldRule(paths=("status", "state"))},
    entity_key_options=(
        ("project", "key"),
        ("repo", "kind", "number"),
        ("repo", "number"),
        ("key",),
    ),
)


class HindsightMemoryListClient:
    """Read the bounded, deterministic Hindsight memory-list API."""

    def __init__(self, base_url: str, bank_id: str, *, page_size: int = 1000, max_records: int = 50_000) -> None:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"base_url must be an HTTP(S) URL: {base_url!r}")
        self.base_url = base_url.rstrip("/") + "/"
        self.bank_id = bank_id
        self.page_size = page_size
        self.max_records = max_records

    def list_records(self) -> tuple[list[dict], dict]:
        records: list[dict] = []
        offset = 0
        total: int | None = None
        truncated = False

        while len(records) < self.max_records:
            limit = min(self.page_size, self.max_records - len(records))
            query = urlencode({"limit": limit, "offset": offset})
            path = f"v1/default/banks/{quote(self.bank_id, safe='')}/memories/list?{query}"
            request = Request(urljoin(self.base_url, path), headers={"Accept": "application/json"})
            try:
                with urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read())
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"could not list Hindsight records for bank {self.bank_id!r}: {exc}") from exc

            page = payload.get("items", [])
            if not isinstance(page, list):
                raise RuntimeError("Hindsight memory-list response has a non-array items field")
            records.extend(item for item in page if isinstance(item, dict))
            total_value = payload.get("total")
            if isinstance(total_value, int):
                total = total_value
            if not page or len(page) < limit or (total is not None and offset + len(page) >= total):
                break
            offset += len(page)
        else:
            truncated = True

        if len(records) >= self.max_records and total is not None and total > len(records):
            truncated = True
        return records[: self.max_records], {
            "scanned_records": len(records[: self.max_records]),
            "reported_total_records": total,
            "truncated": truncated,
        }


class RecordProvider(Protocol):
    """Storage-neutral input boundary for the deterministic projector."""

    def list_records(self) -> tuple[list[dict], dict]: ...


class LatestStateService:
    """Apply server-owned view and scope definitions to one Hindsight bank."""

    def __init__(
        self,
        client: RecordProvider,
        views: dict[str, ViewDefinition],
        scopes: dict[str, ScopeDefinition],
    ) -> None:
        self.client = client
        self.views = views
        self.scopes = scopes

    def query(
        self,
        *,
        view_id: str,
        scope: str,
        as_of: str | None,
        include_provenance: bool,
    ) -> dict:
        view = self.views.get(view_id)
        if view is None:
            raise ValueError(f"unknown view_id: {view_id}")
        scope_definition = self.scopes.get(scope)
        if scope_definition is None:
            raise ValueError(f"scope is not permitted: {scope}")
        records, scan = self.client.list_records()
        result = project_latest_state(
            records,
            view,
            scope_definition,
            as_of=as_of,
            include_provenance=include_provenance,
        )
        result["freshness"].update(scan)
        return result


def _load_views(path: Path | None) -> dict[str, ViewDefinition]:
    if path is None:
        return {DEFAULT_VIEW.view_id: DEFAULT_VIEW}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load view definitions from {path}: {exc}") from exc
    definitions = value if isinstance(value, list) else [value]
    views = {}
    for definition in definitions:
        view = ViewDefinition.from_dict(definition)
        if view.view_id in views:
            raise ValueError(f"duplicate view_id: {view.view_id}")
        views[view.view_id] = view
    return views


def _parse_metadata_equals(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        key, separator, expected = value.partition("=")
        if not separator or not key:
            raise ValueError(f"scope metadata must use key=value: {value!r}")
        result[key] = expected
    return result


def _run_mcp_server(
    *,
    base_url: str,
    bank_id: str,
    view_config: Path | None = None,
    scope_name: str = "default",
    scope_tags: list[str] | None = None,
    scope_metadata: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8899,
    transport: str = "stdio",
    page_size: int = 1000,
    max_records: int = 50_000,
) -> None:
    from engram import mcp_compat

    service = LatestStateService(
        HindsightMemoryListClient(base_url, bank_id, page_size=page_size, max_records=max_records),
        _load_views(view_config),
        {
            scope_name: ScopeDefinition(
                name=scope_name,
                metadata_equals=_parse_metadata_equals(scope_metadata or []),
                required_tags=frozenset(scope_tags or []),
            )
        },
    )
    mcp = mcp_compat.make_server("engram-latest-state", host=host, port=port)

    @mcp.tool()
    def query_latest_state_view(
        view_id: str = "latest-state",
        scope: str = "default",
        as_of: str | None = None,
        include_provenance: bool = True,
    ) -> str:
        """Return a deterministic latest-state projection for a permitted view."""
        try:
            result = service.query(
                view_id=view_id,
                scope=scope,
                as_of=as_of,
                include_provenance=include_provenance,
            )
        except (RuntimeError, ValueError) as exc:
            result = {"error": str(exc)}
        return json.dumps(result, default=str)

    if transport == "stdio":
        mcp_compat.run_server(mcp, transport="stdio")
    else:
        mcp_compat.run_server(mcp, transport=transport, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("HINDSIGHT_URL", "http://localhost:8888"))
    parser.add_argument("--bank-id", required=True)
    parser.add_argument("--view-config", type=Path)
    parser.add_argument("--scope-name", default="default")
    parser.add_argument("--scope-tag", action="append", default=[])
    parser.add_argument("--scope-metadata", action="append", default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-records", type=int, default=50_000)
    args = parser.parse_args()
    _run_mcp_server(
        base_url=args.base_url,
        bank_id=args.bank_id,
        view_config=args.view_config,
        scope_name=args.scope_name,
        scope_tags=args.scope_tag,
        scope_metadata=args.scope_metadata,
        host=args.host,
        port=args.port,
        transport=args.transport,
        page_size=args.page_size,
        max_records=args.max_records,
    )


if __name__ == "__main__":
    main()

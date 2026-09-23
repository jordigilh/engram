"""One-shot CocoIndex materializer for a selected-file code overlay.

This is a spike entry point used by ``engram-code-overlay``. It deliberately
uses a separate state database and table; it does not touch the production
``cocoindex.code_embeddings`` corpus.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
from typing import Any

import cocoindex as coco
from cocoindex.connectors import localfs, postgres
from cocoindex.resources.file import PatternFilePathMatcher

from engram import chunking


@dataclasses.dataclass
class OverlayCodeEmbedding:
    id: str
    filepath: str
    chunk_index: int
    code: str
    embedding: list[float]
    search_text: str


PG_POOL: coco.ContextKey[Any] = coco.ContextKey("code_overlay_pg_pool")
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder):
    builder.settings.db_path = pathlib.Path(
        os.environ.get("COCOINDEX_DB", os.path.expanduser("~/.engram/cocoindex-overlay.db"))
    )
    pool = await postgres.create_pool(
        os.environ.get(
            "COCOINDEX_PG_URL",
            "postgresql://hindsight:hindsight@localhost:5432/hindsight",
        ),
        min_size=1,
        max_size=2,
    )
    builder.provide(PG_POOL, pool)
    yield
    pool.close()


@coco.fn(memo=True)
async def process_overlay_file(
    file: localfs.File,
    table: Any,
    base_dir: pathlib.Path,
    repo_tag: str,
) -> None:
    content = await file.read_text()
    if not content or not content.strip():
        return
    absolute = str(file.file_path.resolve())
    prefix = str(base_dir) + "/"
    relative = absolute.removeprefix(prefix)
    filepath = f"{repo_tag}/{relative}"
    chunks = chunking.split_code(content, filename=filepath, chunk_size=1000, chunk_overlap=300)
    embeddings = await chunking.embed_code_chunks(chunks)
    for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        table.declare_row(
            row=OverlayCodeEmbedding(
                id=f"{filepath}:{index}",
                filepath=filepath,
                chunk_index=index,
                code=chunk,
                embedding=embedding,
                search_text=f"{filepath} {chunk}",
            )
        )


@coco.fn
async def overlay_main(root: pathlib.Path, repo_tag: str, table_name: str) -> None:
    table_name = _identifier(table_name)
    embedding_dim = await chunking.code_embedding_dim()
    schema = await postgres.TableSchema.from_class(
        OverlayCodeEmbedding,
        primary_key=["id"],
        column_overrides={
            "embedding": postgres.PgType(
                f"vector({embedding_dim})",
                encoder=lambda value: "[" + ",".join(str(item) for item in value) + "]",
            ),
        },
    )
    table = await postgres.mount_table_target(
        PG_POOL,
        table_name,
        schema,
        pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")
    function_name = f"update_{table_name}_search_vector"
    trigger_name = f"trg_{table_name}_fts"
    index_name = f"idx_{table_name}_fts"
    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql=f"""
            ALTER TABLE cocoindex.{table_name}
                ADD COLUMN IF NOT EXISTS search_vector tsvector;
            CREATE INDEX IF NOT EXISTS {index_name}
                ON cocoindex.{table_name} USING gin(search_vector);
            CREATE OR REPLACE FUNCTION cocoindex.{function_name}()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS {trigger_name} ON cocoindex.{table_name};
            CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.{table_name}
                FOR EACH ROW EXECUTE FUNCTION cocoindex.{function_name}();
            UPDATE cocoindex.{table_name}
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql=f"""
            DROP TRIGGER IF EXISTS {trigger_name} ON cocoindex.{table_name};
            DROP FUNCTION IF EXISTS cocoindex.{function_name}();
            DROP INDEX IF EXISTS cocoindex.{index_name};
            ALTER TABLE cocoindex.{table_name}
                DROP COLUMN IF EXISTS search_vector;
        """,
    )
    files = localfs.walk_dir(
        root,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*"]),
        live=False,
    )
    await coco.mount_each(
        coco.component_subpath("selected-files"),
        process_overlay_file,
        files.items(),
        table,
        root,
        repo_tag,
    )


def build_overlay(root: pathlib.Path, repo_tag: str, table_name: str) -> None:
    app = coco.App(
        "engram-code-overlay",
        overlay_main,
        root=root,
        repo_tag=repo_tag,
        table_name=table_name,
    )
    app.update_blocking(report_to_stdout=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--repo-tag", required=True)
    parser.add_argument("--table", required=True)
    args = parser.parse_args()
    build_overlay(args.root, args.repo_tag, args.table)


if __name__ == "__main__":
    main()

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

SAS_TABLES = [
    "decisions",
    "decision_revisions",
    "rule_sets",
    "rule_set_revisions",
    "rules",
    "rule_ordering",
    "decision_rule_sets",
    "published_modules",
    "execution_records",
    "rule_change_operations",
]
POLICY_TABLES = [
    "policy_documents",
    "policy_document_versions",
    "policy_sections",
    "extracted_requirements",
    "sas_asset_revisions",
    "sas_asset_dependencies",
    "sas_sync_runs",
    "sas_sync_items",
    "business_term_mappings",
    "requirement_matches",
    "audit_runs",
    "audit_findings",
    "generated_test_cases",
    "test_executions",
    "human_reviews",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


def metadata_for(service):
    metadata = MetaData()
    for name in SAS_TABLES if service == "sas" else POLICY_TABLES:
        Table(
            name,
            metadata,
            Column("id", String(80), primary_key=True),
            Column("parent_id", String(80), index=True),
            Column("revision", String(80)),
            Column("data", JSONB, nullable=False),
            Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
        )
    if service != "sas":
        Table(
            "sas_indexed_assets",
            metadata,
            Column("id", String(80), primary_key=True),
            Column("kind", String(20), index=True),
            Column("parent_id", String(80), index=True),
            Column("revision", String(80)),
            Column("content_hash", String(64)),
            Column("text_hash", String(64)),
            Column("search_text", String),
            Column("embedding_model", String(200)),
            Column("embedding", Vector()),
            Column("raw", JSONB),
            Column("canonical", JSONB),
            Column("deleted", Boolean, default=False),
            Column("sync_time", String(40)),
        )
        # Explicit relational foreign keys for dependency lineage.
        Table(
            "asset_links",
            metadata,
            Column("parent_id", String(80), ForeignKey("sas_indexed_assets.id"), primary_key=True),
            Column("child_id", String(80), ForeignKey("sas_indexed_assets.id"), primary_key=True),
            Column("position", Integer),
        )
    return metadata


class Store:
    def __init__(self, service, url=None):
        self.metadata = metadata_for(service)
        self.engine = create_engine(url or os.environ["DATABASE_URL"], pool_pre_ping=True)

    def table(self, name):
        return self.metadata.tables[name]

    def initialize(self):
        """Create the current schema for a new installation without altering existing data."""
        with self.engine.begin() as connection:
            if "sas_indexed_assets" in self.metadata.tables:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            self.metadata.create_all(connection)

    @contextmanager
    def session(self):
        with Session(self.engine) as session, session.begin():
            yield session

    def get(self, name, id):
        with self.session() as s:
            row = s.execute(self.table(name).select().where(self.table(name).c.id == id)).mappings().first()
            return dict(row) if row else None

    def all(self, name, parent_id=None):
        table = self.table(name)
        query = table.select()
        if parent_id is not None:
            query = query.where(table.c.parent_id == parent_id)
        with self.session() as s:
            return [dict(r) for r in s.execute(query).mappings()]

    def put(self, name, id, data, parent_id=None, revision=None):
        from sqlalchemy.dialects.postgresql import insert

        table = self.table(name)
        values = dict(id=id, data=data, parent_id=parent_id, revision=revision)
        with self.session() as s:
            s.execute(
                insert(table).values(**values).on_conflict_do_update(index_elements=[table.c.id], set_=values)
            )
        return data

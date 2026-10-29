"""Validate this plugin's query against the pinned Linear schema. No network."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("graphql")
from graphql import build_schema, parse, validate

from query import ASSIGNED_QUERY, PAGE_SIZE

SCHEMA_TEXT = (Path(__file__).resolve().parents[1] / "schema" / "schema.graphql").read_text(encoding="utf-8")
SCHEMA = build_schema(SCHEMA_TEXT)

COMPARISON = """
query UpdatedBefore($after: String, $before: DateTimeOrDuration) {
  viewer {
    assignedIssues(
      first: 50
      after: $after
      filter: { updatedAt: { lt: $before } }
    ) {
      nodes { updatedAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def test_sent_query_is_valid():
    assert f"first: {PAGE_SIZE}" in ASSIGNED_QUERY
    errors = validate(SCHEMA, parse(ASSIGNED_QUERY))
    assert errors == []


def test_updated_at_less_than_is_in_the_schema():
    """Official examples do not show this comparison. The pinned schema does."""
    errors = validate(SCHEMA, parse(COMPARISON))
    assert errors == []

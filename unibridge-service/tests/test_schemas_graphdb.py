import pytest
from pydantic import ValidationError
from app.config import settings
from app.schemas import (
    DBConnectionCreate,
    QueryRequest,
    QueryResponse,
    QueryTemplateCreate,
    QueryTemplateExecuteRequest,
)


def test_db_connection_create_accepts_graphdb():
    payload = DBConnectionCreate(
        alias="kg",
        db_type="graphdb",
        host="localhost",
        port=7200,
        database="my-repo",
        username="admin",
        password="pw",
        protocol="http",
    )
    assert payload.db_type == "graphdb"


def test_db_connection_create_rejects_unknown_db_type():
    with pytest.raises(ValidationError):
        DBConnectionCreate(
            alias="x",
            db_type="cassandra",
            host="h",
            port=1,
            database="d",
            username="u",
            password="p",
        )


def test_query_response_graph_field_defaults_to_none():
    resp = QueryResponse(columns=["a"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=1)
    assert resp.graph is None


def test_query_response_graph_field_accepts_turtle():
    resp = QueryResponse(
        columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=1,
        graph="@prefix ex: <http://example.org/> . ex:a ex:b ex:c .",
    )
    assert resp.graph.startswith("@prefix")


@pytest.mark.parametrize(
    "model,payload",
    [
        (QueryRequest, {"database": "kg", "sql": "SELECT ?s WHERE { ?s ?p ?o }"}),
        (
            QueryTemplateCreate,
            {
                "path": "kg/report",
                "name": "KG report",
                "database": "kg",
                "sql": "SELECT ?s WHERE { ?s ?p ?o }",
            },
        ),
        (QueryTemplateExecuteRequest, {}),
    ],
)
def test_query_timeout_is_capped_at_connection_limit(model, payload):
    model(**payload, timeout=300)
    with pytest.raises(ValidationError):
        model(**payload, timeout=301)


def test_query_request_limit_is_capped_at_max_row_limit():
    """The API boundary rejects limits above the executor's hard row ceiling."""
    assert QueryRequest(database="main", sql="SELECT 1", limit=1).limit == 1
    assert (
        QueryRequest(database="main", sql="SELECT 1", limit=settings.MAX_ROW_LIMIT).limit
        == settings.MAX_ROW_LIMIT
    )

    with pytest.raises(ValidationError):
        QueryRequest(database="main", sql="SELECT 1", limit=2_000_000)
    with pytest.raises(ValidationError):
        QueryRequest(database="main", sql="SELECT 1", limit=0)


def test_query_request_limit_bound_matches_settings():
    """Field constraints must be literals, so guard against drift from config."""
    assert settings.MAX_ROW_LIMIT == 1_000_000
    metadata = QueryRequest.model_fields["limit"].metadata
    assert any(getattr(m, "le", None) == settings.MAX_ROW_LIMIT for m in metadata)

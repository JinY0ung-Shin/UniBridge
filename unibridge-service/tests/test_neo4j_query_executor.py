import time

import pytest

from app.services.query_executor import execute_neo4j_query


class FakeRecord:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class FakeResult:
    def __init__(self, keys, records):
        self._keys = keys
        self._records = records

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self, result=None, delay=0):
        self.result = result or FakeResult([], [])
        self.delay = delay
        self.query = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.query = query
        self.params = params
        if self.delay:
            time.sleep(self.delay)
        return self.result


class FakeDriver:
    def __init__(self, session):
        self._session = session
        self.database = None

    def session(self, *, database):
        self.database = database
        return self._session


@pytest.mark.asyncio
async def test_execute_neo4j_query_limits_rows_and_returns_metadata():
    result = FakeResult(
        ["name", "age"],
        [
            FakeRecord(["Alice", 30]),
            FakeRecord(["Bob", 31]),
            FakeRecord(["Carol", 32]),
        ],
    )
    session = FakeSession(result)
    driver = FakeDriver(session)

    response = await execute_neo4j_query(
        driver,
        "neo4j",
        "MATCH (n) RETURN n.name AS name, n.age AS age",
        {"active": True},
        limit=2,
    )

    assert response.columns == ["name", "age"]
    assert response.rows == [["Alice", 30], ["Bob", 31]]
    assert response.row_count == 2
    assert response.truncated is True
    assert response.elapsed_ms >= 0
    assert session.query == "MATCH (n) RETURN n.name AS name, n.age AS age"
    assert session.params == {"active": True}
    assert driver.database == "neo4j"


@pytest.mark.asyncio
async def test_execute_neo4j_query_uses_empty_params_when_params_is_none():
    session = FakeSession(FakeResult(["n"], [FakeRecord(["node"])]))
    driver = FakeDriver(session)

    await execute_neo4j_query(driver, "neo4j", "MATCH (n) RETURN n", params=None, limit=1)

    assert session.params == {}


@pytest.mark.asyncio
async def test_execute_neo4j_query_times_out():
    session = FakeSession(FakeResult(["n"], [FakeRecord(["node"])]), delay=0.05)
    driver = FakeDriver(session)

    with pytest.raises(TimeoutError):
        await execute_neo4j_query(
            driver,
            "neo4j",
            "MATCH (n) RETURN n",
            limit=1,
            timeout=0.001,
        )

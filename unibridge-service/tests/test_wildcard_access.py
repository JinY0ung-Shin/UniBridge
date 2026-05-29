from app.auth import ApiKeyUser


def _db_allowed(user: ApiKeyUser, database: str) -> bool:
    return "*" in user.allowed_databases or database in user.allowed_databases


def test_wildcard_allows_any_database():
    u = ApiKeyUser(consumer_name="self_x", allowed_databases=["*"], allowed_routes=["query-api"])
    assert _db_allowed(u, "anything")


def test_explicit_list_still_scoped():
    u = ApiKeyUser(consumer_name="k", allowed_databases=["mydb"], allowed_routes=[])
    assert _db_allowed(u, "mydb")
    assert not _db_allowed(u, "other")

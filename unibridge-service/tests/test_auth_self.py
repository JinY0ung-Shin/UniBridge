from app.auth import ALL_PERMISSIONS, ROLE_PRIORITY, CurrentUser


def test_apikeys_self_permission_registered():
    assert "apikeys.self" in ALL_PERMISSIONS


def test_role_priority_is_admin_user_only():
    assert ROLE_PRIORITY == ["admin", "user"]


def test_current_user_has_sub_field():
    u = CurrentUser(username="alice", role="user", sub="abc-123")
    assert u.sub == "abc-123"

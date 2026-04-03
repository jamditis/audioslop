"""Tests for user and invite CRUD in db.py."""

import sys
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import (
    init_db,
    create_job,
    list_jobs,
    create_user,
    get_user_by_name,
    get_user_by_id,
    list_users,
    delete_user,
    count_users,
    create_invite,
    get_invite_by_token,
    list_invites,
    use_invite,
    delete_invite,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def test_create_user_returns_id(db_path):
    user_id = create_user(db_path, name="alice", password_hash="hashed_pw")
    assert user_id is not None
    assert len(user_id) == 12


def test_create_user_default_not_admin(db_path):
    user_id = create_user(db_path, name="bob", password_hash="hashed_pw")
    user = get_user_by_id(db_path, user_id)
    assert user["is_admin"] == 0


def test_create_user_as_admin(db_path):
    user_id = create_user(db_path, name="admin", password_hash="hashed_pw", is_admin=1)
    user = get_user_by_id(db_path, user_id)
    assert user["is_admin"] == 1


def test_create_user_with_invite_id(db_path):
    user_id = create_user(db_path, name="carol", password_hash="hashed_pw", invite_id="inv123")
    user = get_user_by_id(db_path, user_id)
    assert user["invite_id"] == "inv123"


def test_create_user_without_invite_id(db_path):
    user_id = create_user(db_path, name="dave", password_hash="hashed_pw")
    user = get_user_by_id(db_path, user_id)
    assert user["invite_id"] is None


def test_get_user_by_name_found(db_path):
    user_id = create_user(db_path, name="eve", password_hash="hashed_pw")
    user = get_user_by_name(db_path, "eve")
    assert user is not None
    assert user["id"] == user_id
    assert user["name"] == "eve"
    assert user["password_hash"] == "hashed_pw"


def test_get_user_by_name_not_found(db_path):
    result = get_user_by_name(db_path, "nobody")
    assert result is None


def test_get_user_by_id_found(db_path):
    user_id = create_user(db_path, name="frank", password_hash="hashed_pw")
    user = get_user_by_id(db_path, user_id)
    assert user is not None
    assert user["id"] == user_id
    assert user["name"] == "frank"


def test_get_user_by_id_not_found(db_path):
    result = get_user_by_id(db_path, "doesnotexist")
    assert result is None


def test_list_users_empty(db_path):
    users = list_users(db_path)
    assert users == []


def test_list_users_multiple(db_path):
    create_user(db_path, name="alpha", password_hash="h1")
    create_user(db_path, name="beta", password_hash="h2")
    create_user(db_path, name="gamma", password_hash="h3")
    users = list_users(db_path)
    assert len(users) == 3
    names = {u["name"] for u in users}
    assert names == {"alpha", "beta", "gamma"}


def test_count_users_zero(db_path):
    assert count_users(db_path) == 0


def test_count_users_nonzero(db_path):
    create_user(db_path, name="u1", password_hash="h1")
    create_user(db_path, name="u2", password_hash="h2")
    assert count_users(db_path) == 2


def test_delete_user(db_path):
    user_id = create_user(db_path, name="grace", password_hash="hashed_pw")
    assert get_user_by_id(db_path, user_id) is not None
    delete_user(db_path, user_id)
    assert get_user_by_id(db_path, user_id) is None


def test_delete_user_nonexistent_is_noop(db_path):
    # Should not raise
    delete_user(db_path, "nosuchid")


def test_user_name_is_unique(db_path):
    create_user(db_path, name="heidi", password_hash="h1")
    with pytest.raises(Exception):
        create_user(db_path, name="heidi", password_hash="h2")


def test_user_has_created_at(db_path):
    user_id = create_user(db_path, name="ivan", password_hash="hashed_pw")
    user = get_user_by_id(db_path, user_id)
    assert user["created_at"] is not None


# ---------------------------------------------------------------------------
# jobs table user_id column
# ---------------------------------------------------------------------------

def test_create_job_with_user_id(db_path):
    user_id = create_user(db_path, name="judy", password_hash="hashed_pw")
    job_id = create_job(db_path, "book.pdf", user_id=user_id)
    from db import get_job
    job = get_job(db_path, job_id)
    assert job["user_id"] == user_id


def test_create_job_without_user_id(db_path):
    job_id = create_job(db_path, "anon.pdf")
    from db import get_job
    job = get_job(db_path, job_id)
    assert job["user_id"] is None


def test_list_jobs_filtered_by_user_id(db_path):
    uid1 = create_user(db_path, name="user1", password_hash="h1")
    uid2 = create_user(db_path, name="user2", password_hash="h2")
    create_job(db_path, "a.pdf", user_id=uid1)
    create_job(db_path, "b.pdf", user_id=uid1)
    create_job(db_path, "c.pdf", user_id=uid2)

    jobs_u1 = list_jobs(db_path, user_id=uid1)
    assert len(jobs_u1) == 2
    assert all(j["user_id"] == uid1 for j in jobs_u1)

    jobs_u2 = list_jobs(db_path, user_id=uid2)
    assert len(jobs_u2) == 1
    assert jobs_u2[0]["user_id"] == uid2


def test_list_jobs_no_filter_returns_all(db_path):
    uid = create_user(db_path, name="alluser", password_hash="h1")
    create_job(db_path, "a.pdf", user_id=uid)
    create_job(db_path, "b.pdf")
    jobs = list_jobs(db_path)
    assert len(jobs) == 2


# ---------------------------------------------------------------------------
# Invite CRUD
# ---------------------------------------------------------------------------

def test_create_invite_returns_id_and_token(db_path):
    result = create_invite(db_path, created_by="adminuser")
    assert "id" in result
    assert "token" in result
    assert len(result["token"]) >= 32  # secrets.token_urlsafe(24) = 32 chars


def test_create_invite_default_no_expiry(db_path):
    result = create_invite(db_path, created_by="adminuser")
    invite = get_invite_by_token(db_path, result["token"])
    assert invite["expires_at"] is None


def test_create_invite_with_expiry(db_path):
    result = create_invite(db_path, created_by="adminuser", expires_at="2030-01-01 00:00:00")
    invite = get_invite_by_token(db_path, result["token"])
    assert invite["expires_at"] == "2030-01-01 00:00:00"


def test_get_invite_by_token_found(db_path):
    result = create_invite(db_path, created_by="adminuser")
    invite = get_invite_by_token(db_path, result["token"])
    assert invite is not None
    assert invite["id"] == result["id"]
    assert invite["created_by"] == "adminuser"
    assert invite["used_by"] is None
    assert invite["used_at"] is None


def test_get_invite_by_token_not_found(db_path):
    result = get_invite_by_token(db_path, "no-such-token")
    assert result is None


def test_list_invites_empty(db_path):
    invites = list_invites(db_path)
    assert invites == []


def test_list_invites_multiple(db_path):
    create_invite(db_path, created_by="admin")
    create_invite(db_path, created_by="admin")
    create_invite(db_path, created_by="admin")
    invites = list_invites(db_path)
    assert len(invites) == 3


def test_use_invite(db_path):
    result = create_invite(db_path, created_by="admin")
    token = result["token"]

    use_invite(db_path, token, used_by="newuser_id")

    invite = get_invite_by_token(db_path, token)
    assert invite["used_by"] == "newuser_id"
    assert invite["used_at"] is not None


def test_use_invite_nonexistent_is_noop(db_path):
    # Should not raise
    use_invite(db_path, "no-such-token", used_by="someone")


def test_delete_invite(db_path):
    result = create_invite(db_path, created_by="admin")
    invite_id = result["id"]

    delete_invite(db_path, invite_id)

    invites = list_invites(db_path)
    assert all(i["id"] != invite_id for i in invites)


def test_delete_invite_nonexistent_is_noop(db_path):
    # Should not raise
    delete_invite(db_path, "nosuchid")


def test_invite_token_is_unique(db_path):
    """Two invites must have different tokens."""
    r1 = create_invite(db_path, created_by="admin")
    r2 = create_invite(db_path, created_by="admin")
    assert r1["token"] != r2["token"]

# audioslop deployment implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy audioslop as a two-node system -- Flask on landofjawn (always-on, public via Cloudflare Tunnel) and GPU worker on Legion (polling over Tailscale) -- with individual user accounts, invite links, and audio served from Cloudflare R2.

**Architecture:** Web server on landofjawn handles auth, uploads, text cleaning, and job management. Legion runs a standalone worker that polls for synthesis jobs, processes them with F5-TTS, uploads audio to R2, and reports completion. SQLite stays as the DB, now with users/invites tables.

**Tech Stack:** Python/Flask, SQLite, werkzeug (password hashing), boto3 (R2/S3), requests (worker HTTP client), pynvml (VRAM checking)

**Spec:** `docs/superpowers/specs/2026-04-03-deployment-design.md`

---

## File map

### New files
| File | Responsibility |
|------|---------------|
| `r2.py` | R2 client: upload files, generate presigned URLs, delete objects |
| `worker_remote.py` | Standalone GPU worker for Legion -- polls landofjawn, synthesizes, uploads to R2 |
| `manage.py` | CLI admin tool -- create admin user, generate invite links |
| `tests/test_auth.py` | Tests for user/invite DB functions and auth flows |
| `tests/test_worker_api.py` | Tests for worker API endpoints |
| `tests/test_r2.py` | Tests for R2 client (mocked boto3) |
| `templates/signup.html` | Invite link signup page |
| `templates/setup.html` | First-run admin account creation |
| `templates/admin.html` | Admin page -- user list + invite management |

### Modified files
| File | Changes |
|------|---------|
| `db.py` | Add users/invites tables, user CRUD, invite CRUD, user_id on jobs |
| `app.py` | Replace password auth with user auth, add worker API, admin routes, R2 audio serving, inline cleaning |
| `templates/base.html` | Update nav for user name + admin link |
| `templates/login.html` | Add username field |
| `templates/player.html` | Audio src from R2 presigned URL instead of local path |
| `templates/upload.html` | No functional changes needed (jobs scoped server-side) |
| `templates/review.html` | No functional changes needed |

### Removed files
| File | Reason |
|------|--------|
| `worker.py` | Replaced by `worker_remote.py` (remote worker model) |

---

## Task 1: Database -- users and invites

**Files:**
- Modify: `db.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests for user CRUD**

Create `tests/test_auth.py`:

```python
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import (
    init_db,
    create_user,
    get_user_by_name,
    get_user_by_id,
    list_users,
    delete_user,
    count_users,
)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_create_and_get_user(db_path):
    user_id = create_user(db_path, "alice", "hashed_pw_123")
    assert user_id is not None
    assert len(user_id) == 12

    user = get_user_by_id(db_path, user_id)
    assert user["name"] == "alice"
    assert user["password_hash"] == "hashed_pw_123"
    assert user["is_admin"] == 0


def test_create_admin_user(db_path):
    user_id = create_user(db_path, "admin", "hashed", is_admin=1)
    user = get_user_by_id(db_path, user_id)
    assert user["is_admin"] == 1


def test_get_user_by_name(db_path):
    create_user(db_path, "bob", "hashed")
    user = get_user_by_name(db_path, "bob")
    assert user is not None
    assert user["name"] == "bob"


def test_get_user_by_name_missing(db_path):
    assert get_user_by_name(db_path, "nobody") is None


def test_list_users(db_path):
    create_user(db_path, "a", "h1")
    create_user(db_path, "b", "h2")
    users = list_users(db_path)
    assert len(users) == 2


def test_delete_user(db_path):
    uid = create_user(db_path, "doomed", "h")
    delete_user(db_path, uid)
    assert get_user_by_id(db_path, uid) is None


def test_count_users(db_path):
    assert count_users(db_path) == 0
    create_user(db_path, "one", "h")
    assert count_users(db_path) == 1


def test_duplicate_name_raises(db_path):
    create_user(db_path, "alice", "h1")
    with pytest.raises(Exception):
        create_user(db_path, "alice", "h2")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auth.py -v`
Expected: ImportError -- `create_user` not found in db.py

- [ ] **Step 3: Implement user CRUD in db.py**

Add to `db.py` after the existing imports:

```python
from werkzeug.security import generate_password_hash, check_password_hash
```

Add to `init_db()` inside the `executescript`, after the segments table:

```sql
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    invite_id TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    expires_at TIMESTAMP,
    used_by TEXT,
    used_at TIMESTAMP
);
```

Add user_id to jobs table creation (modify existing CREATE TABLE jobs):

```sql
-- Add after error_detail TEXT line:
user_id TEXT
```

Add these functions after `delete_job_cascade`:

```python
def create_user(
    db_path: str,
    name: str,
    password_hash: str,
    is_admin: int = 0,
    invite_id: Optional[str] = None,
) -> str:
    """Create a new user and return their id."""
    user_id = uuid.uuid4().hex[:12]
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO users (id, name, password_hash, is_admin, invite_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, name, password_hash, is_admin, invite_id),
        )
    conn.close()
    return user_id


def get_user_by_id(db_path: str, user_id: str) -> Optional[dict]:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_name(db_path: str, name: str) -> Optional[dict]:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users(db_path: str) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_user(db_path: str, user_id: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.close()


def count_users(db_path: str) -> int:
    conn = _connect(db_path)
    row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
    conn.close()
    return row["cnt"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Write failing tests for invite CRUD**

Append to `tests/test_auth.py`:

```python
from db import (
    create_invite,
    get_invite_by_token,
    list_invites,
    use_invite,
    delete_invite,
)


def test_create_and_get_invite(db_path):
    admin_id = create_user(db_path, "admin", "h", is_admin=1)
    invite_id, token = create_invite(db_path, created_by=admin_id)
    assert invite_id is not None
    assert len(token) == 32

    invite = get_invite_by_token(db_path, token)
    assert invite is not None
    assert invite["created_by"] == admin_id
    assert invite["used_by"] is None


def test_use_invite(db_path):
    admin_id = create_user(db_path, "admin", "h", is_admin=1)
    invite_id, token = create_invite(db_path, created_by=admin_id)
    user_id = create_user(db_path, "newbie", "h2", invite_id=invite_id)
    use_invite(db_path, token, user_id)

    invite = get_invite_by_token(db_path, token)
    assert invite["used_by"] == user_id
    assert invite["used_at"] is not None


def test_used_invite_is_consumed(db_path):
    admin_id = create_user(db_path, "admin", "h", is_admin=1)
    _, token = create_invite(db_path, created_by=admin_id)
    use_invite(db_path, token, "someone")
    invite = get_invite_by_token(db_path, token)
    assert invite["used_by"] is not None


def test_list_invites(db_path):
    admin_id = create_user(db_path, "admin", "h", is_admin=1)
    create_invite(db_path, created_by=admin_id)
    create_invite(db_path, created_by=admin_id)
    invites = list_invites(db_path)
    assert len(invites) == 2


def test_delete_invite(db_path):
    admin_id = create_user(db_path, "admin", "h", is_admin=1)
    invite_id, token = create_invite(db_path, created_by=admin_id)
    delete_invite(db_path, invite_id)
    assert get_invite_by_token(db_path, token) is None
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest tests/test_auth.py -v -k invite`
Expected: ImportError -- `create_invite` not found

- [ ] **Step 7: Implement invite CRUD in db.py**

Add these functions to `db.py`:

```python
import secrets


def create_invite(
    db_path: str,
    created_by: str,
    expires_at: Optional[str] = None,
) -> tuple[str, str]:
    """Create an invite and return (invite_id, token)."""
    invite_id = uuid.uuid4().hex[:12]
    token = secrets.token_urlsafe(24)  # 32 chars
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO invites (id, token, created_by, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (invite_id, token, created_by, expires_at),
        )
    conn.close()
    return invite_id, token


def get_invite_by_token(db_path: str, token: str) -> Optional[dict]:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_invites(db_path: str) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM invites ORDER BY rowid DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def use_invite(db_path: str, token: str, user_id: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            "UPDATE invites SET used_by = ?, used_at = CURRENT_TIMESTAMP WHERE token = ?",
            (user_id, token),
        )
    conn.close()


def delete_invite(db_path: str, invite_id: str) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
    conn.close()
```

- [ ] **Step 8: Run all auth tests**

Run: `python -m pytest tests/test_auth.py -v`
Expected: All 13 tests PASS

- [ ] **Step 9: Run full test suite to verify no regressions**

Run: `python -m pytest tests/ -v`
Expected: All existing tests still pass. The new `user_id TEXT` column on jobs defaults to NULL, so existing test_db.py tests are unaffected.

- [ ] **Step 10: Commit**

```bash
git add db.py tests/test_auth.py
git commit -m "feat: add users and invites tables to db layer"
```

---

## Task 2: R2 client module

**Files:**
- Create: `r2.py`
- Test: `tests/test_r2.py`

- [ ] **Step 1: Write failing tests for R2 client**

Create `tests/test_r2.py`:

```python
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_get_client_creates_boto3_client():
    with patch("r2.boto3") as mock_boto3:
        from r2 import get_client
        import r2
        r2._client = None
        client = get_client(
            account_id="test123",
            access_key="ak",
            secret_key="sk",
        )
        mock_boto3.client.assert_called_once_with(
            "s3",
            endpoint_url="https://test123.r2.cloudflarestorage.com",
            aws_access_key_id="ak",
            aws_secret_access_key="sk",
        )


def test_upload_file_calls_s3():
    mock_client = MagicMock()
    with patch("r2.get_client", return_value=mock_client):
        from r2 import upload_file
        upload_file("/tmp/test.wav", "job123/seg_0000.wav")
        mock_client.upload_file.assert_called_once_with(
            "/tmp/test.wav", "audioslop", "job123/seg_0000.wav"
        )


def test_presigned_url():
    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = "https://r2.example.com/signed"
    with patch("r2.get_client", return_value=mock_client):
        from r2 import presigned_url
        url = presigned_url("job123/full.wav", expires_in=3600)
        assert url == "https://r2.example.com/signed"
        mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "audioslop", "Key": "job123/full.wav"},
            ExpiresIn=3600,
        )


def test_delete_prefix():
    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "job123/seg_0000.wav"},
            {"Key": "job123/full.wav"},
        ]
    }
    with patch("r2.get_client", return_value=mock_client):
        from r2 import delete_prefix
        delete_prefix("job123/")
        mock_client.delete_objects.assert_called_once()
        call_args = mock_client.delete_objects.call_args
        assert call_args[1]["Bucket"] == "audioslop"
        objects = call_args[1]["Delete"]["Objects"]
        assert len(objects) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_r2.py -v`
Expected: ModuleNotFoundError -- no module named `r2`

- [ ] **Step 3: Implement r2.py**

Create `r2.py`:

```python
"""Cloudflare R2 storage client for audioslop."""

import os

import boto3

BUCKET = "audioslop"

_client = None


def get_client(
    account_id: str = None,
    access_key: str = None,
    secret_key: str = None,
):
    """Get or create the S3-compatible R2 client."""
    global _client
    if _client is not None:
        return _client
    account_id = account_id or os.environ.get("R2_ACCOUNT_ID", "")
    access_key = access_key or os.environ.get("R2_ACCESS_KEY", "")
    secret_key = secret_key or os.environ.get("R2_SECRET_KEY", "")
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    return _client


def upload_file(local_path: str, r2_key: str) -> None:
    """Upload a local file to R2."""
    client = get_client()
    client.upload_file(local_path, BUCKET, r2_key)


def presigned_url(r2_key: str, expires_in: int = 3600) -> str:
    """Generate a presigned GET URL for an R2 object."""
    client = get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": r2_key},
        ExpiresIn=expires_in,
    )


def delete_prefix(prefix: str) -> None:
    """Delete all objects under a prefix."""
    client = get_client()
    response = client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    contents = response.get("Contents", [])
    if not contents:
        return
    client.delete_objects(
        Bucket=BUCKET,
        Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_r2.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add r2.py tests/test_r2.py
git commit -m "feat: add R2 storage client module"
```

---

## Task 3: Auth routes in Flask app

**Files:**
- Modify: `app.py`
- Create: `templates/setup.html`
- Create: `templates/signup.html`
- Modify: `templates/login.html`
- Modify: `templates/base.html`

This task replaces the single-password auth with user accounts. No tests for Flask routes in this task -- the auth logic lives in db.py (already tested). Route behavior is verified by the end-to-end smoke test.

- [ ] **Step 1: Update login.html for username + password**

Replace the contents of `templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Log in - audioslop{% endblock %}
{% block content %}
<div style="max-width: 360px; margin: 8rem auto 0;" class="animate-in">
    <h1 style="margin-bottom: 0.25rem;">audioslop</h1>
    <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 2.5rem;">Document to audiobook</p>
    {% if error %}
    <p style="color: var(--error); font-size: 0.85rem; margin-bottom: 1rem;">{{ error }}</p>
    {% endif %}
    <form method="POST" action="/login">
        <input type="text" name="name" placeholder="Username"
               class="input" style="margin-bottom: 0.75rem;" autofocus required
               autocomplete="username">
        <input type="password" name="password" placeholder="Password"
               class="input" style="margin-bottom: 1rem;" required
               autocomplete="current-password">
        <button type="submit" class="btn btn-primary" style="width: 100%; justify-content: center;">
            Log in
        </button>
    </form>
</div>
{% endblock %}
```

- [ ] **Step 2: Create setup.html for first-run admin creation**

Create `templates/setup.html`:

```html
{% extends "base.html" %}
{% block title %}Setup - audioslop{% endblock %}
{% block content %}
<div style="max-width: 360px; margin: 8rem auto 0;" class="animate-in">
    <h1 style="margin-bottom: 0.25rem;">Welcome to audioslop</h1>
    <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 2.5rem;">Create your admin account to get started.</p>
    {% if error %}
    <p style="color: var(--error); font-size: 0.85rem; margin-bottom: 1rem;">{{ error }}</p>
    {% endif %}
    <form method="POST" action="/setup">
        <input type="text" name="name" placeholder="Username"
               class="input" style="margin-bottom: 0.75rem;" autofocus required
               autocomplete="username">
        <input type="password" name="password" placeholder="Password"
               class="input" style="margin-bottom: 0.75rem;" required
               autocomplete="new-password">
        <input type="password" name="password_confirm" placeholder="Confirm password"
               class="input" style="margin-bottom: 1rem;" required
               autocomplete="new-password">
        <button type="submit" class="btn btn-primary" style="width: 100%; justify-content: center;">
            Create admin account
        </button>
    </form>
</div>
{% endblock %}
```

- [ ] **Step 3: Create signup.html for invite link signup**

Create `templates/signup.html`:

```html
{% extends "base.html" %}
{% block title %}Sign up - audioslop{% endblock %}
{% block content %}
<div style="max-width: 360px; margin: 8rem auto 0;" class="animate-in">
    <h1 style="margin-bottom: 0.25rem;">You're invited</h1>
    <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 2.5rem;">Create your account to start making audiobooks.</p>
    {% if error %}
    <p style="color: var(--error); font-size: 0.85rem; margin-bottom: 1rem;">{{ error }}</p>
    {% endif %}
    <form method="POST" action="/invite/{{ token }}">
        <input type="text" name="name" placeholder="Username"
               class="input" style="margin-bottom: 0.75rem;" autofocus required
               autocomplete="username">
        <input type="password" name="password" placeholder="Password"
               class="input" style="margin-bottom: 0.75rem;" required
               autocomplete="new-password">
        <input type="password" name="password_confirm" placeholder="Confirm password"
               class="input" style="margin-bottom: 1rem;" required
               autocomplete="new-password">
        <button type="submit" class="btn btn-primary" style="width: 100%; justify-content: center;">
            Create account
        </button>
    </form>
</div>
{% endblock %}
```

- [ ] **Step 4: Update base.html nav for user info**

In `templates/base.html`, replace the nav section (lines 16-21):

```html
    <nav class="nav-bar">
        <a href="/" class="nav-brand">audioslop</a>
        <div style="display:flex; align-items:center; gap:1rem;">
            {% if session.get('user_name') %}
                {% if session.get('is_admin') %}
                <a href="/admin" class="nav-link">admin</a>
                {% endif %}
                <span style="color: var(--text-muted); font-size: 0.85rem;">{{ session.user_name }}</span>
                <a href="/logout" class="nav-link">log out</a>
            {% endif %}
        </div>
    </nav>
```

- [ ] **Step 5: Rewrite auth section in app.py**

Replace the auth imports, PASSWORD constant, and `require_auth` decorator in `app.py`. Add new imports at the top:

```python
from werkzeug.security import generate_password_hash, check_password_hash
```

Add new db imports:

```python
from db import (
    count_users,
    create_invite,
    create_user,
    get_invite_by_token,
    get_user_by_id,
    get_user_by_name,
    use_invite,
    # ... keep existing imports
)
```

Remove the `PASSWORD = os.environ.get(...)` line.

Add `WORKER_API_KEY`:

```python
WORKER_API_KEY = os.environ.get("AUDIOSLOP_WORKER_KEY", "dev-worker-key")
```

Update `require_auth`:

```python
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if count_users(DB_PATH) == 0:
                return redirect(url_for("setup"))
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return "Forbidden.", 403
        return f(*args, **kwargs)
    return decorated
```

Replace the login route:

```python
@app.route("/login", methods=["GET", "POST"])
def login():
    if count_users(DB_PATH) == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_name(DB_PATH, name)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong username or password.")
    return render_template("login.html")
```

Add setup route:

```python
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if count_users(DB_PATH) > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        if not name or not password:
            return render_template("setup.html", error="Name and password required.")
        if password != confirm:
            return render_template("setup.html", error="Passwords don't match.")
        pw_hash = generate_password_hash(password)
        user_id = create_user(DB_PATH, name, pw_hash, is_admin=1)
        session["user_id"] = user_id
        session["user_name"] = name
        session["is_admin"] = True
        return redirect(url_for("index"))
    return render_template("setup.html")
```

Add invite signup route:

```python
@app.route("/invite/<token>", methods=["GET", "POST"])
def invite_signup(token):
    invite = get_invite_by_token(DB_PATH, token)
    if not invite or invite["used_by"]:
        return render_template("login.html", error="This invite link is invalid or already used.")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        if not name or not password:
            return render_template("signup.html", token=token, error="Name and password required.")
        if password != confirm:
            return render_template("signup.html", token=token, error="Passwords don't match.")
        if get_user_by_name(DB_PATH, name):
            return render_template("signup.html", token=token, error="That username is taken.")
        pw_hash = generate_password_hash(password)
        user_id = create_user(DB_PATH, name, pw_hash, invite_id=invite["id"])
        use_invite(DB_PATH, token, user_id)
        session["user_id"] = user_id
        session["user_name"] = name
        session["is_admin"] = False
        return redirect(url_for("index"))
    return render_template("signup.html", token=token)
```

- [ ] **Step 6: Update job creation to include user_id**

In `app.py`, update the `api_upload` function. Change the `create_job` call to include user_id:

```python
    job_id = create_job(
        DB_PATH,
        filename=file.filename,
        speed=speed,
        voice_ref=voice_ref,
        title_pause=title_pause,
        para_pause=para_pause,
        user_id=session.get("user_id"),
    )
```

Update `create_job` in `db.py` to accept and store user_id:

```python
def create_job(
    db_path: str,
    filename: str,
    speed: float = 0.85,
    voice_ref: str = "amditis.wav",
    title_pause: float = 2.0,
    para_pause: float = 0.75,
    user_id: Optional[str] = None,
) -> str:
    """Create a new job and return its id (uuid hex[:12])."""
    job_id = uuid.uuid4().hex[:12]
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO jobs (id, filename, speed, voice_ref, title_pause, para_pause, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, filename, speed, voice_ref, title_pause, para_pause, user_id),
        )
    conn.close()
    return job_id
```

- [ ] **Step 7: Scope job list to current user (non-admins)**

Update the `index` route in `app.py`:

```python
@app.route("/")
@require_auth
def index():
    if session.get("is_admin"):
        jobs = list_jobs(DB_PATH)
    else:
        jobs = list_jobs(DB_PATH, user_id=session["user_id"])
    voices = sorted(p.name for p in REF_DIR.glob("*.wav"))
    return render_template("upload.html", jobs=jobs, voices=voices)
```

Add `user_id` filter to `list_jobs` in `db.py`:

```python
def list_jobs(db_path: str, limit: int = 50, user_id: Optional[str] = None) -> list[dict]:
    """Return jobs ordered newest first, optionally filtered by user."""
    conn = _connect(db_path)
    if user_id:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
```

- [ ] **Step 8: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass. The `list_jobs` signature change is backward-compatible (user_id defaults to None).

- [ ] **Step 9: Commit**

```bash
git add app.py db.py templates/login.html templates/setup.html templates/signup.html templates/base.html
git commit -m "feat: replace password auth with user accounts and invite links"
```

---

## Task 4: Admin page

**Files:**
- Create: `templates/admin.html`
- Modify: `app.py`

- [ ] **Step 1: Create admin.html template**

Create `templates/admin.html`. The admin page shows invite links, users, and worker status. Use safe DOM methods (textContent, createElement) rather than innerHTML for dynamic content. The worker status section uses a `<dl>` with data attributes that JS populates via textContent.

```html
{% extends "base.html" %}
{% block title %}Admin - audioslop{% endblock %}
{% block content %}
<div class="space-y-10">

    <section class="animate-in">
        <h1 class="mb-6">Admin</h1>
    </section>

    <!-- Invite links -->
    <section class="animate-in animate-delay-1">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;">
            <h2>Invite links</h2>
            <button id="create-invite-btn" class="btn btn-primary">New invite</button>
        </div>

        <div id="new-invite-url" class="hidden" style="margin-bottom:1rem;">
            <div class="status-bar status-done" style="display:flex; align-items:center; gap:0.75rem; flex-wrap:wrap;">
                <span style="font-size:0.85rem;">Invite link created:</span>
                <code id="invite-url-text" style="font-size:0.85rem; word-break:break-all;"></code>
                <button id="copy-invite-btn" class="btn btn-ghost" style="padding:0.3rem 0.6rem; font-size:0.8rem;">Copy</button>
            </div>
        </div>

        {% if invites %}
        <div class="overflow-x-auto">
            <table class="table">
                <thead>
                    <tr>
                        <th>Token</th>
                        <th>Created by</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for inv in invites %}
                    <tr>
                        <td><code style="font-size:0.8rem;">{{ inv.token[:12] }}...</code></td>
                        <td>{{ inv.creator_name or inv.created_by }}</td>
                        <td>
                            {% if inv.used_by %}
                            <span class="badge badge-done">used</span>
                            {% else %}
                            <span class="badge badge-review">open</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if not inv.used_by %}
                            <button class="btn btn-danger delete-invite-btn" data-invite-id="{{ inv.id }}">Revoke</button>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p style="color: var(--text-muted); font-size: 0.875rem;">No invite links yet.</p>
        {% endif %}
    </section>

    <!-- Users -->
    <section class="animate-in animate-delay-2">
        <h2 class="mb-4">Users</h2>

        {% if users %}
        <div class="overflow-x-auto">
            <table class="table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Role</th>
                        <th>Joined</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for u in users %}
                    <tr>
                        <td>{{ u.name }}</td>
                        <td>
                            {% if u.is_admin %}
                            <span class="badge badge-done">admin</span>
                            {% else %}
                            <span class="badge badge-pending">user</span>
                            {% endif %}
                        </td>
                        <td style="white-space:nowrap; color:var(--text-muted);">{{ u.created_at }}</td>
                        <td>
                            {% if not u.is_admin and u.id != session.user_id %}
                            <button class="btn btn-danger delete-user-btn" data-user-id="{{ u.id }}">Remove</button>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
    </section>

    <!-- Worker status -->
    <section class="animate-in animate-delay-3">
        <h2 class="mb-4">Worker status</h2>
        <div id="worker-status" class="card" style="font-size:0.875rem; color:var(--text-muted);">
            <span id="worker-status-text">Checking...</span>
        </div>
    </section>

</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
    'use strict';

    // Create invite
    document.getElementById('create-invite-btn').addEventListener('click', function () {
        fetch('/api/admin/invites', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.invite_url) {
                document.getElementById('invite-url-text').textContent = data.invite_url;
                document.getElementById('new-invite-url').classList.remove('hidden');
            }
        });
    });

    // Copy invite URL
    document.getElementById('copy-invite-btn').addEventListener('click', function () {
        var text = document.getElementById('invite-url-text').textContent;
        navigator.clipboard.writeText(text);
        this.textContent = 'Copied';
        var btn = this;
        setTimeout(function () { btn.textContent = 'Copy'; }, 2000);
    });

    // Delete invite
    document.querySelectorAll('.delete-invite-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-invite-id');
            fetch('/api/admin/invites/' + id, { method: 'DELETE' })
            .then(function (r) { if (r.ok) window.location.reload(); });
        });
    });

    // Delete user
    document.querySelectorAll('.delete-user-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var id = btn.getAttribute('data-user-id');
            if (!confirm('Remove this user?')) return;
            fetch('/api/admin/users/' + id, { method: 'DELETE' })
            .then(function (r) { if (r.ok) window.location.reload(); });
        });
    });

    // Worker heartbeat -- build DOM safely with textContent
    var statusEl = document.getElementById('worker-status-text');
    fetch('/api/admin/worker-status')
    .then(function (r) { return r.json(); })
    .then(function (data) {
        if (data.last_seen) {
            var parts = [
                data.hostname,
                ' -- ',
                data.gpu_name,
                ' (' + data.gpu_memory_used_mb + '/' + data.gpu_memory_total_mb + ' MB)',
                ' -- Last seen: ' + data.last_seen,
            ];
            if (data.current_job_id) {
                parts.push(' -- Working on: ' + data.current_job_id);
            } else {
                parts.push(' -- Idle');
            }
            statusEl.textContent = parts.join('');
        } else {
            statusEl.textContent = 'No worker has checked in yet.';
        }
    })
    .catch(function () {
        statusEl.textContent = 'Could not reach worker status endpoint.';
    });
}());
</script>
{% endblock %}
```

- [ ] **Step 2: Add admin routes to app.py**

Add admin page route and API endpoints:

```python
# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin")
@require_auth
@require_admin
def admin_page():
    users = list_users(DB_PATH)
    invites = list_invites(DB_PATH)
    user_map = {u["id"]: u["name"] for u in users}
    for inv in invites:
        inv["creator_name"] = user_map.get(inv["created_by"], "unknown")
    return render_template("admin.html", users=users, invites=invites)


@app.route("/api/admin/invites", methods=["POST"])
@require_auth
@require_admin
def api_create_invite():
    invite_id, token = create_invite(DB_PATH, created_by=session["user_id"])
    invite_url = request.host_url.rstrip("/") + "/invite/" + token
    return jsonify({"invite_id": invite_id, "token": token, "invite_url": invite_url}), 201


@app.route("/api/admin/invites/<invite_id>", methods=["DELETE"])
@require_auth
@require_admin
def api_delete_invite(invite_id):
    delete_invite(DB_PATH, invite_id)
    return jsonify({"ok": True})


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
@require_auth
@require_admin
def api_delete_user(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"error": "Cannot delete yourself."}), 400
    delete_user(DB_PATH, user_id)
    return jsonify({"ok": True})
```

Also add a worker status store and endpoint (in-memory, updated by heartbeat):

```python
_worker_status = {}


@app.route("/api/admin/worker-status")
@require_auth
@require_admin
def api_worker_status():
    return jsonify(_worker_status if _worker_status else {})
```

Add the `delete_invite`, `list_invites`, `list_users`, `delete_user` imports to the db import block at the top of app.py.

- [ ] **Step 3: Manually verify admin page**

Run: `python app.py`
Visit: `http://localhost:5000/setup` -- create admin, then go to `/admin`
Expected: see invite generation, user list, worker status placeholder

- [ ] **Step 4: Commit**

```bash
git add templates/admin.html app.py
git commit -m "feat: add admin page with invite and user management"
```

---

## Task 5: Worker API endpoints

**Files:**
- Modify: `app.py`
- Test: `tests/test_worker_api.py`

- [ ] **Step 1: Write failing tests for worker API**

Create `tests/test_worker_api.py`:

```python
import json
import pytest
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["AUDIOSLOP_WORKER_KEY"] = "test-worker-key"

from app import app
from db import init_db, create_job, create_segment, update_job


@pytest.fixture
def client(tmp_path):
    test_db = str(tmp_path / "test.db")
    app.config["TESTING"] = True

    import app as app_module
    original_db = app_module.DB_PATH
    app_module.DB_PATH = test_db
    init_db(test_db)

    with app.test_client() as c:
        yield c, test_db

    app_module.DB_PATH = original_db


def auth_header():
    return {"Authorization": "Bearer test-worker-key"}


def test_worker_jobs_requires_auth(client):
    c, db = client
    r = c.get("/api/worker/jobs?status=synthesizing")
    assert r.status_code == 401


def test_worker_jobs_wrong_key(client):
    c, db = client
    r = c.get("/api/worker/jobs?status=synthesizing", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_worker_jobs_returns_synthesizing(client):
    c, db = client
    job_id = create_job(db, "test.pdf")
    update_job(db, job_id, status="synthesizing")
    r = c.get("/api/worker/jobs?status=synthesizing", headers=auth_header())
    assert r.status_code == 200
    data = r.get_json()
    assert len(data) == 1
    assert data[0]["id"] == job_id


def test_worker_segments(client):
    c, db = client
    job_id = create_job(db, "test.pdf")
    create_segment(db, job_id, 0, source_text="Hello world.")
    r = c.get("/api/worker/job/" + job_id + "/segments", headers=auth_header())
    assert r.status_code == 200
    data = r.get_json()
    assert len(data) == 1
    assert data[0]["source_text"] == "Hello world."


def test_worker_segment_complete(client):
    c, db = client
    job_id = create_job(db, "test.pdf")
    create_segment(db, job_id, 0, source_text="Hello.")
    update_job(db, job_id, status="synthesizing", segments_total=1)
    r = c.post(
        "/api/worker/job/" + job_id + "/segment/0/complete",
        headers=auth_header(),
        json={
            "audio_r2_key": "job123/seg_0000.wav",
            "accuracy": 0.95,
            "duration_seconds": 3.5,
            "word_timings": [],
        },
    )
    assert r.status_code == 200


def test_worker_job_complete(client):
    c, db = client
    job_id = create_job(db, "test.pdf")
    update_job(db, job_id, status="synthesizing")
    r = c.post(
        "/api/worker/job/" + job_id + "/complete",
        headers=auth_header(),
        json={"final_audio_r2_key": "job123/full.wav"},
    )
    assert r.status_code == 200


def test_worker_job_fail(client):
    c, db = client
    job_id = create_job(db, "test.pdf")
    update_job(db, job_id, status="synthesizing")
    r = c.post(
        "/api/worker/job/" + job_id + "/fail",
        headers=auth_header(),
        json={"error_msg": "CUDA OOM", "error_detail": "traceback..."},
    )
    assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worker_api.py -v`
Expected: 404 errors -- worker API routes don't exist yet

- [ ] **Step 3: Add worker API auth decorator to app.py**

```python
def require_worker_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + WORKER_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
```

- [ ] **Step 4: Add worker API endpoints to app.py**

```python
# ---------------------------------------------------------------------------
# Worker API (called by Legion GPU worker over Tailscale)
# ---------------------------------------------------------------------------

@app.route("/api/worker/jobs")
@require_worker_auth
def api_worker_jobs():
    status = request.args.get("status", "synthesizing")
    jobs = list_jobs(DB_PATH)
    filtered = [j for j in jobs if j["status"] == status]
    filtered.reverse()  # Oldest first for FIFO
    return jsonify([{
        "id": j["id"],
        "filename": j["filename"],
        "speed": j["speed"],
        "voice_ref": j["voice_ref"],
        "title_pause": j["title_pause"],
        "para_pause": j["para_pause"],
        "segments_total": j["segments_total"],
    } for j in filtered])


@app.route("/api/worker/job/<job_id>/segments")
@require_worker_auth
def api_worker_segments(job_id):
    segments = get_segments(DB_PATH, job_id)
    return jsonify([{
        "seg_index": s["seg_index"],
        "source_text": s["source_text"],
        "is_title": s["is_title"],
        "pause_after": s["pause_after"],
        "audio_file": s["audio_file"],
    } for s in segments])


@app.route("/api/worker/ref/<filename>")
@require_worker_auth
def api_worker_ref(filename):
    return send_from_directory(str(REF_DIR), filename)


@app.route("/api/worker/job/<job_id>/segment/<int:idx>/complete", methods=["POST"])
@require_worker_auth
def api_worker_segment_complete(job_id, idx):
    data = request.get_json(silent=True) or {}
    update_segment(
        DB_PATH, job_id, idx,
        audio_file=data.get("audio_r2_key", ""),
        accuracy=data.get("accuracy"),
        duration_seconds=data.get("duration_seconds"),
        word_timings_json=json.dumps(data.get("word_timings", [])),
    )
    job = get_job(DB_PATH, job_id)
    if job:
        done = idx + 1
        total = job["segments_total"] or 1
        pct = int((done / total) * 100)
        update_job(DB_PATH, job_id, segments_done=done, progress_pct=pct)
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "segment_done", "Segment " + str(idx) + " done (remote)", seg_index=idx)
    return jsonify({"ok": True})


@app.route("/api/worker/job/<job_id>/complete", methods=["POST"])
@require_worker_auth
def api_worker_job_complete(job_id):
    data = request.get_json(silent=True) or {}
    update_job(
        DB_PATH, job_id,
        status="done",
        progress_pct=100,
        final_audio=data.get("final_audio_r2_key", ""),
    )
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "synthesis_done", "Synthesis complete (remote)")
    return jsonify({"ok": True})


@app.route("/api/worker/job/<job_id>/fail", methods=["POST"])
@require_worker_auth
def api_worker_job_fail(job_id):
    data = request.get_json(silent=True) or {}
    update_job(
        DB_PATH, job_id,
        status="failed",
        error_msg=data.get("error_msg", "Worker reported failure"),
        error_detail=data.get("error_detail", ""),
    )
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "error", data.get("error_msg", "Remote worker failure"))
    return jsonify({"ok": True})


@app.route("/api/worker/heartbeat", methods=["POST"])
@require_worker_auth
def api_worker_heartbeat():
    global _worker_status
    data = request.get_json(silent=True) or {}
    from datetime import datetime, timezone
    data["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    _worker_status = data
    return jsonify({"ok": True})
```

Add `import json` to the top of app.py if not already present.

- [ ] **Step 5: Run worker API tests**

Run: `python -m pytest tests/test_worker_api.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_worker_api.py
git commit -m "feat: add worker API endpoints for remote GPU synthesis"
```

---

## Task 6: Inline text cleaning (remove worker thread dependency)

**Files:**
- Modify: `app.py`

The current flow: upload sets status to "cleaning", worker thread picks it up. New flow: upload handler runs cleaning synchronously (< 1s), sets status to "review" immediately.

- [ ] **Step 1: Move cleaning logic into upload handler**

In `app.py`, replace the entire `api_upload` function body with:

```python
@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": "This file type isn't supported. Try .docx, .pdf, .txt, .md, or .srt."
        }), 400

    speed = float(request.form.get("speed", 0.85))
    voice_ref = request.form.get("voice_ref", "amditis.wav")
    title_pause = float(request.form.get("title_pause", 2.0))
    para_pause = float(request.form.get("para_pause", 0.75))

    job_id = create_job(
        DB_PATH,
        filename=file.filename,
        speed=speed,
        voice_ref=voice_ref,
        title_pause=title_pause,
        para_pause=para_pause,
        user_id=session.get("user_id"),
    )

    UPLOAD_DIR.mkdir(exist_ok=True)
    save_path = UPLOAD_DIR / (job_id + "_" + file.filename)
    file.save(str(save_path))

    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "upload", "Uploaded " + file.filename)

    try:
        from audioslop import EXTRACTORS, chunk_text, clean_for_tts
        from synthesize import split_into_segments, is_title_line

        if save_path.name.lower().endswith(".md.docx"):
            file_ext = ".docx"
        else:
            file_ext = save_path.suffix.lower()

        extractor = EXTRACTORS.get(file_ext)
        if not extractor:
            raise ValueError("No extractor for '" + file_ext + "'")

        raw_text = extractor(save_path)
        if not raw_text or not raw_text.strip():
            raise ValueError("Extracted text is empty")

        cleaned = clean_for_tts(raw_text)
        chunks = chunk_text(cleaned, max_chars=4000)

        cleaned_dir = job_dir / "cleaned"
        cleaned_dir.mkdir(parents=True, exist_ok=True)

        seg_index = 0
        for i, chunk_content in enumerate(chunks):
            chunk_filename = "part" + str(i).zfill(3) + ".txt"
            chunk_path = cleaned_dir / chunk_filename
            chunk_path.write_text(chunk_content, encoding="utf-8")

            segments = split_into_segments(
                chunk_content,
                title_pause=title_pause,
                para_pause=para_pause,
            )

            for seg in segments:
                create_segment(
                    DB_PATH,
                    job_id=job_id,
                    seg_index=seg_index,
                    chunk_file=chunk_filename,
                    source_text=seg["text"],
                    is_title=1 if is_title_line(seg["text"]) else 0,
                    pause_after=seg["pause_after"],
                )
                seg_index += 1

        update_job(DB_PATH, job_id, status="review", segments_total=seg_index)
        log_activity(job_dir, "clean_done", "Cleaned: " + str(len(chunks)) + " chunk(s), " + str(seg_index) + " segment(s)")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        update_job(DB_PATH, job_id, status="failed", error_msg=str(e), error_detail=tb)
        log_activity(job_dir, "clean_error", str(e))

    return jsonify({"job_id": job_id}), 201
```

- [ ] **Step 2: Remove worker thread startup from app.py**

Remove the `_worker`, `get_worker()` function, and the `get_worker()` call in the `__main__` block. Also remove the `get_worker()` call from `api_cancel_job`.

Update `api_cancel_job`:

```python
@app.route("/api/job/<job_id>/cancel", methods=["POST"])
@require_auth
def api_cancel_job(job_id):
    job = get_job(DB_PATH, job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    update_job(DB_PATH, job_id, status="cancelled")
    job_dir = JOBS_DIR / job_id
    log_activity(job_dir, "cancel", "Job cancelled")
    return jsonify({"ok": True})
```

Update `__main__`:

```python
if __name__ == "__main__":
    UPLOAD_DIR.mkdir(exist_ok=True)
    JOBS_DIR.mkdir(exist_ok=True)
    REF_DIR.mkdir(exist_ok=True)
    init_db(DB_PATH)
    app.run(debug=True, port=5000, use_reloader=False)
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass. `tests/test_worker.py` may now fail since it imports the old Worker class -- if so, delete it next step.

- [ ] **Step 4: Delete old worker.py and its test**

```bash
rm worker.py tests/test_worker.py
```

- [ ] **Step 5: Commit**

```bash
git add app.py
git rm worker.py tests/test_worker.py
git commit -m "refactor: inline text cleaning, remove in-process worker thread"
```

---

## Task 7: R2 audio serving in player

**Files:**
- Modify: `app.py`
- Modify: `templates/player.html`
- Modify: `static/player.js`

- [ ] **Step 1: Add R2 presigned URL to audio serving**

In `app.py`, replace the `api_serve_audio` endpoint:

```python
@app.route("/api/job/<job_id>/audio/<path:filename>")
@require_auth
def api_serve_audio(job_id, filename):
    """Redirect to R2 presigned URL for audio files."""
    from r2 import presigned_url
    r2_key = job_id + "/" + filename
    url = presigned_url(r2_key)
    return redirect(url)
```

Add a JSON endpoint for the player:

```python
@app.route("/api/job/<job_id>/audio-url")
@require_auth
def api_audio_url(job_id):
    """Return presigned R2 URL for the job's final audio."""
    job = get_job(DB_PATH, job_id)
    if not job or not job["final_audio"]:
        return jsonify({"error": "Audio not ready."}), 404
    from r2 import presigned_url
    url = presigned_url(job["final_audio"])
    return jsonify({"url": url})
```

- [ ] **Step 2: Update player.html audio element**

In `templates/player.html`, change line 208-210 from:

```html
<audio id="audio" preload="auto" style="display:none;">
    <source src="/api/job/{{ job.id }}/audio/{{ job.final_audio }}" type="audio/mpeg">
</audio>
```

To:

```html
<audio id="audio" preload="auto" style="display:none;"></audio>
```

- [ ] **Step 3: Update player.js to load audio URL dynamically**

In `static/player.js`, add at the top of the IIFE (after `var isUserSeeking = false;`):

```javascript
  // Load audio from R2 presigned URL
  fetch("/api/job/" + JOB_ID + "/audio-url")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.url) {
        audio.src = data.url;
        audio.load();
      }
    });
```

- [ ] **Step 4: Update download link**

In `templates/player.html`, the download link at line 286 still uses `/api/job/.../audio/...` which now redirects to R2. This works as-is for downloads. No change needed.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/player.html static/player.js
git commit -m "feat: serve audio from R2 presigned URLs"
```

---

## Task 8: Remote GPU worker

**Files:**
- Create: `worker_remote.py`

- [ ] **Step 1: Create worker_remote.py**

Create the standalone GPU worker script. This is a long file -- see the spec section "Legion GPU worker" for the full behavioral spec. The worker:

1. Loads F5-TTS and Whisper models at startup
2. Polls landofjawn every 30s for synthesis jobs
3. Checks VRAM before processing (skips if < 8GB free)
4. Synthesizes segments, uploads to R2, reports progress
5. Handles errors and reports failures

```python
#!/usr/bin/env python3
"""
Remote GPU worker for audioslop.

Polls landofjawn's worker API for synthesis jobs over Tailscale,
synthesizes audio with F5-TTS on the local GPU, uploads results
to Cloudflare R2, and reports completion.

Usage:
    python worker_remote.py --api-url http://100.123.224.40:5000
"""

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
import traceback
import wave as wave_mod
from pathlib import Path

import requests

from r2 import upload_file as r2_upload
from synthesize import (
    concatenate_wav_data,
    generate_silence,
    is_title_line,
    prepare_reference_audio,
    split_into_segments,
    synthesize_segment,
    transcribe_reference,
)

logger = logging.getLogger("audioslop.remote_worker")


def setup_logging():
    if logger.handlers:
        return
    handler = logging.handlers.RotatingFileHandler(
        "audioslop_worker.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(console)
    logger.setLevel(logging.INFO)


def get_gpu_info():
    """Get GPU name, used memory, total memory via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        parts = result.stdout.strip().split(", ")
        if len(parts) == 3:
            return {
                "gpu_name": parts[0],
                "gpu_memory_used_mb": int(parts[1]),
                "gpu_memory_total_mb": int(parts[2]),
            }
    except Exception:
        pass
    return {"gpu_name": "unknown", "gpu_memory_used_mb": 0, "gpu_memory_total_mb": 0}


def check_vram_available(min_free_mb=8000):
    """Return True if enough VRAM is free for synthesis."""
    info = get_gpu_info()
    total = info["gpu_memory_total_mb"]
    used = info["gpu_memory_used_mb"]
    free = total - used
    if free < min_free_mb:
        logger.info("VRAM: %d MB free (need %d). Skipping.", free, min_free_mb)
        return False
    return True


class RemoteWorker:
    def __init__(self, api_url, api_key, ref_dir, work_dir):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.ref_dir = ref_dir
        self.work_dir = work_dir
        self.http = requests.Session()
        self.http.headers["Authorization"] = "Bearer " + api_key
        self.tts = None
        self.whisper_model = None

    def _load_models(self):
        if self.tts is not None:
            return
        logger.info("Loading F5-TTS model...")
        from f5_tts.api import F5TTS
        self.tts = F5TTS()
        logger.info("Loading Whisper model...")
        import whisper
        self.whisper_model = whisper.load_model("base")
        logger.info("Models loaded.")

    def _get(self, path, **kwargs):
        r = self.http.get(self.api_url + path, timeout=30, **kwargs)
        r.raise_for_status()
        return r

    def _post(self, path, **kwargs):
        r = self.http.post(self.api_url + path, timeout=30, **kwargs)
        r.raise_for_status()
        return r

    def heartbeat(self, current_job_id=None):
        import socket
        info = get_gpu_info()
        info["hostname"] = socket.gethostname()
        info["current_job_id"] = current_job_id
        try:
            self._post("/api/worker/heartbeat", json=info)
        except Exception:
            logger.warning("Heartbeat failed")

    def poll_jobs(self):
        try:
            r = self._get("/api/worker/jobs", params={"status": "synthesizing"})
            return r.json()
        except Exception as e:
            logger.warning("Poll failed: %s", e)
            return []

    def fetch_segments(self, job_id):
        r = self._get("/api/worker/job/" + job_id + "/segments")
        return r.json()

    def fetch_ref_audio(self, filename):
        local = self.ref_dir / filename
        if local.exists():
            return local
        self.ref_dir.mkdir(parents=True, exist_ok=True)
        r = self._get("/api/worker/ref/" + filename)
        local.write_bytes(r.content)
        logger.info("Downloaded reference audio: %s", filename)
        return local

    def report_segment(self, job_id, idx, data):
        self._post("/api/worker/job/" + job_id + "/segment/" + str(idx) + "/complete", json=data)

    def report_complete(self, job_id, final_key):
        self._post("/api/worker/job/" + job_id + "/complete",
                    json={"final_audio_r2_key": final_key})

    def report_fail(self, job_id, error_msg, error_detail):
        self._post("/api/worker/job/" + job_id + "/fail",
                    json={"error_msg": error_msg, "error_detail": error_detail})

    def is_job_active(self, job_id):
        try:
            jobs = self.poll_jobs()
            return any(j["id"] == job_id for j in jobs)
        except Exception:
            return True

    def process_job(self, job):
        job_id = job["id"]
        logger.info("Processing job %s (%s)", job_id, job["filename"])

        self._load_models()

        segments = self.fetch_segments(job_id)
        ref_path = self.fetch_ref_audio(job["voice_ref"])
        ref_audio_path = prepare_reference_audio(ref_path)
        ref_text = transcribe_reference(ref_audio_path)

        job_work = self.work_dir / job_id
        job_work.mkdir(parents=True, exist_ok=True)

        all_parts = []
        max_retries = 2

        for seg in segments:
            if not self.is_job_active(job_id):
                logger.info("Job %s cancelled, stopping.", job_id)
                return

            seg_idx = seg["seg_index"]
            source_text = seg["source_text"]
            is_title = seg["is_title"]
            pause_after = seg["pause_after"]

            if seg.get("audio_file"):
                logger.debug("Segment %d already done, skipping", seg_idx)
                continue

            tts_text = source_text
            if is_title and tts_text and tts_text[-1] not in ".!?:;,":
                tts_text = tts_text + "."

            best_pcm = None
            best_accuracy = 0.0
            best_timings = []

            for attempt in range(1, max_retries + 1):
                pcm = synthesize_segment(
                    self.tts, ref_audio_path, ref_text, tts_text, job["speed"]
                )
                if not pcm:
                    logger.warning("Segment %d attempt %d: no audio", seg_idx, attempt)
                    continue

                if len(source_text) > 20:
                    from qa import verify_segment
                    seg_qa = verify_segment(source_text, pcm, seg_idx, self.whisper_model)
                    accuracy = seg_qa.accuracy
                    timings = seg_qa.word_timings
                else:
                    accuracy = 1.0
                    timings = []

                if accuracy > best_accuracy:
                    best_pcm = pcm
                    best_accuracy = accuracy
                    best_timings = timings

                if accuracy >= 0.90:
                    break

            if not best_pcm:
                best_pcm = generate_silence(0.5)
                best_accuracy = 0.0
                best_timings = []

            seg_filename = "seg_" + str(seg_idx).zfill(4) + ".wav"
            seg_path = job_work / seg_filename
            with wave_mod.open(str(seg_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(best_pcm)

            duration = len(best_pcm) / (24000 * 2)

            r2_key = job_id + "/" + seg_filename
            r2_upload(str(seg_path), r2_key)

            timings_serialized = [
                {"word": wt.word, "start": wt.start, "end": wt.end, "gap_before": wt.gap_before}
                for wt in best_timings
            ]

            self.report_segment(job_id, seg_idx, {
                "audio_r2_key": r2_key,
                "accuracy": round(best_accuracy, 3),
                "duration_seconds": round(duration, 2),
                "word_timings": timings_serialized,
            })

            all_parts.append(best_pcm)
            if pause_after > 0:
                all_parts.append(generate_silence(pause_after))

            logger.info("Segment %d done (accuracy: %.1f%%)", seg_idx, best_accuracy * 100)
            self.heartbeat(current_job_id=job_id)

        final_name = job_id + "_full.wav"
        final_path = job_work / final_name
        concatenate_wav_data(all_parts, final_path)

        final_r2_key = job_id + "/" + final_name
        r2_upload(str(final_path), final_r2_key)

        self.report_complete(job_id, final_r2_key)
        logger.info("Job %s complete: %s", job_id, final_r2_key)

    def run(self, poll_interval=30):
        logger.info("Remote worker starting. API: %s", self.api_url)
        self.heartbeat()

        while True:
            try:
                if not check_vram_available():
                    time.sleep(poll_interval)
                    continue

                self.heartbeat()
                jobs = self.poll_jobs()

                if jobs:
                    job = jobs[0]
                    try:
                        self.process_job(job)
                    except Exception as e:
                        tb = traceback.format_exc()
                        error_msg = str(e)
                        if "cuda out of memory" in error_msg.lower() or "oom" in error_msg.lower():
                            error_msg = "GPU ran out of memory. Try a smaller file or close other GPU apps."
                        logger.error("Job %s failed: %s", job["id"], tb)
                        self.report_fail(job["id"], error_msg, tb)

            except Exception:
                logger.exception("Error in poll loop")

            time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="audioslop remote GPU worker")
    parser.add_argument("--api-url", default="http://100.123.224.40:5000")
    parser.add_argument("--api-key", default=None,
                        help="Worker API key (or set AUDIOSLOP_WORKER_KEY)")
    parser.add_argument("--ref-dir", default="ref")
    parser.add_argument("--work-dir", default="worker_output")
    parser.add_argument("--poll-interval", type=int, default=30)
    args = parser.parse_args()

    setup_logging()

    api_key = args.api_key or os.environ.get("AUDIOSLOP_WORKER_KEY", "")
    if not api_key:
        logger.error("No worker API key. Set --api-key or AUDIOSLOP_WORKER_KEY env var.")
        sys.exit(1)

    worker = RemoteWorker(
        api_url=args.api_url,
        api_key=api_key,
        ref_dir=Path(args.ref_dir),
        work_dir=Path(args.work_dir),
    )
    worker.run(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it parses**

Run: `python -c "import ast; ast.parse(open('worker_remote.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add worker_remote.py
git commit -m "feat: add remote GPU worker for Legion-side synthesis"
```

---

## Task 9: CLI admin tool

**Files:**
- Create: `manage.py`

- [ ] **Step 1: Create manage.py**

```python
#!/usr/bin/env python3
"""CLI admin tool for audioslop."""

import argparse
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

from db import (
    count_users,
    create_invite,
    create_user,
    get_user_by_name,
    init_db,
    list_invites,
    list_users,
)

DB_PATH = str(Path(__file__).parent / "audioslop.db")


def cmd_create_admin(args):
    if get_user_by_name(DB_PATH, args.name):
        print("User '" + args.name + "' already exists.")
        sys.exit(1)
    pw_hash = generate_password_hash(args.password)
    user_id = create_user(DB_PATH, args.name, pw_hash, is_admin=1)
    print("Admin user created: " + args.name + " (id: " + user_id + ")")


def cmd_create_invite(args):
    users = list_users(DB_PATH)
    admins = [u for u in users if u["is_admin"]]
    if not admins:
        print("No admin users exist. Create one first with: manage.py create-admin")
        sys.exit(1)
    admin_id = admins[0]["id"]
    invite_id, token = create_invite(DB_PATH, created_by=admin_id)
    base_url = args.base_url.rstrip("/")
    print("Invite link: " + base_url + "/invite/" + token)


def cmd_list_users(args):
    users = list_users(DB_PATH)
    if not users:
        print("No users.")
        return
    for u in users:
        role = "admin" if u["is_admin"] else "user"
        print("  " + u["name"] + " (" + role + ") - joined " + str(u["created_at"]))


def cmd_list_invites(args):
    invites = list_invites(DB_PATH)
    if not invites:
        print("No invites.")
        return
    for inv in invites:
        status = "used" if inv["used_by"] else "open"
        print("  " + inv["token"][:16] + "... (" + status + ")")


def main():
    parser = argparse.ArgumentParser(description="audioslop admin CLI")
    sub = parser.add_subparsers(dest="command")

    p_admin = sub.add_parser("create-admin", help="Create an admin user")
    p_admin.add_argument("name")
    p_admin.add_argument("password")

    p_invite = sub.add_parser("create-invite", help="Generate an invite link")
    p_invite.add_argument("--base-url", default="https://audioslop.amditis.tech")

    sub.add_parser("list-users", help="List all users")
    sub.add_parser("list-invites", help="List all invites")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    init_db(DB_PATH)

    commands = {
        "create-admin": cmd_create_admin,
        "create-invite": cmd_create_invite,
        "list-users": cmd_list_users,
        "list-invites": cmd_list_invites,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it parses**

Run: `python -c "import ast; ast.parse(open('manage.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add manage.py
git commit -m "feat: add CLI admin tool for user and invite management"
```

---

## Task 10: Deployment to landofjawn

This task is infrastructure setup, not code. Run these commands from Legion via SSH to landofjawn.

- [ ] **Step 1: Clone repo on landofjawn**

```bash
ssh jamditis@100.123.224.40
cd ~/projects
git clone <repo-url> audioslop
cd audioslop
```

- [ ] **Step 2: Create Python virtual environment**

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask python-docx pdfplumber boto3 werkzeug
```

Note: F5-TTS, torch, whisper are NOT needed on landofjawn -- only on Legion.

- [ ] **Step 3: Store secrets in pass on houseofjawn**

From houseofjawn, generate and store:

```bash
# Generate worker API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | pass insert -m claude/services/audioslop-worker-key

# R2 credentials (create in Cloudflare dashboard first)
pass insert claude/services/audioslop-r2-access-key
pass insert claude/services/audioslop-r2-secret-key
pass insert claude/services/audioslop-r2-account-id

# Flask session secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | pass insert -m claude/services/audioslop-secret
```

- [ ] **Step 4: Create systemd service on landofjawn**

```bash
sudo tee /etc/systemd/system/audioslop.service << 'SVCEOF'
[Unit]
Description=audioslop web server
After=network.target

[Service]
Type=simple
User=jamditis
WorkingDirectory=/home/jamditis/projects/audioslop
ExecStart=/home/jamditis/projects/audioslop/venv/bin/python app.py
Restart=always
RestartSec=5
EnvironmentFile=/home/jamditis/projects/audioslop/.env

[Install]
WantedBy=multi-user.target
SVCEOF
```

Create the .env file (fetch values from houseofjawn pass):

```bash
cat > /home/jamditis/projects/audioslop/.env << 'ENVEOF'
AUDIOSLOP_SECRET=<value from pass>
AUDIOSLOP_WORKER_KEY=<value from pass>
R2_ACCESS_KEY=<value from pass>
R2_SECRET_KEY=<value from pass>
R2_ACCOUNT_ID=<value from pass>
ENVEOF
chmod 600 /home/jamditis/projects/audioslop/.env
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable audioslop
sudo systemctl start audioslop
```

- [ ] **Step 5: Configure Cloudflare Tunnel on landofjawn**

Check existing cloudflared config:

```bash
cat ~/.cloudflared/config.yml
```

Add the audioslop route. If using Cloudflare Zero Trust dashboard, add it there. Then restart:

```bash
sudo systemctl restart cloudflared
```

- [ ] **Step 6: Create R2 bucket in Cloudflare dashboard**

Go to Cloudflare dashboard > R2 > Create bucket > Name: `audioslop`, Region: ENAM.

- [ ] **Step 7: Verify the site loads**

```bash
curl -I https://audioslop.amditis.tech/login
```

Expected: HTTP 200

- [ ] **Step 8: Create admin account**

Visit `https://audioslop.amditis.tech` (first visitor sees setup page) or use CLI:

```bash
cd /home/jamditis/projects/audioslop
source venv/bin/activate
python manage.py create-admin joe <password>
```

- [ ] **Step 9: Set up worker on Legion**

On Legion, set environment variables and start the worker:

```bash
export AUDIOSLOP_WORKER_KEY=$(ssh jamditis@100.122.208.15 "~/.claude/pass-get claude/services/audioslop-worker-key")
export R2_ACCESS_KEY=$(ssh jamditis@100.122.208.15 "~/.claude/pass-get claude/services/audioslop-r2-access-key")
export R2_SECRET_KEY=$(ssh jamditis@100.122.208.15 "~/.claude/pass-get claude/services/audioslop-r2-secret-key")
export R2_ACCOUNT_ID=$(ssh jamditis@100.122.208.15 "~/.claude/pass-get claude/services/audioslop-r2-account-id")

python worker_remote.py --api-url http://100.123.224.40:5000
```

- [ ] **Step 10: End-to-end test**

1. Open `https://audioslop.amditis.tech` in a browser
2. Log in with admin account
3. Upload a test document
4. Verify it cleans and enters "review" status
5. Click "Generate" to start synthesis
6. Confirm Legion worker picks up the job (check worker log)
7. Wait for synthesis to complete
8. Play audio in the player page
9. Generate an invite link from admin page
10. Open invite link in incognito, create account, verify access

- [ ] **Step 11: Commit any deployment fixes**

```bash
git add -A
git commit -m "fix: deployment adjustments from end-to-end testing"
```

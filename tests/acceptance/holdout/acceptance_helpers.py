"""Subprocess, HTTP, and SQLite helpers for the black-box holdout."""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

PYTHON = Path(r"C:/search_agent/.venv/Scripts/python.exe")
LAUNCHER = "from ainews.cli import main; main()"


def project_root(pytestconfig) -> Path:
    return Path(os.environ.get("AINEWS_TEST_ROOT", str(pytestconfig.rootpath))).resolve()


def database_url(path: Path) -> str:
    return "sqlite+aiosqlite:///" + path.resolve().as_posix()


@pytest.fixture
def checkout(tmp_path, pytestconfig):
    """Make the documented checkout-shaped runtime copy without reading source contents."""
    source = project_root(pytestconfig) / "src"
    isolated = tmp_path / "checkout"
    isolated.mkdir()
    shutil.copytree(source, isolated / "src")
    return isolated


def environment(
    root: Path,
    db: Path,
    *,
    demo: bool = False,
    scheduler: bool = False,
    timezone: str = "UTC",
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(root / "src"),
            "PYTHONNOUSERSITE": "1",
            "DATABASE_URL": database_url(db),
            "OPENAI_API_KEY": "acceptance-dummy-openai-key",
            "TAVILY_API_KEY": "acceptance-dummy-tavily-key",
            "SCHEDULER_ENABLED": "true" if scheduler else "false",
            "PHOENIX_ENABLED": "false",
            "DEMO_MODE": "true" if demo else "false",
            "TIMEZONE": timezone,
            "OPENAI_MODEL": "gpt-6-luna",
            "OPENAI_MODEL_SUMMARIZE": "gpt-6-luna",
            "OPENAI_MODEL_JUDGE": "gpt-6-luna",
            "TZ": "UTC",
        }
    )
    if overrides:
        env.update(overrides)
    return env


def run_cli(
    root: Path,
    db: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 30,
):
    child_env = env or environment(root, db)
    return subprocess.run(
        [str(PYTHON), "-c", LAUNCHER, *args],
        cwd=root,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def demo(checkout, tmp_path):
    db = tmp_path / "acceptance.sqlite3"
    env = environment(checkout, db, demo=True)
    seeded = run_cli(checkout, db, "demo", "seed", env=env)
    assert seeded.returncode == 0, f"demo seed failed: {seeded.stdout}\n{seeded.stderr}"
    disable_sources(db)
    return checkout, db


def disable_sources(db_path: Path) -> None:
    with sqlite3_connect(db_path) as db:
        columns = table_columns(db, "sources")
        if "enabled" in columns:
            db.execute('UPDATE sources SET "enabled" = 0')
        elif "active" in columns:
            db.execute('UPDATE sources SET "active" = 0')
        db.commit()


def sqlite3_connect(path: Path):
    import sqlite3

    return sqlite3.connect(path)


def table_columns(db, table: str) -> list[str]:
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def rows(db_path: Path, query: str, params=()):
    with sqlite3_connect(db_path) as db:
        return db.execute(query, params).fetchall()


def row_dict(db, table: str, key: str, value):
    cursor = db.execute(f'SELECT * FROM "{table}" WHERE "{key}" = ?', (value,))
    names = [col[0] for col in cursor.description]
    row = cursor.fetchone()
    assert row is not None, f"missing {table}.{key}={value} fixture row"
    return dict(zip(names, row, strict=True))


def primary_key(db, table: str) -> str:
    info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    keys = sorted((row[5], row[1], row[2]) for row in info if row[5])
    assert keys, f"{table} must have a primary key for fixture cloning"
    return keys[0][1]


def fresh_key(db, table: str, key: str):
    info = {row[1]: row for row in db.execute(f'PRAGMA table_info("{table}")')}
    declared = info[key][2].upper()
    if "INT" in declared:
        current = db.execute(f'SELECT MAX("{key}") FROM "{table}"').fetchone()[0]
        return (current or 0) + 10000
    return "acceptance-" + uuid.uuid4().hex


def clone_row(db, table: str, key: str, value, changes: dict | None = None):
    record = row_dict(db, table, key, value)
    pk = primary_key(db, table)
    record[pk] = fresh_key(db, table, pk)
    stamp = uuid.uuid4().hex
    for name in tuple(record):
        folded = name.casefold()
        if folded in {
            "url",
            "url_canonical",
            "canonical_url",
            "link",
            "guid",
            "external_id",
            "slug",
        }:
            record[name] = f"https://acceptance.invalid/{stamp}"
        elif folded in {"title", "title_local"} and isinstance(record[name], str):
            record[name] = f"Acceptance {stamp}"
    changes = changes or {}
    fixture_time = next(
        (
            value
            for name, value in changes.items()
            if name.casefold() in {"created_at", "started_at", "finished_at", "updated_at"}
        ),
        None,
    )
    record.update({name: value for name, value in changes.items() if name in record})
    if fixture_time is not None:
        set_matching_dates(record, fixture_time)
    insert_record(db, table, record)
    return record[pk]


def set_matching_dates(record: dict, value: str) -> None:
    for name in tuple(record):
        if name.casefold().endswith(("_at", "_date", "_time")) or name.casefold() in {
            "published",
            "fetched",
            "created",
            "updated",
        }:
            record[name] = value


def clone_article_summary(db_path: Path, *, tag: str, relevant: bool, created_at: str, title: str):
    with sqlite3_connect(db_path) as db:
        article_id = db.execute("SELECT id FROM articles LIMIT 1").fetchone()[0]
        article = row_dict(db, "articles", "id", article_id)
        new_article_id = fresh_key(db, "articles", primary_key(db, "articles"))
        article[primary_key(db, "articles")] = new_article_id
        stamp = uuid.uuid4().hex
        for name in tuple(article):
            if name.casefold() in {
                "url",
                "url_canonical",
                "canonical_url",
                "link",
                "guid",
                "external_id",
                "slug",
            }:
                article[name] = f"https://acceptance.invalid/{stamp}"
            elif name.casefold() in {"title", "title_local"} and isinstance(article[name], str):
                article[name] = title
        set_matching_dates(article, created_at)
        insert_record(db, "articles", article)

        summary_id = db.execute("SELECT id FROM summaries LIMIT 1").fetchone()[0]
        summary = row_dict(db, "summaries", "id", summary_id)
        summary_pk = primary_key(db, "summaries")
        new_summary_id = fresh_key(db, "summaries", summary_pk)
        summary[summary_pk] = new_summary_id
        summary["article_id"] = new_article_id
        if "relevant" in summary:
            summary["relevant"] = int(relevant)
        if "tags_json" in summary:
            summary["tags_json"] = json.dumps([tag])
        if "title_local" in summary:
            summary["title_local"] = title
        set_matching_dates(summary, created_at)
        insert_record(db, "summaries", summary)
        db.commit()
        return new_summary_id


def set_all_zero(db, table: str, names: tuple[str, ...]) -> None:
    columns = table_columns(db, table)
    for name in names:
        if name in columns:
            db.execute(f'UPDATE "{table}" SET "{name}" = 0')


def latest_bulletin(db):
    return db.execute(
        "SELECT id, day, language FROM bulletins ORDER BY day DESC, version DESC LIMIT 1"
    ).fetchone()


def insert_record(db, table: str, record: dict) -> None:
    names = list(record)
    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in names)
    marks = ", ".join("?" for _ in names)
    db.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({marks})',
        tuple(record[name] for name in names),
    )


class Server:
    def __init__(self, root: Path, db: Path, *, timezone="UTC", scheduler=False):
        self.port = free_port()
        env = environment(root, db, scheduler=scheduler, timezone=timezone)
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(self.port)
        log_dir = db.parent
        self.stdout_path = log_dir / "server.stdout.log"
        self.stderr_path = log_dir / "server.stderr.log"
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(PYTHON), "-c", LAUNCHER, "serve"],
            cwd=root,
            env=env,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
        )
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def get(self, path: str):
        try:
            with self.opener.open(self.base + path, timeout=4) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def post(self, path: str, fields: dict[str, str]):
        request = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base,
            },
        )
        try:
            with self.opener.open(request, timeout=4) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._close_log_handles()
                stdout = self.stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr = self.stderr_path.read_text(encoding="utf-8", errors="replace")
                raise AssertionError(
                    f"server exited before readiness (code {self.process.returncode})\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                with self.opener.open(self.base + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=4)
        self._close_log_handles()
        stdout = self.stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = self.stderr_path.read_text(encoding="utf-8", errors="replace")
        raise AssertionError(
            f"server did not become ready within 12 seconds\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=4)
        self._close_log_handles()

    def _close_log_handles(self):
        for handle in (self._stdout_handle, self._stderr_handle):
            if not handle.closed:
                handle.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server_factory():
    servers = []

    def start(root, db, **kwargs):
        server = Server(root, db, **kwargs)
        servers.append(server)
        server.wait_ready()
        return server

    yield start
    for server in servers:
        server.close()

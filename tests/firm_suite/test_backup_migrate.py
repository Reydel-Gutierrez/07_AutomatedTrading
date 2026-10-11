from __future__ import annotations

import json
import shutil
import subprocess
import tarfile

import pytest

from firm import backup
from firm.core import Firm
from firm.migrate import archive_legacy, import_legacy, legacy_nav_history
from firm.money import D

from tests.firm_suite.fakes import FakeRobinhood


@pytest.fixture
def firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    f = Firm(db, reader=rh, writer=lambda: rh, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    f.allocate("tim", D("909"), by="owner")
    return f


def test_encrypt_roundtrip_and_tamper_detection():
    key = b"k" * 32
    blob = backup.encrypt(b"ledger", key)
    assert backup.decrypt(blob, key) == b"ledger"
    with pytest.raises(Exception):
        backup.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), key)
    with pytest.raises(Exception):
        backup.decrypt(blob, b"x" * 32)


def test_retention_keeps_daily_weekly_monthly():
    names = [f"firm-2026{m:02d}{d:02d}T063000Z.db.gz.enc" for m in range(1, 11) for d in range(1, 29)]
    keep = backup.retention(names)
    assert names[-1] in keep
    assert len([n for n in keep if n.startswith("firm-202610")]) >= 7
    assert any(n.startswith("firm-202601") for n in keep)  # a monthly from January survives
    assert len(keep) <= 7 + 8 + 12


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_full_backup_push_and_restore_verification(firm, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    env = {"HOME": str(tmp_path / "home"), "FIRM_BACKUP_REMOTE": str(remote)}
    backup.create_key(env)
    with pytest.raises(backup.BackupError):
        backup.create_key(env)  # never overwrites a key
    status = backup.run_backup(firm, environ=env)
    assert status["ok"] and status["verified"], status
    # A second run replaces the branch with the retention set (no history growth).
    firm.clock.advance(days=1)
    assert backup.run_backup(firm, environ=env)["ok"]
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout.strip().splitlines()
    assert len(log) == 1
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "main"], capture_output=True, text=True).stdout.split()
    assert len([f for f in files if f.endswith(".enc")]) == 2
    # Restore from the local copy and compare the ledger fingerprint.
    local = sorted((tmp_path / "firm-state" / "backups").glob("*.enc"))[-1]
    restored = backup.restore_file(local, tmp_path / "restored.db", backup.load_key(env))
    assert restored["counts"]["ledger"] == firm.db.one("SELECT COUNT(*) AS n FROM ledger")["n"]


def test_backup_without_remote_fails_loudly_but_keeps_local_copy(firm, tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    backup.create_key(env)
    status = backup.run_backup(firm, environ=env)
    assert not status["ok"] and "FIRM_BACKUP_REMOTE" in status["error"]
    assert firm.events.last_of("BACKUP_FAILED") is not None
    assert list((tmp_path / "firm-state" / "backups").glob("*.enc"))


def test_archive_and_history_import(firm, tmp_path):
    root = tmp_path / "legacy"
    snaps = root / "state" / "live_book" / "snapshots"
    snaps.mkdir(parents=True)
    for i, (ts, nav) in enumerate([("2026-09-01T15:00:00+00:00", 1000), ("2026-09-01T20:00:00+00:00", 1001), ("2026-09-02T15:00:00+00:00", 1010)]):
        (snaps / f"{i}.json").write_text(json.dumps({"runtime_mode": "LIVE", "created_at": ts, "context": {"current_nav": nav, "cash": nav}}))
    (root / "logs").mkdir()
    (root / "logs" / "agent.jsonl").write_text("{}\n")
    (root / "state" / "firm").mkdir()
    (root / "state" / "firm" / "firm.db").write_text("not archived")
    history = legacy_nav_history(root)
    assert [p["nav"] for p in history] == [1001, 1010]
    archive = archive_legacy(root, out_dir=tmp_path / "out", stamp="20261017")
    with tarfile.open(archive["path"]) as tar:
        names = tar.getnames()
    assert "logs/agent.jsonl" in names and not any(n.startswith("state/firm") for n in names)
    summary = import_legacy(firm, archive, root)
    assert len(summary["nav_history"]) == 2
    assert firm.db.one("SELECT sha256 FROM legacy_import")["sha256"] == archive["sha256"]

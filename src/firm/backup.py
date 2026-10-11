"""Encrypted, off-Pi, restore-verified backups of the firm database.

Nightly:
  1. SQLite online backup → integrity_check on the copy → ledger digest
  2. gzip → AES-256-GCM (key file on the Pi, a copy in the owner's password manager)
  3. push to a private git repository over SSH with a repo-scoped deploy key; the
     branch is rewritten as one commit holding the retention set
     (7 daily, 8 weekly, 12 monthly), so history never accumulates
  4. verify: fresh clone → decrypt → open → integrity_check → digest must match

Configuration (Pi env file):
  FIRM_BACKUP_REMOTE     git@github.com:<owner>/<repo>.git
  FIRM_BACKUP_KEY_FILE   default ~/.agentic-portfolio/backup.key
  FIRM_BACKUP_SSH_KEY    default ~/.agentic-portfolio/backup_deploy_key
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from firm.clock import iso
from firm.paths import state_root

if TYPE_CHECKING:
    from firm.core import Firm

MAGIC = b"FIRMBK1\n"
KEEP_DAILY, KEEP_WEEKLY, KEEP_MONTHLY = 7, 8, 12
LOCAL_KEEP = 3


class BackupError(RuntimeError):
    pass


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def key_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("FIRM_BACKUP_KEY_FILE") or _home(env) / ".agentic-portfolio" / "backup.key")


def ssh_key_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("FIRM_BACKUP_SSH_KEY") or _home(env) / ".agentic-portfolio" / "backup_deploy_key")


def create_key(env: Mapping[str, str]) -> Path:
    path = key_path(env)
    if path.exists():
        raise BackupError(f"key already exists at {path}; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(os.urandom(32)).decode() + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    return path


def load_key(env: Mapping[str, str]) -> bytes:
    path = key_path(env)
    if not path.is_file():
        raise BackupError(f"backup key missing at {path} (run: python scripts/firm_admin.py backup-init)")
    key = base64.b64decode(path.read_text(encoding="ascii").strip())
    if len(key) != 32:
        raise BackupError("backup key must be 32 bytes")
    return key


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(MAGIC):
        raise BackupError("not a firm backup file")
    nonce, body = blob[len(MAGIC) : len(MAGIC) + 12], blob[len(MAGIC) + 12 :]
    return AESGCM(key).decrypt(nonce, body, MAGIC)


def ledger_digest(db_file: Path) -> dict[str, Any]:
    """Fingerprint of the financial record: row counts plus a hash of every ledger row."""
    conn = sqlite3.connect(db_file)
    try:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise BackupError(f"integrity_check: {status}")
        h = hashlib.sha256()
        for row in conn.execute("SELECT id, txn_id, ts, book, kind, bucket, symbol, quantity, cash FROM ledger ORDER BY id"):
            h.update(repr(row).encode())
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("ledger", "orders", "events", "lots", "marks")}
    finally:
        conn.close()
    return {"ledger_sha256": h.hexdigest(), "counts": counts}


def retention(names: list[str]) -> set[str]:
    """Keep the newest 7 daily, the newest per ISO week (8), and the newest per month (12)."""
    stamps = sorted((datetime.strptime(n.split("-")[1].split(".")[0], "%Y%m%dT%H%M%SZ"), n) for n in names if n.startswith("firm-") and n.endswith(".enc"))
    keep: set[str] = set()
    daily: dict[str, str] = {}
    weekly: dict[tuple[int, int], str] = {}
    monthly: dict[tuple[int, int], str] = {}
    for dt, name in stamps:
        daily[dt.date().isoformat()] = name
        weekly[tuple(dt.isocalendar()[:2])] = name
        monthly[(dt.year, dt.month)] = name
    keep |= set(list(daily.values())[-KEEP_DAILY:])
    keep |= set(list(weekly.values())[-KEEP_WEEKLY:])
    keep |= set(list(monthly.values())[-KEEP_MONTHLY:])
    return keep


def _git(args: list[str], *, cwd: Path, env: Mapping[str, str]) -> str:
    ssh_key = ssh_key_path(env)
    known = ssh_key.parent / "backup_known_hosts"
    git_env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {ssh_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    res = subprocess.run(["git", *args], cwd=cwd, env=git_env, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise BackupError(f"git {' '.join(args[:2])} failed: {(res.stderr or res.stdout).strip()[:400]}")
    return res.stdout


def make_backup_file(firm: "Firm", workdir: Path, key: bytes) -> tuple[Path, dict[str, Any]]:
    stamp = firm.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = workdir / "firm.db"
    firm.db.backup_to(raw)
    digest = ledger_digest(raw)
    blob = encrypt(gzip.compress(raw.read_bytes(), compresslevel=9), key)
    name = f"firm-{stamp}.db.gz.enc"
    out = workdir / name
    out.write_bytes(blob)
    manifest = {"file": name, "created_at": iso(firm.clock()), "ciphertext_sha256": hashlib.sha256(blob).hexdigest(), **digest}
    return out, manifest


def restore_file(path: Path, out: Path, key: bytes) -> dict[str, Any]:
    out.write_bytes(gzip.decompress(decrypt(path.read_bytes(), key)))
    return ledger_digest(out)


def run_backup(firm: "Firm", *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    remote = str(env.get("FIRM_BACKUP_REMOTE") or "").strip()
    status: dict[str, Any] = {"at": iso(firm.clock()), "ok": False, "remote": bool(remote)}
    try:
        key = load_key(env)
        local_dir = state_root() / "backups"
        local_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="firm-backup-") as tmp:
            work = Path(tmp)
            file, manifest = make_backup_file(firm, work, key)
            shutil.copy2(file, local_dir / file.name)
            (local_dir / (file.name + ".json")).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            for old in sorted(local_dir.glob("firm-*.enc"))[:-LOCAL_KEEP]:
                old.unlink(missing_ok=True)
                (local_dir / (old.name + ".json")).unlink(missing_ok=True)
            status.update(file=file.name, counts=manifest["counts"])
            if not remote:
                raise BackupError("FIRM_BACKUP_REMOTE is not set; backup kept on the Pi only")
            _push(file, manifest, remote, work, env)
            status["verified"] = _verify_remote(file.name, manifest, remote, work, key, env)
        status["ok"] = True
        firm.events.emit("BACKUP_COMPLETED", f"Encrypted backup {status['file']} stored off-Pi and restore-verified.", actor="backup", payload=status)
    except Exception as exc:  # noqa: BLE001
        status["error"] = f"{type(exc).__name__}: {exc}"
        firm.events.emit("BACKUP_FAILED", f"Backup failed: {status['error']}", actor="backup", severity="critical", payload=status)
    firm.settings.put("backup_status", status, by="backup")
    return status


def _push(file: Path, manifest: dict[str, Any], remote: str, work: Path, env: Mapping[str, str]) -> None:
    repo = work / "repo"
    try:
        _git(["clone", "--depth", "1", remote, str(repo)], cwd=work, env=env)
    except BackupError as exc:
        if "empty repository" not in str(exc) and "Remote branch" not in str(exc):
            raise
    if not (repo / ".git").exists():
        repo.mkdir(exist_ok=True)
        _git(["init", "-q", "-b", "main"], cwd=repo, env=env)
    shutil.copy2(file, repo / file.name)
    (repo / (file.name + ".json")).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    present = [p.name for p in repo.glob("firm-*.enc")]
    keep = retention(present)
    for name in present:
        if name not in keep:
            (repo / name).unlink()
            (repo / (name + ".json")).unlink(missing_ok=True)
    (repo / "README.md").write_text(
        "Encrypted backups of the Agentic firm database (AES-256-GCM). Restore with\n"
        "`python scripts/firm_admin.py backup-restore <file> <out.db>` and the key from the password manager.\n",
        encoding="utf-8",
    )
    _git(["checkout", "-q", "--orphan", "snapshot"], cwd=repo, env=env)
    _git(["add", "-A"], cwd=repo, env=env)
    _git(["-c", "user.name=firm-backup", "-c", "user.email=firm-backup@localhost", "commit", "-q", "-m", f"backup {manifest['file']}"], cwd=repo, env=env)
    _git(["push", "-q", "--force", remote, "snapshot:main"], cwd=repo, env=env)


def _verify_remote(name: str, manifest: dict[str, Any], remote: str, work: Path, key: bytes, env: Mapping[str, str]) -> bool:
    check = work / "verify"
    _git(["clone", "--depth", "1", remote, str(check)], cwd=work, env=env)
    blob = (check / name).read_bytes()
    if hashlib.sha256(blob).hexdigest() != manifest["ciphertext_sha256"]:
        raise BackupError("remote copy differs from what was uploaded")
    restored = restore_file(check / name, work / "restored.db", key)
    if restored["ledger_sha256"] != manifest["ledger_sha256"] or restored["counts"] != manifest["counts"]:
        raise BackupError("restored database does not match the source ledger")
    return True

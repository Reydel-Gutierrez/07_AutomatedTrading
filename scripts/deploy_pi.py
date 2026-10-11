"""Deploy a committed revision to the Raspberry Pi and verify it.

Runs on the development machine. The Pi side is ``deploy/pi_remote.sh``
(uploaded from this working tree on every run, so older revisions can be
deployed or rolled back to with the current tooling).

  python scripts/deploy_pi.py                 # deploy HEAD
  python scripts/deploy_pi.py <ref>           # deploy a branch/tag/sha
  python scripts/deploy_pi.py --dry-run       # every gate (tests, Pi tests, smoke), no activation
  python scripts/deploy_pi.py --status        # what is deployed, service, in-flight orders
  python scripts/deploy_pi.py --rollback      # restore the code backed up by the last deploy

Gates, in order (any failure stops before the service is touched):
  1. ref resolves to a commit that is on origin (``--allow-unpushed`` to skip)
  2. full test suite on a clean ``git archive`` of the commit, locally
  3. Pi preflight: no in-flight execution intent; outside regular market hours
     (``--allow-market-hours`` to override); baseline production smoke test
  4. full test suite on the Pi, with the production venv, on a scratch copy
Then: back up current code, stop, sync, install changed deps, start, and verify
(fresh process ONLINE, every file byte-identical, /healthz, production smoke
with no regression vs. baseline). A failed activation or verification rolls
back automatically unless ``--no-auto-rollback``.

Never touches state/, logs/, reports/, the env file, or execution switches.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTE_SCRIPT = ROOT / "deploy" / "pi_remote.sh"
REMOTE_DIR = "agentic-deploys"  # relative to the deploy user's home on the Pi
SMOKE_LINE = re.compile(r"^(PASS|FAIL)  (\S+)")


class DeployError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip() if capture else ""
        raise DeployError(f"command failed ({res.returncode}): {' '.join(cmd[:4])}… {detail}")
    return res


def git(*args: str) -> str:
    return run(["git", *args], cwd=ROOT, capture=True).stdout.strip()


class Pi:
    def __init__(self, host: str, user: str, identity: str | None) -> None:
        self.target = f"{user}@{host}"
        self.opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if identity:
            self.opts += ["-i", os.path.expanduser(identity)]

    def sh(self, command: str, *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        return run(["ssh", *self.opts, self.target, command], check=check, capture=capture)

    def remote(self, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        quoted = " ".join(shlex.quote(a) for a in args)
        return self.sh(f"bash ~/{REMOTE_DIR}/pi_remote.sh {quoted}", check=check, capture=capture)

    def put(self, local: Path, remote_rel: str) -> None:
        run(["scp", "-q", *self.opts, str(local), f"{self.target}:{remote_rel}"])

    def upload_tooling(self) -> None:
        self.sh(f"mkdir -p ~/{REMOTE_DIR}")
        # Windows checkouts may carry CRLF; bash on the Pi must get LF.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "pi_remote.sh"
            script.write_bytes(REMOTE_SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
            self.put(script, f"{REMOTE_DIR}/pi_remote.sh")


def parse_smoke(text: str) -> dict[str, bool]:
    results = {}
    for line in text.splitlines():
        m = SMOKE_LINE.match(line)
        if m and not m.group(2)[0].isdigit():
            results[m.group(2)] = m.group(1) == "PASS"
    return results


def smoke_regressions(before: dict[str, bool], after: dict[str, bool]) -> list[str]:
    """Checks that passed on the old code and fail (or vanished) on the new code."""
    return sorted(name for name, ok in before.items() if ok and not after.get(name, False))


def resolve(ref: str, *, allow_unpushed: bool) -> str:
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if not allow_unpushed:
        run(["git", "fetch", "--quiet", "origin"], cwd=ROOT)
        if not git("branch", "-r", "--contains", sha):
            raise DeployError(f"{sha[:12]} is not on origin; push it first or pass --allow-unpushed")
    return sha


def build_archive(sha: str, dest: Path) -> Path:
    """git archive of the commit, plus the frontend built from that same clean export."""
    tar = dest / f"{sha}.tar"
    run(["git", "archive", "--format=tar", "-o", str(tar), sha], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="agentic-deploy-web-") as tmp:
        with tarfile.open(tar) as archive:
            archive.extractall(tmp, filter="data")
        web = Path(tmp) / "web"
        if (web / "package.json").is_file():
            npm = shutil.which("npm") or shutil.which("npm.cmd")
            if not npm:
                raise DeployError("npm is required to build the HQ frontend")
            log("building the HQ frontend from the clean export")
            run([npm, "ci", "--no-audit", "--no-fund", "--silent"], cwd=web)
            run([npm, "run", "build", "--silent"], cwd=web)
            with tarfile.open(tar, "a") as archive:
                archive.add(web / "dist", arcname="web/dist")
    return tar


def local_tests(tar: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="agentic-deploy-test-") as tmp:
        with tarfile.open(tar) as archive:
            archive.extractall(tmp, filter="data")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTIC_", "DASHBOARD_", "OPENAI_", "ANTHROPIC_", "TELEGRAM_", "FIRM_"))}
        log("running test suite on a clean export (local)")
        res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=tmp, env=env)
        if res.returncode != 0:
            raise DeployError("local test suite failed")


def preflight(pi: Pi, *, allow_market_hours: bool) -> dict:
    info = json.loads(pi.remote("preflight", capture=True).stdout)
    if info["inflight_intents"]:
        raise DeployError(f"in-flight execution intents; let LIVE_ORDER_RECONCILE settle them first: {info['inflight_intents']}")
    health = info.get("health") or {}
    if health.get("regular_hours_open") and not allow_market_hours:
        raise DeployError("regular market hours are open; deploy after the close or pass --allow-market-hours")
    return info


def smoke(pi: Pi, label: str) -> dict[str, bool]:
    res = pi.remote("smoke", f"smoke_{label}.txt", check=False, capture=True)
    results = parse_smoke(res.stdout)
    failed = sorted(k for k, ok in results.items() if not ok)
    log(f"smoke ({label}): {sum(results.values())}/{len(results)} pass" + (f"; failing: {', '.join(failed)}" if failed else ""))
    if not results:
        log(res.stdout + res.stderr)
    return results


def rollback(pi: Pi, reason: str) -> None:
    log(f"ROLLING BACK: {reason}")
    pi.remote("rollback")


def deploy(args: argparse.Namespace) -> int:
    pi = Pi(args.host, args.user, args.identity)
    sha = resolve(args.ref, allow_unpushed=args.allow_unpushed)
    subject = git("log", "-1", "--format=%s", sha)
    log(f"target {sha[:12]} — {subject}")
    pi.upload_tooling()
    info = preflight(pi, allow_market_hours=args.allow_market_hours)
    log(f"Pi currently runs {info['deployed_sha'][:12]}; service {info['service_active']}; health {info.get('health')}")

    with tempfile.TemporaryDirectory(prefix="agentic-deploy-") as tmp:
        tar = build_archive(sha, Path(tmp))
        if not args.skip_local_tests:
            local_tests(tar)
        pi.put(tar, f"{REMOTE_DIR}/{sha}.tar")
    pi.remote("stage", sha, f"{sha}.tar")
    if not args.skip_pi_tests:
        log("running test suite on the Pi (production venv, scratch copy)")
        if pi.remote("test", sha, check=False).returncode != 0:
            raise DeployError("Pi test suite failed; nothing was changed")

    baseline = smoke(pi, "before")
    if args.dry_run:
        pi.remote("cleanup", sha, check=False)
        log(f"DRY RUN passed for {sha[:12]}; the service was not touched")
        return 0
    preflight(pi, allow_market_hours=args.allow_market_hours)  # re-check right before touching the service
    meta = {
        "sha": sha,
        "ref": args.ref,
        "subject": subject,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "deployed_by": f"{getpass.getuser()} via scripts/deploy_pi.py",
    }
    try:
        pi.remote("activate", sha, json.dumps(meta))
        pi.remote("verify", sha)
        after = smoke(pi, "after")
        if args.require_smoke_pass:
            # A release that replaces the smoke test itself (e.g. the legacy → firm
            # cutover) cannot be compared check-by-check; every new check must pass.
            failing = sorted(k for k, ok in after.items() if not ok)
            if not after or failing:
                raise DeployError(f"smoke checks failing after deploy: {', '.join(failing) or 'no results'}")
        else:
            regressed = smoke_regressions(baseline, after)
            if regressed:
                raise DeployError(f"smoke checks regressed: {', '.join(regressed)}")
    except DeployError as exc:
        if args.no_auto_rollback:
            log(f"verification failed and auto-rollback is off: {exc}")
            return 1
        rollback(pi, str(exc))
        return 1
    pi.remote("cleanup", sha, check=False)
    log(f"DEPLOYED {sha[:12]} and verified")
    return 0


def status(args: argparse.Namespace) -> int:
    pi = Pi(args.host, args.user, args.identity)
    pi.upload_tooling()
    info = json.loads(pi.remote("preflight", capture=True).stdout)
    sha = info["deployed_sha"]
    if sha != "unknown":
        try:
            info["deployed_subject"] = git("log", "-1", "--format=%s", sha)
            info["commits_behind_HEAD"] = int(git("rev-list", "--count", f"{sha}..HEAD"))
        except DeployError:
            info["deployed_subject"] = "commit not found locally"
    print(json.dumps(info, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref", nargs="?", default="HEAD")
    parser.add_argument("--host", default=os.environ.get("AGENTIC_PI_HOST", "sentry-dev-1"))
    parser.add_argument("--user", default=os.environ.get("AGENTIC_PI_USER", "reydel"))
    parser.add_argument("--identity", default=os.environ.get("AGENTIC_PI_IDENTITY"), help="SSH private key (default: ssh config)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="run every gate, do not activate")
    parser.add_argument("--allow-unpushed", action="store_true")
    parser.add_argument("--allow-market-hours", action="store_true")
    parser.add_argument("--skip-local-tests", action="store_true")
    parser.add_argument("--skip-pi-tests", action="store_true")
    parser.add_argument("--no-auto-rollback", action="store_true")
    parser.add_argument("--require-smoke-pass", action="store_true", help="every post-deploy smoke check must pass (instead of comparing with the old release)")
    args = parser.parse_args(argv)
    try:
        if args.status:
            return status(args)
        if args.rollback:
            pi = Pi(args.host, args.user, args.identity)
            pi.upload_tooling()
            rollback(pi, "requested")
            return 0
        return deploy(args)
    except DeployError as exc:
        log(f"ABORTED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

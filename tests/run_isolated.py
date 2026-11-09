"""Run the test suite against a throwaway copy of the repository.

Several older regression tests write through module-level paths into the real
trades.csv, bot_audit_log.csv and data/tournament_trades.json. Running
``python -m unittest discover`` from the repository root therefore corrupts
production logs. This runner avoids that:

* source comes from the working tree, every data/config file from git HEAD, in
  a temporary directory that is deleted afterwards;
* .env is never copied and broker credentials are replaced with dummies;
* socket connections are refused, so no test can reach a broker.

Usage (from the repository root):
    python tests/run_isolated.py                     # whole suite
    python tests/run_isolated.py tests.test_bid_exit # selected test modules
    python tests/run_isolated.py --head              # HEAD source (baseline)

Exits non-zero if a test fails or anything attempted a network connection.
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOOTSTRAP = r"""
import socket, sys, traceback, unittest

ATTEMPTS = []
def refuse(self, address):
    frames = [f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[:-1]
              if "site-packages" not in f.filename and "Lib" not in f.filename]
    ATTEMPTS.append((str(address), " > ".join(frames[-5:])))
    raise OSError("network disabled by tests/run_isolated.py")
socket.socket.connect = refuse
socket.socket.connect_ex = refuse

sys.path.insert(0, ".")
names = sys.argv[1:]
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(names) if names else loader.discover("tests")
result = unittest.TextTestRunner(verbosity=1, stream=sys.stderr).run(suite)
sys.stderr.write(f"\nISOLATED RUN: tests={result.testsRun} failures={len(result.failures)} "
                 f"errors={len(result.errors)} skipped={len(result.skipped)} "
                 f"network_attempts={len(ATTEMPTS)}\n")
for address, frames in ATTEMPTS[:20]:
    sys.stderr.write(f"  NETWORK ATTEMPT {address} via {frames}\n")
sys.exit(0 if result.wasSuccessful() and not ATTEMPTS else 1)
"""


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True).stdout


def build_copy(target, use_head):
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD"))) as archive:
        archive.extractall(target, filter="data")
    if use_head:
        return
    changed = git("ls-files", "--modified", "--others", "--exclude-standard", "--", "*.py")
    for relative in changed.decode().splitlines():
        source = os.path.join(REPO, relative)
        if os.path.isfile(source):
            destination = os.path.join(target, relative)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)


def main(argv):
    use_head = "--head" in argv
    names = [arg for arg in argv if arg != "--head"]
    workspace = tempfile.mkdtemp(prefix="hawkaniha_tests_")
    try:
        build_copy(workspace, use_head)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", HAWKANIHA_ISOLATED_TESTS="1")
        for key in list(env):
            if key.startswith("TRADIER_"):
                del env[key]
        env.update(TRADIER_TOKEN="isolated-dummy", TRADIER_ACCOUNT="isolated-dummy",
                   TRADIER_BASE_URL="https://sandbox.tradier.com/v1")
        bootstrap = os.path.join(workspace, "_isolated_bootstrap.py")
        with open(bootstrap, "w", encoding="utf-8") as handle:
            handle.write(BOOTSTRAP)
        return subprocess.run([sys.executable, bootstrap, *names], cwd=workspace, env=env).returncode
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

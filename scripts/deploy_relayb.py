#!/usr/bin/env python3
"""Deploy this checkout to relayB from macOS in one command.

Why the Mac fetches
-------------------
relayB (the Windows instrument PC) cannot reliably pull from GitHub itself: git
started over ssh there exits 128 for the SSH remote, and the HTTPS remote is
flaky. This Mac does have GitHub credentials, so it fetches `main` first and
then ships only the missing commits as a git bundle (one file copied with scp).

What one run does
-----------------
1. Read the local repository root, local HEAD and origin/main.
2. Fetch GitHub `main`: SSH URL first, HTTPS URL as the fallback.
3. Read the commit currently deployed on relayB.
4. If both commits are identical, print "already up to date" and exit 0.
5. Otherwise build a git bundle on top of a temporary branch (`relayb-deploy`),
   falling back to a full bundle when the deployed commit is not an ancestor.
6. Copy the bundle and a generated .bat to relayB and run the .bat there:
   stop the scheduled task, apply the bundle with a fast-forward merge, refresh
   backend dependencies, rebuild the frontend, start the task again and wait for
   port 8000.
7. Verify the HTTP API (/api/health with camera_driver=ic4, /api/capture/sets,
   /api/capture/draft) and that the served frontend asset matches the fresh
   build.
8. Clean up the local bundle, the temporary branch and the relayB temp files.

Only the Python standard library is used. Exit code 0 means "deployed and
healthy" or "already up to date"; 1 means a step failed (the failing step and
the last 30 lines of its output are printed).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque

SSH_HOST = "relayB"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

REMOTE_REPO = r"D:\LEED\static-astrophoto"
REMOTE_TASK = "StaticAstroPhoto"
REMOTE_PORT = 8000
REMOTE_API = "http://192.168.1.101:8000"

GIT_BRANCH = "main"
GIT_SSH_URL = "git@github.com:Hunfen/diffraction-stacking.git"
GIT_HTTPS_URL = "https://github.com/Hunfen/diffraction-stacking.git"

BUNDLE_BRANCH = "relayb-deploy"
BUNDLE_NAME = "repo.bundle"
BAT_NAME = "deploy.bat"
REMOTE_DIR_PREFIX = "relayb-deploy-"

TAIL_LINES = 30
API_START_TIMEOUT_S = 60

# Fetch timeouts. The SSH attempt only needs a connect timeout (a dead host is detected in
# seconds), while the HTTPS fallback is capped so a stalled transfer cannot hang the run:
# fewer than 1 byte/s for 30 s aborts that fetch. Both are passed to that single git call
# only, so no global git configuration is touched.
FETCH_SSH_CONNECT_TIMEOUT_S = 20
FETCH_LOW_SPEED_LIMIT = 1
FETCH_LOW_SPEED_TIME_S = 30

ASSET_PATTERN = re.compile(r"assets/(index-[A-Za-z0-9_.-]+\.js)")

BAT_TEMPLATE = r"""@echo off
setlocal
set "REPO={repo}"
set "BUNDLE=%~dp0{bundle}"
set "TASK={task}"
set "BRANCH={branch}"

echo [relayb] step 1: stop scheduled task %TASK%
schtasks /End /TN %TASK%
if errorlevel 1 echo [relayb] task was not running, continuing

echo [relayb] step 2: wait for port {port} to stop listening
set /a WAIT=0
:wait_stopped
netstat -ano | findstr /C:":{port}" | findstr /C:"LISTENING"
if errorlevel 1 goto port_stopped
set /a WAIT+=1
if %WAIT% GEQ 20 goto port_stuck
ping -n 3 127.0.0.1
goto wait_stopped
:port_stuck
echo [relayb][ERROR] port {port} is still listening after %WAIT% checks
exit /b 11
:port_stopped
echo [relayb] port {port} is free

echo [relayb] step 3: fetch the bundle into %BRANCH%
git -C "%REPO%" fetch "%BUNDLE%" refs/heads/%BRANCH%
if errorlevel 1 goto fail_fetch

echo [relayb] step 4: fast-forward merge
git -C "%REPO%" merge --ff-only FETCH_HEAD
if errorlevel 1 goto fail_merge
for /f "delims=" %%i in ('git -C "%REPO%" rev-parse HEAD') do echo [relayb] merged commit %%i

echo [relayb] step 5: refresh backend dependencies
"%REPO%\backend\.venv\Scripts\python.exe" -m pip install -r "%REPO%\backend\requirements.txt"
if errorlevel 1 goto fail_pip
echo [relayb] backend dependencies are up to date

{frontend_block}
{restart_block}
echo [relayb] done
exit /b 0

:fail_fetch
echo [relayb][ERROR] git fetch from the bundle failed
exit /b 12
:fail_merge
echo [relayb][ERROR] git merge --ff-only failed (the deployed checkout cannot fast-forward)
exit /b 13
:fail_pip
echo [relayb][ERROR] pip install -r backend\requirements.txt failed
exit /b 14
:fail_frontend
echo [relayb][ERROR] frontend rebuild failed (npm install or npm run build)
exit /b 15
:fail_start
echo [relayb][ERROR] schtasks /Run failed
exit /b 16
:port_timeout
echo [relayb][ERROR] port {port} did not start listening in time
exit /b 17
"""

FRONTEND_BLOCK = r"""echo [relayb] step 6: rebuild the frontend
cd /d "%REPO%\frontend"
if errorlevel 1 goto fail_frontend
call npm install --no-audit --no-fund
if errorlevel 1 goto fail_frontend
call npm run build
if errorlevel 1 goto fail_frontend
cd /d "%REPO%"
echo [relayb] frontend build complete"""

FRONTEND_SKIP_BLOCK = r"""echo [relayb] step 6: frontend rebuild skipped (--skip-frontend)"""

RESTART_BLOCK = r"""echo [relayb] step 7: start scheduled task %TASK%
schtasks /Run /TN %TASK%
if errorlevel 1 goto fail_start
echo [relayb] step 8: wait for port {port} to listen
set /a POLL=0
:poll_port
netstat -ano | findstr /C:":{port}" | findstr /C:"LISTENING"
if not errorlevel 1 goto port_up
set /a POLL+=1
if %POLL% GEQ 30 goto port_timeout
ping -n 3 127.0.0.1
goto poll_port
:port_up
echo [relayb] port {port} is listening"""

RESTART_SKIP_BLOCK = r"""echo [relayb] step 7: service restart skipped (--no-restart)
echo [relayb] start it later with: schtasks /Run /TN %TASK%"""


class DeployError(Exception):
    """A deployment step failed.

    ``service_stopped`` marks failures that happen after the remote .bat has already
    stopped the scheduled task: in that case the API is down until someone starts it.
    """

    def __init__(self, step, command=None, returncode=None, output="", service_stopped=False):
        super().__init__(step)
        self.step = step
        self.command = command
        self.returncode = returncode
        self.output = output
        self.service_stopped = service_stopped


def log(message):
    print("[deploy] %s" % message, flush=True)


def log_step(message):
    print("[deploy] === %s" % message, flush=True)


def log_ok(message):
    print("[deploy] OK: %s" % message, flush=True)


def log_warn(message):
    print("[deploy][WARN] %s" % message, file=sys.stderr, flush=True)


def combined_output(process):
    parts = [part for part in (process.stdout, process.stderr) if part]
    return "\n".join(parts).strip()


def tail_of(text):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-TAIL_LINES:])


def run(command, step=None):
    """Run a local command, echoing it so a failure can be reproduced."""
    log("$ %s" % " ".join(command))
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0 and step is not None:
        raise DeployError(
            step,
            command=" ".join(command),
            returncode=process.returncode,
            output=combined_output(process),
        )
    return process


def ssh(command, step=None):
    """Run one command on relayB and capture all of its output."""
    return run(["ssh"] + SSH_OPTIONS + [SSH_HOST, command], step=step)


def git(repo_root, *args, step=None):
    return run(["git", "-C", repo_root] + list(args), step=step)


def git_try(repo_root, *args, env=None):
    return subprocess.run(
        ["git", "-C", repo_root] + list(args),
        capture_output=True,
        text=True,
        env=env,
    )


def git_stdout(repo_root, *args, step=None):
    process = git(repo_root, *args, step=step)
    return process.stdout.strip()


def read_repo_root():
    process = run(["git", "rev-parse", "--show-toplevel"], step="git rev-parse --show-toplevel")
    root = process.stdout.strip()
    if not root:
        raise DeployError(
            "git rev-parse --show-toplevel returned nothing",
            command="git rev-parse --show-toplevel",
            returncode=process.returncode,
            output=combined_output(process),
        )
    return root


def read_local_head(repo_root):
    return git_stdout(repo_root, "rev-parse", "HEAD", step="read the local HEAD commit")


def read_origin_main(repo_root):
    process = git_try(repo_root, "rev-parse", "origin/main")
    if process.returncode != 0:
        return ""
    return process.stdout.strip()


def fetch_remote(repo_root):
    """Fetch GitHub main, SSH first and HTTPS second. Returns (url, label, sha).

    Every attempt carries its own timeout: the SSH one fails after about
    ``FETCH_SSH_CONNECT_TIMEOUT_S`` seconds when the host is unreachable, and the HTTPS
    fallback gives up when it receives less than ``FETCH_LOW_SPEED_LIMIT`` byte/s for
    ``FETCH_LOW_SPEED_TIME_S`` seconds. Both are scoped to that single git call (an env
    entry for ssh, ``-c`` options for https), so the global git config stays untouched.
    """
    log_step("fetching %s: SSH first, HTTPS as the fallback" % GIT_BRANCH)
    ssh_env = dict(os.environ)
    ssh_env["GIT_SSH_COMMAND"] = "ssh -o ConnectTimeout=%d" % FETCH_SSH_CONNECT_TIMEOUT_S
    https_args = [
        "-c", "http.lowSpeedLimit=%d" % FETCH_LOW_SPEED_LIMIT,
        "-c", "http.lowSpeedTime=%d" % FETCH_LOW_SPEED_TIME_S,
    ]
    attempts = (
        (GIT_SSH_URL, "ssh", [], ssh_env,
         "GIT_SSH_COMMAND ssh -o ConnectTimeout=%ds" % FETCH_SSH_CONNECT_TIMEOUT_S),
        (GIT_HTTPS_URL, "https", https_args, None,
         "http.lowSpeedLimit=%d http.lowSpeedTime=%ds"
         % (FETCH_LOW_SPEED_LIMIT, FETCH_LOW_SPEED_TIME_S)),
    )
    failures = []
    for url, label, extra_args, env, timeout_note in attempts:
        log_step("fetching %s over %s: %s (timeout: %s)" % (GIT_BRANCH, label, url, timeout_note))
        process = git_try(repo_root, *(extra_args + ["fetch", url, GIT_BRANCH]), env=env)
        if process.returncode == 0:
            remote_sha = git_stdout(repo_root, "rev-parse", "FETCH_HEAD", step="read the fetched commit")
            log_ok("fetched %s over %s (%s): %s" % (GIT_BRANCH, label, url, remote_sha))
            return url, label, remote_sha
        log_warn("fetch over %s failed (exit %d); trying the next URL" % (label, process.returncode))
        failures.append("[%s] %s\n%s" % (label, url, tail_of(combined_output(process))))
    raise DeployError(
        "git fetch failed over both the SSH and the HTTPS URL",
        command="git fetch %s %s\ngit fetch %s %s"
                % (GIT_SSH_URL, GIT_BRANCH, GIT_HTTPS_URL, GIT_BRANCH),
        output="\n".join(failures),
    )


def read_deployed_sha():
    """Read the commit deployed on relayB.

    The repository path is passed unquoted on purpose: this host runs cmd.exe,
    and a quoted remote command string picks up a stray trailing quote.
    """
    command = "git -C %s rev-parse HEAD" % REMOTE_REPO
    process = ssh(command, step="read the deployed commit on %s" % SSH_HOST)
    sha = process.stdout.strip()
    if not re.match(r"^[0-9a-f]{40}$", sha):
        raise DeployError(
            "read the deployed commit on %s (unexpected output)" % SSH_HOST,
            command="ssh %s %s" % (SSH_HOST, command),
            returncode=process.returncode,
            output=combined_output(process),
        )
    return sha


def read_remote_temp_dir():
    for variable in ("%TEMP%", "%USERPROFILE%"):
        process = ssh("echo %s" % variable)
        value = process.stdout.strip()
        if process.returncode == 0 and re.match(r"^[A-Za-z]:\\", value):
            if variable == "%USERPROFILE%":
                value = value + r"\AppData\Local\Temp"
            if " " in value:
                raise DeployError(
                    "the relayB temp directory contains a space: %s" % value,
                    output="this script passes remote paths unquoted",
                )
            return value
    raise DeployError("read the temp directory on %s" % SSH_HOST)


def can_send_incremental_bundle(repo_root, deployed_sha, remote_sha):
    """True when relayB's commit exists locally and is an ancestor of the new one."""
    if git_try(repo_root, "cat-file", "-e", deployed_sha + "^{commit}").returncode != 0:
        log("relayB commit %s is not in this repository; a full bundle is needed" % deployed_sha)
        return False
    ancestor = git_try(repo_root, "merge-base", "--is-ancestor", deployed_sha, remote_sha)
    if ancestor.returncode != 0:
        log("relayB commit %s is not an ancestor of %s; a full bundle is needed" % (deployed_sha, remote_sha))
        return False
    return True


def create_bundle(repo_root, work_dir, deployed_sha, remote_sha):
    """Create the bundle through a temporary branch so it carries a usable ref."""
    bundle_path = os.path.join(work_dir, BUNDLE_NAME)
    git(repo_root, "branch", "-f", BUNDLE_BRANCH, remote_sha, step="git branch -f %s" % BUNDLE_BRANCH)

    mode = "full"
    if can_send_incremental_bundle(repo_root, deployed_sha, remote_sha):
        range_spec = "%s..%s" % (deployed_sha, BUNDLE_BRANCH)
        log_step("creating an incremental bundle: %s" % range_spec)
        process = git_try(repo_root, "bundle", "create", bundle_path, range_spec)
        if process.returncode == 0:
            mode = "incremental"
        else:
            log_warn("the incremental range is not usable; falling back to a full bundle")
            log_warn(tail_of(combined_output(process)))

    if mode == "full":
        log_step("creating a full bundle of %s" % BUNDLE_BRANCH)
        git(repo_root, "bundle", "create", bundle_path, BUNDLE_BRANCH, step="git bundle create (full)")

    git(repo_root, "bundle", "verify", bundle_path, step="git bundle verify")
    log_ok("%s bundle: %s (%d bytes)" % (mode, bundle_path, os.path.getsize(bundle_path)))
    return bundle_path, mode


def scp_to_remote(local_path, remote_path):
    # Forward slashes keep scp's Windows path handling predictable.
    target = "%s:%s" % (SSH_HOST, remote_path.replace("\\", "/"))
    run(["scp", "-q", local_path, target], step="scp %s to %s" % (os.path.basename(local_path), SSH_HOST))


def build_bat(args):
    frontend_block = FRONTEND_SKIP_BLOCK if args.skip_frontend else FRONTEND_BLOCK
    # The blocks are inserted as values, so their own placeholders must be filled here.
    restart_block = (
        RESTART_SKIP_BLOCK if args.no_restart else RESTART_BLOCK.format(port=REMOTE_PORT)
    )
    return BAT_TEMPLATE.format(
        repo=REMOTE_REPO,
        bundle=BUNDLE_NAME,
        task=REMOTE_TASK,
        branch=BUNDLE_BRANCH,
        port=REMOTE_PORT,
        frontend_block=frontend_block,
        restart_block=restart_block,
    )


def write_bat(args, work_dir):
    bat_path = os.path.join(work_dir, BAT_NAME)
    with open(bat_path, "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(build_bat(args))
    return bat_path


def decode_remote_line(raw):
    """Decode one line of relayB console output without relying on the locale.

    relayB is a Chinese Windows host: cmd.exe and schtasks write CP936 (GBK)
    bytes, so decoding them as UTF-8 raises UnicodeDecodeError. Try UTF-8 first
    (git, npm and the .bat's own echoes), then CP936, and finally fall back to
    replacement characters: one odd byte must never abort a run that has
    already stopped the service.
    """
    for encoding in ("utf-8", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_remote_bat(remote_bat_path):
    """Run the .bat through ssh, streaming its output while keeping the tail.

    The output is streamed to this terminal; it is never redirected to a file or
    to /dev/null on the remote side (doing that hangs the ssh session on relayB).
    stdout is read as bytes and decoded per line (UTF-8, then CP936) because the
    remote console is CP936 while text=True would use this machine's locale.
    """
    command = ["ssh"] + SSH_OPTIONS + [SSH_HOST, remote_bat_path]
    log("$ %s" % " ".join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    lines = []
    tail = deque(maxlen=TAIL_LINES)
    assert process.stdout is not None
    for raw_line in process.stdout:
        text = decode_remote_line(raw_line).rstrip("\r\n")
        print("    %s" % text, flush=True)
        lines.append(text)
        tail.append(text)
    returncode = process.wait()
    return returncode, lines, list(tail)


def find_marker(lines, marker):
    for line in lines:
        position = line.find(marker)
        if position >= 0:
            return line[position + len(marker):].strip()
    return ""


def http_get(url, timeout=15):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        return None, str(error).encode("utf-8")


def wait_for_api(base_url, timeout=API_START_TIMEOUT_S):
    deadline = time.time() + timeout
    last_detail = "no response"
    while time.time() < deadline:
        status, body = http_get(base_url + "/api/health", timeout=5)
        if status == 200:
            return body
        last_detail = "status=%s body=%s" % (status, body.decode("utf-8", "replace")[:200])
        time.sleep(2)
    raise DeployError(
        "relayB API did not answer %s/api/health within %d s" % (base_url, timeout),
        output=last_detail,
    )


def extract_asset(html):
    match = ASSET_PATTERN.search(html)
    return match.group(1) if match else ""


def check_health(args):
    log_step("checking the HTTP API on %s" % REMOTE_API)
    body = wait_for_api(REMOTE_API)
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise DeployError("/api/health did not return JSON", output=body.decode("utf-8", "replace")[:400])
    driver = str(payload.get("camera_driver"))
    if driver != "ic4":
        raise DeployError(
            "/api/health reports camera_driver=%s instead of ic4" % driver,
            output=json.dumps(payload, ensure_ascii=False)[:400],
        )
    log_ok("/api/health: camera_driver=ic4")

    for path in ("/api/capture/sets", "/api/capture/draft"):
        status, body = http_get(REMOTE_API + path)
        if status != 200:
            raise DeployError(
                "GET %s returned %s" % (path, status),
                output=body.decode("utf-8", "replace")[:400],
            )
        log_ok("GET %s -> 200" % path)

    if args.skip_frontend:
        log("frontend asset check skipped (--skip-frontend)")
        return

    status, index_body = http_get(REMOTE_API + "/")
    if status != 200:
        raise DeployError("GET / returned %s" % status, output=index_body.decode("utf-8", "replace")[:400])
    served_asset = extract_asset(index_body.decode("utf-8", "replace"))
    process = ssh("type %s\\frontend\\dist\\index.html" % REMOTE_REPO)
    built_asset = extract_asset(process.stdout)
    if process.returncode != 0:
        raise DeployError(
            "read frontend\\dist\\index.html on %s" % SSH_HOST,
            command="ssh %s type ...index.html" % SSH_HOST,
            returncode=process.returncode,
            output=combined_output(process),
        )
    if not served_asset or served_asset != built_asset:
        raise DeployError(
            "the served frontend asset does not match the fresh build",
            output="served=%s built=%s" % (served_asset or "none", built_asset or "none"),
        )
    log_ok("served asset %s matches the fresh build" % served_asset)


def cleanup(repo_root, workspace, remote_dir, created_branch, keep_temp, deploy_succeeded):
    if keep_temp:
        log("keeping temp files (--keep-temp): %s" % workspace)
        if remote_dir:
            log("keeping %s:%s" % (SSH_HOST, remote_dir))
        return
    if not deploy_succeeded and remote_dir:
        log_warn("deployment failed; the relayB temp directory is removed as well (use --keep-temp to keep it)")
    if created_branch:
        process = git_try(repo_root, "branch", "-D", BUNDLE_BRANCH)
        if process.returncode == 0:
            log("removed the temporary branch %s" % BUNDLE_BRANCH)
        else:
            log_warn("could not remove the temporary branch %s" % BUNDLE_BRANCH)
    if workspace and os.path.isdir(workspace):
        shutil.rmtree(workspace, ignore_errors=True)
        log("removed the local temp directory %s" % workspace)
    if remote_dir:
        process = ssh("rmdir /s /q %s" % remote_dir)
        if process.returncode != 0:
            log_warn("could not remove %s on %s" % (remote_dir, SSH_HOST))
        else:
            log("removed %s on %s" % (remote_dir, SSH_HOST))


def print_plan(args, repo_root, deployed_sha, remote_sha, fetch_url, up_to_date):
    if not deployed_sha:
        bundle_mode = "full (the target commit is unknown)"
    elif can_send_incremental_bundle(repo_root, deployed_sha, remote_sha):
        bundle_mode = "incremental"
    else:
        bundle_mode = "full"
    print("", flush=True)
    log_step("plan (dry run: nothing on %s is touched)" % SSH_HOST)
    log("update source      : %s (used for this run)" % fetch_url)
    log("relayB deployed    : %s" % (deployed_sha or "unknown (relayB could not be reached)"))
    log("target main        : %s" % remote_sha)
    if up_to_date:
        log("relayB is already at the target commit: steps 1-5 would be skipped")
    log("1. ssh %s mkdir <temp>\\%s<timestamp>" % (SSH_HOST, REMOTE_DIR_PREFIX))
    log("2. git branch -f %s %s" % (BUNDLE_BRANCH, remote_sha))
    if deployed_sha:
        log("3. git bundle create <temp>\\%s %s..%s   (%s)" % (BUNDLE_NAME, deployed_sha, BUNDLE_BRANCH, bundle_mode))
    else:
        log("3. git bundle create <temp>\\%s %s   (%s)" % (BUNDLE_NAME, BUNDLE_BRANCH, bundle_mode))
    log("4. scp <temp>\\%s -> %s:<temp>\\%s" % (BUNDLE_NAME, SSH_HOST, BUNDLE_NAME))
    log("5. scp <temp>\\%s -> %s:<temp>\\%s" % (BAT_NAME, SSH_HOST, BAT_NAME))
    log("6. ssh %s <temp>\\%s" % (SSH_HOST, BAT_NAME))
    log("   - schtasks /End /TN %s" % REMOTE_TASK)
    log("   - wait until port %d stops listening" % REMOTE_PORT)
    log("   - git -C %s fetch \"%%BUNDLE%%\" refs/heads/%s" % (REMOTE_REPO, BUNDLE_BRANCH))
    log("   - git -C %s merge --ff-only FETCH_HEAD" % REMOTE_REPO)
    log("   - backend\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt")
    if args.skip_frontend:
        log("   - frontend rebuild: skipped (--skip-frontend)")
    else:
        log("   - cd frontend && npm install --no-audit --no-fund && npm run build")
    if args.no_restart:
        log("   - restart: skipped (--no-restart), the service stays stopped")
    else:
        log("   - schtasks /Run /TN %s, then wait up to %d s for port %d" % (REMOTE_TASK, API_START_TIMEOUT_S, REMOTE_PORT))
    if args.no_restart:
        log("7. HTTP checks: skipped (--no-restart)")
    else:
        log("7. GET %s/api/health (camera_driver=ic4), /api/capture/sets, /api/capture/draft, /" % REMOTE_API)
    log("8. remove the local bundle, the %s branch and the relayB temp directory" % BUNDLE_BRANCH)
    print("", flush=True)


def deploy(args):
    repo_root = read_repo_root()
    local_sha = read_local_head(repo_root)
    origin_sha = read_origin_main(repo_root)
    fetch_url, fetch_label, remote_sha = fetch_remote(repo_root)

    deployed_sha = ""
    try:
        deployed_sha = read_deployed_sha()
    except DeployError as error:
        if not args.dry_run:
            raise
        # A dry run only previews the plan, so an unreachable target is a warning.
        log_warn("cannot read the deployed commit on %s" % SSH_HOST)
        if error.output:
            log_warn(tail_of(error.output).splitlines()[-1])

    print("", flush=True)
    log("repository         : %s" % repo_root)
    log("local HEAD         : %s" % local_sha)
    log("origin/main        : %s" % (origin_sha or "unknown"))
    log("remote main        : %s (fetched over %s)" % (remote_sha, fetch_label))
    log("relayB deployed    : %s" % (deployed_sha or "unknown"))
    print("", flush=True)

    up_to_date = bool(deployed_sha) and deployed_sha == remote_sha
    if up_to_date:
        log_ok("relayB already up to date with %s; the service is left alone" % remote_sha)
    if args.dry_run:
        print_plan(args, repo_root, deployed_sha, remote_sha, fetch_url, up_to_date)
        log_ok("dry run finished, nothing was changed")
        return 0
    if up_to_date:
        return 0

    workspace = tempfile.mkdtemp(prefix=REMOTE_DIR_PREFIX)
    remote_dir = ""
    created_branch = False
    succeeded = False
    try:
        branch_ref = "refs/heads/" + BUNDLE_BRANCH
        branch_existed = git_try(repo_root, "rev-parse", "--verify", "--quiet", branch_ref).returncode == 0
        # Recorded before the bundle is built: create_bundle creates the branch first, so a
        # bundle failure must still let cleanup remove a branch this run introduced.
        created_branch = not branch_existed
        if branch_existed:
            log_warn("the local branch %s already exists and is reused" % BUNDLE_BRANCH)

        log_step("preparing the bundle")
        bundle_path, mode = create_bundle(repo_root, workspace, deployed_sha, remote_sha)

        remote_dir = "%s\\%s%s" % (read_remote_temp_dir(), REMOTE_DIR_PREFIX, time.strftime("%Y%m%d-%H%M%S"))
        log_step("uploading to %s:%s" % (SSH_HOST, remote_dir))
        ssh("mkdir %s" % remote_dir, step="create %s on %s" % (remote_dir, SSH_HOST))
        scp_to_remote(bundle_path, remote_dir + "\\" + BUNDLE_NAME)
        scp_to_remote(write_bat(args, workspace), remote_dir + "\\" + BAT_NAME)

        log_step("running the deployment script on %s (this can take a few minutes)" % SSH_HOST)
        returncode, lines, tail = run_remote_bat(remote_dir + "\\" + BAT_NAME)
        if returncode != 0:
            raise DeployError(
                "the deployment script on %s failed" % SSH_HOST,
                command="ssh %s %s\\%s" % (SSH_HOST, remote_dir, BAT_NAME),
                returncode=returncode,
                output="\n".join(tail),
                service_stopped=True,
            )

        merged_sha = find_marker(lines, "merged commit ")
        if merged_sha != remote_sha:
            raise DeployError(
                "relayB did not end up on %s" % remote_sha,
                output="the deployment script reported %s" % (merged_sha or "nothing"),
            )
        log_ok("relayB now runs %s (%s bundle)" % (merged_sha, mode))

        if args.no_restart:
            log_warn("service left stopped (--no-restart); start it with:")
            log_warn("  ssh %s schtasks /Run /TN %s" % (SSH_HOST, REMOTE_TASK))
        else:
            check_health(args)

        succeeded = True
        log_ok("deployment finished")
        return 0
    finally:
        cleanup(repo_root, workspace, remote_dir, created_branch, args.keep_temp, succeeded)


def failure_headline(step):
    """Step text with at most one "failed": several steps already end with that word."""
    text = (step or "").strip()
    if "failed" in text.lower():
        return text
    return "%s failed" % text


def print_failure(error):
    print("", file=sys.stderr, flush=True)
    print("[deploy][ERROR] %s" % failure_headline(error.step), file=sys.stderr, flush=True)
    if error.command:
        # Multi-line commands are printed one per line so each can be copied verbatim.
        for line in error.command.splitlines():
            print("[deploy][ERROR] command: %s" % line, file=sys.stderr, flush=True)
    if error.returncode is not None:
        print("[deploy][ERROR] exit code: %s" % error.returncode, file=sys.stderr, flush=True)
    if error.output:
        print("[deploy][ERROR] last %d lines of output:" % TAIL_LINES, file=sys.stderr, flush=True)
        for line in tail_of(error.output).splitlines():
            print("    %s" % line, file=sys.stderr, flush=True)
    if getattr(error, "service_stopped", False):
        print("[deploy][ERROR] the scheduled task %s was stopped for the deployment and was "
              "not started again; restart it on %s with:" % (REMOTE_TASK, SSH_HOST),
              file=sys.stderr, flush=True)
        print("    ssh %s schtasks /Run /TN %s" % (SSH_HOST, REMOTE_TASK),
              file=sys.stderr, flush=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="deploy_relayb.py",
        description=(
            "Deploy the current GitHub main branch to relayB (A133-LTOmicron, "
            "D:\\LEED\\static-astrophoto) from macOS: fetch over SSH with an HTTPS "
            "fallback, ship the missing commits as a git bundle, run a generated "
            ".bat on the target (stop task, fast-forward merge, pip install, npm "
            "build, start task) and verify the HTTP API."
        ),
        epilog=(
            "examples:\n"
            "  python3 scripts/deploy_relayb.py                 # full deployment\n"
            "  python3 scripts/deploy_relayb.py --dry-run       # read both commits, print the plan\n"
            "  python3 scripts/deploy_relayb.py --skip-frontend # backend-only update\n"
            "  python3 scripts/deploy_relayb.py --no-restart    # merge and rebuild, leave the task stopped\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "fetch main and read the commit deployed on relayB, print the steps that "
            "would run and exit without stopping the service or writing anything on "
            "the target machine"
        ),
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help=(
            "stop the task, apply the merge and refresh dependencies, but do not "
            "start StaticAstroPhoto again (the service stays stopped, HTTP checks are "
            "skipped)"
        ),
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="skip npm install and npm run build on relayB (the asset check is skipped too)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="keep the local bundle, the relayb-deploy branch and the relayB temp directory for debugging",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return deploy(args)
    except DeployError as error:
        print_failure(error)
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        print("[deploy][ERROR] interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

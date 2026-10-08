"""Real OS boundaries for both generation and post-session evaluation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .effort import CEILING_ORDER


def docker_base(image, name=None):
    args = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}", "--pids-limit", "512", "--shm-size", "256m", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=2g", "-e", "HOME=/tmp/home", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    if name:
        args += ["--name", name]
    return args


def generation_command(config, output, bridge, catalogue, tool, model, cli_args, name):
    args = docker_base(config["runtime"]["image"], name)
    args += ["-i", "--mount", f"type=bind,source={output},target=/workspace/output", "--mount", f"type=bind,source={bridge},target=/bridge,readonly", "--mount", f"type=bind,source={catalogue},target=/catalog,readonly", "-e", "BENCH_TOOL=" + tool, "-e", "BENCH_MODEL=" + model, "-e", "BENCH_GATEWAY_SOCKET=/bridge/gateway.sock", "-e", "BENCH_INTERNAL_HOST=" + config["runtime"]["internal_host"], config["runtime"]["image"], "python", "/opt/bench/launch.py"]
    return args + cli_args


def evaluate_command(config, output, evidence, task_id, name=None):
    return docker_base(config["runtime"]["image"], name) + ["--mount", f"type=bind,source={output},target=/workspace/output,readonly", "--mount", f"type=bind,source={evidence},target=/evidence", "-e", "BENCH_INTERNAL_HOST=" + config["runtime"]["internal_host"], "-e", "BENCH_TASK=" + task_id, config["runtime"]["image"], "python", "/opt/bench/evaluate_worker.py"]


def image_id(config):
    result = subprocess.run(["docker", "image", "inspect", config["runtime"]["image"], "--format", "{{.Id}}"], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def preflight(config, project_root):
    root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="bench-probe-", dir=root / "runtime") as tmp:
        tmp = Path(tmp)
        work = tmp / "work"
        work.mkdir()
        sentinel = tmp / "other-run-sentinel"
        sentinel.write_text("must-not-be-readable")
        probe = """
import json, os, socket
from pathlib import Path
secret_path=Path(os.environ['HOST_SENTINEL'])
blocked=[]
for path in [secret_path,Path('/home/ubuntu/.claude/CLAUDE.md'),Path('/home/ubuntu/.config/opencode/AGENTS.md'),Path('/var/run/docker.sock')]:
    try:
        path.read_bytes()
    except (FileNotFoundError,PermissionError,IsADirectoryError):
        blocked.append(str(path))
    else:
        raise RuntimeError('isolation violation: '+str(path))
s=socket.socket(); s.settimeout(2)
network_errno=s.connect_ex(('1.1.1.1',443)); s.close()
if network_errno==0: raise RuntimeError('external network reachable')
Path('/workspace/output/probe-write.txt').write_text('current-run-only')
real_keys=[k for k in os.environ if k in {'ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','OPENCODE_API_KEY'}]
if real_keys: raise RuntimeError('host credentials inherited')
print(json.dumps({'files_blocked':blocked,'external_network_errno':network_errno,'credential_env_absent':True,'current_run_write':True}))
"""
        args = docker_base(config["runtime"]["image"]) + ["--mount", f"type=bind,source={work},target=/workspace/output", "-e", "HOST_SENTINEL=" + str(sentinel), config["runtime"]["image"], "python", "-c", probe]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("Docker isolation preflight failed: " + result.stderr[-2000:])
        facts = json.loads(result.stdout)
        if (work / "probe-write.txt").read_text() != "current-run-only" or sentinel.read_text() != "must-not-be-readable":
            raise RuntimeError("Sandbox file data integrity failed")
        # opencode writes its help to stderr and claude to stdout, so both
        # streams must be read: checking only one would report a flag that is
        # actually present as missing, or the reverse.
        inspect_runtime = "import subprocess,json,hashlib; from pathlib import Path\ndef helptext(*cmd):\n    p=subprocess.run(cmd,capture_output=True,text=True)\n    return p.stdout+p.stderr\nprint(json.dumps({'versions':{k:subprocess.check_output([k,'--version'],text=True).strip() for k in ['claude','opencode']},'runtime_sources':{k:hashlib.sha256(Path('/opt/bench',k).read_bytes()).hexdigest() for k in ['launch.py','evaluate_worker.py']},'effort_flags':{'claude':'--effort' in helptext('claude','--help'),'opencode':'--variant' in helptext('opencode','run','--help')}}))"
        versions = subprocess.run(docker_base(config["runtime"]["image"]) + [config["runtime"]["image"], "python", "-c", inspect_runtime], capture_output=True, text=True, timeout=60, check=True)
        runtime = json.loads(versions.stdout)
        expected_sources = {name: hashlib.sha256((root / "runtime" / name).read_bytes()).hexdigest() for name in runtime["runtime_sources"]}
        if runtime["runtime_sources"] != expected_sources:
            raise RuntimeError("Runtime image source mismatch; rebuild the configured image before starting a batch")
        # Effort flags are read from the installed CLIs, not assumed: a silent
        # no-op flag would leave every run on an unrecorded vendor default.
        if runtime["effort_flags"] != {"claude": True, "opencode": True}:
            raise RuntimeError(f"Runtime CLIs lack the effort flags this benchmark records: {runtime['effort_flags']}")
        effort_setting = config["runtime"].get("effort", "maximum")
        if effort_setting not in ("maximum", "off"):
            raise RuntimeError(f"Unknown runtime.effort setting: {effort_setting!r}")
        facts.update({"image_id": image_id(config), "versions": runtime["versions"], "runtime_sources": runtime["runtime_sources"], "strategy": "docker-network-none-unix-inference-bridge-v1", "effort_setting": effort_setting, "effort_levels": list(CEILING_ORDER) if effort_setting == "maximum" else []})
        return facts

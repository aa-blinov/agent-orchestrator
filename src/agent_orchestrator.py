#!/usr/bin/env python3
"""Локальный stdio-мост MCP для контролируемой передачи задач кодовым CLI."""
import concurrent.futures
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

HOME = os.path.realpath(os.path.expanduser("~"))
MAX_TASK = 20000
MAX_OUTPUT = 18000
DELEGATE_TIMEOUT = 900
CONSULT_TIMEOUT = 360
MAX_CONSULTANTS = 3
ACTIVE_CHILD_GROUPS = set()

DEFAULT_AGENTS = {
    "agy": {
        "env": "AGY_BIN", "command": "agy", "label": "Antigravity CLI",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["--mode", "plan"],
            "implement": ["--mode", "accept-edits"],
        },
        "task_args": {"research": ["--print={task}"], "implement": ["--print={task}"]},
    },
    "claude": {
        "env": "CLAUDE_BIN", "command": "claude", "label": "Claude Code",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["--permission-mode", "plan", "--print"],
            "implement": ["--permission-mode", "acceptEdits", "--print"],
        },
        "task_args": {"research": ["--", "{task}"], "implement": ["--", "{task}"]},
    },
    "opencode": {
        "env": "OPENCODE_BIN", "command": "opencode", "label": "OpenCode",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["run", "--format", "default", "--agent", "plan"],
            "implement": ["run", "--format", "default", "--agent", "build", "--auto"],
        },
        "task_args": {"research": ["--", "{task}"], "implement": ["--", "{task}"]},
    },
}

CONFIG_ROOT = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CONFIG_PATH = os.path.join(CONFIG_ROOT, "agent-orchestrator", "config.json")

def load_settings(path=CONFIG_PATH):
    """Загружает настройки пользователей, не требуя их для штатного запуска."""
    settings = {"agents": {}, "profiles": {"native": {"description": "Use each CLI's configured default model", "models": {}}}, "default_profile": "native"}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as source:
            user_settings = json.load(source)
        if not isinstance(user_settings, dict):
            raise ValueError("{} must contain a JSON object".format(path))
        if not isinstance(user_settings.get("agents", {}), dict):
            raise ValueError("config 'agents' must be an object")
        if not isinstance(user_settings.get("profiles", {}), dict):
            raise ValueError("config 'profiles' must be an object")
        settings["agents"].update(user_settings.get("agents", {}))
        settings["profiles"].update(user_settings.get("profiles", {}))
        settings["default_profile"] = user_settings.get("default_profile", "native")

    agents = copy.deepcopy(DEFAULT_AGENTS)
    for name, override in settings["agents"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise ValueError("invalid agent name {!r}; use lowercase letters, numbers, '_' or '-'".format(name))
        if not isinstance(override, dict):
            raise ValueError("agent {!r} settings must be an object".format(name))
        if override.get("enabled", True) is False:
            agents.pop(name, None)
            continue
        spec = copy.deepcopy(agents.get(name, {}))
        for key, value in override.items():
            if key == "enabled":
                continue
            if key in ("args", "task_args") and isinstance(value, dict):
                spec.setdefault(key, {}).update(value)
            else:
                spec[key] = value
        for key in ("command", "label", "args", "task_args"):
            if key not in spec:
                raise ValueError("agent {!r} needs {!r} in config".format(name, key))
        if not isinstance(spec["command"], str) or not spec["command"].strip():
            raise ValueError("agent {!r} command must be a non-empty string".format(name))
        if "env" in spec and not isinstance(spec["env"], str):
            raise ValueError("agent {!r} env must be a string".format(name))
        for mode in ("research", "implement"):
            if (not isinstance(spec["args"].get(mode), list)
                    or not all(isinstance(value, str) for value in spec["args"][mode])
                    or not isinstance(spec["task_args"].get(mode), list)
                    or not all(isinstance(value, str) for value in spec["task_args"][mode])):
                raise ValueError("agent {!r} needs args and task_args for {} mode".format(name, mode))
        if "preferred_model" in spec and not isinstance(spec["preferred_model"], str):
            raise ValueError("agent {!r} preferred_model must be a string".format(name))
        if "model_args" in spec and (not isinstance(spec["model_args"], list)
                                     or not all(isinstance(value, str) for value in spec["model_args"])):
            raise ValueError("agent {!r} model_args must be a list of strings".format(name))
        if spec.get("preferred_model") and not spec.get("model_args"):
            raise ValueError("agent {!r} needs model_args to use preferred_model".format(name))
        if not isinstance(spec["label"], str):
            raise ValueError("agent {!r} label must be a string".format(name))
        agents[name] = spec

    profiles = settings["profiles"]
    if not profiles or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) for name in profiles):
        raise ValueError("profile names must use lowercase letters, numbers, '_' or '-'")
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or not isinstance(profile.get("models", {}), dict):
            raise ValueError("profile {!r} must contain a models object".format(name))
        unknown = set(profile.get("models", {})) - set(agents)
        if unknown:
            raise ValueError("profile {!r} refers to unknown agents: {}".format(name, ", ".join(sorted(unknown))))
        if any(not isinstance(model, str) for model in profile.get("models", {}).values()):
            raise ValueError("profile {!r} model names must be strings".format(name))
        for agent, model in profile.get("models", {}).items():
            if model and not agents[agent].get("model_args"):
                raise ValueError("agent {!r} needs model_args to use a profile model".format(agent))
    if settings["default_profile"] not in profiles:
        raise ValueError("default_profile must name a profile in config")
    return agents, profiles, settings["default_profile"]

AGENTS, PROFILES, DEFAULT_PROFILE = load_settings()

# The host that loaded this MCP server is the orchestrator. Excluding it avoids
# recursive calls while leaving every other installed CLI available.
ORCHESTRATOR = os.environ.get("AGENT_ORCHESTRATOR_HOST", "generic").strip().lower() or "generic"

def available_agents():
    return {name: spec for name, spec in AGENTS.items() if name != ORCHESTRATOR}

def agent_binary(agent):
    """Находит CLI по переопределению или в текущем PATH."""
    spec = AGENTS[agent]
    env_name = spec.get("env", "AGENT_ORCHESTRATOR_BIN_" + agent.upper().replace("-", "_"))
    binary = os.environ.get(env_name) or (spec["command"] if os.path.isfile(spec["command"]) else shutil.which(spec["command"]))
    if not binary or not os.path.isfile(binary):
        raise RuntimeError("{} CLI not found; install it or set {}".format(agent, env_name))
    return binary

def agent_model(agent, profile=None):
    profile = profile or DEFAULT_PROFILE
    if profile not in PROFILES:
        raise ValueError("unknown model profile: {}".format(profile))
    return PROFILES[profile].get("models", {}).get(agent) or AGENTS[agent].get("preferred_model", "")

def profile_schema():
    return {"type": "string", "enum": sorted(PROFILES), "default": DEFAULT_PROFILE,
            "description": "Model profile; omitted means {}.".format(DEFAULT_PROFILE)}

def tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required}}

def tools_for_host():
    agents = available_agents()
    names = list(agents)
    profile = profile_schema()
    tools = [tool("agent_catalog", "Show available coding agents, preferred models, and model profiles.", {}, [])]
    if len(names) >= 2:
        tools.append(tool(
            "consult_agents",
            "Ask up to three other agents in parallel for independent read-only opinions. Use for hard tradeoffs, architecture, uncertain root causes, or a second review. The current orchestrator is excluded. Optionally select a model profile.",
            {"agents": {"type": "array", "items": {"type": "string", "enum": names}, "minItems": 2, "maxItems": min(MAX_CONSULTANTS, len(names))}, "task": {"type": "string"}, "cwd": {"type": "string"}, "profile": profile},
            ["agents", "task", "cwd"]))
    if names:
        tools.append(tool(
            "delegate_agent",
            "Hand a bounded task to another installed agent. Research mode is read-only. Implement mode creates a fresh Git worktree/branch and returns status and diff. It never merges or pushes; the current orchestrator reviews and integrates the result. Optionally select a model profile.",
            {"agent": {"type": "string", "enum": names}, "task": {"type": "string"}, "cwd": {"type": "string"}, "mode": {"type": "string", "enum": ["research", "implement"]}, "profile": profile},
            ["agent", "task", "cwd", "mode"]))
    return tools

TOOLS = tools_for_host()

def rpc_error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    write_packet({"jsonrpc": "2.0", "id": msg_id, "error": err})

def write_packet(packet):
    sys.stdout.write(json.dumps(packet, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def result(msg_id, value):
    write_packet({"jsonrpc": "2.0", "id": msg_id, "result": value})

def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}

def validate_args(args, task_required=True):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    task = args.get("task", "")
    if task_required and (not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK):
        raise ValueError("task must be a non-empty string of at most 20,000 characters")
    raw_path = args.get("cwd")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("cwd must be a non-empty path")
    path = os.path.realpath(os.path.expanduser(raw_path))
    if path != HOME and not path.startswith(HOME + os.sep):
        raise ValueError("cwd must be inside the home folder")
    if not os.path.isdir(path):
        raise ValueError("cwd must be an existing folder")
    return task, path

def cli_command(agent, task, mode, profile=None):
    if agent not in available_agents():
        raise ValueError("unknown agent: {}".format(agent))
    if mode not in ("research", "implement"):
        raise ValueError("unknown mode: {}".format(mode))
    binary = agent_binary(agent)
    spec = AGENTS[agent]
    model = agent_model(agent, profile)
    def expand(parts):
        values = []
        for part in parts:
            value = str(part).replace("{task}", task).replace("{model}", model).replace("{agent}", agent).replace("{mode}", mode)
            values.append(value)
        return values
    args = [binary] + expand(spec["args"][mode])
    if model and spec.get("model_args"):
        args.extend(expand(spec["model_args"]))
    args.extend(expand(spec["task_args"][mode]))
    return args

def execute(agent, task, mode, cwd, timeout, profile=None):
    """Запускает группу процессов и не отдаёт дочернему процессу stdin MCP."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    process = subprocess.Popen(cli_command(agent, task, mode, profile), cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    ACTIVE_CHILD_GROUPS.add(process.pid)
    try:
        raw, _ = process.communicate(timeout=timeout)
        output = raw.decode("utf-8", errors="replace")
        return process.returncode, output[-MAX_OUTPUT:], False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.communicate()
        partial = exc.output or b""
        if not isinstance(partial, str):
            partial = partial.decode("utf-8", errors="replace")
        return 124, (partial[-MAX_OUTPUT:] + "\nTimed out after {} seconds; process group was stopped.".format(timeout)), True
    finally:
        ACTIVE_CHILD_GROUPS.discard(process.pid)

def git(path, *args):
    proc = subprocess.run(["git", "-C", path] + list(args), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=30, check=False)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError("git {} failed: {}".format(" ".join(args), (stderr or stdout).strip()))
    return stdout

def repo_root(path):
    return git(path, "rev-parse", "--show-toplevel").strip()

def consultation_command(agent, task, profile=None):
    # Планировщик OpenCode работает для чтения; у Claude и agy есть явные режимы плана.
    return cli_command(agent, task, "research", profile)

def consult(args):
    task, path = validate_args(args)
    profile = args.get("profile")
    if profile is not None and profile not in PROFILES:
        raise ValueError("unknown model profile: {}".format(profile))
    agents = args.get("agents")
    if not isinstance(agents, list) or len(agents) < 2 or len(agents) > MAX_CONSULTANTS:
        raise ValueError("agents must contain two or three distinct names")
    if len(set(agents)) != len(agents) or any(a not in available_agents() for a in agents):
        raise ValueError("agents must be distinct choices from agy, claude, opencode")
    prompt = task + "\n\nRead-only consultation: do not edit files or run commands that change project state. Give reasoning, risks, and a recommended approach."
    outputs = {}
    statuses = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = {pool.submit(execute, a, prompt, "research", path, CONSULT_TIMEOUT, profile): a for a in agents}
        for future in concurrent.futures.as_completed(futures):
            agent = futures[future]
            try:
                code, body, timed_out = future.result()
                statuses[agent] = (code, timed_out)
                outputs[agent] = "### {} — exit {}, {}\n{}".format(agent, code, "timed out" if timed_out else "done", body)
            except Exception as exc:
                statuses[agent] = (1, False)
                outputs[agent] = "### {} — launch error\n{}".format(agent, exc)
    report = "\n\n".join(outputs[a] for a in agents)
    failed = all(code != 0 or timed_out for code, timed_out in statuses.values())
    return text_result(report, failed)

def implement(agent, task, path, profile=None):
    root = repo_root(path)
    relative_path = os.path.relpath(path, root)
    base = git(root, "rev-parse", "HEAD").strip()
    dirty = git(root, "status", "--porcelain", "--untracked-files=all").strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = uuid.uuid4().hex[:8]
    branch = "agent-orchestrator/{}-{}-{}".format(agent, stamp, unique)
    worktree = os.path.join(HOME, ".agent-orchestrator", "worktrees", os.path.basename(root) + "-" + stamp + "-" + unique)
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    augmented = task + "\n\nWork in this worktree only. Do not push, merge, or alter other worktrees. Do not commit; leave changes for the orchestrator to review. Summarize implementation and checks."
    try:
        git(root, "worktree", "add", "-b", branch, worktree, base)
        agent_cwd = worktree if relative_path == "." else os.path.join(worktree, relative_path)
        os.makedirs(agent_cwd, exist_ok=True)
        code, output, timed_out = execute(agent, augmented, "implement", agent_cwd, DELEGATE_TIMEOUT, profile)
        # intent-to-add включает новые файлы в возвращаемый diff.
        git(worktree, "add", "-N", "--", ".")
        status = git(worktree, "status", "--short", "--untracked-files=all").rstrip()
        names = git(worktree, "diff", base, "--name-only", "--").splitlines()
        stat = git(worktree, "diff", base, "--stat", "--").rstrip()
        diff = git(worktree, "diff", base, "--no-ext-diff", "--").rstrip()
        if len(diff) > MAX_OUTPUT:
            diff = diff[:MAX_OUTPUT] + "\n...[diff truncated; full diff is in the worktree]"
        report = "Agent: {}\nExit code: {}{}\nWorktree: {}\nBranch: {}\nBase commit: {}\n".format(agent, code, " (timed out)" if timed_out else "", worktree, branch, base)
        if dirty:
            report += "Warning: source repository had uncommitted changes; they are not included in this worktree.\n"
        report += "Changed paths:\n{}\n\nStatus:\n{}\n\nDiff summary:\n{}\n\nDiff:\n{}\n\nAgent response:\n{}".format("\n".join(names) or "(none)", status or "clean", stat or "(no diff)", diff or "(no diff)", output)
        return report, code == 0 and not timed_out
    except Exception as exc:
        # При ошибке Git после запуска сообщаем путь к сохранённой рабочей копии.
        return ("Agent: {}\nWorktree: {}\nBranch: {}\nBase commit: {}\n"
                "The worktree was preserved for inspection. Error collecting the final report: {}\n"
                "Agent response:\n{}".format(agent, worktree, branch, base, exc, output if 'output' in locals() else "(agent did not start)"), False)

def call_tool(name, args):
    if name == "agent_catalog":
        agent_lines = []
        for key, spec in available_agents().items():
            model = agent_model(key)
            agent_lines.append("- {}: {} (model: {})".format(key, spec["label"], model or "CLI default"))
        profile_lines = ["- {}: {}".format(key, value.get("description", "custom model choices")) for key, value in sorted(PROFILES.items())]
        return "Current orchestrator: {}. Available peer agents:\n{}\n\nModel profiles (default: {}):\n{}\n\nSelect a profile when delegating or consulting. The profile only overrides models explicitly configured for that agent; otherwise its preferred model or CLI default is used. Implementation runs in a Git worktree, not an operating-system sandbox; the selected CLI retains the user's permissions.".format(ORCHESTRATOR, "\n".join(agent_lines) or "(none)", DEFAULT_PROFILE, "\n".join(profile_lines))
    task, path = validate_args(args)
    if name == "consult_agents":
        return consult(args)
    agent, mode = args.get("agent"), args.get("mode")
    if agent not in available_agents() or mode not in ("research", "implement"):
        raise ValueError("select an agent and mode research or implement")
    profile = args.get("profile")
    if profile is not None and profile not in PROFILES:
        raise ValueError("unknown model profile: {}".format(profile))
    if mode == "implement":
        report, okay = implement(agent, task, path, profile)
        return text_result(report, not okay)
    code, output, timed_out = execute(agent, task, mode, path, CONSULT_TIMEOUT, profile)
    return text_result("Agent: {}\nExit code: {}{}\n{}".format(agent, code, " (timed out)" if timed_out else "", output), code != 0 or timed_out)

def handle(request):
    if not isinstance(request, dict):
        rpc_error(None, -32600, "Invalid Request")
        return
    msg_id = request.get("id")
    method = request.get("method")
    if not isinstance(method, str):
        rpc_error(msg_id, -32600, "method must be a string")
        return
    if "id" not in request:
        return
    params = request.get("params", {})
    if not isinstance(params, dict):
        rpc_error(msg_id, -32602, "params must be an object")
    elif method == "initialize":
        result(msg_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-orchestrator", "version": "0.3.0"}})
    elif method == "ping":
        result(msg_id, {})
    elif method == "tools/list":
        result(msg_id, {"tools": tools_for_host()})
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
            rpc_error(msg_id, -32602, "Unknown tool: {}".format(name))
            return
        try:
            value = call_tool(name, params.get("arguments", {}))
            result(msg_id, value if isinstance(value, dict) else text_result(value))
        except ValueError as exc:
            rpc_error(msg_id, -32602, str(exc))
        except Exception as exc:
            result(msg_id, text_result("{}: {}".format(type(exc).__name__, exc), True))
    elif method and method.startswith("notifications/"):
        return
    else:
        rpc_error(msg_id, -32601, "Method not found")

def main():
    def stop_children(signum, frame):
        for pid in list(ACTIVE_CHILD_GROUPS):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError as exc:
            rpc_error(None, -32700, "Parse error", str(exc))
            continue
        try:
            handle(request)
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            if request_id is not None:
                result(request_id, text_result("{}: {}".format(type(exc).__name__, exc), True))

if __name__ == "__main__":
    main()

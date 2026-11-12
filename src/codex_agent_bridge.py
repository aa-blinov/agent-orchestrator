#!/usr/bin/env python3
"""Локальный stdio-мост MCP для контролируемой передачи задач кодовым CLI."""
import concurrent.futures
import json
import os
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

AGENTS = {
    "agy": {"env": "AGY_BIN", "command": "agy", "label": "Antigravity CLI (Google/Gemini account and models)"},
    "claude": {"env": "CLAUDE_BIN", "command": "claude", "label": "Claude Code (Claude models and coding harness)"},
    "opencode": {"env": "OPENCODE_BIN", "command": "opencode", "label": "OpenCode (model/provider configured in OpenCode)"},
}

def agent_binary(agent):
    """Находит CLI по переопределению или в текущем PATH."""
    spec = AGENTS[agent]
    binary = os.environ.get(spec["env"]) or shutil.which(spec["command"])
    if not binary or not os.path.isfile(binary):
        raise RuntimeError("{} CLI not found; install it or set {}".format(agent, spec["env"]))
    return binary

def tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required}}

TOOLS = [
    tool("agent_catalog", "Show available coding harnesses and how to choose among them. Does not start an agent.", {}, []),
    tool("consult_agents", "Ask up to three agents in parallel for independent read-only opinions. Use for hard tradeoffs, architecture, uncertain root causes, or a second review. No agent should edit files.", {"agents": {"type": "array", "items": {"type": "string", "enum": list(AGENTS)}, "minItems": 2, "maxItems": MAX_CONSULTANTS}, "task": {"type": "string"}, "cwd": {"type": "string"}}, ["agents", "task", "cwd"]),
    tool("delegate_agent", "Hand a bounded implementation task to one agent. Research mode is read-only. Implement mode creates a fresh Git worktree/branch, runs the agent there, then returns status, changed paths, diff summary, and a bounded diff. It never merges or pushes. Codex must review and integrate the result.", {"agent": {"type": "string", "enum": list(AGENTS)}, "task": {"type": "string"}, "cwd": {"type": "string"}, "mode": {"type": "string", "enum": ["research", "implement"]}}, ["agent", "task", "cwd", "mode"]),
]

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

def cli_command(agent, task, mode):
    if agent not in AGENTS:
        raise ValueError("unknown agent: {}".format(agent))
    binary = agent_binary(agent)
    if agent == "agy":
        return [binary, "--mode", "plan" if mode == "research" else "accept-edits", "--print=" + task]
    if agent == "claude":
        return [binary, "--permission-mode", "plan" if mode == "research" else "acceptEdits", "--print", "--", task]
    args = [binary, "run", "--format", "default", "--agent", "plan" if mode == "research" else "build"]
    if mode == "implement":
        args.append("--auto")
    return args + ["--", task]

def execute(agent, task, mode, cwd, timeout):
    """Запускает группу процессов и не отдаёт дочернему процессу stdin MCP."""
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    process = subprocess.Popen(cli_command(agent, task, mode), cwd=cwd, env=env,
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

def consultation_command(agent, task):
    # Планировщик OpenCode работает для чтения; у Claude и agy есть явные режимы плана.
    return cli_command(agent, task, "research")

def consult(args):
    task, path = validate_args(args)
    agents = args.get("agents")
    if not isinstance(agents, list) or len(agents) < 2 or len(agents) > MAX_CONSULTANTS:
        raise ValueError("agents must contain two or three distinct names")
    if len(set(agents)) != len(agents) or any(a not in AGENTS for a in agents):
        raise ValueError("agents must be distinct choices from agy, claude, opencode")
    prompt = task + "\n\nRead-only consultation: do not edit files or run commands that change project state. Give reasoning, risks, and a recommended approach."
    outputs = {}
    statuses = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = {pool.submit(execute, a, prompt, "research", path, CONSULT_TIMEOUT): a for a in agents}
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

def implement(agent, task, path):
    root = repo_root(path)
    relative_path = os.path.relpath(path, root)
    base = git(root, "rev-parse", "HEAD").strip()
    dirty = git(root, "status", "--porcelain", "--untracked-files=all").strip()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    unique = uuid.uuid4().hex[:8]
    branch = "codex-agent/{}-{}-{}".format(agent, stamp, unique)
    worktree = os.path.join(HOME, ".codex", "agent-worktrees", os.path.basename(root) + "-" + stamp + "-" + unique)
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    augmented = task + "\n\nWork in this worktree only. Do not push, merge, or alter other worktrees. Do not commit; leave changes for Codex to review. Summarize implementation and checks."
    try:
        git(root, "worktree", "add", "-b", branch, worktree, base)
        agent_cwd = worktree if relative_path == "." else os.path.join(worktree, relative_path)
        os.makedirs(agent_cwd, exist_ok=True)
        code, output, timed_out = execute(agent, augmented, "implement", agent_cwd, DELEGATE_TIMEOUT)
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
        return "Available agents:\n" + "\n".join("- {}: {}".format(k, v["label"]) for k, v in AGENTS.items()) + "\n\nRoute by capability and configured model, not brand alone. Use agy for Google/Antigravity-specific questions, Claude for an independent critique or review, and OpenCode when its selected provider/model fits the task. Implement mode uses a Git worktree, not an operating-system sandbox; the selected CLI retains the user's permissions."
    task, path = validate_args(args)
    if name == "consult_agents":
        return consult(args)
    agent, mode = args.get("agent"), args.get("mode")
    if agent not in AGENTS or mode not in ("research", "implement"):
        raise ValueError("select an agent and mode research or implement")
    if mode == "implement":
        report, okay = implement(agent, task, path)
        return text_result(report, not okay)
    code, output, timed_out = execute(agent, task, mode, path, CONSULT_TIMEOUT)
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
        result(msg_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-agent-bridge", "version": "0.1.0"}})
    elif method == "ping":
        result(msg_id, {})
    elif method == "tools/list":
        result(msg_id, {"tools": TOOLS})
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

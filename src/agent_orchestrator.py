#!/usr/bin/env python3
"""Локальный stdio-мост MCP для контролируемой передачи задач кодовым CLI."""
import concurrent.futures
import copy
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

HOME = os.path.realpath(os.path.expanduser("~"))
MAX_TASK = 20000
MAX_OUTPUT = 18000
DELEGATE_TIMEOUT = 7200
CONSULT_TIMEOUT = 7200
MAX_CONSULTANTS = 3
MAX_SESSION_AGENT_CALLS = 4
MAX_SESSION_PEERS = 3
ACTIVE_CHILD_GROUPS = set()

DEFAULT_AGENTS = {
    "codex": {
        "env": "CODEX_BIN", "command": "codex", "label": "Codex CLI",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check", "--json"],
            "implement": ["exec", "--ephemeral", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json"],
        },
        "task_args": {"research": ["--", "{task}"], "implement": ["--", "{task}"]},
    },
    "agy": {
        "env": "AGY_BIN", "command": "agy", "label": "Antigravity CLI",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["--mode", "plan", "--output-format", "json"],
            "implement": ["--mode", "accept-edits", "--output-format", "json"],
        },
        "task_args": {"research": ["--print={task}"], "implement": ["--print={task}"]},
    },
    "claude": {
        "env": "CLAUDE_BIN", "command": "claude", "label": "Claude Code",
        "model_args": ["--model", "{model}"],
        "args": {
            "research": ["--permission-mode", "plan", "--print", "--output-format", "json"],
            "implement": ["--permission-mode", "acceptEdits", "--print", "--output-format", "json"],
        },
        "task_args": {"research": ["--", "{task}"], "implement": ["--", "{task}"]},
    },
    "opencode": {
        "env": "OPENCODE_BIN", "command": "opencode", "label": "OpenCode",
        "model_args": ["--model", "{model}"],
        "args": {
            # The shared OpenCode server can keep the project root from the
            # lead session and ignore subprocess cwd. A private server makes
            # each delegated call honor its isolated worktree.
            "research": ["run", "--standalone", "--format", "json", "--agent", "plan"],
            "implement": ["run", "--standalone", "--format", "json", "--agent", "build", "--auto"],
        },
        "task_args": {"research": ["--", "{task}"], "implement": ["--", "{task}"]},
    },
}

CONFIG_ROOT = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CONFIG_PATH = os.path.join(CONFIG_ROOT, "agent-orchestrator", "config.json")
DATA_ROOT = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
USAGE_DB = os.path.join(DATA_ROOT, "agent-orchestrator", "usage.sqlite3")

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
        preferred_models = spec.get("preferred_models", {})
        if not isinstance(preferred_models, dict) or set(preferred_models) - {"research", "implement"}:
            raise ValueError("agent {!r} preferred_models may contain research and implement only".format(name))
        if any(not isinstance(model, str) for model in preferred_models.values()):
            raise ValueError("agent {!r} preferred_models must contain strings".format(name))
        if "model_args" in spec and (not isinstance(spec["model_args"], list)
                                     or not all(isinstance(value, str) for value in spec["model_args"])):
            raise ValueError("agent {!r} model_args must be a list of strings".format(name))
        if (spec.get("preferred_model") or any(preferred_models.values())) and not spec.get("model_args"):
            raise ValueError("agent {!r} needs model_args to use preferred models".format(name))
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

def resolve_task_id(value=None):
    if value is None or value == "":
        return "task-{}-{}".format(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8])
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("task_id must be 1-128 letters, numbers, dots, underscores, colons, or hyphens")
    return value

def required_task_id(args):
    """Requires a stable task ID so repeated agent runs share one hard budget."""
    if not isinstance(args, dict) or not isinstance(args.get("task_id"), str) or not args["task_id"].strip():
        raise ValueError("task_id is required for agent calls; reuse the same value across one task")
    return resolve_task_id(args["task_id"])

def required_session_id(args):
    """Requires the stable caller-supplied identifier for the current chat."""
    if not isinstance(args, dict) or not isinstance(args.get("session_id"), str) or not args["session_id"].strip():
        raise ValueError("session_id is required; create one identifier for this chat and reuse it for all agent calls")
    return resolve_task_id(args["session_id"])

def reserve_agent_calls(session_id, agents, path=None):
    """Atomically reserves CLI-call slots, including parallel consultations."""
    path = path or USAGE_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path, timeout=10) as database:
        database.execute("""CREATE TABLE IF NOT EXISTS session_agent_budgets (
            session_id TEXT PRIMARY KEY,
            calls_reserved INTEGER NOT NULL,
            agents_json TEXT NOT NULL
        )""")
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT calls_reserved, agents_json FROM session_agent_budgets WHERE session_id = ?",
                               (session_id,)).fetchone()
        if row:
            calls_reserved, agent_names = row[0], set(json.loads(row[1]))
        else:
            calls_reserved, agent_names = 0, set()
        requested_agents = set(agents)
        if calls_reserved + len(agents) > MAX_SESSION_AGENT_CALLS:
            raise ValueError("chat session {!r} has used {}/{} agent calls; this request needs {} more. No agents were started.".format(
                session_id, calls_reserved, MAX_SESSION_AGENT_CALLS, len(agents)))
        combined_agents = agent_names | requested_agents
        if len(combined_agents) > MAX_SESSION_PEERS:
            raise ValueError("chat session {!r} already involves {} distinct peer agents; the limit is {}. No agents were started.".format(
                session_id, len(agent_names), MAX_SESSION_PEERS))
        database.execute("""INSERT INTO session_agent_budgets(session_id, calls_reserved, agents_json) VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET calls_reserved=excluded.calls_reserved, agents_json=excluded.agents_json""",
            (session_id, calls_reserved + len(agents), json.dumps(sorted(combined_agents))))
        database.commit()
    os.chmod(path, 0o600)

def store_usage(task_id, agent, model, mode, exit_code, timed_out, duration, usage, path=None):
    """Сохраняет только метаданные вызова и сообщённые CLI счётчики; текст задачи не сохраняется."""
    path = path or USAGE_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    values = usage or {}
    with sqlite3.connect(path, timeout=10) as database:
        database.execute("""CREATE TABLE IF NOT EXISTS agent_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            model TEXT NOT NULL,
            mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            timed_out INTEGER NOT NULL,
            duration_seconds REAL NOT NULL,
            usage_reported INTEGER NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cached_input_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER
        )""")
        database.execute("CREATE INDEX IF NOT EXISTS idx_agent_calls_task_id ON agent_calls(task_id)")
        database.execute("""INSERT INTO agent_calls (
            task_id, agent, model, mode, created_at, exit_code, timed_out, duration_seconds,
            usage_reported, input_tokens, output_tokens, cached_input_tokens,
            cache_write_tokens, reasoning_tokens, total_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            task_id, agent, model, mode, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            int(exit_code), int(timed_out), round(duration, 3), int(bool(usage)),
            values.get("input_tokens"), values.get("output_tokens"),
            values.get("cached_input_tokens"), values.get("cache_write_tokens"),
            values.get("reasoning_tokens"), values.get("total_tokens"),
        ))
    os.chmod(path, 0o600)

def agent_binary(agent):
    """Находит CLI по переопределению или в текущем PATH."""
    spec = AGENTS[agent]
    env_name = spec.get("env", "AGENT_ORCHESTRATOR_BIN_" + agent.upper().replace("-", "_"))
    binary = os.environ.get(env_name) or (spec["command"] if os.path.isfile(spec["command"]) else shutil.which(spec["command"]))
    if not binary or not os.path.isfile(binary):
        raise RuntimeError("{} CLI not found; install it or set {}".format(agent, env_name))
    return binary

def agent_model(agent, profile=None, mode=None):
    profile = profile or DEFAULT_PROFILE
    if profile not in PROFILES:
        raise ValueError("unknown model profile: {}".format(profile))
    return (PROFILES[profile].get("models", {}).get(agent)
            or AGENTS[agent].get("preferred_models", {}).get(mode, "")
            or AGENTS[agent].get("preferred_model", ""))

def profile_schema():
    return {"type": "string", "enum": sorted(PROFILES), "default": DEFAULT_PROFILE,
            "description": "Model profile; omitted means {}.".format(DEFAULT_PROFILE)}

def tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required}}

def tools_for_host():
    agents = available_agents()
    names = list(agents)
    profile = profile_schema()
    tools = [
        tool("agent_catalog", "Show available coding agents, preferred models, and model profiles.", {}, []),
        tool("usage_report", "Read locally stored token usage by task and agent. Pass task_id to group usage for one task, or session_id to inspect the per-chat agent-call budget.",
             {"task_id": {"type": "string", "description": "Optional task group identifier; omitted lists recent task groups."},
              "session_id": {"type": "string", "description": "Optional chat identifier; shows reserved calls out of the session budget."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, []),
    ]
    if len(names) >= 2:
        tools.append(tool(
            "consult_agents",
            "Используй экономно: обычно достаточно одного исполнителя, а ведущий сам проверяет изменения и проверки. Запускай двух или трёх агентов параллельно, только если независимые мнения могут изменить важное или неопределённое решение; это заменяет отдельный исследовательский вызов. Жёсткий лимит на чат общий с delegate_agent: четыре запуска агента и не более трёх разных помощников. Создай один session_id для этого чата и повторяй его во всех вызовах; task_id отдельно группирует расходы по задаче. Превышение лимита отклоняется до запуска агентов. Не повторяй вызов без сбоя или конкретного замечания.",
            {"agents": {"type": "array", "items": {"type": "string", "enum": names}, "minItems": 2, "maxItems": min(MAX_CONSULTANTS, len(names))}, "task": {"type": "string"}, "cwd": {"type": "string"}, "profile": profile, "task_id": {"type": "string", "description": "Обязательный идентификатор задачи для группировки расходов."}, "session_id": {"type": "string", "description": "Обязательный постоянный идентификатор этого чата. Повторяй его во всех вызовах. Общий лимит: четыре запуска и три разных помощника."}},
            ["agents", "task", "cwd", "task_id", "session_id"]))
    if names:
        tools.append(tool(
            "delegate_agent",
            "Передай одному установленному агенту ограниченную задачу. Обычно достаточно одного исполнителя: ведущий сам проверяет изменения и запускает проверки. Режим research используй для одного независимого мнения или проверки, если этого требует риск. Добавляй помощника только ради отдельного нерешённого вопроса; повторяй вызов лишь после сбоя или конкретного замечания. Жёсткий лимит на чат общий с consult_agents: четыре запуска и не более трёх разных помощников. Повторяй один session_id во всех вызовах этого чата; task_id отдельно группирует расходы по задаче. Превышение лимита отклоняется до запуска. Режим implement создаёт отдельную ветку и рабочую копию Git, возвращает diff и не объединяет и не отправляет изменения.",
            {"agent": {"type": "string", "enum": names}, "task": {"type": "string"}, "cwd": {"type": "string"}, "mode": {"type": "string", "enum": ["research", "implement"]}, "profile": profile, "task_id": {"type": "string", "description": "Обязательный идентификатор задачи для группировки расходов."}, "session_id": {"type": "string", "description": "Обязательный постоянный идентификатор этого чата. Повторяй его во всех вызовах. Общий лимит: четыре запуска и три разных помощника."}},
            ["agent", "task", "cwd", "mode", "task_id", "session_id"]))
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
    model = agent_model(agent, profile, mode)
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

def json_events(output):
    """Извлекает завершённые структурированные ответы из потока CLI."""
    events = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    if not events and output.lstrip().startswith("{"):
        try:
            value = json.loads(output)
            if isinstance(value, dict):
                events.append(value)
        except ValueError:
            pass
    return events

def normalized_usage(value):
    """Приводит известные названия полей разных CLI к общей форме."""
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "total_input_tokens", "input"),
        "output_tokens": ("output_tokens", "total_output_tokens", "output"),
        "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens"),
        "cache_write_tokens": ("cache_creation_input_tokens", "cache_write_tokens"),
        "reasoning_tokens": ("reasoning_output_tokens", "thinking_tokens", "reasoning"),
        "total_tokens": ("total_tokens", "total"),
    }
    normalized = {}
    for target, names in aliases.items():
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
                normalized[target] = int(candidate)
                break
    cache = value.get("cache")
    if isinstance(cache, dict):
        for key, target in (("read", "cached_input_tokens"), ("write", "cache_write_tokens")):
            candidate = cache.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
                normalized[target] = int(candidate)
    return normalized or None

def extract_usage(agent, output):
    """Возвращает человекочитаемый ответ и только те счётчики, что сообщил CLI."""
    events = json_events(output)
    usage = None
    texts = []
    if agent == "codex":
        for event in events:
            if event.get("type") == "turn.completed":
                usage = normalized_usage(event.get("usage")) or usage
            item = event.get("item", {})
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    texts.append(item["text"])
    elif agent in ("claude", "agy"):
        for event in events:
            usage = normalized_usage(event.get("usage")) or usage
            text = event.get("result") if agent == "claude" else event.get("response")
            if isinstance(text, str):
                texts.append(text)
    elif agent == "opencode":
        step_usages = []
        for event in events:
            part = event.get("part", {})
            if not isinstance(part, dict):
                part = {}
            if event.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            if event.get("type") == "step_finish":
                found = normalized_usage(part.get("tokens") or event.get("tokens"))
                if found:
                    step_usages.append(found)
        if step_usages:
            usage = {}
            for step in step_usages:
                for key, count in step.items():
                    usage[key] = usage.get(key, 0) + count
    if usage is None:
        # Codex may expose a total in its human-readable footer on older versions.
        match = re.search(r"tokens used\s+([0-9][0-9 ,]*)", output, re.IGNORECASE)
        if match:
            usage = {"total_tokens": int(re.sub(r"[^0-9]", "", match.group(1)))}
    if texts:
        output = "\n".join(text.strip() for text in texts if text.strip())
    return output, usage

USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens")
USAGE_LABELS = {"input_tokens": "input", "output_tokens": "output", "cached_input_tokens": "cached input",
                "cache_write_tokens": "cache write", "reasoning_tokens": "reasoning", "total_tokens": "total"}

def usage_summary(usage):
    if not usage:
        return "Token usage: not reported by CLI."
    fields = ["{} {}".format(usage[key], USAGE_LABELS[key]) for key in USAGE_FIELDS if key in usage]
    return "Token usage: {} (reported by CLI).".format(", ".join(fields))

def counter_groups(rows):
    """Суммирует счётчики только внутри одной пары агент/запрошенная модель.

    Разные провайдеры считают кэш и рассуждения по-разному, поэтому сложение между
    группами дало бы число без смысла. NULL означает «не сообщено» и в сумму не входит,
    а ноль — сообщённое значение, поэтому покрытие считается отдельно от суммы.
    """
    groups = {}
    for row in rows:
        key = (row["agent"], row["model"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"agent": row["agent"], "model": row["model"], "calls": 0, "reported": 0,
                                   "present": dict.fromkeys(USAGE_FIELDS, 0), "sums": dict.fromkeys(USAGE_FIELDS, 0)}
        group["calls"] += 1
        group["reported"] += 1 if row["usage_reported"] else 0
        for field in USAGE_FIELDS:
            if row[field] is not None:
                group["present"][field] += 1
                group["sums"][field] += row[field]
    return list(groups.values())

def counter_text(group):
    """Выводит сумму и покрытие (сообщивших вызовов / всех вызовов) для каждого счётчика."""
    parts = []
    for field in USAGE_FIELDS:
        present = group["present"][field]
        if present:
            parts.append("{} {} ({}/{})".format(USAGE_LABELS[field], group["sums"][field], present, group["calls"]))
        else:
            parts.append("{} unavailable (0/{})".format(USAGE_LABELS[field], group["calls"]))
    return ", ".join(parts)

def group_line(group):
    return "{} / {}: {} calls, {} with usage reported | {}".format(
        group["agent"], group["model"], group["calls"], group["reported"], counter_text(group))

def session_budget_line(session_id, path=None):
    """Shows consumed reservations for one caller-supplied chat identifier."""
    path = path or USAGE_DB
    if not os.path.isfile(path):
        calls_used, agent_count = 0, 0
    else:
        with sqlite3.connect(path, timeout=10) as database:
            exists = database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_agent_budgets'").fetchone()
            row = database.execute("SELECT calls_reserved, agents_json FROM session_agent_budgets WHERE session_id = ?",
                                   (session_id,)).fetchone() if exists else None
        calls_used, agent_count = (row[0], len(json.loads(row[1]))) if row else (0, 0)
    return "Chat session agent budget: {}/{} calls used; {}/{} distinct peer agents.".format(
        calls_used, MAX_SESSION_AGENT_CALLS, agent_count, MAX_SESSION_PEERS)

def usage_report(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    task_id = resolve_task_id(args.get("task_id")) if args.get("task_id") else None
    session_id = resolve_task_id(args.get("session_id")) if args.get("session_id") else None
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if not os.path.isfile(USAGE_DB):
        if session_id:
            return session_budget_line(session_id)
        return "No usage records yet. Records are stored locally at {}.".format(USAGE_DB)
    if session_id and not task_id:
        return session_budget_line(session_id)
    with sqlite3.connect(USAGE_DB, timeout=10) as database:
        database.row_factory = sqlite3.Row
        if task_id:
            rows = database.execute("SELECT * FROM agent_calls WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
            if not rows:
                suffix = " " + session_budget_line(session_id) if session_id else ""
                return "No usage records found for task_id {}.{}".format(task_id, suffix)
            report = ["Usage for task_id {} ({} calls).".format(task_id, len(rows))]
            if session_id:
                report.append(session_budget_line(session_id))
            for row in rows:
                usage = {key: row[key] for key in USAGE_FIELDS if row[key] is not None}
                report.append("- {} | {} | {} / {} | exit {}{} | {:.1f}s | {}".format(
                    row["created_at"], row["agent"], row["mode"], row["model"], row["exit_code"],
                    " / timed out" if row["timed_out"] else "", row["duration_seconds"],
                    usage_summary(usage) if row["usage_reported"] else "Token usage: not reported by CLI."))
            report.append("Counters by agent and model. Sums include only calls from that agent and model; (present/calls) is how many of those calls reported the counter. Groups are not combined.")
            report.extend("- " + group_line(group) for group in counter_groups(rows))
            report.append("Cache counters are shown as each provider reports them; do not infer a shared relationship between cache and input fields.")
            return "\n".join(report)
        tasks = database.execute("""SELECT task_id, MIN(created_at) AS first_seen, COUNT(*) AS calls, MAX(id) AS last_id
            FROM agent_calls GROUP BY task_id ORDER BY last_id DESC LIMIT ?""", (limit,)).fetchall()
        if not tasks:
            return "No usage records yet."
        task_ids = [task["task_id"] for task in tasks]
        call_rows = database.execute("SELECT * FROM agent_calls WHERE task_id IN ({}) ORDER BY id".format(
            ",".join("?" * len(task_ids))), task_ids).fetchall()
    calls_by_task = {}
    for row in call_rows:
        calls_by_task.setdefault(row["task_id"], []).append(row)
    report = ["Recent task usage. Counters are summed per agent and model only; (present/calls) is how many of those calls reported the counter."]
    for task in tasks:
        rows = calls_by_task.get(task["task_id"], [])
        report.append("- {} | task_id={} | {} calls, {} with usage reported".format(
            task["first_seen"], task["task_id"], len(rows), sum(row["usage_reported"] for row in rows)))
        report.extend("  - " + group_line(group) for group in counter_groups(rows))
    report.append("Pass a task_id to inspect its individual calls. Records are stored locally at {}.".format(USAGE_DB))
    return "\n".join(report)

def execute(agent, task, mode, cwd, timeout, profile=None, task_id=None):
    """Запускает группу процессов и не отдаёт дочернему процессу stdin MCP."""
    started = time.monotonic()
    task_id = resolve_task_id(task_id)
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    process = subprocess.Popen(cli_command(agent, task, mode, profile), cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    ACTIVE_CHILD_GROUPS.add(process.pid)
    def finish(code, output, timed_out):
        output, usage = extract_usage(agent, output)
        saved = True
        try:
            store_usage(task_id, agent, agent_model(agent, profile, mode) or "cli-default", mode,
                        code, timed_out, time.monotonic() - started, usage)
        except Exception as exc:
            saved = False
            sys.stderr.write("agent-orchestrator: unable to save usage metrics: {}\n".format(exc))
            sys.stderr.flush()
        return code, output[-MAX_OUTPUT:], timed_out, usage, saved
    try:
        raw, _ = process.communicate(timeout=timeout)
        output = raw.decode("utf-8", errors="replace")
        return finish(process.returncode, output, False)
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
        return finish(124, partial + "\nTimed out after {} seconds; process group was stopped.".format(timeout), True)
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
    task_id = required_task_id(args)
    session_id = required_session_id(args)
    profile = args.get("profile")
    if profile is not None and profile not in PROFILES:
        raise ValueError("unknown model profile: {}".format(profile))
    agents = args.get("agents")
    if not isinstance(agents, list) or len(agents) < 2 or len(agents) > MAX_CONSULTANTS:
        raise ValueError("agents must contain two or three distinct names")
    if len(set(agents)) != len(agents) or any(a not in available_agents() for a in agents):
        raise ValueError("agents must be distinct installed peer agents")
    reserve_agent_calls(session_id, agents)
    prompt = task + "\n\nRead-only consultation: do not edit files or run commands that change project state. Give reasoning, risks, and a recommended approach."
    outputs = {}
    statuses = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = {pool.submit(execute, a, prompt, "research", path, CONSULT_TIMEOUT, profile, task_id): a for a in agents}
        for future in concurrent.futures.as_completed(futures):
            agent = futures[future]
            try:
                code, body, timed_out, usage, saved = future.result()
                statuses[agent] = (code, timed_out)
                outputs[agent] = "### {} — exit {}, {}; usage record {}\n{}".format(
                    agent, code, "timed out" if timed_out else "done", "saved" if saved else "not saved", body)
            except Exception as exc:
                statuses[agent] = (1, False)
                outputs[agent] = "### {} — launch error\n{}".format(agent, exc)
    report = "\n\n".join(outputs[a] for a in agents)
    report = "Task ID: {} (usage saved locally)\n\n{}".format(task_id, report)
    failed = all(code != 0 or timed_out for code, timed_out in statuses.values())
    return text_result(report, failed)

def implement(agent, task, path, profile=None, task_id=None, session_id=None):
    task_id = resolve_task_id(task_id)
    session_id = resolve_task_id(session_id)
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
    reserve_agent_calls(session_id, [agent])
    try:
        git(root, "worktree", "add", "-b", branch, worktree, base)
        agent_cwd = worktree if relative_path == "." else os.path.join(worktree, relative_path)
        os.makedirs(agent_cwd, exist_ok=True)
        code, output, timed_out, usage, saved = execute(agent, augmented, "implement", agent_cwd, DELEGATE_TIMEOUT, profile, task_id)
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
        report += "Task ID: {} (usage record {})\nChanged paths:\n{}\n\nStatus:\n{}\n\nDiff summary:\n{}\n\nDiff:\n{}\n\nAgent response:\n{}".format(task_id, "saved" if saved else "not saved", "\n".join(names) or "(none)", status or "clean", stat or "(no diff)", diff or "(no diff)", output)
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
            research_model = agent_model(key, mode="research") or "CLI default"
            implement_model = agent_model(key, mode="implement") or "CLI default"
            agent_lines.append("- {}: {}; complex/research: {}; implementation: {}".format(
                key, spec["label"], research_model, implement_model))
        profile_lines = ["- {}: {}".format(key, value.get("description", "custom model choices")) for key, value in sorted(PROFILES.items())]
        return "Current orchestrator: {}. Available peer agents:\n{}\n\nModel profiles (default: {}):\n{}\n\nEach agent can use a separate preferred model for research/complex reasoning and implementation. A selected profile overrides those role preferences; when no model is configured, the CLI's own default is used. Implementation runs in a Git worktree, not an operating-system sandbox; the selected CLI retains the user's permissions.".format(ORCHESTRATOR, "\n".join(agent_lines) or "(none)", DEFAULT_PROFILE, "\n".join(profile_lines))
    if name == "usage_report":
        return usage_report(args)
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
        task_id = required_task_id(args)
        session_id = required_session_id(args)
        report, okay = implement(agent, task, path, profile, task_id, session_id)
        return text_result(report, not okay)
    task_id = required_task_id(args)
    session_id = required_session_id(args)
    reserve_agent_calls(session_id, [agent])
    code, output, timed_out, usage, saved = execute(agent, task, mode, path, CONSULT_TIMEOUT, profile, task_id)
    return text_result("Task ID: {} (usage record {})\nAgent: {}\nExit code: {}{}\n{}".format(task_id, "saved" if saved else "not saved", agent, code, " (timed out)" if timed_out else "", output), code != 0 or timed_out)

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
        result(msg_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "agent-orchestrator", "version": "0.4.2"},
                        "instructions": "Для каждого чата создай один session_id и повторяй его во всех вызовах delegate_agent и consult_agents. Лимит действует на чат: не более четырёх запусков агентов и трёх разных помощников. Обычно достаточно одного исполнителя; ведущий сам проверяет diff и проверки. Подключай других только при конкретном риске или нерешённом вопросе. task_id отдельно группирует вызовы по задаче."})
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

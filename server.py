"""Token Monitor - 读取 Claude SDK 本地数据，提供 API + 前端展示"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Claude Token Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST"], allow_headers=["*"])

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CONFIG_FILE = Path(__file__).parent / "monitor-groups.json"


# ─── 配置 ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    """加载监控配置"""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_exclude_patterns() -> list[str]:
    """加载排除关键词列表"""
    return load_config().get("exclude", [])


def parse_project_name(dir_name: str) -> str:
    """从目录名解析出可读的项目名"""
    # -Users-sevenstars-Projects-Trinity -> Trinity
    # -Users-sevenstars -> ~ (home)
    # -private-var-folders-... -> tmp session
    name = dir_name.lstrip("-")

    if name.startswith("Users-sevenstars-Projects-"):
        return name.replace("Users-sevenstars-Projects-", "")
    elif name.startswith("Users-sevenstars-code-symphony-workspaces-"):
        workspace = name.replace("Users-sevenstars-code-symphony-workspaces-", "")
        return f"symphony/{workspace}"
    elif name.startswith("Users-sevenstars--"):
        return name.replace("Users-sevenstars--", "")
    elif name == "Users-sevenstars" or name == "Users-severstars":
        return "~"
    elif name.startswith("private-var-folders") or name.startswith("private-tmp"):
        # 提取有意义的部分
        parts = name.split("-workspaces-")
        if len(parts) > 1:
            runner = ""
            for keyword in ["symphony", "task-forge", "agent-runner"]:
                if keyword in parts[0]:
                    runner = keyword
                    break
            workspace = parts[-1]
            return f"{runner}/{workspace}" if runner else f"tmp/{workspace}"
        return "tmp"
    elif name.startswith("Users-sevenstars-"):
        return name[len("Users-sevenstars-"):]
    else:
        return name




# ─── 实时监控 ──────────────────────────────────────────────────────────

# 记录每个文件上次读到的位置
_file_cursors: dict[str, int] = {}
# 所有已知项目 {显示名: [目录名列表]}（含活跃 + 闲置）
_known_projects: dict[str, list[str]] = {}
# 项目闲置时间戳 {显示名: timestamp}
_idle_since: dict[str, float] = {}

ACTIVE_MINUTES = 5  # JSONL 文件在此时间内被修改 → 项目正在消耗 token
IDLE_REMOVE_MINUTES = 30  # 闲置超过此时间 → 真正移除面板


def discover_active_projects(minutes: int = ACTIVE_MINUTES) -> dict[str, list[str]]:
    """发现正在消耗 token 的项目（JSONL 文件最近 N 分钟有写入）"""
    cutoff_ts = (datetime.now() - timedelta(minutes=minutes)).timestamp()
    exclude = load_exclude_patterns()
    projects: dict[str, list[str]] = {}

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        if any(pat in project_dir.name for pat in exclude):
            continue

        has_recent = False
        for pattern in ("*.jsonl", "*/subagents/*.jsonl"):
            for f in project_dir.glob(pattern):
                try:
                    if f.stat().st_mtime > cutoff_ts:
                        has_recent = True
                        break
                except OSError:
                    continue
            if has_recent:
                break

        if has_recent:
            name = parse_project_name(project_dir.name)
            if name not in projects:
                projects[name] = []
            projects[name].append(project_dir.name)

    return projects


def get_active_jsonl_files(project_dir: Path) -> list[Path]:
    """找到最近有修改的 JSONL 文件（主会话 + 子代理）"""
    cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
    files = []
    for f in project_dir.glob("*.jsonl"):
        if f.stat().st_mtime > cutoff_ts:
            files.append(f)
    for f in project_dir.glob("*/subagents/*.jsonl"):
        if f.stat().st_mtime > cutoff_ts:
            files.append(f)
    return files


def read_new_lines(filepath: Path) -> list[str]:
    """读取文件自上次读取后新增的行"""
    key = str(filepath)
    try:
        size = filepath.stat().st_size
    except OSError:
        return []

    cursor = _file_cursors.get(key, 0)
    if size <= cursor:
        # 文件没变（或被截断，重置）
        if size < cursor:
            _file_cursors[key] = 0
            cursor = 0
        else:
            return []

    lines = []
    try:
        with open(filepath) as f:
            f.seek(cursor)
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
            _file_cursors[key] = f.tell()
    except OSError:
        pass
    return lines


@app.get("/api/live/projects")
def get_live_projects():
    """返回所有项目列表，供实时监控页面选择"""
    projects = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        projects.append({
            "dir_name": project_dir.name,
            "name": parse_project_name(project_dir.name),
        })
    projects.sort(key=lambda p: p["name"].lower())
    return projects


@app.get("/api/config")
def get_config():
    """返回监控配置"""
    return load_config()


def _collect_events_for_dirs(dir_names: list[str], set_cursor: bool = False) -> list[dict]:
    """从多个目录收集 token 事件（仅最近 24h）"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    events = []
    for dir_name in dir_names:
        project_dir = PROJECTS_DIR / dir_name
        if not project_dir.is_dir():
            continue
        files = get_active_jsonl_files(project_dir)
        for filepath in files:
            session_id = filepath.stem
            is_subagent = "subagents" in str(filepath)
            try:
                with open(filepath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if record.get("type") != "assistant":
                            continue
                        ts = record.get("timestamp")
                        if ts and ts < cutoff:
                            continue
                        msg = record.get("message", {})
                        usage = msg.get("usage", {})
                        events.append({
                            "timestamp": ts,
                            "session_id": session_id,
                            "is_subagent": is_subagent,
                            "model": msg.get("model", "unknown"),
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "cache_read": usage.get("cache_read_input_tokens", 0),
                            "cache_creation": usage.get("cache_creation_input_tokens", 0),
                        })
                    if set_cursor:
                        _file_cursors[str(filepath)] = f.tell()
            except OSError:
                continue
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def _poll_events_for_dirs(dir_names: list[str]) -> list[dict]:
    """轮询多个目录的新增事件"""
    events = []
    for dir_name in dir_names:
        project_dir = PROJECTS_DIR / dir_name
        if not project_dir.is_dir():
            continue
        files = get_active_jsonl_files(project_dir)
        for filepath in files:
            session_id = filepath.stem
            is_subagent = "subagents" in str(filepath)
            for line in read_new_lines(filepath):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                msg = record.get("message", {})
                usage = msg.get("usage", {})
                events.append({
                    "timestamp": record.get("timestamp"),
                    "session_id": session_id,
                    "is_subagent": is_subagent,
                    "model": msg.get("model", "unknown"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def _make_summary(events: list[dict]) -> dict:
    return {
        "total_input": sum(e["input_tokens"] for e in events),
        "total_output": sum(e["output_tokens"] for e in events),
        "total_cache_read": sum(e["cache_read"] for e in events),
        "total_cache_creation": sum(e["cache_creation"] for e in events),
        "message_count": len(events),
    }


# ─── 外部上报 ──────────────────────────────────────────────────────────

# 外部 SDK 应用上报的事件缓冲 {项目名: [事件]}
_external_events: dict[str, list[dict]] = {}
# 外部项目的累计汇总
_external_summaries: dict[str, dict] = {}


@app.post("/api/live/report")
async def live_report(request: Request):
    """接收外部 SDK 应用上报的 token 事件"""
    body = await request.json()
    project = body.get("project")
    event = body.get("event")
    if not project or not event:
        return {"ok": False, "error": "missing project or event"}

    # 补全字段
    event.setdefault("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    event.setdefault("session_id", "sdk")
    event.setdefault("is_subagent", False)
    event.setdefault("model", "unknown")
    event.setdefault("input_tokens", 0)
    event.setdefault("output_tokens", 0)
    event.setdefault("cache_read", 0)
    event.setdefault("cache_creation", 0)

    # 追加到缓冲
    _external_events.setdefault(project, []).append(event)

    # 更新汇总
    if project not in _external_summaries:
        _external_summaries[project] = {
            "total_input": 0, "total_output": 0,
            "total_cache_read": 0, "total_cache_creation": 0,
            "message_count": 0,
        }
    s = _external_summaries[project]
    s["total_input"] += event["input_tokens"]
    s["total_output"] += event["output_tokens"]
    s["total_cache_read"] += event["cache_read"]
    s["total_cache_creation"] += event["cache_creation"]
    s["message_count"] += 1

    return {"ok": True}


@app.get("/api/live/multi/init")
def live_multi_init():
    """初始化 — 只返回正在消耗 token 的项目（含外部上报）"""
    global _known_projects, _idle_since
    _known_projects = discover_active_projects()
    _idle_since = {}
    result = {}
    for name, dir_names in _known_projects.items():
        events = _collect_events_for_dirs(dir_names, set_cursor=True)
        result[name] = {"events": events, "summary": _make_summary(events)}

    # 合入外部上报的项目
    for name, summary in _external_summaries.items():
        if name not in result:
            events = _external_events.get(name, [])
            result[name] = {"events": events, "summary": {**summary}}
            _external_events[name] = []  # init 后清空缓冲

    return result


@app.get("/api/live/multi/poll")
def live_multi_poll():
    """轮询：推送新事件 + 发现新项目 + 闲置/恢复状态管理"""
    global _known_projects
    result = {}

    # 轮询活跃（非闲置）项目的新事件
    for name, dir_names in list(_known_projects.items()):
        if name in _idle_since:
            continue
        events = _poll_events_for_dirs(dir_names)
        if events:
            result[name] = {"events": events}

    # 重新扫描当前活跃项目
    current = discover_active_projects()

    # 闲置项目恢复
    for name in list(_idle_since.keys()):
        if name in current:
            del _idle_since[name]
            _known_projects[name] = current[name]
            events = _poll_events_for_dirs(current[name])
            if name in result:
                result[name].setdefault("events", []).extend(events)
            else:
                result[name] = {"events": events}
            result[name]["resumed"] = True

    # 全新项目上线
    for name, dir_names in current.items():
        if name not in _known_projects:
            _known_projects[name] = dir_names
            events = _collect_events_for_dirs(dir_names, set_cursor=True)
            result[name] = {
                "events": events,
                "summary": _make_summary(events),
                "is_new": True,
            }

    # 新闲置的项目（活跃→闲置）
    now = datetime.now().timestamp()
    newly_idle = []
    for name in _known_projects:
        if name not in current and name not in _idle_since:
            _idle_since[name] = now
            newly_idle.append(name)
    if newly_idle:
        result["_idle"] = newly_idle

    # 长时间闲置 → 真正移除
    remove = []
    for name, since in list(_idle_since.items()):
        if now - since > IDLE_REMOVE_MINUTES * 60:
            remove.append(name)
            del _idle_since[name]
            del _known_projects[name]
    if remove:
        result["_remove"] = remove

    # 外部上报的新事件
    for name, events in list(_external_events.items()):
        if not events:
            continue
        if name in result:
            result[name].setdefault("events", []).extend(events)
        else:
            summary = _external_summaries.get(name, _make_summary(events))
            result[name] = {"events": events, "summary": {**summary}, "is_new": True}
        _external_events[name] = []

    return result



@app.get("/", response_class=HTMLResponse)
def index():
    response = FileResponse(
        Path(__file__).parent / "live.html",
        media_type="text/html",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5588)

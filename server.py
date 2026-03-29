"""Token Monitor - 读取 Claude SDK 本地数据，提供 API + 前端展示"""

import json
import os
import glob
from datetime import datetime, timedelta
from pathlib import Path
from functools import lru_cache
from collections import defaultdict

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Claude Token Monitor")

CLAUDE_DIR = Path.home() / ".claude"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CONFIG_FILE = Path(__file__).parent / "monitor-groups.json"


# ─── 监控组配置 ─────────────────────────────────────────────────────────

def load_groups() -> dict[str, list[str]]:
    """加载项目分组配置"""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_groups(groups: dict[str, list[str]]):
    with open(CONFIG_FILE, "w") as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)


def read_stats_cache():
    """读取 stats-cache.json"""
    if not STATS_CACHE.exists():
        return {}
    with open(STATS_CACHE) as f:
        return json.load(f)


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
    else:
        return name


def scan_sessions(project_dir: Path, days: int = 30) -> list[dict]:
    """扫描项目目录下的 JSONL 会话文件，提取 token 用量"""
    sessions = []
    cutoff = datetime.now() - timedelta(days=days)

    jsonl_files = list(project_dir.glob("*.jsonl"))
    for jsonl_file in jsonl_files:
        session_data = {
            "session_id": jsonl_file.stem,
            "file": str(jsonl_file),
            "messages": 0,
            "models": set(),
            "total_input": 0,
            "total_output": 0,
            "total_cache_read": 0,
            "total_cache_creation": 0,
            "first_ts": None,
            "last_ts": None,
            "cwd": None,
        }

        try:
            with open(jsonl_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = record.get("timestamp")
                    if ts:
                        if session_data["first_ts"] is None:
                            session_data["first_ts"] = ts
                        session_data["last_ts"] = ts

                    if not session_data["cwd"] and record.get("cwd"):
                        session_data["cwd"] = record["cwd"]

                    if record.get("type") == "assistant":
                        msg = record.get("message", {})
                        usage = msg.get("usage", {})
                        model = msg.get("model", "unknown")

                        session_data["messages"] += 1
                        session_data["models"].add(model)
                        session_data["total_input"] += usage.get("input_tokens", 0)
                        session_data["total_output"] += usage.get("output_tokens", 0)
                        session_data["total_cache_read"] += usage.get("cache_read_input_tokens", 0)
                        session_data["total_cache_creation"] += usage.get("cache_creation_input_tokens", 0)

        except (OSError, PermissionError):
            continue

        # 过滤掉太旧的或空的
        if session_data["messages"] == 0:
            continue
        if session_data["first_ts"]:
            try:
                first_dt = datetime.fromisoformat(session_data["first_ts"].replace("Z", "+00:00"))
                if first_dt.replace(tzinfo=None) < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

        session_data["models"] = list(session_data["models"])
        sessions.append(session_data)

    return sessions


def scan_subagent_sessions(project_dir: Path, days: int = 30) -> list[dict]:
    """扫描子代理的 JSONL 文件"""
    sessions = []
    cutoff = datetime.now() - timedelta(days=days)

    for subagent_dir in project_dir.glob("*/subagents"):
        for jsonl_file in subagent_dir.glob("*.jsonl"):
            session_data = {
                "session_id": jsonl_file.stem,
                "file": str(jsonl_file),
                "messages": 0,
                "models": set(),
                "total_input": 0,
                "total_output": 0,
                "total_cache_read": 0,
                "total_cache_creation": 0,
                "first_ts": None,
                "last_ts": None,
                "cwd": None,
                "is_subagent": True,
            }

            try:
                with open(jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        ts = record.get("timestamp")
                        if ts:
                            if session_data["first_ts"] is None:
                                session_data["first_ts"] = ts
                            session_data["last_ts"] = ts

                        if not session_data["cwd"] and record.get("cwd"):
                            session_data["cwd"] = record["cwd"]

                        if record.get("type") == "assistant":
                            msg = record.get("message", {})
                            usage = msg.get("usage", {})
                            model = msg.get("model", "unknown")

                            session_data["messages"] += 1
                            session_data["models"].add(model)
                            session_data["total_input"] += usage.get("input_tokens", 0)
                            session_data["total_output"] += usage.get("output_tokens", 0)
                            session_data["total_cache_read"] += usage.get("cache_read_input_tokens", 0)
                            session_data["total_cache_creation"] += usage.get("cache_creation_input_tokens", 0)

            except (OSError, PermissionError):
                continue

            if session_data["messages"] == 0:
                continue
            if session_data["first_ts"]:
                try:
                    first_dt = datetime.fromisoformat(session_data["first_ts"].replace("Z", "+00:00"))
                    if first_dt.replace(tzinfo=None) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            session_data["models"] = list(session_data["models"])
            sessions.append(session_data)

    return sessions


# ─── API ────────────────────────────────────────────────────────────────


@app.get("/api/overview")
def get_overview():
    """总览：累计 token、模型用量、总会话数"""
    stats = read_stats_cache()
    return {
        "model_usage": stats.get("modelUsage", {}),
        "total_sessions": stats.get("totalSessions", 0),
        "total_messages": stats.get("totalMessages", 0),
        "first_session_date": stats.get("firstSessionDate"),
        "longest_session": stats.get("longestSession"),
    }


@app.get("/api/daily")
def get_daily(days: int = Query(default=30, ge=1, le=365)):
    """每日 token 趋势 + 活跃度"""
    stats = read_stats_cache()
    daily_tokens = stats.get("dailyModelTokens", [])
    daily_activity = stats.get("dailyActivity", [])

    # 合并两个数组
    activity_map = {d["date"]: d for d in daily_activity}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result = []
    for entry in daily_tokens:
        if entry["date"] < cutoff:
            continue
        act = activity_map.get(entry["date"], {})
        result.append({
            "date": entry["date"],
            "tokens_by_model": entry.get("tokensByModel", {}),
            "message_count": act.get("messageCount", 0),
            "session_count": act.get("sessionCount", 0),
            "tool_call_count": act.get("toolCallCount", 0),
        })

    return result


@app.get("/api/projects")
def get_projects(days: int = Query(default=30, ge=1, le=365)):
    """按项目聚合 token 用量"""
    projects = []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        dir_name = project_dir.name
        project_name = parse_project_name(dir_name)
        sessions = scan_sessions(project_dir, days)
        subagent_sessions = scan_subagent_sessions(project_dir, days)

        all_sessions = sessions + subagent_sessions
        if not all_sessions:
            continue

        total_input = sum(s["total_input"] for s in all_sessions)
        total_output = sum(s["total_output"] for s in all_sessions)
        total_cache_read = sum(s["total_cache_read"] for s in all_sessions)
        total_cache_creation = sum(s["total_cache_creation"] for s in all_sessions)
        models = set()
        for s in all_sessions:
            models.update(s["models"])

        # 最近活跃时间
        last_active = max(
            (s["last_ts"] for s in all_sessions if s["last_ts"]),
            default=None,
        )

        projects.append({
            "name": project_name,
            "dir_name": dir_name,
            "session_count": len(sessions),
            "subagent_count": len(subagent_sessions),
            "total_input": total_input,
            "total_output": total_output,
            "total_cache_read": total_cache_read,
            "total_cache_creation": total_cache_creation,
            "total_tokens": total_input + total_output + total_cache_read + total_cache_creation,
            "models": sorted(models),
            "last_active": last_active,
        })

    projects.sort(key=lambda p: p["total_tokens"], reverse=True)
    return projects


@app.get("/api/sessions")
def get_sessions(
    project: str = Query(..., description="项目目录名"),
    days: int = Query(default=30, ge=1, le=365),
):
    """某个项目下的所有会话详情"""
    project_dir = PROJECTS_DIR / project
    if not project_dir.is_dir():
        return []

    sessions = scan_sessions(project_dir, days)
    subagent_sessions = scan_subagent_sessions(project_dir, days)
    all_sessions = sessions + subagent_sessions
    all_sessions.sort(key=lambda s: s.get("last_ts") or "", reverse=True)

    return all_sessions


@app.get("/api/hourly")
def get_hourly():
    """按小时的消息分布"""
    stats = read_stats_cache()
    return stats.get("hourCounts", {})


# ─── 实时监控 ──────────────────────────────────────────────────────────

# 记录每个文件上次读到的位置
_file_cursors: dict[str, int] = {}


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


@app.get("/api/groups")
def get_groups():
    """返回监控组配置"""
    return load_groups()


def _collect_events_for_dirs(dir_names: list[str], set_cursor: bool = False) -> list[dict]:
    """从多个目录收集 token 事件"""
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


@app.get("/api/live/multi/init")
def live_multi_init():
    """初始化所有监控组的实时数据"""
    groups = load_groups()
    result = {}
    for group_name, dir_names in groups.items():
        events = _collect_events_for_dirs(dir_names, set_cursor=True)
        result[group_name] = {
            "events": events,
            "summary": {
                "total_input": sum(e["input_tokens"] for e in events),
                "total_output": sum(e["output_tokens"] for e in events),
                "total_cache_read": sum(e["cache_read"] for e in events),
                "total_cache_creation": sum(e["cache_creation"] for e in events),
                "message_count": len(events),
            },
        }
    return result


@app.get("/api/live/multi/poll")
def live_multi_poll():
    """轮询所有监控组的新增事件"""
    groups = load_groups()
    result = {}
    for group_name, dir_names in groups.items():
        events = _poll_events_for_dirs(dir_names)
        if events:
            result[group_name] = {"events": events}
    return result


@app.get("/api/live/init")
def live_init(project: str = Query(...)):
    """初始化实时监控：读取该项目最近 24h 的逐条消息"""
    project_dir = PROJECTS_DIR / project
    if not project_dir.is_dir():
        return {"events": [], "summary": {}}

    files = get_active_jsonl_files(project_dir)
    events = []

    for filepath in files:
        # 初始化时读全文件，设置游标到末尾
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
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    ts = record.get("timestamp")
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
                _file_cursors[str(filepath)] = f.tell()
        except OSError:
            continue

    events.sort(key=lambda e: e.get("timestamp") or "")

    # 汇总
    summary = {
        "total_input": sum(e["input_tokens"] for e in events),
        "total_output": sum(e["output_tokens"] for e in events),
        "total_cache_read": sum(e["cache_read"] for e in events),
        "total_cache_creation": sum(e["cache_creation"] for e in events),
        "message_count": len(events),
    }

    return {"events": events, "summary": summary}


@app.get("/api/live/poll")
def live_poll(project: str = Query(...)):
    """轮询新增的 token 事件"""
    project_dir = PROJECTS_DIR / project
    if not project_dir.is_dir():
        return {"events": []}

    files = get_active_jsonl_files(project_dir)
    events = []

    for filepath in files:
        session_id = filepath.stem
        is_subagent = "subagents" in str(filepath)
        new_lines = read_new_lines(filepath)

        for line in new_lines:
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
    return {"events": events}


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(
        Path(__file__).parent / "index.html",
        media_type="text/html",
    )


@app.get("/live", response_class=HTMLResponse)
def live_page():
    return FileResponse(
        Path(__file__).parent / "live.html",
        media_type="text/html",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5588)

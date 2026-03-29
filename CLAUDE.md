# Claude Token Monitor

本地 Claude SDK token 消耗可视化面板。只读 `~/.claude/` 下的数据文件，零侵入。

## 技术栈

- Python 3.12 + FastAPI + uvicorn
- 前端: 单页 HTML + Chart.js (CDN) + Canvas 粒子动画
- 数据源: `~/.claude/stats-cache.json` + `~/.claude/projects/**/*.jsonl`

## 命令

```bash
# 启动
uv run --with fastapi --with uvicorn server.py

# 访问
open http://127.0.0.1:5588       # 总览面板
open http://127.0.0.1:5588/live  # 实时粒子宇宙
```

## 结构

```
server.py            # API 服务 + JSONL 解析 + 实时轮询
index.html           # 总览面板（每日趋势、模型占比、项目排行）
live.html            # 实时粒子宇宙（多项目同屏、点击展开详情）
monitor-groups.json  # 项目分组配置
```

## 项目分组

编辑 `monitor-groups.json` 配置监控组，将多个 Claude 数据目录合并为一个逻辑项目。

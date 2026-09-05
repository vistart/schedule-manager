# schedule-manager

面向 LLM 工具调用的日程管理系统，提供 MCP Server 和 CLI 两种接口。

## 技术栈

- **Python 3.12+**
- **rhosocial-activerecord** — ActiveRecord 模式 ORM
- **rhosocial-activerecord-postgres** — PostgreSQL 异步后端（psycopg3）
- **MCP SDK v2** — FastMCP，提供标准 MCP 工具接口
- **python-dateutil** — RFC 5545 RRULE 解析
- **Pydantic** — 模型校验

## 安装

```bash
git clone https://github.com/vistart/schedule-manager.git
cd schedule-manager
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 配置

复制 `.env.example` 为 `.env`，填入数据库连接信息：

```bash
cp .env.example .env
```

`.env` 示例：

```
SCHEDULE_DB_HOST=192.168.1.3
SCHEDULE_DB_PORT=17689
SCHEDULE_DB_NAME=schedule_manager_db
SCHEDULE_DB_USER=root
SCHEDULE_DB_PASSWORD=your_password
```

初始化数据库表：

```bash
schedule-manager-setup-db
```

## 数据模型

`schedules` 表结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER (PK) | 自增主键 |
| `title` | TEXT | 日程标题（必填，非空） |
| `description` | TEXT | 详细描述 |
| `status` | TEXT | 状态：`pending` / `in_progress` / `completed` / `cancelled` |
| `priority` | INTEGER | 优先级 1（最高）~ 5（最低），默认 3 |
| `start_time` | TIMESTAMP | 开始时间 |
| `due_time` | TIMESTAMP | 截止时间 |
| `completed_at` | TIMESTAMP | 完成时间 |
| `location` | TEXT | 地点 |
| `tags` | JSONB | 标签列表 |
| `rrule` | TEXT | RFC 5545 循环规则 |
| `rdate` | JSONB | 循环例外日期 |
| `exdate` | JSONB | 排除日期 |
| `created_at` | TIMESTAMP | 创建时间（自动） |
| `updated_at` | TIMESTAMP | 更新时间（自动） |
| `deleted_at` | TIMESTAMP | 软删除时间（NULL = 未删除） |

## CLI 使用

### 输出格式

默认 JSON 信封格式：

```json
{"status": "ok", "data": {...}}
{"status": "error", "error": {"code": "NOT_FOUND", "message": "..."}}
```

加 `--human` 输出人类可读文本。

### 全局选项

| 选项 | 说明 |
|------|------|
| `--human` | 输出人类可读文本 |
| `--describe` | 输出机器可读的 JSON Schema 并退出 |
| `--help` | 显示帮助 |

### 命令

#### `create` — 创建日程

```bash
schedule-manager create --title '团队站会' --priority 3
schedule-manager create --title '部署 v2' --due-time '2026-09-10T14:00:00' --tags 'deploy,critical'
schedule-manager create --title '每日晨会' --rrule 'FREQ=DAILY' --start-time '2026-09-06T09:00:00'
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `--title` | string | 是 | 标题（非空） |
| `--description` / `-d` | string | 否 | 描述 |
| `--status` | string | 否 | 状态（默认 pending） |
| `--priority` | int | 否 | 优先级 1-5（默认 3） |
| `--start-time` | string | 否 | ISO 8601 开始时间 |
| `--due-time` | string | 否 | ISO 8601 截止时间 |
| `--location` | string | 否 | 地点 |
| `--tags` | string | 否 | 逗号分隔的标签 |
| `--rrule` | string | 否 | RFC 5545 循环规则 |
| `--dry-run` | flag | 否 | 预览，不真正创建 |

#### `get` — 查询日程

```bash
schedule-manager get --id 1
```

#### `update` — 更新日程（只修改提供的字段）

```bash
schedule-manager update --id 1 --status completed
schedule-manager update --id 1 --title '新标题' --priority 1
```

#### `delete` — 软删除日程

```bash
schedule-manager delete --id 1
```

#### `complete` — 标记完成

```bash
schedule-manager complete --id 1
```

#### `list` — 分页列表

```bash
schedule-manager list
schedule-manager list --status pending --sort-by priority --sort-order asc
schedule-manager list --page 2 --page-size 10
schedule-manager list --keyword 会议
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--page` | 1 | 页码 |
| `--page-size` | 20 | 每页条数（1-100） |
| `--status` | - | 按状态筛选 |
| `--priority` | - | 按优先级筛选 |
| `--keyword` | - | 关键词搜索（匹配标题和描述） |
| `--sort-by` | due_time | 排序字段：due_time / created_at / updated_at / priority / title |
| `--sort-order` | asc | 排序方向：asc / desc |

#### `search` — 关键词搜索

```bash
schedule-manager search --keyword deadline
```

### 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 参数错误 |
| 20 | 资源不存在 |
| 40 | 校验错误 |

## MCP Server

通过 `mcp` CLI 启动：

```bash
mcp run schedule_manager.mcp_server
```

或在 MCP 客户端配置中添加：

```json
{
  "mcpServers": {
    "schedule-manager": {
      "command": "schedule-manager-mcp",
      "args": []
    }
  }
}
```

### 工具列表

| 工具 | 说明 |
|------|------|
| `create_schedule` | 创建日程 |
| `get_schedule` | 按 ID 查询 |
| `update_schedule` | 更新日程（只改传入的字段） |
| `delete_schedule` | 软删除 |
| `list_schedules` | 分页列表（支持筛选、排序） |
| `complete_schedule` | 标记完成 |
| `search_schedules` | 关键词搜索 |

所有工具参数均为命名参数，返回 JSON 字典。

## 开发

```bash
pip install -e ".[test,dev]"
```

### 测试

```bash
pytest tests/ -v
```

需要 PostgreSQL 数据库。测试前确保 `.env` 配置正确并已执行 `schedule-manager-setup-db`。

### 项目结构

```
src/schedule_manager/
├── __init__.py
├── config.py       # 数据库配置（从 .env 加载）
├── model.py        # Schedule ActiveRecord 模型
├── mcp_server.py   # FastMCP Server，7 个工具
├── cli.py          # CLI 入口，LLM 优化设计
└── setup_db.py     # 数据库表初始化脚本
```

## 许可证

MIT

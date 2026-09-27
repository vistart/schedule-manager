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

> 若要用本地编辑模式装 `rhosocial-activerecord` / `rhosocial-activerecord-postgres`、
> 单独配 pip 源，或接入 opencode，见 [接入 opencode](#接入-opencode) 的完整步骤。

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

启动（stdio）：

```bash
schedule-manager-mcp
```

安装后提供的 `schedule-manager-mcp` 控制台入口会读取 `SCHEDULE_DB_*` 环境变量
（缺省回退到项目根目录的 `.env`），并保证 `schedules` 表存在。

### 接入 opencode

opencode 通过原生 MCP 支持接入，配置放在项目根目录的 `opencode.json`。

**步骤 1：装好虚拟环境与数据库**

```bash
cd /path/to/schedule-manager

# 虚拟环境。--without-pip + --system-site-packages 与本项目现有环境一致，
# pip 来自 ~/.local 的用户级 site-packages，因此没有 .venv*/bin/pip，
# 一律用 `python -m pip` 调用。
python3 -m venv --without-pip --system-site-packages .venv3.14-ubuntu26.04

# 可选：给该虚拟环境单独配 pip 源（放在 venv 根目录，pip 会按 site 级自动读取，
# 不影响其它项目和全局配置）
cat > .venv3.14-ubuntu26.04/pip.conf <<'EOF'
[global]
index-url = https://mirrors.aliyun.com/pypi/simple/
trusted-host = mirrors.aliyun.com
timeout = 120
EOF

# 本地编辑模式安装两个 ORM 依赖（改动即时生效，无需重装）
.venv3.14-ubuntu26.04/bin/python -m pip install -e /path/to/rhosocial/python-activerecord
.venv3.14-ubuntu26.04/bin/python -m pip install -e /path/to/rhosocial/python-activerecord-postgres

# 安装本项目，注册 schedule-manager / schedule-manager-setup-db / schedule-manager-mcp
.venv3.14-ubuntu26.04/bin/python -m pip install -e ".[test]"
```

确认依赖确实指向本地源码树：

```bash
.venv3.14-ubuntu26.04/bin/python -m pip show rhosocial-activerecord | rg "Editable"
```

按 `.env.example` 建好 `.env` 并填入真实连接参数，然后建库建表
（数据库本身不存在时需先 `CREATE DATABASE`）：

```bash
.venv3.14-ubuntu26.04/bin/schedule-manager-setup-db
```

**步骤 2：写 `opencode.json`**

连接参数一律不落在仓库里，五个变量全部用 `{env:VAR}` 从启动 opencode 的 shell 读取：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "schedule-manager": {
      "type": "local",
      "command": [".venv3.14-ubuntu26.04/bin/schedule-manager-mcp"],
      "cwd": ".",
      "enabled": true,
      "timeout": 120000,
      "environment": {
        "SCHEDULE_DB_HOST": "{env:SCHEDULE_DB_HOST}",
        "SCHEDULE_DB_PORT": "{env:SCHEDULE_DB_PORT}",
        "SCHEDULE_DB_NAME": "{env:SCHEDULE_DB_NAME}",
        "SCHEDULE_DB_USER": "{env:SCHEDULE_DB_USER}",
        "SCHEDULE_DB_PASSWORD": "{env:SCHEDULE_DB_PASSWORD}"
      }
    }
  }
}
```

- `command` 用**相对路径**配合 `cwd`（相对路径按 workspace 解析），这样配置本身不含
  本机绝对路径，可以直接提交共享。若把 `opencode.json` 放到别处或用全局配置，
  改成绝对路径并去掉 `cwd`。
- `timeout` 必调，原因见下方「两个容易踩的点」。

**步骤 3：把 `.env` 导出到 shell**

`.env` 本身已被 `.gitignore` 排除，唯一的连接信息来源。用 `set -a` 一次性导出其中
所有变量，让 `{env:VAR}` 有值可取：

```bash
set -a; . ./.env; set +a
```

**步骤 4：验证连通性**

```bash
opencode mcp list
```

期望输出 `✓ schedule-manager connected`。若显示 `failed`，见下方排错。

**步骤 5：重启 opencode**

config 只在启动时加载，**必须退出并重启 opencode** 才生效；当前会话仍使用旧配置。

之后即可用自然语言控制日程，例如「把下周一上午十点的团队站会建成日程，优先级 2」。

#### 两个容易踩的点

- **`timeout` 必须调大**。schema 里 `McpLocalConfig.timeout` 默认只有 5000ms，而本服务
  导入依赖较重、冷启动约 15s，不改会直接超时。
- **`.env` 不再兜底**。`load_dotenv(override=False)` 不会覆盖已存在的环境变量，所以
  `environment` 里声明过的键，即使取到空值也不会再回退 `.env`；此时会抛出
  `DatabaseError` 并指明缺失的变量名。这就是步骤 3 必须先执行的原因。

#### 排错

| 现象 | 原因与处理 |
|------|-----------|
| `MCP error -32000: Connection closed` | `environment` 里的变量取到空值，多半是漏了步骤 3。用 `opencode mcp list --print-logs` 看 stderr，或直接运行 `schedule-manager-mcp` 复现，会打印出缺失的变量名 |
| `File not found: .../schedule_manager.mcp_server` | 用了 `mcp run <点号模块>`。mcp 2.x 的 `mcp run` 只接受文件路径，请改用 `schedule-manager-mcp` 控制台入口 |
| 请求超时 | `timeout` 太小。冷启动约 15s，建议 ≥ 60000 |
| `connection refused` | 端口不对或数据库不可达。`psql -h "$SCHEDULE_DB_HOST" -p "$SCHEDULE_DB_PORT" -U "$SCHEDULE_DB_USER" -l` 先确认连通 |

#### 切换数据库

改 `.env` 即可（每个 opencode 配置对应一个库，会话内固定）。需要按会话切库就得改成
"命名 profile + 工具参数选库"的方案（当前未实现）。如果不想在 `opencode.json` 里
出现 `environment` 块，把整个块删掉即可，服务会直接读 `.env`。

### 接入其它 MCP 客户端

```json
{
  "mcpServers": {
    "schedule-manager": {
      "command": "/abs/path/to/schedule-manager/.venv3.14-ubuntu26.04/bin/schedule-manager-mcp",
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
├── config.py       # 数据库配置（环境变量优先，回退 .env）
├── model.py        # Schedule ActiveRecord 模型
├── schema.py       # schedules 表 DDL（消费 DDLSource 声明）
├── mcp_server.py   # MCP Server（MCPServer），7 个工具
├── cli.py          # CLI 入口，LLM 优化设计
└── setup_db.py     # 数据库表初始化脚本
```

## 许可证

MIT

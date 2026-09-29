# schedule-manager

面向 LLM 工具调用的多用户日程管理系统，提供远程 MCP Server（Streamable HTTP）和 CLI 两种接口。
**每个请求携带 Bearer 令牌，所有查询自动限定在该令牌对应的用户范围内。**

## 技术栈

- **Python 3.12+**
- **rhosocial-activerecord** — ActiveRecord 模式 ORM
- **rhosocial-activerecord-postgres** — PostgreSQL 异步后端（psycopg3）
- **MCP SDK v2**（`MCPServer`）— Streamable HTTP 传输 + OAuth 2.1 资源服务器
- **python-dateutil** — RFC 5545 RRULE 解析
- **Pydantic** — 模型校验

## 核心设计：身份如何隔离

```
HTTP 请求
  BearerAuthBackend → TokenTableVerifier → User.resolve_token()   ← 哈希查库 + 校验，每次请求一次
      → 写 auth_context_var（SDK 挂载的 AuthContextMiddleware）
  → 工具执行
      → Schedule.query() → current_user_id() → WHERE user_id = <已验证>   ← 零解析
```

三条强制点，覆盖全部读写路径：

| 路径 | 机制 |
|---|---|
| 读 | `UserScopedQuery.__init__` 在**构造期**把身份挂进 `where_clause` |
| 写 | `UserOwnedMixin.prepare_save_data` 覆写 `user_id`（INSERT 与 UPDATE 都覆写） |
| 身份来源 | `identity.current_user_id()`，全项目唯一出口 |

**为什么接缝选在 `__init__` 而不是各个终结方法**：`Model.query()` 就是
`return cls.__query_class__(cls)`（`base/query_mixin.py:135`），谓词在构造期
就位，之后 `all` / `one` / `count` / `exists` / `aggregate` / `sum_` /
`update_all` / `delete_all` 全部自动继承。
若改为逐个覆写终结方法则需要 5~6 处，因为执行路径并不统一——
`count` 与数值聚合走 `await self.aggregate()`（`aggregate.py:450`），
而 `all()` 走 `self.to_sql()`。漏掉任一处**不报错**，只会静默返回别人的数据。

**跨用户访问表现为 `NOT_FOUND` 而非 `FORBIDDEN`**，避免通过 id 探测他人数据是否存在。

## 安装

```bash
git clone https://github.com/vistart/schedule-manager.git
cd schedule-manager
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

> 本地编辑模式安装 ORM 依赖（改动即时生效）、接入 opencode 的完整步骤见
> [接入 opencode](#接入-opencode)。

## 配置

```bash
cp .env.example .env
```

| 变量 | 说明 |
|---|---|
| `SCHEDULE_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` | 数据库连接 |
| `SCHEDULE_TOKEN` | 本地调用者的 Bearer 令牌（明文仅此一份，库里只存哈希） |
| `SCHEDULE_TOKEN_FILE` | 令牌文件路径，**优先于** `SCHEDULE_TOKEN`；可 `chmod 600` |
| `SCHEDULE_BIND_HOST` / `_BIND_PORT` | 远程服务监听地址，默认 `127.0.0.1:8000` |
| `SCHEDULE_PUBLIC_URL` | 对外可达 URL，即 OAuth 资源标识；非本地必须 HTTPS |
| `SCHEDULE_ALLOWED_HOSTS` / `_ALLOWED_ORIGINS` | DNS rebinding 防护白名单 |

`SCHEDULE_TOKEN_FILE` 更安全：导出到环境变量的值在 `/proc/<pid>/environ` 里可见，
文件可以只让本人读。

### 初始化与开户

```bash
schedule-manager-setup-db      # 建表 + 建索引
schedule-manager user open --username alice --label laptop
```

`user open` 会打印一次明文令牌。之后库里只保留 SHA-256 摘要，**无法恢复**。

> 所有语句都是 `IF NOT EXISTS`。**本项目不做数据库迁移** —— DDL 即 schema，
> 携带旧结构的库不会被就地升级。

## 数据模型

| 表 | 关键列 | 说明 |
|---|---|---|
| `users` | `username` 唯一、`is_active` | 账户。销户是停用不是删除，历史归属必须可追溯 |
| `api_tokens` | `token_hash` 唯一、`revoked_at`、`expires_at` | `user_id` → `users` `ON DELETE RESTRICT` |
| `api_token_scopes` | 复合主键 `(api_token_id, scope)` | 一行一个 scope；`ON DELETE CASCADE`（子集合无独立含义） |
| `schedules` | `user_id` | → `users` `ON DELETE RESTRICT`，其余字段同旧版 |

`ON DELETE` 语义按关系性质区分：`schedules` / `api_tokens` 指向 `users` 用
`RESTRICT`（承载独立数据，不能连带清空）；`api_token_scopes` 指向 `api_tokens`
用 `CASCADE`（子集合，令牌没了就是垃圾）。

**scope 是封闭词表**：`schedules:read`、`schedules:write`。SDK 用
`AuthSettings.required_scopes` 生成 PRM 的 `scopes_supported`，库里出现词表外的
scope 会让两者漂移。无 scope 行的令牌 = 任何工具都不可用（fail-closed）。

## 令牌设计

```
生成:  sm_<32 bytes urlsafe>          仅签发时出现一次
存库:  sha256(token).hexdigest()       CHAR(64) UNIQUE
```

- **存哈希不存明文**。这是公共服务，库被读就等于全部凭据泄露。
- **用 SHA-256 而非 bcrypt/argon2**：令牌是 256 位高熵随机串，不是低熵口令，
  没有抗暴力破解需求，无需故意拖慢。查找走唯一索引，连常量时间比较都不需要。
- **不透明而非 JWT**：天然没有 `aud` 混淆（为别的服务签发的令牌调不了本服务），
  代价是无法自包含身份，换来的是无需外部 IdP、吊销即时生效。

## MCP Server

### 启动

```bash
schedule-manager-mcp
```

监听 `SCHEDULE_BIND_HOST:SCHEDULE_BIND_PORT`，MCP 端点在 `/mcp`。

**DNS rebinding 防护只在绑定地址是 loopback 时自动开启**
（`mcpserver/server.py:1144-1150`）。部署到真实域名上必须显式设置
`SCHEDULE_ALLOWED_HOSTS` / `SCHEDULE_ALLOWED_ORIGINS`，否则端点处于无保护状态。

### 认证与授权

| 情况 | 响应 |
|---|---|
| 无令牌 / 令牌不存在 | `401` + `WWW-Authenticate`（含 `resource_metadata`） |
| 令牌已吊销 / 过期 | `401` |
| 用户已停用 | `401` |
| scope 不足 | `403` + `WWW-Authenticate: error="insufficient_scope", scope="<缺失的>"` |
| 访问他人日程 | `NOT_FOUND`（工具返回的错误字典） |

scope 按**单个工具**判定，而不是全局：

| 工具 | 所需 scope |
|---|---|
| `whoami` | 无 |
| `get_schedule` / `list_schedules` / `search_schedules` | `schedules:read` |
| `create_schedule` / `update_schedule` / `delete_schedule` / `complete_schedule` | `schedules:write` |

只读令牌不能写，但能读；`whoami` 不需要任何 scope，所以零授权的令牌也知道
自己是谁。

### 工具列表

| 工具 | 说明 |
|---|---|
| `whoami` | 当前用户 id / username / 令牌的 scopes / 过期时间 |
| `create_schedule` | 创建日程 |
| `get_schedule` | 按 ID 查询 |
| `update_schedule` | 更新（只改传入的字段） |
| `delete_schedule` | 软删除 |
| `list_schedules` | 分页列表（筛选、排序） |
| `complete_schedule` | 标记完成 |
| `search_schedules` | 关键词搜索 |

所有工具参数均为命名参数，返回 JSON 字典。

**没有任何工具可以跨用户枚举。** `User` / `ApiToken` 出于管理需要不是用户隔离
模型，把它们做成工具等于开放全量用户转储。

### 接入 opencode

配置写在**全局** `~/.config/opencode/opencode.json`（不在仓库里）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "schedule-manager": {
      "type": "remote",
      "url": "http://127.0.0.1:18000/mcp",
      "enabled": true,
      "oauth": false,
      "timeout": 60000,
      "headers": { "Authorization": "Bearer sm_xxx" }
    }
  }
}
```

- `oauth: false` 关闭 opencode 的自动 OAuth 探测——服务端用的是静态令牌。
- `url` 里的 host:port 必须和容器启动时的 `SCHEDULE_PUBLIC_URL` 一致，否则 Host 头
  对不上，被 DNS-rebinding 防护挡成 421。
- **不要把令牌放进 `command` 的 args**，那会出现在 `ps aux` 里。

放全局而不是项目 `opencode.json` 有两个原因。**一是覆盖面**：opencode 的配置优先级是
「远程 < 全局 < `OPENCODE_CONFIG` < 项目 < `.opencode/` < `OPENCODE_CONFIG_CONTENT`」，
项目里的 `opencode.json` 会压过全局——同一个 server 名两处都写，在项目目录里生效的永远
是项目那份，全局那条等于白写。**二是密钥**：项目配置是要入库的，而全局配置不是。

所以令牌可以直接写在全局配置里，不必依赖环境变量。代价是这个文件从此是一份明文凭据，
安全性取决于该文件的权限——它等价于把令牌放在一个 `0600` 的文件里，而不是放在
`SCHEDULE_TOKEN` 环境变量里（后者任何能读 `/proc/<pid>/environ` 的进程都能看到）。
想让它离开明文，opencode 提供了 `{file:...}`，语义等同 `SCHEDULE_TOKEN_FILE`：

```json
"headers": { "Authorization": "Bearer {file:~/.config/schedule-manager/token}" }
```

**如果这份配置要入库，就必须用 `{env:SCHEDULE_TOKEN}` 而不是字面量。** 写死
`"Bearer sm_alice"`，同事 clone 仓库后他的 opencode 就成了 alice——那个形式本身就是防
"入库配置冒充身份"的手段。本仓库没有项目级 `opencode.json`，正是因为这个取舍。

验证：

```bash
opencode mcp list                   # 期望 schedule-manager connected
opencode mcp debug schedule-manager # 连不上时看 HTTP 与鉴权细节
```

config 只在启动时加载，改完**必须重启 opencode**。

#### 临时改指向（不改任何文件）

`OPENCODE_CONFIG_CONTENT` 的优先级高于所有配置文件，可以在启动时覆盖。注意两点：
覆盖条目**必须写全**——配置源之间是逐键合并的，但**每个配置源独立校验**，只写
`headers` 会被拒绝（`Missing key mcp.schedule-manager.enabled`），不会替你从别处补齐；
`timeout` 之类没冲突的键则会从下层配置继承。

```bash
export OPENCODE_CONFIG_CONTENT='{"mcp":{"schedule-manager":{
  "type":"remote","url":"http://127.0.0.1:8000/mcp","enabled":true,
  "oauth":false,"timeout":60000,
  "headers":{"Authorization":"Bearer {env:SCHEDULE_TOKEN}"}}}}'
opencode
```

### 健康检查

`GET /healthz` 无需令牌，返回 `200 {"status":"ok"}` 或 `503 {"status":"unavailable"}`。
它会真的 `SELECT 1` 探一次数据库——只报告"进程活着"的探针，会把一个连不上库
的容器报成健康。lifespan 还没建好连接池时（`503`）也是正常状态。

另一个无需令牌的端点是 `/.well-known/oauth-protected-resource`（OAuth 资源元数据），
但它不碰数据库，不能当健康检查用。

### 容器部署

镜像用多阶段构建：builder 里 `pip wheel` 产出全部 wheel，运行阶段
`--no-index` 离线安装，因此被测过的那份依赖集合就是发布的那份。进程以非 root
运行，`CMD` 用 exec 形式，SIGTERM 才能到 uvicorn 并触发 lifespan 里的
`close_pool`。

```bash
bash docker/build-wheels.sh       # 把两个 ORM wheel 放进 wheels/（首次必做）
docker compose up -d --build
docker compose run --rm app schedule-manager user open --username alice
```

`build-wheels.sh` 存在的原因：PyPI 上的 `rhosocial-activerecord` 是 **dev29**、
`rhosocial-activerecord-postgres` 是 **dev16**，而 release 分支已经是 **dev30** /
**dev17**，本地 venv 装的就是这两个分支。直接 `pip install .` 的话镜像跑的是
落后一版的 ORM。脚本会先把源码拷到 `/tmp` 再构建——两个仓库在 Windows 盘挂载上，
setuptools 扫包要几分钟，在原生文件系统上是几秒。

依赖下载走阿里云镜像（`ARG PIP_INDEX_URL`，可用 `--build-arg` 覆盖）。注意改任何
一行源码都会让这一层失效、下次构建重下全部依赖；把依赖层拆出来能解决，代价是一个
需要 `|| true` 的空包桩，可能静默产出两个同版本 wheel，所以没做。

镜像里已经写死了三件事：

| ENV | 值 | 原因 |
|---|---|---|
| `SCHEDULE_BIND_HOST` | `0.0.0.0` | 默认是 `127.0.0.1`，容器内等于外部不可达 |
| `SCHEDULE_AUTO_MIGRATE` | `0` | 多副本同时启动会抢 DDL 锁，schema 交给部署步骤 |
| `HEALTHCHECK` | 打 `/healthz` | `python:3.12-slim` 里没有 curl |

必须**运行时**传入的：

| ENV | 说明 |
|---|---|
| `SCHEDULE_DB_*` | `.env` 不会被打进镜像（里面有密码和 `SCHEDULE_TOKEN`） |
| `SCHEDULE_PUBLIC_URL` | 客户端实际访问的 HTTPS 地址，也是令牌绑定的 resource id |
| `SCHEDULE_ALLOWED_HOSTS` / `_ORIGINS` | 默认允许列表由 `PUBLIC_URL` 推导，反代改写 Host 时要显式给 |

连接数 = **副本数 × workers × pool_max**，总量控制在 32 以内；调 worker 前先看
这个乘积。

生产部署的差别只有两处：前面加一层终止 TLS 的反代（uvicorn 这边没配证书），以及
把 `ports` 换成内网地址或直接删掉——令牌在 header 里，明文过一次网络就等于泄漏。

## CLI

```bash
export SCHEDULE_TOKEN=sm_xxx
schedule-manager whoami
schedule-manager create --title '团队站会' --priority 3
schedule-manager list --status pending --sort-by priority
schedule-manager search --keyword deadline
```

CLI 与远程 MCP 接受**同一种凭据**，所以一个令牌两边都能用。

### 输出格式

默认 JSON 信封，`--human` 输出人类可读文本，`--describe` 输出机器可读 schema。

```json
{"status": "ok", "data": {...}}
{"status": "error", "error": {"code": "NOT_FOUND", "message": "..."}}
```

### 全局选项

| 选项 | 说明 |
|---|---|
| `--token` | Bearer 令牌，回落到 `SCHEDULE_TOKEN` → `SCHEDULE_TOKEN_FILE` |
| `--human` | 人类可读文本 |
| `--describe` | JSON Schema 并退出 |
| `--help` | 帮助 |

### 业务命令

`whoami` / `create` / `get` / `update` / `delete` / `complete` / `list` / `search`

参数同旧版，另加 `--dry-run`（`create` / `update` / `delete` / `complete`）。

### 管理命令

不暴露为 MCP 工具，只走 CLI。

| 命令 | 说明 |
|---|---|
| `user open --username X [--scope ...] [--label ...]` | 开户并签发首个令牌（明文打印一次） |
| `user close --id N` | 停用账户并吊销其全部令牌；日程保留但不可达 |
| `token issue --id N [--scope ...] [--label ...]` | 追加令牌 |
| `token revoke --token-id N` | 吊销令牌 |
| `token list [--id N]` | 列出令牌，只显示摘要前 8 位，**永不显示令牌本身** |

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般错误（含配置问题） |
| 2 | 参数错误 |
| 20 | 资源不存在（**含他人的日程**） |
| 40 | 校验错误 |
| 41 | 未认证（缺令牌 / 不存在 / 已吊销 / 已过期 / 账户已停用） |
| 42 | 禁止 |

## 开发

```bash
pip install -e ".[test,dev]"
```

### 测试

```bash
pytest tests/ -v
```

| 文件 | 需要数据库 | 覆盖 |
|---|---|---|
| `test_offline.py` | 否 | 身份上下文、生成 SQL、DDL、校验 |
| `test_live.py` | 是 | 账户、凭据、CRUD、查询、隔离矩阵 |
| `test_cli.py` | 是 | 真实子进程：信封、退出码、跨用户拒绝 |

数据库不可达时后两者**跳过**而非失败，`test_offline.py` 任何环境都能跑。

隔离契约优先用生成 SQL 断言（无需连库，直接检查契约而非观察结果）：

```python
with as_user(7):
    assert '"user_id" = %s' in Schedule.query().to_sql()[0]
```

跨用户用例必须断言两半：调用失败**且**目标行未被改动。

`test_cli.py` 较慢（每例约 14s），因为每次子进程调用都要付冷启动导入的代价。

### 项目结构

```
src/schedule_manager/
├── config.py       # DB / 服务端 / 令牌配置
├── db.py           # 共享 backend 装配（见下）
├── errors.py       # 三个领域异常
├── identity.py     # current_user_id()，身份唯一出口
├── auth.py         # SDK TokenVerifier 薄适配器
├── models/         # base.py / user.py / token.py / schedule.py
├── schema/         # _column.py / users.py / tokens.py / schedules.py
├── mcp_server.py   # Streamable HTTP
├── cli.py          # CLI 入口
└── setup_db.py     # 建表

Dockerfile         # 多阶段构建，运行阶段离线装 wheel
compose.yaml       # postgres + 一次性建表任务 + 服务
docker/
└── build-wheels.sh # 把本地 ORM release 分支打成 wheels/ 里的 wheel
.dockerignore      # 关键是把 .env 挡在构建上下文外
wheels/            # build-wheels.sh 的产物，被 .gitignore 忽略
```

### 两个容易踩的坑

**`Model.configure()` 是按类生效的。** 每调用一次就新建一个 backend 实例并
独立建连接。配置四个模型 = 四条连接，于是通过某个模型开启的事务**覆盖不到**
通过另一个模型发出的语句——它们在不同会话上，互相看不见未提交的数据。
一律走 `db.connect()`：它配置一个类，再把 backend 分发给其余模型。

**嵌套事务会提前提交。** Postgres backend 只有单连接，事务状态是实例级的，
内层 `backend.transaction()` 会把外层的工作先提交掉，外层再提交一次。
`models/user.py` 里的 `transaction()` 辅助函数在已有事务时直接复用，而不是新开。

## 许可证

MIT

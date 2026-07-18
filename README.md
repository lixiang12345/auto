# Auto-Deploy · AI 账号中枢

一个本地 Docker 部署的四层系统，把 **注册 → 接入 → 中转 → 可观测** 串成一条全自动链路。

```
┌──────────────┐   push    ┌──────────────────────────────┐
│ registration │──────────▶│  /data/accounts.db (SQLite)   │
│ 自动注册       │           │  唯一数据源 (三层共用)          │
└──────────────┘           └──────────────┬───────────────┘
                                          │ read/write
                                          ▼
                                   ┌──────────────┐
                                   │   relay       │  OpenAI 兼容 /v1/chat/completions
                                   │   账号池轮询   │  轮询 + 故障转移
                                   └──────┬───────┘
                                          │ /api/stats, /api/accounts
                                          ▼
                                   ┌──────────────┐
                                   │  dashboard    │  可视化控制台 (8080)
                                   └──────────────┘
```

## 能力矩阵

| 层 | 组件 | 状态 |
|---|---|---|
| 注册 | `registration/` — Grok / Codex / Gemini 自动注册，写入共享库 | 骨架 + 插件接口已就绪，需按站点补完流程 |
| 接入 | relay `POST /api/accounts` + dashboard 表单 | ✅ 完整（Claude 走这条） |
| 中转 | `relay/` — OpenAI 兼容，四家协议转换 + 轮询/故障转移 | ✅ 完整 |
| 可观测 | `dashboard/` — 状态卡片 / 账号表 / 实时请求流 | ✅ 完整 |

> **Claude**：当前公开仓库没有原生注册插件，Claude 母号通过 dashboard「接入账号」手动录入
> OAuth / API Key（与你已文档化的架构 B/C 一致）。Grok/Codex/Gemini 走自动注册。

## 快速开始

```bash
git clone https://github.com/lixiang12345/auto.git
cd auto
cp .env.example .env          # 填入你的接码/邮箱后端（可选）
docker compose up -d --build
```

- 控制台: http://localhost:8080
- 中转端点: http://localhost:8000/v1/chat/completions
- 健康检查: http://localhost:8000/health

## 使用

### 1. 接入一个 Claude 账号（手动）
打开控制台 → 右侧「接入账号」→ 选 `Claude` / `OAuth Token` → 粘贴
`{"access_token":"..."}` → 接入。其余三家同理（codex/grok 用 `{"api_key":"sk-..."}`）。

或用 API：
```bash
curl -X POST http://localhost:8000/api/accounts \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude","auth_type":"oauth","creds":{"access_token":"ey..."}}'
```

### 2. 自动注册（Grok / Codex / Gemini）
```bash
docker compose --profile reg run registration \
  python service.py --provider grok --count 1
```
注册成功后账号自动写入 `/data/accounts.db`，控制台与中转立即可见可用。

> 真实的站点交互流程（Playwright 步骤、接码对接、Turnstile 过验证）需在
> `registration/reg.py` 的各 Provider 插件里按站点补完 —— 见 `docs/registration.md`。
> 本仓库提供的是**可运行骨架 + 统一数据管线**，不是成品绕过脚本。

### 3. 调用中转
```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"grok/grok-4","messages":[{"role":"user","content":"hi"}]}'
```
`model` 格式为 `provider/model`，如 `claude/claude-sonnet-4-5`、`codex/gpt-5.5`、
`gemini/gemini-2.5-pro`、`grok/grok-4`。

## 配置（.env）

| 变量 | 说明 |
|---|---|
| `MAIL_BACKEND` | 临时邮箱后端（默认 `local` 仅占位，接你自己的 MoeMail/Cloudflare Worker） |
| `REG_PROXY` | 注册用代理 |
| `SMS_API_KEY` | 接码平台 key（Grok/Codex 手机验证时需要） |
| `SMS_COUNTRY` | 接码国家，默认 `us` |
| `HEADLESS` | 注册浏览器是否无头，默认 `true` |

## 目录结构

```
auto/
├── docker-compose.yml
├── relay/          # OpenAI 兼容中转 + 账号管理 API
│   ├── main.py
│   └── providers.py
├── dashboard/      # 可视化控制台 (单页，无构建步骤)
│   └── index.html
├── registration/   # 自动注册服务（Grok/Codex/Gemini）
│   ├── service.py
│   └── reg.py
├── shared/         # 三层共用的数据层 (SQLite)
│   └── db.py
└── docs/
    └── registration.md
```

## 合规提示

自动注册可能违反上游服务条款；Claude 等多家无公开注册插件。本系统仅供你拥有
合法权限的账号使用，风险自负。中转层（账号池 / 计费 / 对外售卖）相关合规请参照
你已有的 `ops/` 运营文档。

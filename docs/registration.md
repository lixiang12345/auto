# 注册流程补全指南

`registration/reg.py` 提供了可运行的骨架与统一数据管线（注册成功即写入
共享 `accounts.db`，控制台与中转立即可用）。每个 Provider 插件里标了
`skeleton` 的地方，需要按站点补完真实交互步骤。以下是各家的切入点与已知要点。

## Grok (accounts.x.ai)
- 入口：`GrokPlugin.register()`
- 已知流程（参考 grok-regkit）：临时邮箱 → 注册 accounts.x.ai → 邮箱验证 →
  （混合模式下短浏览器采 Turnstile/castle）→ 产出 SSO 或 mint OIDC (`xai-*.json`)。
- 需要的上游：`creds_json` 形如 `{"auth_type":"oauth","access_token":"...","refresh_token":"..."}`
  或 SSO cookie；转成中继可用的形式后由 `create_account` 写入。

## Codex / OpenAI (chatgpt.com)
- 入口：`CodexPlugin.register()`
- 已知流程（参考 codex-register-v3）：chatgpt.com 注册 → 邮箱验证（6 位码）→
  填资料 → 完成 → 自动 Codex PKCE OAuth2 换 `access_token` / `refresh_token`。
- 需要的上游：`{"auth_type":"oauth","access_token":"...","refresh_token":"..."}`。

## Gemini (accounts.google.com)
- 入口：`GeminiPlugin.register()`
- 已知流程：Google 账号注册 → 邮箱/手机验证 → 在 AI Studio 生成 API Key。
- 需要的上游：`{"auth_type":"apikey","api_key":"AIza..."}`。

## Claude（不在自动注册范围）
Claude 无公开注册插件，走手动接入：dashboard「接入账号」或
`POST /api/accounts {"provider":"claude","auth_type":"oauth","creds":{"access_token":"..."}}`。

## 接邮箱 / 接码
- `TempMail` 默认 `local` 仅生成地址、不收信。把你的 MoeMail / Cloudflare Worker /
  SMS-Activate / HeroSMS 对接写进 `TempMail.wait_code()` 与 `RegConfig.sms_*`。
- 验证信拉取失败要重试并带指数退避（参考 codex-register-v3 的 5 次重试状态机）。

## 浏览器环境
- Docker 内用无头 Chromium（`--no-sandbox` 由容器环境决定）；本地 macOS 调试可设
  `HEADLESS=false` 看真实流程。
- 反检测：注入 stealth JS / 用 Camoufox 指纹混淆可显著降低风控命中，按需引入。

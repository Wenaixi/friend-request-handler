# 好友请求处理（whitefox.friend-request-handler）

## 官方上游与版本溯源
- 官方仓库（Upstream Repository）：https://github.com/whitefox20011120/friend-request-handler
- 官方作者（Author / Maintainer）：whitefox20011120
- 功能定位：智能好友申请审批与鉴权插件
- 本地演进定位：基于官方上游最新版本，融入本地生产环境兼容性定制与稳定性加固。
- 本地专属定制：本地对齐 core 内独立 Webhook 端口与已通知标记容量回收机制。


源: whitefox
插件 ID: `whitefox.friend-request-handler`

## 主人改过的关键文件

- `config.toml`：
  - webhook 在 core 内 `0.0.0.0:18080`，path `/maibot/friend_request`（**不与 NapCat HTTP 9999 同端口**，避免冲突）。
- NapCat `onebot11_*.json` `httpClients`：添加一项 `http://core:18080/maibot/friend_request`（无 token）。
- `plugin.py`（2026-08-01 TDD 修复，4 commits）：
  - `_notified_flags`：`set` → `OrderedDict`，trim 用 `popitem(last=False)` FIFO
  - `_pending_tasks: set[asyncio.Task]` + `on_unload` cancel/gather
  - `_save_state`：async + `asyncio.Lock` + 写 `.tmp` 再 `os.replace`
  - `wait_for(..., timeout=45)` 包 `_on_friend_request`；secret 空时 `on_load` warning

## 配置要点

- 依赖 MaiBot core 内 HTTP 服务（plugin 自己起 aiohttp TCPSite）。
- 上游 `.gitignore` 是 github/python 完整版（已存在）；主人段追加作一致性。
- `webhook.secret` 建议填；空时会 warning 但**不** raise（合法配置，只是跳过验签）。

## 接入 & 依赖

- `core` 容器 → port 18080 → plugin.py 内 aiohttp webhook。
- NapCat `onebot11_*.json` HTTP clients 入口。

## 关键日志

- `好友申请处理插件已加载`
- secret 空：`webhook.secret 为空，将跳过签名校验…`
- 收到好友请求时 webhook 触发。

## 踩坑

- **不要把 webhook 监听到 NapCat HTTP 9999 同端口**——会被 fork 冲突或反向代理错。
- `_notified_flags` 不能用 set 做有界 trim：`list(set)` 无序，会误删最新 flag。
- fire-and-forget `create_task` 必须保留引用，否则 unload 时无法 cancel。
- 状态文件写必须 atomic（`.tmp` + `os.replace`），否则 kill 后半截 JSON。

## 测试

- `tests/test_review_fixes.py`：15 条
  - 原 8 条（HIGH 3 项 + MEDIUM 2 项，2026-08-01）
  - 新 7 条 `TestHandleLlmDecisionRemarkFix`（2026-08-19）：覆盖 set_friend_add_request 携带 remark / 拒绝时不调备注生成 / auto_remark=false 路径 / fallback 到 _get_nickname / 调用顺序 / remark 空时不调 set_friend_remark / _call_llm 失败回退 manual
- `tests/conftest.py`：maibot_sdk stub（本机无 SDK 也能跑）
- 跑：`python -m pytest tests/test_review_fixes.py -q`

## 历史脚注

- 2026-07-28: 从服务器同步到本地仓库（baseline head=`0e0ebf3`）；补充 `.gitignore` 主人段与本 CLAUDE.md。
- 2026-08-01: TDD 修 review HIGH/MEDIUM（commits `a68b13c` / `10bcc66` / `7400bc8` / `1935a94`）。
- 2026-08-19: 主人反馈两个问题——LLM 严格（扩列/想你被拒）+ LLM 通过的好友没备注。commit `f2350a0` 修复：
  - `LLM_PROMPT` 重写：删除反向 trigger 列举，改为默认通过 + 3 条硬拒绝清单（可疑 URL/明确营销话术/辱骂色情赌博政治反动）；强调「问题模板含喜欢你/扩列不要拒」
  - `_build_applicant_info_text` 精简：删除性别/年龄/等级/登录天数（被 LLM 当风险评分），验证消息提前到最前
  - `handle_llm_decision` 修备注 bug：把 `_generate_remark` 提前到 `set_friend_add_request` 之前，**set_friend_add_request 调用时带 remark 参数**（NapCat `SetFriendAddRequest._handle` 在 handleFriendRequest 后立即 setBuddyRemark，无 isBuddy 检查，原子生效）
  - 根因（NapCat 5c18a625 源码证实）：单独 `set_friend_remark` 会先 `isBuddy()` 检查，新建好友关系刚通过时 NapCat 本地缓存还没刷新成 buddy → throw "用户 XX 不是好友"，被 plugin 的 `raise_on_error=False` 静默吞掉。手动路径成功是因为 `set_friend_add_request` 时**带 remark**，NapCat 内部原子处理无 isBuddy 检查
  - 备份：`handlers.py.bak.20260819_002124`（服务器）
  - 服务器端到端验证：容器内 mock LLM 返回「通过」+ 备注「真·小周」，实测 `set_friend_add_request` params 真的包含 `remark='真·小周'`

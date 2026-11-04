# 海螺 Conch

> 一个自己写的小桥：**QQ 里的那个人**，跑在你自己电脑上。

海螺把 OneBot 协议端（SnowLuma / NapCat 都行）接上模型，做成一个**能说话的角色**，
而不是一个问答机器人。它只管两件事：

1. **她能在群里说话**——按人设、按心情、按当下在聊什么；
2. **每句话出门前都过一道闸**——插件说不发，就不发。

顺带留了几个扩展点，让记忆、看图、刷视频这些能力以插件形式接进来，
不必什么都塞进主程序。

---

## 它长什么样

```
QQ ──► 协议端（SnowLuma / NapCat）
          │  WS
          ▼
        海螺 ──┬─ 插件：海螺（说话主逻辑）
               ├─ 插件：心情（情绪状态，影响语气与长度）
               ├─ 插件：留影（有人发图，她先看一眼）
               └─ 工具：环形的心 / 刷视频 / 微光域……（MCP，按需接入）
          │
          ├─► 面板  http://127.0.0.1:7801   （说话、人设、词库、用量、更新）
          └─► 模型  OpenAI 兼容接口（DeepSeek / 阿里云百炼 / Kimi / 智谱 / 自定义）
```

---

## 三条设计取舍

**一、只做桥，不做平台。**
没有插件市场、没有工作流编辑、没有多租户。想加能力就写个插件放 `plugins/`，
不想用就把文件夹删了。

**二、插件只有四个扩展点。**
`speech.prompt`（往提示词里加东西）、`speech.gate`（发之前问一句能不能发）、
`message.received`、`message.observed`。够用，而且一眼能看完。

**三、隐私是硬约束，不是选项。**
仓库自带隐私门禁（`scripts/privacy_gate.py`），提交前自动查一遍：本机路径、密钥、
QQ 号、手机号、数据库文件。拦下就提交不了。规则可以看，也可以加。

---

## 安装

需要 Python 3.11+。

```bash
git clone https://github.com/ya-shua/conch.git
cd conch
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
```

复制配置，填上协议端地址、白名单群、模型 key：

```bash
copy config.example.json config.json
```

跑起来：

```bash
.venv\Scripts\python -m qqbridge.main --config config.json
```

面板：`http://127.0.0.1:7801/?token=<panel.token>`。
第一次打开会让你设一个登录密码——**设了之后，光有 token 也进不去浏览器界面**，
token 只留给脚本用。

---

## 配置里最容易填错的三处

| 字段 | 说明 |
| --- | --- |
| `onebot.ws_url` / `http_url` | 协议端的 WS 和 HTTP 端口**不是同一个**，别填混 |
| `onebot.access_token` / `ws_access_token` | 有些协议端 HTTP 与 WS 用**两个不同的 token** |
| `allow.groups` | 不填就是谁都不理；填了群号才在那个群说话 |

`owner_qq` 是管理员：私聊始终放行，可以远程指挥她往群里发消息。

---

## 插件

```
plugins/conch    说话主逻辑（合流、人设、遥控、发送）
plugins/tide     心情：情绪会衰减、会饿会困，影响语气和句子长短
plugins/eyes     留影：有人发图，她先看一眼再回
```

每个插件是「一个文件夹 + `metadata.json` + `main.py`（导出 `setup` / `teardown`）」。
面板里能单独开关。

### 工具（MCP）

配置里的 `tools.servers` 可以挂任意 stdio MCP 服务端，模型会自己决定什么时候调：

```json
"tools": {
  "enabled": true,
  "max_rounds": 4,
  "servers": [
    {
      "name": "ring",
      "command": "node",
      "args": ["D:/path/to/memory/mcp/server.mjs"],
      "cwd": "D:/path/to/memory",
      "scope": "owner",
      "timeout_s": 30
    }
  ]
}
```

`scope: "owner"` 表示只在**管理员私聊**里可用。群里默认一个工具都不给——
群里的话是别人写的，不该因为一句话就让她去记事、去刷视频。

---

## 更新

面板「访问与安全 → 版本与更新」里有版本号和两个按钮：

- **检查更新**：问 GitHub 最新 release（只读，不带凭据、不上传任何本机信息）
- **一键更新**：`git pull --ff-only`，快进合并；失败原样报错，不 `--force`、不动你的本地改动

更新完重启海螺生效。

---

## 隐私门禁

```bash
python scripts/privacy_gate.py --staged   # 只查暂存区
python scripts/privacy_gate.py --all      # 查全部已跟踪文件
```

开局装一次钩子，之后每次 `git commit` 自动跑：

```bash
git config core.hooksPath .githooks
```

查六类：本机绝对路径、密钥、QQ 号、手机号（拦）；裸数字、邮箱（只警告）。
误报就在那一行加 `privacy-ok`。

---

## 发版（维护者）

版本号在 `qqbridge/__init__.py`，发版流程固定四步：

```bash
python scripts/privacy_gate.py --all     # 1. 过门禁，有拦截就别发
git add -A && git commit -m "release: v0.1.1"
python scripts/publish_release.py        # 2. 推代码 + 打 tag + 建 release
```

`publish_release.py` 走 GitHub 的 Git Data API 而不是 `git push`——
有些网络只有 `api.github.com` 通、`github.com:443` 被挡，push 必然失败。
它做的事和 push 等价，顺带把 tag 和 release 一起建好。

token 只从 `GITHUB_TOKEN` 环境变量或 `%USERPROFILE%\Desktop\token.txt` 读，不会打印。

## 许可

MIT。见 [LICENSE](LICENSE)。
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
               ├─ 插件：情绪（外部接进来，影响语气与长度）
               ├─ 插件：留影（有人发图，她先看一眼）
               ├─ 插件：表情包（群里的表情自动收进收藏）
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

## 她说话的节奏

面板「发言策略」里能调，都存在 `config.json` 的 `strategy` 下：

| 字段 | 面板上叫什么 | 作用 |
| --- | --- | --- |
| `trigger` | 认 @ / 认名字 / 所有消息 | 什么样的话会进她的视野 |
| `participate` | 必回 / 可以接话 / 只听 | 进视野之后回不回 |
| `cooldown_sec` | 两次发言最短间隔 | 防连珠炮 |
| `max_per_hour` | 每小时最多发 | 超了就闭嘴到下一小时 |
| `max_consecutive` | 连续最多发 | 没人接话就自动退场 |
| `wake_gap_sec` | 两次开口最短间隔 | 她主动开口之间的间隔，0 = 不限 |
| `sleep_after_sec` | 安静多久算睡着 | 群里安静超过这么久就睡；睡着后只有 @ 她 / 叫名字才理，有人说话就醒 |
| `merge_window_ms` | 唤醒间隔（合流窗口） | 小于它的几条消息并成一句听 |

---

## 插件

仓库里只装两个插件，其它能力一律从外面接：

```
plugins/conch     说话主逻辑（合流、人设、遥控、发送）
plugins/websearch 联网：她自己上网查词/查梗，查过的短词顺手记进词库
（外部）tide      情绪：接 Tide（汐）本体，桥接这边只当适配层，靠 plugin_paths 挂进来
（外部）eyes      留影：有人发图，她先看一眼
自带 stickers     表情包：同步 QQ 收藏 + 收群里的表情，能进提示词、能发出去
                  （不走插件那套：设置就在面板「表情包」页右上角）
自带 look         看一眼：借留影的看图能力（留影是外部程序，插件页里单列）
```

v0.1.6 起不再内置情绪和留影：它们是独立程序，在 `config.json` 的 `plugin_paths`
里指一下就行，仓库不再跟着一起长。

每个插件是「一个文件夹 + `metadata.json` + `main.py`（导出 `setup` / `teardown`）」。
面板里能单独开关、重载、跳转它自己的页面。

### 插件可以自带启动方式

`metadata.json` 里写一段 `launch`，海螺就会把它做成一张能启动、能跳转、能看日志的卡片，
网址和令牌都自动拼——不用改主程序，也不用写 `launchers.json`：

```json
"launch": {
  "cmd": "D:\\Tools\\seek\\seek.exe",
  "args": ["--serve", "--port", "6123"],
  "port": 6123
}
```

点「页面」就是 `http://127.0.0.1:6123/`；如果服务把令牌放在 `state/console-token` 这类常见位置，
会自动带上 `?token=...` 直接登录。完整字段见 [docs/plugin-launch.md](docs/plugin-launch.md)。

### 加插件、改设置

插件就是一个文件夹，放进 `plugins/`（或 `config.json` 的 `plugin_paths` 指着的目录），
重启海螺或点卡片上的「重载」就出现在插件页。不需要任何市场。

插件在 `metadata.json` 里写 `settings` 数组，卡片上就会出现「设置」，
面板按字段渲染成表单（勾选 / 数字 / 文本 / 下拉），保存后自动重载。
没写 `settings` 的插件保留原来的 JSON 编辑框。细则见
[docs/plugin-settings.md](docs/plugin-settings.md)。

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
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
.venv\Scripts\python main.py --config config.json
```

面板：`http://127.0.0.1:7801/?token=<panel.token>`。
第一次打开会让你设一个登录密码——**设了之后，光有 token 也进不去浏览器界面**，
token 只留给脚本用。

### 托盘（Windows）

桌面上只留一个「海螺」快捷方式，点它就是 `start_conch.vbs` → `scripts/tray.py`：

- 右下角一个图标，全家桶（SnowLuma / 记忆库 / 情绪面板 / 插件服务）都在后台跑
- 双击图标 = 打开面板；右键菜单第一行是当前状态，下面：打开面板 / 重启海螺 / **退出（全部停止）**
- 关掉浏览器页面 **不会** 停任何服务
- 只有点「退出」，才会执行 `scripts/stop_all.py` 把服务全部收掉

> Windows 11 默认把新图标塞进「`^` 隐藏的图标」里。想让它一直待在任务栏：
> 点 `^` → 把海螺**拖**到任务栏上；或者 设置 → 个性化 → 任务栏 → 其他系统托盘图标 → 海螺 打开。

自检：`python scripts/tray.py --quit` 走的就是菜单「退出」那条路（托盘在跑时它会留张字条
让托盘自己收尾；托盘没在跑就直接停服务）。

### QQ（雪花 SnowLuma）

- 雪花是「注入已经登录的 QQ 进程」，注入过的 QQ 在雪花重启后**会自动接回**，
  不需要重新扫码 —— 冷启动实测约 11 秒 3001 就上线
- 前提是那个 QQ 客户端还开着、没退出登录。真关掉 QQ 的话还是得去
  `http://127.0.0.1:5099` 扫一次码
- 代码里**别**把雪花的 `hookAutoLoad` 打开：它会把发现的每个 QQ 都注入，
  而各号的 OneBot 端口都是 3000/3001，会互相抢端口（抢输的那个号绑不上，
  海螺就 `connected=false`）。切换机器人号时，把要用的那个号在
  `SnowLuma/config/onebot_<uin>.json` 里恢复网络，其它号清空 `networks` 里的四个数组

---

## 配置里最容易填错的三处

| 字段 | 说明 |
| --- | --- |
| `onebot.ws_url` / `http_url` | 协议端的 WS 和 HTTP 端口**不是同一个**，别填混 |
| `onebot.access_token` / `ws_access_token` | 有些协议端 HTTP 与 WS 用**两个不同的 token** |
| `allow.groups` | 不填就是谁都不理；填了群号才在那个群说话 |

`owner_qq` 是管理员：私聊始终放行，可以远程指挥她往群里发消息。
要多个管理员、或者想让她换个叫法，用面板「白名单 → 称呼卡」：

- 一卡一人，可以挂多个 QQ 号（这些号她当成同一个人）；多张卡就是多个人
- 每张卡能设「私聊怎么叫」「群里怎么叫」，还能按群单独设（本群 > 群里默认 > 不填）
- 称呼只是「希望你这么叫」，真正能用的词还是人设卡里那一套；不填就完全不占提示词
- 不建卡也行：`owner_qq` 会自动当一张叫「管理员」的卡

### 模型接入（多把 Key）

面板「模型接入」页：

- **Key 可以放很多把**，从上往下依次用；前一把欠费/失效/被限流就自动换下一把，全不行才报错
- 加：粘一把 → 点「添加」（或直接回车）；删：每行右边的 ✕（删最后一把会提醒「删了模型就调不了了」）
- **模型名**别手抄：点「获取模型」，它会用当前这把 Key 问一次 `/models`，
  然后弹出候选列表 —— 上面有搜索框（敲 `deep` 只剩 deepseek 系），↑↓ 选、Enter 选中、Esc 关掉
- 每把 Key 右边还有个「模型」按钮，能单独看这把 Key 支持哪些（不同中转给的不一样）
- 有些中转不实现 `/models`，那就直接把模型名敲进去，一样能用

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

## 她听得懂语音

QQ 语音是 **silk**（ffmpeg 解不了），海螺自己解：`plugins/conch/tools/silk_decode.cjs` + `vendor/silk-wasm`
（纯 wasm，不用编译）→ 16k wav → OpenAI 兼容的 `/audio/transcriptions`。

- ASR 配置**跟「留影」共享**（`留影/data/vision.json` 的 `asr` 段，默认硅基流动 `SenseVoiceSmall`）；
  想单独用就在 `config.json` 里写 `asr`，留空即跟随
- 单模型并发，默认同时 2 条：单条 ~1–1.7 秒，3 条同时 ~2.2 秒
- 转写结果标成 `[语音转写] …` 进上下文，人设里让她按上下文顺字但**不许改意思**，关键信息拿不准就问一句
- 转不出来当没听见：不回、不写记录，不会发半句乱码

细节见 [docs/语音转写.md](docs/语音转写.md)。

---

## 插件

仓库自带两个插件，其它能力一律从外面接：

```
plugins/conch     说话主逻辑（合流、人设、遥控、称呼卡、记忆、发送）
plugins/websearch 联网：她自己上网查词/查梗，查过的短词顺手记进词库
（外部）tide      情绪：接 Tide（汐）本体，桥接这边只当适配层，靠 plugin_paths 挂进来
（外部）eyes      留影：有人发图，她先看一眼
自带 stickers     表情包：同步 QQ 收藏 + 收群里的表情，能进提示词、能发出去
                  （不走插件那套：设置就在面板「表情包」页右上角）
自带 look         看一眼：借留影的看图能力（留影是外部程序，插件页里单列）
```

v0.1.6 起不再内置情绪和留影：它们是独立程序，在 `config.json` 的 `plugin_paths`
里指一下就行，仓库不再跟着一起长。说话插件（`conch`）2026-11-09 起回到仓库
`plugins/conch/`——克隆下来就是完整一套：宿主 + 说话插件，能直接跑。

每个插件是「一个文件夹 + `metadata.json` + `main.py`（导出 `setup` / `teardown`）」。
面板里能单独开关、重载、跳转它自己的页面。
回复快慢在面板「发言策略 → 频率与把关」里调，三个数：合流窗口（打完之后安静多久才开口）、
一句一条的停顿（基础值）、单句停顿上限。改完立刻生效，不用重启。
同一页还有：一轮最多几条（默认 4，多的并进最后一条）、重启后补回漏掉的消息（回看 30 分钟）、
听到收尾话就不回（词表可改）。收尾话只认 10 字以内的短句，带问号不算。
她发图时先查本地表情库，对不上就去「表情包收录库」的大厅按标签挑一张（`127.0.0.1:7802`）；
下载的图缓存在 `data/hall/`。提示词里会说明"本地空着也能写【表情包：描述】"。

她看到的每一行长这样：

```
[群123456] 阿星(小鱼) #123456789: 在吗
[群123456] 群主 老墨 #111222333: 在吗
[群123456] 管理员A #1234567890: 在吗
[私聊] 管理员 #1234567890: 肥鱼
```

- 方括号是「在哪」，冒号前是「谁」，`#` 后面是 QQ 号（昵称会改，号码不会）
- `管理员A` / `管理员` 是称呼卡里的人；`群主` `群管` 只是他在那个群里的职务，不是管理员
- 别人把昵称改成「管理员」，会被标成 `冒充管理员 #号码`




插件页的状态只有两个词，所有卡片（插件、独立服务）一个口径：

- **运行中**：插件已启用，而且它自己声明的端口真的在听（没声明端口就看启用状态）
- **已关闭**：没启用，或者端口没起来

想让卡片有「页面」按钮、能显示端口，在 `metadata.json` 里写：

```json
"page": "http://127.0.0.1:7802/",
"port": 7802
```

`page` 只写地址也行，端口会从地址里抠出来；`launch.port` 同样可用。

### 插件可以自带启动方式

`metadata.json` 里写一段 `launch`，海螺就会把它做成一张能启动、能跳转、能看日志的卡片，
网址和令牌都自动拼——不用改主程序，也不用写 `launchers.json`：

```json
"launch": {
  "cmd": "<安装路径>\\seek.exe",
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

版本号在 `core/version.py`，发版流程固定四步：

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
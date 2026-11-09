# 插件自带启动方式（launch）

插件在自己的 `metadata.json` 里声明 **怎么启动、网址在哪、令牌在哪**。
海螺读到就自动做成一张服务卡片——不用再手写 `launchers.json`，也不用改主程序。

## 最小写法

```json
{
  "id": "seek",
  "name": "寻光",
  "version": "0.1.0",
  "description": "一个自己就跑起来的小工具，带网页界面。",
  "launch": {
    "cmd": "<安装路径>\\seek.exe",
    "args": ["--serve", "--port", "6123"],
    "port": 6123
  }
}
```

放进去就完事：

- 插件页出现「寻光」卡片
- 点「启动」→ 后台拉起（无窗口），日志落到 `data/logs/seek.log`
- 点「页面」→ 打开 `http://127.0.0.1:6123/`
- 点「重载」→ 停了再起
- 点「日志」→ 面板里直接看最近 200 行

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `cmd` | 二选一 | 可执行文件（python / node / 自己的 exe 都行） |
| `runtime` | 二选一 | `node` / `python`：不写 `cmd` 时由海螺去找解释器（见下） |
| `entry` | 否 | 配合 `runtime`：相对插件目录的入口文件，自动排在参数最前面 |
| `args` | 否 | 参数数组，注意 JSON 里反斜杠要写两次 |
| `cwd` | 否 | 工作目录，默认插件自己的文件夹（写相对路径则相对插件目录） |
| `name` | 否 | 卡片上的名字，默认用插件 `name` 或 `id` |
| `note` | 否 | 卡片上的说明，默认用插件 `description` |
| `port` | 否 | 有端口就用来判断在不在跑，并拼出默认网址 |
| `url` | 否 | 页面地址，写它就优先，不写就按 `port` 拼 |
| `url_token_file` | 否 | 令牌文件（相对 `cwd`），会自动拼成 `?token=...` |
| `env` | 否 | 额外环境变量对象 |
| `log_dir` | 否 | 日志目录，默认 `data/logs` |
| `secrets` | 否 | 需要人填的密码，形如 `[{"key":"password","label":"密码"}]` |

## 别写死路径：`runtime` + `entry`

写 `cmd: "D:\\...\\node.exe"` 的好处是直白，坏处是**一挪目录就废**。所以推荐这样：

```json
{
  "id": "eyes",
  "name": "留影",
  "launch": {
    "runtime": "node",
    "entry": "src/panel.mjs",
    "args": ["--port", "5188"],
    "port": 5188
  }
}
```

解释器由海螺自己找：

- `runtime: "node"` → `config.json` 的 `look.node` → `PATH` 里的 `node`
- `runtime: "python"` → 插件自己的 `.venv\Scripts\pythonw.exe` → 海螺的 `.venv\Scripts\pythonw.exe` → `PATH`

`entry` 用 `/` 或 `\` 都行。整个插件目录随便搬，`metadata.json` 一个字都不用改。

## 外部应用：没有 `main.py` 也能登记

有些目录（留影、环形的心、微光域、声来、刷视频）是**独立程序**，不是宿主内插件，没有 `main.py`。
它们的 `metadata.json` 只要标 `"kind": "app"`（不标也会按「有没有 main.py」自动判断）：

- 插件页正常出现卡片，带版本、说明、页面、启动/关闭
- 卡片上写「外部应用」，不会显示成「海螺插件」
- 点启动/关闭走的是启动器（`launchers.json` 或它自己的 `launch`），加载器不会去 import 它

如果哪天真给它们加了 `main.py`，把 `kind` 去掉就回到普通插件。

## 令牌是怎么找到的

按顺序试，找到第一个非空就用：

1. `launch.url_token_file` 指定的文件（相对 `cwd`）
2. `state/console-token`
3. `state/token`
4. `data/token`
5. `data/console-token`
6. `console-token`
7. `.token`
8. `token.txt`
9. `token`

拼成 `http://127.0.0.1:<port>/?token=<令牌>`，点「页面」直接登录，不用手抄。

找不到令牌不影响使用，只是不加 `?token=`。

## 和 `launchers.json` 的关系

`launchers.json`（本机文件，不进仓库）里手写的条目**优先**，同一个 `id` 只出现一次。
但两边现在是**按字段合并**的：手写那条没写的字段（`cmd` / `cwd` / `args` / `port` / `url` / `note`）
会从插件自己的 `metadata.json` 补上。

所以理想的用法是：

- `metadata.json` 管「怎么跑」（`runtime` + `entry` + 端口 + 网址），跟着插件走、跟着仓库走
- `launchers.json` 只管本机私有的东西（密码、额外环境变量、临时改端口）

挪目录时插件那份会自动指向新位置；`launchers.json` 里留着的旧绝对路径反而会盖住它 ——
如果你把路径也写在了 `launchers.json`，挪完记得把那几行删掉，让插件自己说。

## 一键启动也会带上

桌面「海螺启动」走 `scripts/start_all.py`，它读的是同一份清单：
`launchers.json` + 所有插件的 `launch` 段。新装的插件声明了 `launch`，开机就会一起起来。

端口一直等不到（30 秒）会在 `data/logs/start.log` 里写一行失败原因，不会卡住后面的服务。
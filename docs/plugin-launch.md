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
| `cmd` | 是 | 可执行文件（python / node / 自己的 exe 都行） |
| `args` | 否 | 参数数组，注意 JSON 里反斜杠要写两次 |
| `cwd` | 否 | 工作目录，默认插件自己的文件夹 |
| `name` | 否 | 卡片上的名字，默认用插件 `name` 或 `id` |
| `note` | 否 | 卡片上的说明，默认用插件 `description` |
| `port` | 否 | 有端口就用来判断在不在跑，并拼出默认网址 |
| `url` | 否 | 页面地址，写它就优先，不写就按 `port` 拼 |
| `url_token_file` | 否 | 令牌文件（相对 `cwd`），会自动拼成 `?token=...` |
| `env` | 否 | 额外环境变量对象 |
| `log_dir` | 否 | 日志目录，默认 `data/logs` |
| `secrets` | 否 | 需要人填的密码，形如 `[{"key":"password","label":"密码"}]` |

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

`launchers.json`（本机文件，不进仓库）里手写的条目**优先**。
同一个 `id` 两边都有时，只出现一次，用手写的那条——想临时改端口、改路径，改 `launchers.json` 就行，
不用动插件。

## 一键启动也会带上

桌面「海螺启动」走 `scripts/start_all.py`，它读的是同一份清单：
`launchers.json` + 所有插件的 `launch` 段。新装的插件声明了 `launch`，开机就会一起起来。

端口一直等不到（30 秒）会在 `data/logs/start.log` 里写一行失败原因，不会卡住后面的服务。
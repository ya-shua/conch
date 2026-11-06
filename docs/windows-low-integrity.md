# 服务跑在「低完整性」下：症状与修复

Windows 有一条规则：**进程的完整性级别 = 父进程与可执行文件标签里更低的那个**。
如果 exe 所在的目录被打了 `Low` 标签，那么从这里启动的进程一律是 `Low`，
它的子进程、孙进程也全是 `Low`。

DSH 的沙箱会给工作区打这种标签。于是装在 `D:\AI\conch` 下的海螺
（`.venv\Scripts\pythonw.exe`）一启动就是 Low，被它拉起来的 SnowLuma、情绪、
留影、娱乐、环形的心也全是 Low。

## 症状

- SnowLuma 日志里：`WARN [App] Node integrity: process=Low (RID=4096)`（正常应是 `INFO ... Medium (RID=8192)`）
- SnowLuma 控制台顶部有运行环境告警
- SnowLuma 写不了自己的日志：`[logger.storage] write ...\logs\snowluma-YYYY-MM-DD.log: EPERM: operation not permitted`
- 别的服务偶发写不了自己目录里的旧文件（旧文件是更高级别进程建的，Low 改不动）

> 「node 版本低」这个说法是误会：SnowLuma 那句告警讲的是**进程完整性**，不是 Node 版本。
> 自带的 `node.exe` 是 v24.22.1，`check-node-version.cjs` 要求 `^24.22.1 || >=26.12.1`，本来就满足。

## 判断

```powershell
# 从 venv 里起一个假进程，看它是什么级别
# 4096 = Low（有问题），8192 = Medium（正常）
```

或者直接看 SnowLuma 日志里的 `Node integrity:` 那一行。

## 修复

把标签抬回 Medium。递归改标签需要目录上的 WRITE_OWNER，如果报 `Access is denied`，
先给自己补权限再改：

```powershell
$me = "$env:USERDOMAIN\$env:USERNAME"

icacls 'D:\AI\conch'            /setintegritylevel '(OI)(CI)Medium' /T /C
icacls 'D:\AI\conch\qqbridge'   /grant "${me}:(OI)(CI)F"
icacls 'D:\AI\conch\qqbridge'   /setintegritylevel '(OI)(CI)Medium' /T /C /Q
```

第一条处理工作区根；第二、三条处理 `qqbridge`（根目录本身也要改，否则新建的文件还是继承 Low）。
改完必须是 `Failed processing 0 files`。

然后重启海螺：SnowLuma 日志第一行应该变成
`INFO [App] Node integrity: process=Medium (RID=8192)`，EPERM 那几行消失。

## 备注

- 只改标签，不动文件内容和所有者；DACL 只加了当前用户自己的完全控制，没有删别人的。
- 沙箱如果再次给工作区打 Low 标签，重启后会退回老样子，按上面三条再跑一次即可。
- 判断依据是 `whoami /groups` 里的 `Mandatory Label` 只能看到自己，别的进程要看它的 token，
  光看文件是看不出来的（`Get-Acl` 默认读不到完整性标签）。
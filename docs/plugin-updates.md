# 插件版本检查（只看，不动）

> 2026-11-09 加。**海螺只负责告诉你"哪个插件有新版本"，更新在插件自己的页面里做。**

## 为什么宿主不代劳

插件各有各的目录结构、配置和迁移步骤（情绪要挪状态、表情库要迁 sqlite、
留影还要重启自己的面板）。宿主去下载解压替换，等于替别人改代码：出问题说不清，
也容易把配置盖掉。所以这里只做**只读检查**，一行文件都不写进插件目录。

## 插件怎么声明

`metadata.json` 里加 `source`，两种写法：

```json
"source": "ya-shua/tide"
```

```json
"source": { "repo": "ya-shua/tide", "check": "tag" }
```

- `repo`：GitHub 的 `owner/name`
- `check`：`release`（默认，读最新 release）或 `tag`（读最新 tag）
- 不写 `source`：这个插件不参与检查，面板上什么都不显示（不打扰）

## 面板上是什么样

- 插件卡片版本号后面出现一枚 `可更新 v1.2.0` 的浅色徽标
- 插件页右上角有「检查插件更新」按钮：点一下才真的去 GitHub 查
- 没声明 `source` 的插件不显示徽标；查不到的（网络问题、仓库没打 tag）静默，不弹错误
- **没有"一键更新"按钮**——点插件自己的「页面」，在那边升级

## 缓存与限流

- 结果写 `data/plugin_updates.json`，6 小时有效
- 打开面板只读这个文件，**不发网络请求**
- 只有点「检查插件更新」才发请求（`force`）
- GitHub 匿名 API 限流 60 次/小时；6 小时缓存就是为了不撞限

## 现在声明了 source 的插件

| 插件 | source |
| --- | --- |
| 情绪（tide/mood） | `ya-shua/tide` |
| 表情包收录库（meme-library） | `ya-shua/meme-library` |

宿主自己的插件（`plugins/conch`、`plugins/websearch`）跟着宿主整包更新，
不需要单独声明——宿主「检查更新」会把它们一起换掉。

## 接口

```
POST /api/plugins/check-updates   {"force": true}
→ {"ok": true, "checked": 2, "items": {"tide": {"current": "0.1.0", "latest": "v1.2.0", "has_update": true, "error": ""}}}
```

`GET /api/plugins` 每个插件多一个 `update` 字段（来自缓存，不联网）。
# 插件设置表单（settings）

插件在 `metadata.json` 里写一个 `settings` 数组，面板就会**按字段渲染成表单**——
用户填数字、勾选项，不用对着 JSON 改。不写 `settings` 的插件仍然保留原来的 JSON 编辑框。

> 海螺自带的功能（比如表情包）不走这套，设置直接在它自己的页面里。

## 写法

```json
{
  "id": "demo",
  "name": "示例插件",
  "settings": [
    {"key": "enabled", "label": "启用", "type": "bool", "default": true,
     "hint": "关掉就完全不动：不处理消息、不占额度。"},
    {"key": "cooldown_ms", "label": "冷却", "type": "int",
     "default": 60000, "min": 0, "unit": "毫秒"},
    {"key": "mode", "label": "模式", "type": "str", "default": "normal",
     "options": [["normal", "正常"], ["quiet", "安静"]]}
  ]
}
```

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | 是 | 存进 `config.json` 的键名 |
| `label` | 否 | 表单上显示的名字，缺省用 `key` |
| `type` | 否 | `bool` / `int` / `float` / `str`，默认 `str` |
| `default` | 否 | 没配过时的值，也会显示在表单里 |
| `min` / `max` | 否 | 数字范围，**保存时会夹回范围内**（填 999、上限 30，存进去就是 30） |
| `unit` | 否 | 数字后面的小字，比如「毫秒」「张」 |
| `hint` | 否 | 字段下方的说明 |
| `options` | 否 | `[["值","显示名"], …]`，有它就是下拉框 |
| `maxLength` | 否 | 字符串截断长度 |

## 保存行为

- 面板只提交表单里出现的字段，**不动**其它键（手写塞进去的高级配置不会被冲掉）
- 保存后默认顺手重载这个插件（`reload: true`），不用重启海螺
- 值写进插件目录下的 `config.json`（该文件不进仓库）

## 接口

```http
GET  /api/plugins/{id}/config      # {schema, values, raw, text}
PUT  /api/plugins/{id}/config      # {"values": {...}, "reload": true}
PUT  /api/plugins/{id}/config      # {"text": "{...}"}  ← 老入口，原样 JSON
```

`GET` 同时返回 `text`，所以想给「高级用户」留个 JSON 后门也不冲突。
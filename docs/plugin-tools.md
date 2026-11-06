# 让插件给模型挂一个工具

模型能调两类工具：桌面上的 **MCP 程序**（海螺插件自己接的），和 **插件自己实现的**。
后者靠工具盒 `ToolBox`：插件在 `setup(ctx)` 里登记，模型就会在聊天时看到它。

现成例子：`plugins/websearch/`（联网）。

## 三步

### 1. 写工具长什么样

模型看见的是 OpenAI 那种 function 定义：

```python
TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "web_lookup",            # 名字别和别的工具重
        "description": "上网查一个你不懂的词、梗、事件。用一次就够，别连着查好几遍。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "要查的东西"}},
            "required": ["query"],
        },
    },
}
```

### 2. setup 里登记，teardown 里摘掉

```python
def setup(ctx):
    toolbox = ctx.services["tools"]        # 宿主给的，一定有
    toolbox.register(TOOL_SPEC, lambda args: handler(ctx, args), owner="websearch")

def teardown(ctx):
    ctx.services["tools"].unregister_owner("websearch")
```

- `handler(args)` 返回**字符串**就行，模型看到的就是这段文字；返回 dict/list 会自动转 JSON。
- `owner` 用来批量摘除；重载插件时会先 teardown 再 setup，不会重复。
- 同名工具后注册的覆盖前一个（可以拿它做替换）。

### 3. 别在 handler 里干太久

工具调用在说话链路里同步等结果，超过几秒她这轮就卡住了。
要慢活（比如查完顺便写库）就 `asyncio.create_task(...)` 丢到后台：

```python
if looks_like_term(query):
    asyncio.create_task(learn_term(ctx, query, items))   # 不挡回复
```

## 实际跑起来是什么样

```
INFO [plugin.websearch] 联网工具就绪（自动学习开）
INFO [plugin.conch] 调工具 web_lookup({'query': '破防'})
INFO [plugin.websearch] 联网查「破防」→ 6 条
INFO [plugin.websearch] 自动学会「破防」：被戳中、情绪崩了
```

## 开关

插件的 `config.json`（没有就不用建）：

```json
{"tools": {"web_lookup": true, "web_learn": true}}
```

`web_lookup` 关掉 = 她不会再自己查；`web_learn` 关掉 = 查了也不写进词库。
插件卡片上的「关闭」是整条摘掉（`teardown` 会把工具注销）。

## 两个坑

- 工具名不能撞：MCP 那边已经有 38 个，自己的工具最好带前缀（`web_`、`my_`）。
- handler 里抛异常不会炸掉聊天，`ToolBox.call` 会把错误当工具结果回给模型；
  但**别返回空字符串**，模型会以为自己查到了空气，最好明确写「没查到」。
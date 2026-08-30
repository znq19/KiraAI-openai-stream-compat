# KiraAI OpenAI 流式兼容（openai-stream-compat）

一个 **KiraAI 插件级 Provider 桥接插件**，专门解决两类 OpenAI 兼容端点惹的麻烦：

1. **拆信封问题**：部分端点（比如 deepseek 的 cline 通道）非流式响应会被整体塞进 `model_extra['data']` 信封里，顶层 `choices` 是空的、`usage` 是 `None`，KiraAI 官方 OpenAI Provider 解析不到任何内容。
2. **流式开关问题**：同一端点走 **流式 SSE 时反而是标准格式**，本插件默认「流式优先」，顺带把推理字段名（`reasoning` ↔ `reasoning_content`）也兼容掉。

> 纯插件实现，**不动 core 任何代码**。在 WebUI 新建 Provider 时选「OpenAI 流式兼容」即可，使用体验和官方 OpenAI Provider 几乎一样。

---

## ✨ 特性

- 🚀 **流式优先**（默认 `auto` 模式）：先把流式响应聚合成标准 `LLMResponse`，工具调用、推理内容、token 用量全在顶层，不丢任何信息
- 📦 **信封兜底**：流式失败或产出为空时，自动从 `model_extra` 信封路径解析内容
- 🧠 **推理字段兼容**：`reasoning` 字段自动映射为标准 `reasoning_content`（流式、非流式两条路径都兼容）
- 🛠️ **路径全可配**：信封路径、内容路径、推理路径、工具调用路径、用量路径全部支持点分路径自定义，适配各种怪胎端点
- 🧩 **插件级接入**：不改框架，热重载即生效

---

## 📥 安装（小白友好版）

1. 把 `openai-stream-compat` 文件夹整个放进 KiraAI 的 `data/plugins/` 目录
   ```
   data/plugins/
   └── openai-stream-compat/
       ├── main.py
       ├── manifest.json
       ├── schema.json
       ├── icon.svg
       └── icon-dark.svg
   ```
2. 重启 KiraAI（或 WebUI 里热重载插件）
3. 打开 WebUI → **模型提供商** → **新建 Provider**
4. **格式** 选择 **「OpenAI 流式兼容」**
5. 填好 **Base URL** 和 **API Key**
6. 保存后拉取/添加模型，把某个模型设置为默认 LLM 即可开聊

> 需要 Python 依赖：`openai`、`httpx`（KiraAI 本体已依赖，一般无需额外安装）

---

## ⚙️ 配置项说明

### Provider 级（新建 Provider 时填）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `base_url` | string | `https://api.openai.com/v1` | OpenAI 兼容端点的 Base URL |
| `api_key` | sensitive | 空 | 你的 API 密钥 |
| `headers`（高级设置） | json | `{}` | 自定义请求头，透传给 OpenAI 客户端 |

### 模型级（模型的高级设置 → 桥接设置）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `mode` | enum | `auto` | `auto`：流式优先，失败自动退回信封解析；`stream`：始终走流式聚合；`envelope`：始终解析信封 |
| `envelope_path` | string | `data` | 信封载荷在 `response.model_extra` 中的路径（点分）。**留空 = 显式禁用信封兜底**（仅从标准响应解析） |
| `content_path` | string | `choices.0.message.content` | 文本内容的点分路径（相对 response 或信封根） |
| `reasoning_path` | string | `choices.0.message.reasoning` | 推理内容的点分路径（`reasoning` 自动兼容 `reasoning_content`） |
| `tool_calls_path` | string | `choices.0.message.tool_calls` | 工具调用列表的点分路径 |
| `usage_path` | string | `usage` | token 用量对象的点分路径 |

> 所有桥接配置都在「桥接设置」里折叠着，**不填也能跑**（默认 `auto` 模式）。

---

## 🎯 预设示例：deepseek cline 通道

遇到「非流式被塞信封、流式正常」的端点，直接照抄：

```json
{
  "format": "openai-stream-compat",
  "provider_config": {
    "base_url": "https://your-deepseek-cline-endpoint/v1",
    "api_key": "sk-xxxx"
  },
  "model_config": {
    "llm": {
      "deepseek-v4-flash-vision-exp": {
        "section_bridge": {
          "mode": "auto",
          "envelope_path": "data",
          "content_path": "choices.0.message.content",
          "reasoning_path": "choices.0.message.reasoning",
          "tool_calls_path": "choices.0.message.tool_calls",
          "usage_path": "usage"
        }
      }
    }
  }
}
```

如果流式完全不可用、只能走信封，把 `mode` 改成 `envelope` 即可。

---

## 🤔 常见问题

**Q：和官方 OpenAI Provider 有什么区别？**
A：使用方式一样，但 LLM 客户端额外做了「流式聚合 + 信封兜底 + 推理字段兼容」，遇到官方客户端解析为空的情况它能救回来。

**Q：`auto` 模式流式失败了会怎样？**
A：按错误类型分级处理，不会盲目重试：
- **超时 / 连接失败**（`APITimeoutError` / `APIConnectionError`）→ 直接报错。与是否流式无关，重试信封只是再白等一轮
- **401 / 403 / 429**（密钥错误、无权限、限流）→ 直接报错。注定再失败
- **400 / 404 / 415 / 422 / 5xx** → 自动退到信封解析再试一次（可能是端点不支持 `stream` 参数这类可救的错误），日志会打 `falling back to envelope mode`
- 其余异常 → 自动退到信封解析

**Q：我的端点信封结构不是 `data`，怎么改？**
A：看接口文档/抓包确认真实结构，改模型高级设置里的 `envelope_path`（及其余路径）。比如信封是 `model_extra['result']['payload']`，就填 `result.payload`。想完全禁用信封兜底，把 `envelope_path` 留空即可。

**Q：为什么有的端点推理字段叫 `reasoning`？**
A：非标准兼容实现。本插件默认路径就用 `reasoning`，但会自动尝试 `reasoning_content` 作为备选，两种都能读；流式 SSE 里的 `reasoning` 也会从 `delta.model_extra` 读取，不会丢推理内容。

---

## 🧩 工作原理（一句话版）

在插件加载时把 `OpenAIStreamCompatProvider` 注册进 `ProviderManager` 注册表（`_registry` / `_schemas` / `_manifests`），WebUI 即可选到「OpenAI 流式兼容」格式；LLM 客户端继承官方 `OpenAICompatibleLLMClient`，重写 `chat()`（流式优先、分级兜底）和 `chat_stream()`（流式推理字段兼容）：先聚合流式，不行就掏信封。

## 📄 许可证

MIT

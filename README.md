# KiraAI OpenAI 流式兼容（openai-stream-compat）

一个 **KiraAI 插件级 Provider 桥接插件**，专门解决三类 OpenAI 兼容端点惹的麻烦：

1. **拆信封问题**：部分端点（比如 deepseek 的 cline 通道）非流式响应会被整体塞进 `model_extra['data']` 信封里，顶层 `choices` 是空的、`usage` 是 `None`，KiraAI 官方 OpenAI Provider 解析不到任何内容。
2. **流式开关问题**：同一端点走 **流式 SSE 时反而是标准格式**，本插件默认「流式优先」，顺带把推理字段名（`reasoning` ↔ `reasoning_content`）也兼容掉。
3. **用量位置问题**：部分端点的流式 token 用量不在标准「独立 usage-only 事件」里，而是挂在最后一个带内容的 chunk 上或塞在 `event.model_extra['usage']`，本插件三种位置都能提取（解决日志里 Input tokens 显示 None）。

> 纯插件实现，**不动 core 任何代码**。在 WebUI 新建 Provider 时选「OpenAI 流式兼容」即可，使用体验和官方 OpenAI Provider 几乎一样。

---

## ✨ 特性

- 🚀 **流式优先**（默认 `auto` 模式）：先把流式响应聚合成标准 `LLMResponse`，工具调用、推理内容、token 用量全在顶层，不丢任何信息
- 📦 **信封兜底**：流式失败或产出为空时，自动从 `model_extra` 信封路径解析内容
- 🧠 **推理字段兼容**：`reasoning` 字段自动映射为标准 `reasoning_content`（流式、非流式两条路径都兼容）
- 📊 **用量三位置提取**：独立 usage-only 事件 / 最后一个内容 chunk / `model_extra['usage']`，日志不再显示 None
- 🛠️ **路径全可配**：信封路径、内容路径、推理路径、工具调用路径、用量路径全部支持点分路径自定义，适配各种怪胎端点
- 🎙️ **全模型类型**：LLM / TTS / STT / 图像 / 嵌入，与官方 OpenAI Provider 能力对齐（STT 为 KiraAI v2.32.0+ 新增）
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
       ├── provider-schema.json
       ├── icon.svg
       └── icon-dark.svg
   ```
2. 重启 KiraAI（或 WebUI 里热重载插件）
3. 打开 WebUI → **模型提供商** → **新建 Provider**
4. **格式** 选择 **「OpenAI 流式兼容」**
5. 填好 **Base URL** 和 **API Key**
6. 保存后拉取/添加模型，把某个模型设置为默认 LLM 即可开聊

> 需要 Python 依赖：`openai`、`httpx`（KiraAI 本体已依赖，一般无需额外安装）
>
> **版本兼容**：适配 KiraAI `v2.32.0` 最新 OpenAI 官方 Provider（含 STT 语音转写支持）；旧版本（`v2.29.7`+）也能跑，STT 不可用时自动降级。
>
> **升级自 v1.0.x**：`schema.json` 已改为插件配置页描述，Provider 表单字段迁移到 `provider-schema.json`。插件启动时会自动回退读取旧布局，但建议直接使用本仓库最新文件（四个文件一起替换）。

---

## ⚙️ 配置项说明

### 插件配置页（WebUI → 附加功能 → OpenAI 流式兼容）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `default_mode`（默认桥接模式） | enum | `auto` | 新建模型时默认使用的桥接模式。`auto`：流式优先，失败自动退回信封解析（推荐）；`stream`：始终流式聚合；`envelope`：始终解析信封。单个模型可在其「桥接设置」里单独覆盖 |
| `debug_log`（调试日志） | switch | `false` | 开启后打印流式降级原因、usage 提取详情等排查信息，遇到端点适配问题时可临时打开 |

### Provider 级（新建 Provider 时填）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `base_url` | string | `https://api.openai.com/v1` | OpenAI 兼容端点的 Base URL |
| `api_key` | sensitive | 空 | 你的 API 密钥 |
| `headers`（高级设置） | json | `{}` | 自定义请求头，透传给 OpenAI 客户端 |

### 模型级（模型的高级设置 → 桥接设置）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `mode` | enum | `auto` | `auto`：流式优先，失败自动退回信封解析；`stream`：始终走流式聚合；`envelope`：始终解析信封。留空时继承插件配置页的默认模式 |
| `envelope_path` | string | `data` | 信封载荷在 `response.model_extra` 中的路径（点分）。**留空 = 显式禁用信封兜底**（仅从标准响应解析） |
| `content_path` | string | `choices.0.message.content` | 文本内容的点分路径（相对 response 或信封根） |
| `reasoning_path` | string | `choices.0.message.reasoning` | 推理内容的点分路径（`reasoning` 自动兼容 `reasoning_content`） |
| `tool_calls_path` | string | `choices.0.message.tool_calls` | 工具调用列表的点分路径 |
| `usage_path` | string | `usage` | token 用量对象的点分路径 |

> 所有桥接配置都在「桥接设置」里折叠着，**不填也能跑**（默认 `auto` 模式）。

STT 模型（v2.32.0+）另有 `timeout` / `language` / `prompt` 三个配置项，与官方 OpenAI Provider 一致。

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
A：使用方式一样，但 LLM 客户端额外做了「流式聚合 + 信封兜底 + 推理字段兼容 + 用量三位置提取」，遇到官方客户端解析为空 / 用量为 None 的情况它能救回来。

**Q：`auto` 模式流式失败了会怎样？**
A：按错误类型分级处理，不会盲目重试：
- **超时 / 连接失败**（`APITimeoutError` / `APIConnectionError`）→ 直接报错。与是否流式无关，重试信封只是再白等一轮
- **401 / 403 / 429**（密钥错误、无权限、限流）→ 直接报错。注定再失败
- **400 / 404 / 415 / 422 / 5xx** → 自动退到信封解析再试一次（可能是端点不支持 `stream` 参数这类可救的错误），日志会打 `falling back to envelope mode`
- 其余异常 → 自动退到信封解析

**Q：日志里 Input tokens 还是显示 None？**
A：先确认端点的流式响应里到底有没有用量字段：开启插件配置页的「调试日志」，观察 `debug: stream finished but no usage found` 是否出现。若有，说明端点连 `model_extra` 里都没塞用量，属于端点自身不返回，插件无能为力；若端点把用量放在别的路径（比如 `usage.prompt_tokens` 之外的怪名字），可以用模型「桥接设置」里的 `usage_path` 指向它。

**Q：我的端点信封结构不是 `data`，怎么改？**
A：看接口文档/抓包确认真实结构，改模型高级设置里的 `envelope_path`（及其余路径）。比如信封是 `model_extra['result']['payload']`，就填 `result.payload`。想完全禁用信封兜底，把 `envelope_path` 留空即可。

**Q：为什么有的端点推理字段叫 `reasoning`？**
A：非标准兼容实现。本插件默认路径就用 `reasoning`，但会自动尝试 `reasoning_content` 作为备选，两种都能读；流式 SSE 里的 `reasoning` 也会从 `delta.model_extra` 读取，不会丢推理内容。

---

## 🧩 工作原理（一句话版）

在插件加载时把 `OpenAIStreamCompatProvider` 注册进 `ProviderManager` 注册表（`_registry` / `_schemas` / `_manifests`），WebUI 即可选到「OpenAI 流式兼容」格式；LLM 客户端继承官方 `OpenAICompatibleLLMClient`，重写 `chat()`（流式优先、分级兜底）和 `chat_stream()`（流式推理字段兼容 + 用量三位置提取）：先聚合流式，不行就掏信封。其余模型类型（TTS / STT / 图像 / 嵌入）直接复用官方客户端，不重复造轮子。

`schema.json`（插件配置页）与 `provider-schema.json`（Provider 表单）职责分离：前者描述插件默认行为（默认模式 / 调试日志），后者描述 Provider 与模型级字段，避免 WebUI 插件配置页出现令人困惑的 Base URL 等 Provider 专用字段。

## 📄 许可证

[AGPL-3.0](LICENSE)

---

<details>
<summary>📜 更新日志（点击展开）</summary>

### v1.1.0（2026-08-30）

- 🐛 **修复：usage 字段名回退逻辑**。`usage.get("prompt_tokens", fallback)` 在端点返回 `prompt_tokens: null`（而非缺失）时会拿到 `null` 而不是回退值，改为 `is not None` 判断后再回退 `input_tokens` / `output_tokens`；`cached_tokens` 同款处理。流式（`_extract_usage`）与非流式（`_chat_envelope`）两条路径都已修复
- 🎯 **修复：token 数量为 0 不再被误判**。回退判断刻意用 `is not None` 而非 `or`，避免 `cached_tokens: 0`（合法值，代表无缓存命中）被当成缺失
- 🧩 **重构：schema 职责拆分**。新增 `provider-schema.json` 管理 Provider 表单与模型「桥接设置」字段，`schema.json` 只保留插件配置页描述；`initialize` 内置旧版布局回退，升级不会丢表单
- 🔄 **兼容：配置布局双支持**。插件配置读取兼容 section 嵌套与旧版扁平两种存储布局，section 嵌套优先，扁平字段仅作回退；WebUI 保存配置后热重载即时生效，不受旧配置残留影响
- 📐 **新增：模式继承链**。模型级 `section_bridge.mode` 优先 → 插件配置页默认模式 → `auto`，`_bridge_mode` 统一收敛
- 🔥 **新增：Provider 热重载补载**。插件启动时自动遍历已配置 Provider，对 `openai-stream-compat` 格式的既有实例补注册并重新实例化，无需手动重建
- 📄 **修正：许可证标注**。README 许可证由误写的 MIT 更正为 AGPL-3.0，与仓库 LICENSE 一致

### v1.0.x（初始版本）

- 🚀 **首发**：插件级 Provider 注册，流式优先 + 信封兜底，`reasoning` ↔ `reasoning_content` 字段兼容，流式用量提取
- 🛠️ **路径全可配**：信封 / 内容 / 推理 / 工具调用 / 用量路径支持点分自定义
- 🎙️ **全模型类型**：LLM / TTS / 图像 / 嵌入（STT 随 v2.32.0 可用后加入）

</details>

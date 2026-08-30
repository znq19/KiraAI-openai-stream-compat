"""OpenAI Stream Compat — KiraAI 流式兼容桥接 Provider 插件

为 KiraAI 提供 OpenAI 兼容端点的「流式优先 + 信封兜底」LLM 客户端：

- 部分端点（如 deepseek-v4-flash-vision-exp 的 cline 通道）的非流式响应
  会被整体塞进 model_extra['data'] 信封，顶层 choices 为空、usage 为 None，
  导致标准 OpenAICompatibleLLMClient 解析不到任何内容（拆信封问题）。
- 同一端点走流式 SSE 时反而输出标准格式。
- 部分端点推理字段名是 ``reasoning`` 而非 ``reasoning_content``，流式与
  非流式两条路径都做了兼容。

本插件以插件级 Provider 形式注册 ``openai-stream-compat`` 格式：
不改动 core 任何代码，在 WebUI 新建 Provider 时选择「OpenAI 流式兼容」即可。
"""

import json
import time
from pathlib import Path
from typing import AsyncGenerator

from openai import APIStatusError, APITimeoutError, APIConnectionError

from core.plugin import BasePlugin, logger
from core.provider import ModelType, BaseProvider, ModelInfo
from core.provider.llm_model import LLMRequest, LLMResponse, LLMStreamChunk
from core.provider.provider_manager import ProviderManager
from core.config.config_field import build_fields
from core.utils.model_clients import (
    OpenAICompatibleLLMClient,
    OpenAICompatibleTTSClient,
)
from core.utils.media_refs import resolve_media_references

try:  # 官方 openai provider 内置图片/嵌入客户端，缺失时降级为仅 LLM/TTS
    from core.provider.src.openai.model_clients import (
        OpenAIImageClient,
        OpenAIEmbeddingClient,
    )
except Exception:  # pragma: no cover
    OpenAIImageClient = None
    OpenAIEmbeddingClient = None

PLUGIN_DIR = Path(__file__).resolve().parent
PROVIDER_FORMAT = "openai-stream-compat"

DEFAULT_ENVELOPE_PATH = "data"
DEFAULT_CONTENT_PATH = "choices.0.message.content"
DEFAULT_REASONING_PATH = "choices.0.message.reasoning"
DEFAULT_TOOL_CALLS_PATH = "choices.0.message.tool_calls"
DEFAULT_USAGE_PATH = "usage"


def _dig(obj, path_str):
    """按 'a.b.c' / 'a.0.b' 路径在 dict / 对象(model_extra) 混合结构中取嵌套值。"""
    if not path_str:
        return None
    cur = obj
    for part in path_str.split("."):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            try:
                cur = cur[int(part)]
            except (TypeError, IndexError, KeyError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        elif hasattr(cur, part):
            cur = getattr(cur, part)
        elif getattr(cur, "model_extra", None) and isinstance(cur.model_extra, dict):
            cur = cur.model_extra.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _bridge_cfg(model: ModelInfo) -> dict:
    mcfg = model.model_config or {}
    return mcfg.get("section_bridge") or {}


def _to_dict(obj):
    """pydantic 对象 / 普通对象 → dict（解析工具调用等嵌套结构用）"""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return vars(obj)
    return None


class OpenAIStreamCompatLLMClient(OpenAICompatibleLLMClient):
    """流式优先 + 信封兜底的 OpenAI 兼容 LLM 客户端。"""

    def __init__(self, model: ModelInfo):
        super().__init__(model)

    async def chat(self, request: LLMRequest, **kwargs) -> LLMResponse:
        mode = (_bridge_cfg(self.model).get("mode") or "auto").strip().lower()
        if mode == "stream":
            return await self._chat_stream_aggregate(request, **kwargs)
        if mode == "envelope":
            return await self._chat_envelope(request, **kwargs)
        # auto（默认）：流式优先，失败或产出异常时降级为信封解析
        try:
            resp = await self._chat_stream_aggregate(request, **kwargs)
            if not (resp.text_response or resp.reasoning_content or resp.tool_calls):
                raise ValueError("stream produced empty response, fallback to envelope")
            return resp
        except (APITimeoutError, APIConnectionError):
            # 超时 / 连接失败与是否流式无关，重试信封只会再白等一轮 → 直接抛出
            raise
        except APIStatusError as e:
            # 认证 / 限流类错误与是否流式无关，重试信封注定再失败 → 直接抛出
            if e.status_code in (401, 403, 429):
                raise
            # 400/404/415/422/5xx 可能是端点不支持 stream 参数等 → 值得走信封试一次
            logger.warning(
                f"[openai-stream-compat] stream failed (HTTP {e.status_code}), "
                "falling back to envelope mode"
            )
            return await self._chat_envelope(request, **kwargs)
        except Exception:
            logger.warning(
                "[openai-stream-compat] stream failed, falling back to envelope mode"
            )
            return await self._chat_envelope(request, **kwargs)

    async def _chat_stream_aggregate(self, request: LLMRequest, **kwargs) -> LLMResponse:
        kwargs.pop("stream", None)
        start = time.perf_counter()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        async for chunk in self.chat_stream(request, **kwargs):
            if chunk.delta_text:
                text_parts.append(chunk.delta_text)
            if chunk.delta_reasoning:
                reasoning_parts.append(chunk.delta_reasoning)
            for frag in chunk.tool_calls_delta:
                idx = frag.get("index", 0)
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if frag.get("id"):
                    acc["id"] = frag["id"]
                fn = frag.get("function") or {}
                if fn.get("name"):
                    acc["name"] = fn["name"]
                if fn.get("arguments"):
                    acc["arguments"] += fn["arguments"]
            if chunk.usage:
                usage = chunk.usage
        resp = LLMResponse("".join(text_parts))
        resp.reasoning_content = "".join(reasoning_parts)
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            resp.tool_calls.append({
                "id": acc["id"],
                "type": "function",
                "function": {"name": acc["name"], "arguments": acc["arguments"]},
            })
        if usage:
            resp.input_tokens = usage.get("input_tokens")
            resp.output_tokens = usage.get("output_tokens")
            resp.cached_tokens = usage.get("cached_tokens")
        resp.time_consumed = round(time.perf_counter() - start, 2)
        return resp

    async def chat_stream(self, request: LLMRequest, **kwargs) -> AsyncGenerator[LLMStreamChunk, None]:
        """重写父类 chat_stream：额外兼容流式事件里推理字段名为 reasoning
        而非标准 reasoning_content 的端点（从 delta.model_extra 读取）。"""
        client = self._build_client()
        messages = await resolve_media_references(request.messages)
        request_kwargs = self._build_request_kwargs(
            request, messages=messages, stream=True, **kwargs
        )
        request_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await client.chat.completions.create(**request_kwargs)
            async for event in stream:
                # Usage-only event (sent by OpenAI API after the final choice chunk)
                if not event.choices:
                    if event.usage:
                        prompt_details = getattr(event.usage, "prompt_tokens_details", None)
                        cached = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
                        yield LLMStreamChunk(
                            is_final=True,
                            usage={
                                "input_tokens": event.usage.prompt_tokens,
                                "output_tokens": event.usage.completion_tokens,
                                "cached_tokens": cached,
                            },
                        )
                    continue

                choice = event.choices[0]
                delta = choice.delta

                chunk = LLMStreamChunk()

                # Text content
                if delta.content:
                    chunk.delta_text = delta.content

                # Reasoning content: standard reasoning_content, or non-standard
                # reasoning stashed in delta.model_extra (this plugin's raison d'être)
                reasoning = getattr(delta, "reasoning_content", "") or ""
                if not reasoning:
                    extra = getattr(delta, "model_extra", None)
                    if isinstance(extra, dict):
                        reasoning = extra.get("reasoning", "") or ""
                if reasoning:
                    chunk.delta_reasoning = reasoning

                # Tool calls — pass through raw incremental deltas from SDK
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        fragment = {
                            "index": tc.index,
                            "id": tc.id or "",
                            "type": "function",
                            "function": {
                                "name": tc.function.name if tc.function and tc.function.name else "",
                                "arguments": tc.function.arguments if tc.function and tc.function.arguments else "",
                            },
                        }
                        chunk.tool_calls_delta.append(fragment)

                # Finish reason
                finish_reason = choice.finish_reason
                if finish_reason:
                    chunk.is_final = True
                    chunk.finish_reason = finish_reason

                yield chunk

        except APIStatusError:
            raise
        except APITimeoutError:
            raise
        except APIConnectionError:
            raise
        except Exception:
            raise

    async def _chat_envelope(self, request: LLMRequest, **kwargs) -> LLMResponse:
        client = self._build_client()
        messages = await resolve_media_references(request.messages)
        kwargs.pop("stream", None)
        request_kwargs = self._build_request_kwargs(request, messages=messages, **kwargs)

        start = time.perf_counter()
        response = await client.chat.completions.create(**request_kwargs)
        elapsed = round(time.perf_counter() - start, 2)

        bridge = _bridge_cfg(self.model)
        env_path_raw = bridge.get("envelope_path")
        if env_path_raw is None:
            env_path = DEFAULT_ENVELOPE_PATH
        elif str(env_path_raw).strip() == "":
            env_path = None  # 空串 = 显式禁用信封兜底
        else:
            env_path = str(env_path_raw).strip()
        content_path = bridge.get("content_path") or DEFAULT_CONTENT_PATH
        reasoning_path = bridge.get("reasoning_path") or DEFAULT_REASONING_PATH
        tool_calls_path = bridge.get("tool_calls_path") or DEFAULT_TOOL_CALLS_PATH
        usage_path = bridge.get("usage_path") or DEFAULT_USAGE_PATH

        resp = LLMResponse("")
        resp.time_consumed = elapsed

        # 信封根：model_extra[envelope_path]（可能为 list，取首个）
        extra = getattr(response, "model_extra", None)
        envelope = None
        if env_path and isinstance(extra, dict):
            envelope = extra.get(env_path)
        if isinstance(envelope, list) and envelope:
            envelope = envelope[0]

        # 标准路径取不到时，退回从信封取（路径相对信封根）
        def _extract(resp_obj, path, alt_path=None):
            val = _dig(resp_obj, path)
            if val is not None:
                return val
            if envelope is not None:
                val = _dig(envelope, path)
                if val is not None:
                    return val
                if alt_path:
                    val = _dig(envelope, alt_path)
                    if val is not None:
                        return val
            return None

        # 推理字段名兼容：reasoning → reasoning_content
        alt_reasoning = None
        if reasoning_path.endswith(".reasoning"):
            alt_reasoning = reasoning_path[:-len(".reasoning")] + ".reasoning_content"

        content = _extract(response, content_path)
        reasoning = _extract(response, reasoning_path, alt_reasoning)
        tool_calls = _extract(response, tool_calls_path)
        usage = _extract(response, usage_path)

        if content is not None:
            resp.text_response = content if isinstance(content, str) else str(content)
        if reasoning is not None:
            resp.reasoning_content = reasoning if isinstance(reasoning, str) else str(reasoning)

        if tool_calls:
            for tc in tool_calls:
                d = _to_dict(tc)
                if not d:
                    continue
                fn = d.get("function") or {}
                resp.tool_calls.append({
                    "id": d.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", "") if isinstance(fn, dict) else "",
                        "arguments": fn.get("arguments", "") if isinstance(fn, dict) else "",
                    },
                })

        if isinstance(usage, dict):
            resp.input_tokens = usage.get("prompt_tokens")
            resp.output_tokens = usage.get("completion_tokens")
            details = usage.get("prompt_tokens_details") or {}
            resp.cached_tokens = details.get("cached_tokens")
        elif usage is not None:
            resp.input_tokens = getattr(usage, "prompt_tokens", None)
            resp.output_tokens = getattr(usage, "completion_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                resp.cached_tokens = getattr(details, "cached_tokens", None)

        if not (resp.text_response or resp.reasoning_content or resp.tool_calls):
            logger.warning(
                "[openai-stream-compat] envelope parse returned empty; "
                "check envelope/content paths for this endpoint"
            )
        return resp


class OpenAIStreamCompatProvider(BaseProvider):
    """OpenAI 流式兼容 Provider"""

    models = {
        ModelType.LLM: OpenAIStreamCompatLLMClient,
        ModelType.TTS: OpenAICompatibleTTSClient,
    }
    if OpenAIImageClient is not None:
        models[ModelType.IMAGE] = OpenAIImageClient
    if OpenAIEmbeddingClient is not None:
        models[ModelType.EMBEDDING] = OpenAIEmbeddingClient

    def __init__(self, provider_id, provider_name, provider_config):
        super().__init__(provider_id, provider_name, provider_config)

    async def get_llm_list(self) -> list[dict]:
        """Fetch available models from the OpenAI-compatible API (GET /v1/models)."""
        import httpx

        base_url = self.provider_config.get("base_url", "https://api.openai.com/v1").rstrip("/")
        api_key = self.provider_config.get("api_key", "")
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()

        models = []
        for item in data.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue
            models.append({
                "id": model_id,
                "name": item.get("name", model_id),
                "description": item.get("description", ""),
            })
        return models


class OpenAIStreamCompatPlugin(BasePlugin):
    """插件入口：把流式兼容 Provider 注册进 ProviderManager"""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

    async def initialize(self):
        manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
        raw_schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))

        provider_fields = build_fields(raw_schema.get("provider_config") or {})
        model_fields = {}
        for mt, fs in (raw_schema.get("model_config") or {}).items():
            if isinstance(fs, dict):
                model_fields[mt] = build_fields(fs)

        ProviderManager._registry[PROVIDER_FORMAT] = OpenAIStreamCompatProvider
        ProviderManager._manifests[PROVIDER_FORMAT] = manifest
        ProviderManager._manifest_dirs[PROVIDER_FORMAT] = PLUGIN_DIR
        ProviderManager._schemas[PROVIDER_FORMAT] = {
            "provider_config": provider_fields,
            "model_config": model_fields,
        }

        # 框架启动时该 format 若已配置但类尚未注册，这里补实例化
        providers = self.ctx.config.get("providers", {}) or {}
        reloaded = 0
        for pid, pcfg in providers.items():
            if isinstance(pcfg, dict) and pcfg.get("format") == PROVIDER_FORMAT:
                try:
                    self.ctx.provider_mgr.set_provider(pid, pcfg)
                    reloaded += 1
                except Exception as e:
                    logger.error(f"[openai-stream-compat] reload provider {pid} failed: {e}")
        logger.info(f"[openai-stream-compat] registered, reloaded {reloaded} instance(s)")

    async def terminate(self):
        pass

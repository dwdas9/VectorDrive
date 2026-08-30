"""Bounded, local-only Ollama chat transport.

The desktop GUI is allowed to talk to Ollama, but document text and chat
prompts must never be sent to a remote host by a configuration mistake.  This
module therefore accepts only explicit loopback endpoints and constructs its
own ``httpx.Client`` with proxy-environment inheritance and redirects disabled.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from vectordrive.services.errors import VectorDriveError

ChatRole = Literal["system", "user", "assistant"]

DEFAULT_MAX_INPUT_BYTES = 512 * 1024
DEFAULT_MAX_RECORD_BYTES = 256 * 1024
DEFAULT_MAX_STREAM_RECORDS = 10_000
DEFAULT_MAX_OUTPUT_CHARS = 2_000_000
MAX_TAGS_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SHOW_RESPONSE_BYTES = 512 * 1024
MODEL_INSPECTION_TIMEOUT_S = 2.0
MODEL_DISCOVERY_BUDGET_S = 10.0
MAX_INSTALLED_MODELS = 256
MAX_MODEL_NAME_CHARS = 256

_BASE_MODEL_TOKEN = re.compile(r"(?:^|[._:/-])base(?:$|[._:/-])", re.IGNORECASE)


def _is_chat_suitable_model(name: str, capabilities: list) -> bool:
    """Reject completion models that lack a conversational instruction tune."""
    capability_names = {
        capability.casefold() for capability in capabilities if isinstance(capability, str)
    }
    if "completion" not in capability_names:
        return False
    if _BASE_MODEL_TOKEN.search(name):
        return False
    # ``insert`` identifies fill-in-the-middle completion behavior. Those
    # models do not reliably honor role-based chat even if future Ollama
    # versions advertise additional capabilities alongside it.
    if "insert" in capability_names:
        return False
    return True


class OllamaChatError(VectorDriveError):
    """A bounded, user-displayable Ollama chat transport failure."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code="ollama_chat_error", hint=hint)


@dataclass(frozen=True)
class OllamaMessage:
    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError(f"Unsupported Ollama chat role: {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("Ollama message content must be a string")


@dataclass(frozen=True)
class OllamaModel:
    name: str
    size: int | None = None
    modified_at: str | None = None


def _normalize_loopback_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("Ollama endpoint must be a non-empty loopback URL")

    try:
        parsed = urlsplit(endpoint.strip())
        port = parsed.port  # Access validates an invalid/non-numeric port.
    except ValueError as exc:
        raise ValueError(f"Invalid Ollama endpoint: {exc}") from exc

    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Ollama endpoint must be an HTTP(S) loopback URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama endpoint must not contain user credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Ollama endpoint must not contain a path, query, or fragment")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if not is_loopback:
        raise ValueError("Ollama chat is restricted to an explicit loopback endpoint")

    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}"


class OllamaChatClient:
    """Small synchronous client suitable for a background GUI worker."""

    def __init__(
        self,
        endpoint: str,
        *,
        client=None,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_stream_records: int = DEFAULT_MAX_STREAM_RECORDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        self.endpoint = _normalize_loopback_endpoint(endpoint)
        for name, value in (
            ("max_input_bytes", max_input_bytes),
            ("max_record_bytes", max_record_bytes),
            ("max_stream_records", max_stream_records),
            ("max_output_chars", max_output_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_input_bytes = max_input_bytes
        self.max_record_bytes = max_record_bytes
        self.max_stream_records = max_stream_records
        self.max_output_chars = max_output_chars
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            base_url=self.endpoint,
            timeout=httpx.Timeout(connect=3.0, read=120.0, write=10.0, pool=3.0),
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=15.0,
            ),
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OllamaChatClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _require_success(response, operation: str) -> None:
        status = getattr(response, "status_code", 0)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise OllamaChatError(
                f"Ollama {operation} returned HTTP {status}.",
                hint="Check that Ollama is running locally and try again.",
            )

    @staticmethod
    def _read_bounded_json(response, operation: str, max_bytes: int):
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=min(64 * 1024, max_bytes + 1)):
            if not isinstance(chunk, bytes):
                raise OllamaChatError(f"Ollama returned invalid {operation} response bytes.")
            if len(body) + len(chunk) > max_bytes:
                raise OllamaChatError(f"Ollama {operation} response exceeded the safe size limit.")
            body.extend(chunk)
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OllamaChatError(f"Ollama returned malformed {operation} JSON.") from exc

    def list_models(self) -> list[OllamaModel]:
        """Return installed models suitable for role-based conversation.

        ``/api/tags`` describes installed models but not their capabilities,
        so each bounded tag is checked via ``/api/show``.  Models advertising
        only ``embedding`` or raw base/FIM behavior are never offered in the
        chat picker.
        """
        try:
            with self._client.stream(
                "GET",
                "/api/tags",
                follow_redirects=False,
                timeout=MODEL_INSPECTION_TIMEOUT_S,
            ) as response:
                self._require_success(response, "model listing")
                payload = self._read_bounded_json(
                    response, "model listing", MAX_TAGS_RESPONSE_BYTES
                )
        except OllamaChatError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OllamaChatError(
                f"Could not read Ollama's installed models: {type(exc).__name__}.",
                hint="Start Ollama, then refresh the model list.",
            ) from exc

        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise OllamaChatError("Ollama's model response did not contain a models list.")
        if len(models) > MAX_INSTALLED_MODELS:
            raise OllamaChatError(
                f"Ollama returned too many installed models (maximum {MAX_INSTALLED_MODELS})."
            )

        completion_models: list[OllamaModel] = []
        seen: set[str] = set()
        discovery_deadline = time.monotonic() + MODEL_DISCOVERY_BUDGET_S
        for item in models:
            if not isinstance(item, dict):
                raise OllamaChatError("Ollama returned malformed model metadata.")
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name.strip() or len(name) > MAX_MODEL_NAME_CHARS:
                raise OllamaChatError("Ollama returned an invalid model name.")
            name = name.strip()
            if name in seen:
                continue
            seen.add(name)
            capabilities = item.get("capabilities")
            if not isinstance(capabilities, list):
                if time.monotonic() >= discovery_deadline:
                    break
                try:
                    with self._client.stream(
                        "POST",
                        "/api/show",
                        json={"model": name},
                        follow_redirects=False,
                        timeout=MODEL_INSPECTION_TIMEOUT_S,
                    ) as detail_response:
                        self._require_success(detail_response, f"model inspection for {name!r}")
                        details = self._read_bounded_json(
                            detail_response, f"model inspection for {name!r}", MAX_SHOW_RESPONSE_BYTES
                        )
                    capabilities = details.get("capabilities") if isinstance(details, dict) else None
                except (OllamaChatError, httpx.HTTPError, ValueError, TypeError):
                    # A single damaged/unavailable model should not make every
                    # other installed completion model disappear from Chat.
                    continue
            if not isinstance(capabilities, list) or not _is_chat_suitable_model(
                name, capabilities
            ):
                continue
            raw_size = item.get("size")
            size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else None
            raw_modified = item.get("modified_at")
            modified_at = raw_modified if isinstance(raw_modified, str) else None
            completion_models.append(OllamaModel(name=name, size=size, modified_at=modified_at))

        return sorted(completion_models, key=lambda model: (model.name.casefold(), model.name))

    def stream_chat(
        self,
        model: str,
        messages: Sequence[OllamaMessage],
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Iterator[str]:
        """Yield assistant text deltas from Ollama's NDJSON chat stream.

        Cancellation is cooperative: the callback is checked before the
        request, before every transport chunk, and between records. Returning
        from the generator closes the response context immediately.
        """
        if not isinstance(model, str) or not model.strip() or len(model) > MAX_MODEL_NAME_CHARS:
            raise ValueError("Chat model must be a non-empty installed model name")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of OllamaMessage values")
        if not messages:
            raise ValueError("Chat messages must not be empty")

        serialized_messages: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, OllamaMessage):
                raise TypeError("Every chat message must be an OllamaMessage")
            serialized_messages.append({"role": message.role, "content": message.content})
        payload = {"model": model.strip(), "messages": serialized_messages, "stream": True}
        input_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if input_bytes > self.max_input_bytes:
            raise ValueError(
                f"Chat input is too large ({input_bytes} bytes; maximum {self.max_input_bytes})."
            )
        if should_cancel is not None and should_cancel():
            return

        try:
            with self._client.stream(
                "POST",
                "/api/chat",
                json=payload,
                headers={"Accept": "application/x-ndjson"},
                follow_redirects=False,
            ) as response:
                self._require_success(response, "chat request")
                yield from self._read_stream(response, should_cancel)
        except OllamaChatError:
            raise
        except httpx.TimeoutException as exc:
            raise OllamaChatError(
                "Ollama chat timed out.", hint="Retry, or choose a smaller local model."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaChatError(
                f"Could not connect to local Ollama: {type(exc).__name__}.",
                hint="Start Ollama and try again.",
            ) from exc

    def _read_stream(
        self,
        response,
        should_cancel: Callable[[], bool] | None,
    ) -> Iterator[str]:
        buffer = bytearray()
        records = 0
        output_chars = 0
        saw_done = False

        def cancelled() -> bool:
            return should_cancel is not None and should_cancel()

        for chunk in response.iter_bytes(chunk_size=min(64 * 1024, self.max_record_bytes + 1)):
            if cancelled():
                return
            if not isinstance(chunk, bytes):
                raise OllamaChatError("Ollama returned a non-byte stream chunk.")
            if b"\n" not in chunk and len(buffer) + len(chunk) > self.max_record_bytes:
                raise OllamaChatError("Ollama returned an oversized NDJSON record.")
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw_record = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if cancelled():
                    return
                records += 1
                delta, done = self._decode_record(raw_record, records)
                output_chars += len(delta)
                if output_chars > self.max_output_chars:
                    raise OllamaChatError("Ollama chat output exceeded the safe size limit.")
                if delta:
                    yield delta
                if cancelled():
                    return
                if done:
                    saw_done = True
                    return
            if len(buffer) > self.max_record_bytes:
                raise OllamaChatError("Ollama returned an oversized NDJSON record.")

        if cancelled():
            return
        if buffer:
            records += 1
            delta, done = self._decode_record(bytes(buffer), records)
            output_chars += len(delta)
            if output_chars > self.max_output_chars:
                raise OllamaChatError("Ollama chat output exceeded the safe size limit.")
            if delta:
                yield delta
            saw_done = done
        if not saw_done:
            raise OllamaChatError("Ollama chat stream ended before its final record.")

    def _decode_record(self, raw_record: bytes, record_number: int) -> tuple[str, bool]:
        if record_number > self.max_stream_records:
            raise OllamaChatError("Ollama chat stream returned too many records.")
        if len(raw_record) > self.max_record_bytes:
            raise OllamaChatError("Ollama returned an oversized NDJSON record.")
        if not raw_record.strip():
            return "", False
        try:
            record = json.loads(raw_record)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OllamaChatError("Ollama returned malformed NDJSON chat data.") from exc
        if not isinstance(record, dict):
            raise OllamaChatError("Ollama returned a non-object NDJSON record.")
        error = record.get("error")
        if error:
            safe_error = str(error)[:300]
            raise OllamaChatError(f"Ollama chat failed: {safe_error}")
        message = record.get("message")
        if message is None:
            delta = ""
        elif isinstance(message, dict) and isinstance(message.get("content", ""), str):
            delta = message.get("content", "")
        else:
            raise OllamaChatError("Ollama returned malformed assistant message data.")
        return delta, record.get("done") is True

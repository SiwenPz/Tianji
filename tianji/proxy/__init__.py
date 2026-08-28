"""Transparent protocol conversion layer + pool proxy for Tianji.

Public names come from three siblings:
- _pool   — CircuitBreaker / PoolRouter / HTTP handler / run_proxy
- convert — non-streaming Anthropic ↔ OpenAI conversion
- stream  — SSE (streaming) bidirectional conversion
"""

from ._pool import (  # noqa: F401
    _forward_http,
    CircuitBreaker,
    PoolRouter,
    _ForwardError,
    _build_fwd_headers,
    _cfg,
    _pool_json,
    _pool_token,
    _ProxyHandler,
    _verify_token,
    run_proxy,
)
from .convert import convert_request, convert_response  # noqa: F401
from .stream import stream_convert  # noqa: F401

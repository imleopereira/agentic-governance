"""LangChain CallbackHandler that bridges callback hooks to governance events.

Usage::

    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.integrations.langchain_handler import GovernanceCallbackHandler

    sdk = GovernanceSDK(database_url="postgresql://...")
    handler = GovernanceCallbackHandler(sdk=sdk, agent_id="my-agent")
    # Pass handler to LangChain as a callback

The handler is an OBSERVATION surface: it never raises to LangChain even if
the governance SDK has an internal failure. Every callback body is wrapped in
try/except that logs and continues.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import structlog

try:
    from langchain_core.callbacks import BaseCallbackHandler  # type: ignore[import-not-found]
except ImportError:

    class BaseCallbackHandler:  # type: ignore[no-redef]
        """Stub when langchain-core is not installed."""


from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.scope.errors import ScopeViolation

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeatelier_governance.sdk import GovernanceSDK

logger = structlog.get_logger(__name__)


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from a sync context, handling event loop scenarios."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # We're inside a running loop — schedule as a task.
        # This happens when LangChain calls sync callbacks from async code.
        future = asyncio.ensure_future(coro)
        return future
    else:
        return asyncio.run(coro)


class GovernanceCallbackHandler(BaseCallbackHandler):  # type: ignore[misc]
    """LangChain callback handler that maps hooks to governance audit events.

    All callbacks are wrapped in try/except: the handler never raises to
    LangChain. Scope violations are logged as audit events but not raised.

    Args:
        sdk: The initialized GovernanceSDK instance.
        agent_id: The agent identifier for audit and scope checks.
    """

    def __init__(
        self,
        sdk: "GovernanceSDK",
        agent_id: str,
        enforce: bool = False,
        session_id: UUID | None = None,
    ) -> None:
        super().__init__()
        self._sdk = sdk
        self._agent_id = agent_id
        self._enforce = enforce
        self._session_id = session_id if session_id is not None else uuid4()
        self._last_model: str | None = None

    # -- helpers ---------------------------------------------------------------

    async def _audit_log(self, kind: str, metadata: dict[str, Any] | None = None) -> None:
        """Log an audit event, swallowing all errors."""
        try:
            await self._sdk.audit.log(
                AuditEvent(
                    agent_id=self._agent_id,
                    kind=kind,
                    metadata=metadata or {},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.audit_log_failed",
                kind=kind,
                error_type=type(exc).__name__,
            )

    async def _scope_check(self, tool_name: str) -> None:
        """Run a scope check. Logs violations; re-raises when enforce=True."""
        try:
            await self._sdk.scope.check(self._agent_id, tool=tool_name)
        except ScopeViolation:
            # Already logged by scope module as scope.violation audit event.
            logger.warning(
                "governance.langchain_handler.scope_violation",
                agent_id=self._agent_id,
                tool=tool_name,
            )
            if self._enforce:
                raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.scope_check_failed",
                tool=tool_name,
                error_type=type(exc).__name__,
            )

    async def _loop_record(self, tool_name: str) -> None:
        """Record a tool call for loop detection. Swallows all errors.

        Only fires when a ``session_id`` was provided to the handler at
        construction time.  Pass ``session_id=`` to GovernanceCallbackHandler
        to enable per-session loop tracking.
        """
        if self._session_id is None:
            return
        loop_module = getattr(self._sdk, "loop", None)
        if loop_module is None:
            return
        try:
            await loop_module.record_call(self._agent_id, self._session_id, tool_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.loop_record_failed",
                tool=tool_name,
                error_type=type(exc).__name__,
            )

    async def _cost_track(self, tokens: int, usd: float) -> None:
        """Track cost, swallowing all errors."""
        try:
            await self._sdk.cost.track(
                self._agent_id, self._session_id, tokens=tokens, usd=usd
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.cost_track_failed",
                error_type=type(exc).__name__,
            )

    # -- sync callbacks --------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM call starts.

        If the serialized LLM payload contains a ``tools`` list, hidden
        tools are filtered out before the call reaches the model.
        """
        try:
            model = serialized.get("name", serialized.get("id", ["unknown"])[-1])
            # Also check invocation_params for the model name (more reliable)
            invocation_params = kwargs.get("invocation_params", {})
            if isinstance(invocation_params, dict):
                model = invocation_params.get("model_name", invocation_params.get("model", model))
                tools = invocation_params.get("tools")
                if isinstance(tools, list):
                    tool_names = [
                        t.get("function", {}).get("name", "")
                        if isinstance(t, dict)
                        else ""
                        for t in tools
                    ]
                    filtered = self._sdk.scope.filter_tools(self._agent_id, tool_names)
                    if len(filtered) < len(tool_names):
                        hidden = set(tool_names) - set(filtered)
                        invocation_params["tools"] = [
                            t
                            for t in tools
                            if not (
                                isinstance(t, dict)
                                and t.get("function", {}).get("name", "") in hidden
                            )
                        ]
            self._last_model = str(model)
            _run_async(
                self._audit_log(
                    "llm.call",
                    {"model": str(model), "prompt_count": len(prompts)},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_llm_start_failed",
                error_type=type(exc).__name__,
            )

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM call ends."""
        try:
            token_usage: dict[str, Any] = {}
            if hasattr(response, "llm_output") and isinstance(response.llm_output, dict):
                token_usage = response.llm_output.get("token_usage", {})

            total_tokens = int(token_usage.get("total_tokens", 0))
            usd = 0.0
            if self._last_model and total_tokens > 0:
                from codeatelier_governance.cost.pricing import estimate_cost

                prompt_tokens = int(token_usage.get("prompt_tokens", 0))
                completion_tokens = int(token_usage.get("completion_tokens", 0))
                usd = estimate_cost(self._last_model, prompt_tokens, completion_tokens)
            _run_async(
                self._audit_log("llm.result", {"token_usage": token_usage})
            )
            if total_tokens > 0:
                _run_async(self._cost_track(total_tokens, usd))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_llm_end_failed",
                error_type=type(exc).__name__,
            )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM call errors."""
        try:
            _run_async(
                self._audit_log(
                    "llm.error", {"error_type": type(error).__name__}
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_llm_error_failed",
                error_type=type(exc).__name__,
            )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool starts. Runs a scope check and loop detection."""
        try:
            tool_name = serialized.get("name", "unknown")
            _run_async(self._scope_check(str(tool_name)))
            _run_async(self._loop_record(str(tool_name)))
            _run_async(
                self._audit_log("tool.call", {"tool": str(tool_name)})
            )
        except ScopeViolation:
            if self._enforce:
                raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_tool_start_failed",
                error_type=type(exc).__name__,
            )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool ends."""
        try:
            _run_async(self._audit_log("tool.result"))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_tool_end_failed",
                error_type=type(exc).__name__,
            )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool errors."""
        try:
            _run_async(
                self._audit_log(
                    "tool.error", {"error_type": type(error).__name__}
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_tool_error_failed",
                error_type=type(exc).__name__,
            )

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain starts."""
        try:
            chain_name = serialized.get("name", serialized.get("id", ["unknown"])[-1])
            _run_async(
                self._audit_log("chain.start", {"chain": str(chain_name)})
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_chain_start_failed",
                error_type=type(exc).__name__,
            )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain ends."""
        try:
            _run_async(self._audit_log("chain.end"))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_chain_end_failed",
                error_type=type(exc).__name__,
            )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain errors."""
        try:
            _run_async(
                self._audit_log(
                    "chain.error", {"error_type": type(error).__name__}
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.on_chain_error_failed",
                error_type=type(exc).__name__,
            )

    # -- async callbacks -------------------------------------------------------

    async def aon_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_llm_start."""
        try:
            model = serialized.get("name", serialized.get("id", ["unknown"])[-1])
            invocation_params = kwargs.get("invocation_params", {})
            if isinstance(invocation_params, dict):
                model = invocation_params.get("model_name", invocation_params.get("model", model))
            self._last_model = str(model)
            await self._audit_log(
                "llm.call",
                {"model": str(model), "prompt_count": len(prompts)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_llm_start_failed",
                error_type=type(exc).__name__,
            )

    async def aon_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_llm_end."""
        try:
            token_usage: dict[str, Any] = {}
            if hasattr(response, "llm_output") and isinstance(response.llm_output, dict):
                token_usage = response.llm_output.get("token_usage", {})

            total_tokens = int(token_usage.get("total_tokens", 0))
            usd = 0.0
            if self._last_model and total_tokens > 0:
                from codeatelier_governance.cost.pricing import estimate_cost

                prompt_tokens = int(token_usage.get("prompt_tokens", 0))
                completion_tokens = int(token_usage.get("completion_tokens", 0))
                usd = estimate_cost(self._last_model, prompt_tokens, completion_tokens)
            # Run audit log + cost track concurrently (both are observation surfaces)
            coros: list[Any] = [self._audit_log("llm.result", {"token_usage": token_usage})]
            if total_tokens > 0:
                coros.append(self._cost_track(total_tokens, usd))
            await asyncio.gather(*coros)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_llm_end_failed",
                error_type=type(exc).__name__,
            )

    async def aon_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_llm_error."""
        try:
            await self._audit_log(
                "llm.error", {"error_type": type(error).__name__}
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_llm_error_failed",
                error_type=type(exc).__name__,
            )

    async def aon_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_tool_start."""
        try:
            tool_name = serialized.get("name", "unknown")
            await self._scope_check(str(tool_name))
            await self._loop_record(str(tool_name))
            await self._audit_log("tool.call", {"tool": str(tool_name)})
        except ScopeViolation:
            if self._enforce:
                raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_tool_start_failed",
                error_type=type(exc).__name__,
            )

    async def aon_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_tool_end."""
        try:
            await self._audit_log("tool.result")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_tool_end_failed",
                error_type=type(exc).__name__,
            )

    async def aon_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_tool_error."""
        try:
            await self._audit_log(
                "tool.error", {"error_type": type(error).__name__}
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_tool_error_failed",
                error_type=type(exc).__name__,
            )

    async def aon_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_chain_start."""
        try:
            chain_name = serialized.get("name", serialized.get("id", ["unknown"])[-1])
            await self._audit_log("chain.start", {"chain": str(chain_name)})
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_chain_start_failed",
                error_type=type(exc).__name__,
            )

    async def aon_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_chain_end."""
        try:
            await self._audit_log("chain.end")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_chain_end_failed",
                error_type=type(exc).__name__,
            )

    async def aon_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Async variant of on_chain_error."""
        try:
            await self._audit_log(
                "chain.error", {"error_type": type(error).__name__}
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.langchain_handler.aon_chain_error_failed",
                error_type=type(exc).__name__,
            )

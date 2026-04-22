"""Tests for Commit 4 Claude Code hard routing in gateway.run.

These tests cover the pre-reasoning routing hook that can divert free-text
planning/coding/review requests to the home-level Claude Code router script,
while preserving the existing command path and refusing silent GPT fallback
for coding/review failures.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_planning_free_text_routes_before_agent(monkeypatch):
    runner = _make_runner()
    routed = AsyncMock(return_value="claude plan response")
    agent = AsyncMock(side_effect=AssertionError("should not hit native agent"))
    runner._maybe_route_to_claude_code = routed  # type: ignore[attr-defined]
    runner._handle_message_with_agent = agent  # type: ignore[attr-defined]

    result = await runner._handle_message(_make_event("plan the next commit"))

    assert result == "claude plan response"
    routed.assert_awaited_once()
    agent.assert_not_called()


@pytest.mark.asyncio
async def test_non_command_falls_through_to_native_when_router_returns_none(monkeypatch):
    runner = _make_runner()
    routed = AsyncMock(return_value=None)
    agent = AsyncMock(return_value="native response")
    runner._maybe_route_to_claude_code = routed  # type: ignore[attr-defined]
    runner._handle_message_with_agent = agent  # type: ignore[attr-defined]

    result = await runner._handle_message(_make_event("just a normal message"))

    assert result == "native response"
    routed.assert_awaited_once()
    agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_command_does_not_call_router(monkeypatch):
    import gateway.run as gateway_run

    runner = _make_runner()
    runner._maybe_route_to_claude_code = AsyncMock(return_value="should-not-route")  # type: ignore[attr-defined]
    runner._run_agent = AsyncMock(side_effect=AssertionError("unknown slash leaked to agent"))

    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )

    result = await runner._handle_message(_make_event("/definitely-not-a-command"))

    assert result is not None
    assert "Unknown command" in result
    runner._maybe_route_to_claude_code.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_router_helper_returns_none_when_master_toggle_off(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("plan this feature")
    monkeypatch.delenv("HERMES_CLAUDE_CODE_CONTROL", raising=False)

    result = await GatewayRunner._maybe_route_to_claude_code(runner, event, event.source)

    assert result is None


@pytest.mark.asyncio
async def test_router_helper_respects_per_route_toggle(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("plan this feature")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_CONTROL", "1")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_PLAN", "0")

    with patch("gateway.run.Path.home") as home_mock:
        home_mock.return_value = MagicMock(**{"__truediv__.return_value.__truediv__.return_value.exists.return_value": True})
        # Simpler: short-circuit subprocess and hand back a planning JSON blob.
        with patch("gateway.run.asyncio.to_thread", new=AsyncMock(return_value=SimpleNamespace(returncode=0, stdout='{"schema_version":1,"route":"planning","decision":"delegate_plan","confidence":0.8,"skills_to_use":[],"next_profile":"plan_minimal","requires_user_confirmation":false,"response":"ok"}'))):
            result = await GatewayRunner._maybe_route_to_claude_code(runner, event, event.source)

    assert result is None  # route disabled by env toggle


@pytest.mark.asyncio
async def test_router_helper_coding_failure_refuses_silent_fallback(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("implement this change")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_CONTROL", "1")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_CODING", "1")

    with patch("gateway.run.Path.home") as home_mock:
        home_mock.return_value = MagicMock(**{"__truediv__.return_value.__truediv__.return_value.exists.return_value": True})
        bad_json = '{"schema_version":1,"route":"coding","decision":"refuse","confidence":0.1,"skills_to_use":[],"next_profile":"patch_worktree","requires_user_confirmation":true,"confirmation_prompt":"x","response":null}'
        with patch("gateway.run.asyncio.to_thread", new=AsyncMock(return_value=SimpleNamespace(returncode=1, stdout=bad_json))):
            result = await GatewayRunner._maybe_route_to_claude_code(runner, event, event.source)

    assert result is not None
    assert "will not silently patch the repo with GPT-5.4" in result


@pytest.mark.asyncio
async def test_router_helper_review_failure_refuses_silent_downgrade(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("review this diff")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_CONTROL", "1")
    monkeypatch.setenv("HERMES_CLAUDE_CODE_REVIEW", "1")

    with patch("gateway.run.Path.home") as home_mock:
        home_mock.return_value = MagicMock(**{"__truediv__.return_value.__truediv__.return_value.exists.return_value": True})
        bad_json = '{"schema_version":1,"route":"review","decision":"refuse","confidence":0.1,"skills_to_use":[],"next_profile":"review","requires_user_confirmation":false,"response":null}'
        with patch("gateway.run.asyncio.to_thread", new=AsyncMock(return_value=SimpleNamespace(returncode=1, stdout=bad_json))):
            result = await GatewayRunner._maybe_route_to_claude_code(runner, event, event.source)

    assert result is not None
    assert "will not silently downgrade this safety review to GPT-5.4" in result

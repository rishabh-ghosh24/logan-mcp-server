"""Dependency refusals happen before secrets and again at the delete boundary."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_alert_warning import make_handlers
from tests.test_rq8_saved_search_dependencies import SEARCH, FREE, RULE, rule


def configured_handlers(tmp_path, *, blocked=False):
    handlers, client = make_handlers(tmp_path, [])
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE}] if blocked else []
    client.get_scheduled_task_details = AsyncMock(return_value=rule())
    client.get_saved_search = AsyncMock(return_value={
        'id': RULE, '_action': SimpleNamespace(saved_search_id=SEARCH)})
    client.delete_management_saved_search = AsyncMock()
    client.delete_scheduled_task = AsyncMock()
    return handlers, client


async def call(handlers, identifier, **extra):
    [response] = await handlers.handle_tool_call('delete_saved_search', {'saved_search_id': identifier, **extra})
    return response, json.loads(response['text'])


async def delete_independent(handlers, client, identifier=FREE):
    _, challenge = await call(handlers, identifier)
    assert challenge['status'] == 'confirmation_required'
    assert 'selected compartment' in challenge['summary'].lower()
    assert 'no detection rules' in challenge['summary'].lower()
    response, result = await call(handlers, identifier, confirmation_token=challenge['confirmation_token'],
                                  confirmation_secret='correct-secret')
    assert not response.get('is_error') and result == {'deleted': identifier}
    client.delete_management_saved_search.assert_awaited_once_with(identifier)


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier', [SEARCH, RULE])
async def test_blocked_search_returns_full_dependencies_without_a_confirmation_token(tmp_path, identifier):
    handlers, client = configured_handlers(tmp_path, blocked=True)
    response, result = await call(handlers, identifier)
    assert response['is_error'] and result['error_code'] == 'saved_search_in_use'
    assert result['dependencies']['rules'][0]['id'] == RULE
    assert result['dependencies']['rules'][0]['display_name'] == 'Team detection rule'
    assert result['dependencies']['saved_search_id'] == SEARCH
    assert result['dependencies']['scope']['namespace'] == 'testns'
    assert 'confirmation_token' not in result
    client.delete_management_saved_search.assert_not_awaited()
    client.delete_scheduled_task.assert_not_awaited()
    await delete_independent(handlers, client)


@pytest.mark.asyncio
async def test_inspection_failure_refuses_before_confirmation_and_can_recover(tmp_path):
    handlers, client = configured_handlers(tmp_path)
    client.list_scheduled_tasks_for_saved_search.side_effect = RuntimeError('private upstream text')
    response, result = await call(handlers, SEARCH)
    assert response['is_error'] and result['error_code'] == 'dependency_inspection_failed'
    assert 'confirmation_token' not in result and 'private upstream text' not in response['text']
    client.delete_management_saved_search.assert_not_awaited()
    client.list_scheduled_tasks_for_saved_search.side_effect = None
    await delete_independent(handlers, client)


@pytest.mark.asyncio
async def test_rule_created_during_confirmation_validation_prevents_the_delete(tmp_path, monkeypatch):
    handlers, client = configured_handlers(tmp_path)
    _, challenge = await call(handlers, SEARCH)
    assert challenge['status'] == 'confirmation_required'
    validate = handlers.confirmation_manager.validate_confirmation
    def add_rule_after_validation(*args, **kwargs):
        accepted = validate(*args, **kwargs)
        if accepted:
            client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE}]
        return accepted
    monkeypatch.setattr(handlers.confirmation_manager, 'validate_confirmation', add_rule_after_validation)
    response, result = await call(handlers, SEARCH, confirmation_token=challenge['confirmation_token'],
                                  confirmation_secret='correct-secret')
    assert response['is_error'] and result['error_code'] == 'saved_search_in_use'
    assert result['dependencies']['rules'][0]['id'] == RULE
    client.delete_management_saved_search.assert_not_awaited()
    client.delete_scheduled_task.assert_not_awaited()
    monkeypatch.setattr(handlers.confirmation_manager, 'validate_confirmation', validate)
    await delete_independent(handlers, client)


@pytest.mark.asyncio
async def test_missing_secret_does_not_hide_a_known_dependency(tmp_path, monkeypatch):
    handlers, client = configured_handlers(tmp_path, blocked=True)
    store = handlers.confirmation_manager._secret_store
    with monkeypatch.context() as state:
        state.setattr(store, 'has_secret', lambda: False)
        response, result = await call(handlers, SEARCH)
    assert response['is_error'] and result['error_code'] == 'saved_search_in_use'
    client.delete_management_saved_search.assert_not_awaited()
    await delete_independent(handlers, client)


@pytest.mark.asyncio
async def test_read_only_guard_runs_before_dependency_inspection(tmp_path):
    handlers, client = configured_handlers(tmp_path)
    handlers.settings.read_only = True
    _, result = await call(handlers, FREE)
    assert result['status'] == 'read_only_blocked'
    client.list_scheduled_tasks_for_saved_search.assert_not_awaited()
    client.delete_management_saved_search.assert_not_awaited()
    handlers.settings.read_only = False
    await delete_independent(handlers, client)

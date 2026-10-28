"""Live detection-rule references used to protect reusable saved searches."""
import asyncio
from dataclasses import dataclass
from .alert_links import alarms_watching, creator, origin_of


class DependencyInspectionError(RuntimeError):
    """Dependencies could not be checked completely in the selected scope."""

    def __init__(self):
        super().__init__('Could not verify saved-search dependencies. Check service access and retry explicitly.')


class SavedSearchInUse(RuntimeError):
    """Deletion would break one or more detection rules."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__('This search is used by a detection rule. Remove dependent alerts and rules first.')


@dataclass
class DependencySnapshot:
    """One request's rule inventory; never stored in the catalog cache."""

    scope: dict
    rules: list[dict]
    alarms: list[dict]

    def report(self, target_id: str, saved_search_id: str) -> dict:
        rules = [r for r in self.rules if r['saved_search_id'] == saved_search_id]
        consumers = {}
        identity_fields = ('metric_namespace', 'metric_name', 'metric_compartment_id')
        complete = all(not any(rule.get(key) for key in identity_fields)
                       or all(rule.get(key) for key in identity_fields) for rule in rules)
        for rule in rules:
            for alarm in alarms_watching(self.alarms, rule):
                if not alarm.get('metric_compartment_id') or not rule.get('metric_compartment_id'):
                    complete = False
                user, created_on = creator(alarm)
                consumers[alarm['id']] = {
                    'id': alarm['id'], 'display_name': alarm.get('display_name', ''),
                    'created_by': origin_of(alarm), 'created_by_user': user, 'created_on': created_on,
                }
        return {'target_id': target_id, 'saved_search_id': saved_search_id,
                'scope': self.scope, 'rules': rules, 'alerts': list(consumers.values()),
                'alerts_complete': complete}

    def markers(self, target_id: str) -> dict:
        backing_id = next((r['saved_search_id'] for r in self.rules if r['id'] == target_id), target_id)
        report = self.report(target_id, backing_id)
        count = len(report['rules'])
        return {'usage': 'detection_rule' if count else 'reusable',
                'dependent_rules': count,
                'dependent_alerts': len(report['alerts']) if report['alerts_complete'] else None,
                'dependency_status': 'checked' if report['alerts_complete'] else 'unknown'}


async def dependency_snapshot(client, compartment_id: str | None = None, *,
                              detail_concurrency: int = 1) -> DependencySnapshot:
    """Read every rule in this compartment; a partial inventory is an error."""
    try:
        cid = compartment_id or client.compartment_id
        summaries = await client.list_scheduled_tasks_for_saved_search(compartment_id=cid)
        if not 1 <= detail_concurrency <= 4:
            raise ValueError('Detail concurrency must be between one and four.')
        slots = asyncio.Semaphore(detail_concurrency)

        async def read_rule(summary):
            if summary.get('lifecycle_state') == 'DELETED':
                return None
            async with slots:
                details = await client.get_scheduled_task_details(summary['id'])
            if details.get('lifecycle_state') == 'DELETED':
                return None
            if not isinstance(details.get('saved_search_id'), str) or not details['saved_search_id']:
                raise DependencyInspectionError()
            return details

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(read_rule(summary)) for summary in summaries]
        rules = [task.result() for task in tasks if task.result() is not None]
        alarms = await client.list_alarms(cid) if rules else []
        alarms = [alarm for alarm in alarms if alarm.get('lifecycle_state') != 'DELETED']
        return DependencySnapshot({'namespace': client.namespace, 'compartment_id': cid,
                                   'visibility': 'selected_compartment'}, rules, alarms)
    except Exception:
        raise DependencyInspectionError() from None


async def inspect_dependencies(client, saved_search_id: str, compartment_id: str | None = None) -> dict:
    """Resolve legacy rule IDs and return full details for a deletion decision."""
    snapshot = await dependency_snapshot(client, compartment_id)
    native_id = saved_search_id
    try:
        search = await client.get_saved_search(saved_search_id)
        target_compartment = search.get('compartment_id')
        if isinstance(target_compartment, str) and target_compartment != snapshot.scope['compartment_id']:
            raise DependencyInspectionError()
        if not saved_search_id.startswith('ocid1.managementsavedsearch.'):
            action = search.get('_action')
            native_id = getattr(action, 'saved_search_id', None)
            if not isinstance(native_id, str) or not native_id:
                raise DependencyInspectionError()
    except Exception:
        raise DependencyInspectionError() from None
    return snapshot.report(saved_search_id, native_id)


def require_unused(report: dict) -> None:
    """A rule blocks deletion even when no alarm currently reads its metric."""
    if report['rules']:
        raise SavedSearchInUse(report)

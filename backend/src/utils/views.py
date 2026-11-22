from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'author': item.author, 'created_at': iso(item.created_at)}


def run(item, detail=False):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'has_export': bool(item.export_file), 'error': item.error}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
    return item_view


def schedule(item):
    from src.domain.spec import SPEC
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'next_report_day': item.params.get(SPEC['schedule_date_param']) if item.enabled else None}


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'created_at': iso(item.created_at)}


def monitor(item):
    return {'id': item.id, 'run_id': item.run_id, 'window_count': item.window_count,
            'family_alpha': item.family_alpha, 'alpha_per_window': item.alpha_per_window,
            'author': item.author, 'created_at': iso(item.created_at),
            'updated_at': iso(item.updated_at), 'snapshot': item.snapshot}


def report(item):
    return {'id': item.id, 'monitor_id': item.monitor_id, 'window_key': item.window_key,
            'version': item.version, 'count': item.count, 'observed_on': item.observed_on,
            'actor': item.actor, 'created_at': iso(item.created_at)}


def alarm(item):
    return {'id': item.id, 'monitor_id': item.monitor_id, 'window_key': item.window_key,
            'window_date': item.window_date, 'road': item.road, 'intersection': item.intersection,
            'shift': item.shift, 'predicted': item.predicted, 'critical': item.critical,
            'alpha_per_window': item.alpha_per_window, 'fired_on': item.fired_on,
            'fired_count': item.fired_count, 'fired_p_value': item.fired_p_value,
            'state': item.state, 'withdrawn_on': item.withdrawn_on,
            'withdraw_reason': item.withdraw_reason, 'basis': item.basis, 'version': item.version,
            'created_at': iso(item.created_at), 'updated_at': iso(item.updated_at)}

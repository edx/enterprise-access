"""
Scoring for the Personalized Search harness: per-turn checks and the per-arm summary.

Kept apart from the runner (``harness.py``) so the report command can re-score a saved JSONL file
without re-running any model.
"""
import statistics

LATENCY_FIELDS = ('intent_ms', 'retrieval_ms', 'first_text_ms', 'first_section_ms', 'complete_ms')
JUDGE_DIMENSIONS = ('relevance', 'why_quality', 'editorial', 'followup', 'overall')


def score_intent(expect: dict | None, debug: dict) -> dict | None:
    """
    Check a turn's intent outcome against the demo script's expectation.

    Returns None for cases without one (personas). Refinements are checked after merging, the way
    the turn itself merges them.
    """
    if not expect:
        return None
    seen = debug.get('intent_result') or {}
    on_topic, redirect = seen.get('on_topic'), seen.get('redirect_reason')
    want = expect.get('redirect_reason')
    if want in ('off_topic', 'unsafe', 'injection'):
        ok = on_topic is False and redirect == want
    elif want == 'injection_or_on_topic':
        ok = redirect == 'injection' or (on_topic is True and redirect is None)
    elif want == 'injection_or_off_topic':
        ok = on_topic is False and redirect in ('injection', 'off_topic')
    else:
        ok = on_topic is True and redirect is None
        if ok and expect.get('merge') and seen.get('merge') != expect['merge']:
            return {'pass': False, 'why': f"merge={seen.get('merge')}"}
        merged = debug.get('merged_intent') or {}
        constraints = merged.get('constraints') or {}
        checks = []
        if 'max_weeks_le' in expect:
            weeks = constraints.get('max_weeks')
            checks.append(('max_weeks', weeks is not None and weeks <= expect['max_weeks_le']))
        if 'max_hours_per_week_le' in expect:
            hours = constraints.get('max_hours_per_week')
            checks.append(('max_hours_per_week', hours is not None and hours <= expect['max_hours_per_week_le']))
        if 'pacing' in expect:
            checks.append(('pacing', constraints.get('pacing') == expect['pacing']))
        if 'level' in expect:
            checks.append(('level', merged.get('level') == expect['level']))
        if 'level_in' in expect:
            checks.append(('level', merged.get('level') in expect['level_in']))
        if 'item_types' in expect:
            checks.append(('item_types', sorted(merged.get('item_types') or []) == sorted(expect['item_types'])))
        if 'skills_contain' in expect:
            checks.append(('skills', any(expect['skills_contain'] in s.lower() for s in merged.get('skills') or [])))
        if expect.get('career'):
            skills = ' '.join(merged.get('skills') or []).lower()
            checks.append(('career', bool(merged.get('roles')) and any(w in skills for w in ('leader', 'manag'))))
        missed = [name for name, good in checks if not good]
        if ok and missed:
            return {'pass': False, 'why': 'missed ' + ','.join(missed)}
    return {'pass': bool(ok), 'why': '' if ok else f'on_topic={on_topic} redirect={redirect}'}


def recall(expected: list[str], found: list[str]) -> float | None:
    """Share of expected course keys that appear in ``found``; None without ground truth."""
    if not expected:
        return None
    return len(set(expected) & set(found)) / len(set(expected))


def _pct(values, p):
    """Linear-interpolated percentile of the non-null values, or None."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo))


def _timings(rows, field_name):
    """One timing field across rows that have timing."""
    return [r['timing'].get(field_name) for r in rows if r.get('timing')]


def _mean(values, digits=2):
    """Rounded mean of the non-null values, or None."""
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), digits) if values else None


def summarize(records: list[dict]) -> dict:
    """Per-arm summary of harness records (one dict per turn)."""
    arms: dict[str, list[dict]] = {}
    for record in records:
        arms.setdefault(record['arm'], []).append(record)
    summary = {}
    for arm, rows in arms.items():
        done = [r for r in rows if r.get('status') == 'done']
        on_topic = [r for r in done if not r.get('redirected')]
        intents = [r['intent_score'] for r in rows if r.get('intent_score')]
        adversarial = [r['intent_score'] for r in rows if r.get('intent_score') and r['case_id'].startswith('A')]
        judged = [r['judge'] for r in rows if isinstance(r.get('judge'), dict) and 'overall' in r['judge']]
        summary[arm] = {
            'models': rows[0].get('models'),
            'turns': len(rows),
            'errors': {code: sum(1 for r in rows if r.get('error') == code)
                       for code in {r.get('error') for r in rows} if code},
            'intent_pass': f"{sum(s['pass'] for s in intents)}/{len(intents)}" if intents else None,
            'adversarial_pass': f"{sum(s['pass'] for s in adversarial)}/{len(adversarial)}" if adversarial else None,
            'latency_p50': {f: _pct(_timings(on_topic, f), 50) for f in LATENCY_FIELDS},
            'latency_p90': {f: _pct(_timings(on_topic, f), 90) for f in LATENCY_FIELDS},
            'avg_items': _mean([len(r.get('item_ids') or []) for r in on_topic]),
            'turns_with_dropped_lines': sum(1 for r in on_topic if (r.get('timing') or {}).get('dropped_lines')),
            'fallback_sections': sum(1 for r in on_topic if r.get('fallback')),
            'persona_page_recall': _mean([r.get('page_recall') for r in rows]),
            'persona_candidate_recall': _mean([r.get('candidate_recall') for r in rows]),
            'avg_cost_usd': _mean([r.get('cost_usd') for r in rows], 5),
            'judge': {d: _mean([j[d] for j in judged if isinstance(j.get(d), (int, float))]) for d in JUDGE_DIMENSIONS}
            if judged else None,
        }
    return summary


def report_markdown(summary: dict) -> str:
    """A side-by-side table of the per-arm summary."""
    arms = list(summary)
    rows = [
        ('Models (intent / compose)', lambda s: ' / '.join(s['models'] or [])),
        ('Turns (errors)', lambda s: f"{s['turns']} ({sum(s['errors'].values())})"),
        ('Intent pass', lambda s: s['intent_pass'] or '–'),
        ('Adversarial pass', lambda s: s['adversarial_pass'] or '–'),
        *[(f'{label} p50 / p90 (ms)', lambda s, f=f: f"{s['latency_p50'][f]} / {s['latency_p90'][f]}")
          for f, label in (('intent_ms', 'Intent'), ('first_text_ms', 'First text'),
                           ('first_section_ms', 'First tiles'), ('complete_ms', 'Page done'))],
        ('Avg items per page', lambda s: s['avg_items']),
        ('Turns with dropped lines', lambda s: s['turns_with_dropped_lines']),
        ('Fallback sections', lambda s: s['fallback_sections']),
        ('Persona recall: page / candidates',
         lambda s: f"{s['persona_page_recall']} / {s['persona_candidate_recall']}"),
        ('Avg cost per turn (USD)', lambda s: s['avg_cost_usd'] if s['avg_cost_usd'] is not None else 'unknown'),
        ('Judge overall (relevance, why, editorial, follow-up)', lambda s: (
            f"{s['judge']['overall']} ({s['judge']['relevance']}, {s['judge']['why_quality']}, "
            f"{s['judge']['editorial']}, {s['judge']['followup']})" if s['judge'] else '–')),
    ]
    lines = ['| Metric | ' + ' | '.join(arms) + ' |', '| --- |' + ' --- |' * len(arms)]
    for label, fn in rows:
        lines.append(f'| {label} | ' + ' | '.join(str(fn(summary[a])) for a in arms) + ' |')
    return '\n'.join(lines) + '\n'

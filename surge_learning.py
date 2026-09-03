import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path('data')
SRC = DATA / 'latest_microstructure.csv'
DB = DATA / 'surge_case_db.json'
KST = timezone(timedelta(hours=9))

# Small, event-driven learning DB: only new +10% winners are added.
# Existing cases are updated only when their peak/priority tier expands.

def f(row, key):
    try:
        v = row.get(key, '')
        return None if v in ('', None) else float(v)
    except Exception:
        return None


def session_key(dt):
    # Upbit daily candle/session resets at 09:00 KST.
    if dt.hour < 9:
        dt = dt - timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


def compact_snapshot(row):
    keys = [
        'snapshot_kst','price','current_day_return_pct','day_high_return_pct',
        'drawdown_from_day_high_pct','gain_retrace_pct','launch_score','value_accel_pct',
        'slope15','gap120','f120up','higher_low','bid_trade_value_15m',
        'ask_trade_value_15m','buy_sell_ratio_15m','trade_value_anomaly_ratio',
        'buy_pressure_trend','sell_pressure_trend','buy_sell_state','value_accel_direction',
        'flow_signal','candidate_gate','stage','stage_label','stage_reason',
        'max_rally_5d_pct','drawdown_from_5d_high_pct','gain_retrace_5d_pct',
        'post_surge_higher_low','scan_cycle','scan_run_id'
    ]
    out = {k: row.get(k) for k in keys if k in row}
    # The collector already calculates these against stored snapshots, so we copy only
    # the target coin's T-15..T-120 trajectory instead of reopening full history.
    for m in (15,30,45,60,90,120):
        for k in (
            f'price_change_{m}m', f'launch_score_change_{m}m',
            f'slope15_change_{m}m', f'gap120_change_{m}m', f'f120up_change_{m}m',
            f'value_accel_change_{m}m', f'bid_trade_value_change_{m}m',
            f'ask_trade_value_change_{m}m', f'buy_sell_ratio_change_{m}m'
        ):
            if k in row:
                out[k] = row.get(k)
    return out


def load_db():
    if not DB.exists():
        return {'version': 1, 'cases': {}, 'updated_at_kst': None}
    try:
        obj = json.loads(DB.read_text(encoding='utf-8'))
        if isinstance(obj, dict) and isinstance(obj.get('cases'), dict):
            return obj
    except Exception:
        pass
    return {'version': 1, 'cases': {}, 'updated_at_kst': None}


def main():
    if not SRC.exists():
        raise SystemExit('latest_microstructure.csv missing')
    with SRC.open(encoding='utf-8-sig', newline='') as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit('latest_microstructure.csv empty')

    try:
        now = datetime.fromisoformat(rows[0]['snapshot_kst'])
    except Exception:
        now = datetime.now(KST)
    sess = session_key(now)
    db = load_db()
    cases = db['cases']
    new_count = 0
    updated_count = 0

    for row in rows:
        market = row.get('market')
        if not market:
            continue
        cur = f(row, 'current_day_return_pct')
        high = f(row, 'day_high_return_pct')
        observed = max(x for x in (cur, high) if x is not None) if (cur is not None or high is not None) else None
        if observed is None or observed < 10.0:
            continue

        key = f'{sess}|{market}'
        snap = compact_snapshot(row)
        if key not in cases:
            cases[key] = {
                'session_kst': sess,
                'market': market,
                'first_seen_10_kst': row.get('snapshot_kst'),
                'first_seen_10_return_pct': cur,
                'first_seen_day_high_return_pct': high,
                'peak_observed_return_pct': observed,
                'priority': 'deep_25_plus' if observed >= 25 else 'winner_10_plus',
                'first_10_snapshot': snap,
                'latest_snapshot': snap,
                'milestones': [{'tier': 25 if observed >= 25 else 10, 'snapshot_kst': row.get('snapshot_kst'), 'return_pct': observed}],
            }
            new_count += 1
            continue

        case = cases[key]
        prev_peak = float(case.get('peak_observed_return_pct') or 0)
        changed = False
        if observed > prev_peak:
            case['peak_observed_return_pct'] = observed
            changed = True
        case['latest_snapshot'] = snap
        milestones = case.setdefault('milestones', [])
        reached = {int(x.get('tier', 0)) for x in milestones}
        for tier in (15,20,25,30):
            if observed >= tier and tier not in reached:
                milestones.append({'tier': tier, 'snapshot_kst': row.get('snapshot_kst'), 'return_pct': observed})
                changed = True
        if observed >= 25 and case.get('priority') != 'deep_25_plus':
            case['priority'] = 'deep_25_plus'
            changed = True
        if changed:
            updated_count += 1

    db['updated_at_kst'] = now.isoformat()
    db['current_session_kst'] = sess
    db['last_run'] = {'new_10_plus': new_count, 'expanded_cases': updated_count, 'total_cases': len(cases)}
    DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'surge learning: new={new_count} expanded={updated_count} total={len(cases)}')


if __name__ == '__main__':
    main()

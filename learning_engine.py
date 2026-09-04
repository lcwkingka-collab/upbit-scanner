"""Closed-loop learning for the Upbit KRW scanners.

This module intentionally uses only information known at ``as_of`` for features
and only later snapshots for labels.  It therefore avoids both survivor bias
(all mature snapshots are controls) and look-ahead leakage.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path("data")
KST = timezone(timedelta(hours=9))
TARGETS = (5, 10, 25)
HORIZON_MINUTES = int(os.getenv("LEARNING_HORIZON_MINUTES", "360"))
MIN_SAMPLES = int(os.getenv("LEARNING_MIN_SAMPLES", "30"))


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def pct(current, previous):
    return (current / previous - 1) * 100 if current is not None and previous not in (None, 0) else None


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def feature_vector(point, previous=None):
    """Stable feature contract shared by the morning and intraday scanners."""
    bid = number(point.get("bid_trade_value_15m")) or 0
    ask = number(point.get("ask_trade_value_15m")) or 0
    trades = number(point.get("trade_count_15m")) or 0
    prior_trades = number((previous or {}).get("trade_count_15m"))
    net_buy = bid - ask
    return {
        "launch_score": number(point.get("launch_score")),
        "launch_score_delta": number(point.get("delta_launch_score")),
        "slope15": number(point.get("slope15")),
        "slope15_delta": number(point.get("delta_slope15")),
        "gap50": number(point.get("gap50")),
        "gap120": number(point.get("gap120")),
        "gap120_delta": number(point.get("delta_gap120")),
        "conv120": number(point.get("conv120")),
        "cross_days": number(point.get("f120up")),
        "cross_days_delta": number(point.get("delta_f120up")),
        "higher_low": bool(point.get("higher_low")),
        "return_1d": number(point.get("current_day_return_pct")),
        "return_3d": number(point.get("ret3_pct")),
        "return_5d": number(point.get("ret5_pct")),
        "trade_value_anomaly": number(point.get("trade_value_anomaly_ratio")),
        "trade_value_delta": number(point.get("delta_trade_value_15m")),
        "bid_ask_ratio": number(point.get("buy_sell_ratio_15m")),
        "net_buy": net_buy,
        "net_buy_positive": net_buy > 0,
        "trade_count": trades,
        "trade_count_delta": trades - prior_trades if prior_trades is not None else None,
        "ask_absorption": (number(point.get("delta_ask_trade_value_15m")) or 0) < 0,
        "accumulation_streak": number(point.get("accumulation_streak")) or 0,
        "price_structure_improving": bool(point.get("price_structure_improving")),
    }


def predicates(features):
    """Interpretable conditions; combinations, not raw score sums, are learned."""
    return {
        "score_rising": (features["launch_score_delta"] or 0) > 0,
        "slope_rising": (features["slope15_delta"] or 0) > 0,
        "gap120_shrinking": (features["gap120_delta"] or 0) < 0,
        "d15_cross_approach": features["cross_days"] is not None and 0 < features["cross_days"] <= 15,
        "higher_low": features["higher_low"],
        "volume_expanding": (features["trade_value_anomaly"] or 0) >= 1.5 and (features["trade_value_delta"] or 0) > 0,
        "bid_persistent": (features["bid_ask_ratio"] or 0) >= 1.2 and features["net_buy_positive"],
        "trades_rising": (features["trade_count_delta"] or 0) > 0 and features["trade_count"] >= 10,
        "ask_absorption": features["ask_absorption"],
        "accumulation_persistent": features["accumulation_streak"] >= 2,
        "price_structure_improving": features["price_structure_improving"],
    }


def add_derived_fields(points):
    streak = 0
    previous = None
    for point in points:
        bid = number(point.get("bid_trade_value_15m")) or 0
        ask = number(point.get("ask_trade_value_15m")) or 0
        streak = streak + 1 if bid > ask and bid > 0 else 0
        point["accumulation_streak"] = streak
        if previous:
            point.setdefault("delta_trade_value_15m", (
                (bid + ask) - ((number(previous.get("bid_trade_value_15m")) or 0) + (number(previous.get("ask_trade_value_15m")) or 0))
            ))
            point.setdefault("price_structure_improving", bool(
                (number(point.get("price")) or 0) >= (number(previous.get("price")) or math.inf)
                and ((number(point.get("slope15")) or 0) > (number(previous.get("slope15")) or 0)
                     or bool(point.get("higher_low")))
            ))
        previous = point


def build_examples(markets, now_epoch):
    examples, pending = [], 0
    horizon_seconds = HORIZON_MINUTES * 60
    for market, raw_points in markets.items():
        points = sorted((dict(p) for p in raw_points if number(p.get("snapshot_epoch")) is not None), key=lambda p: p["snapshot_epoch"])
        add_derived_fields(points)
        for index, point in enumerate(points):
            start = number(point.get("snapshot_epoch"))
            entry = number(point.get("price"))
            if not entry or now_epoch - start < horizon_seconds:
                pending += 1
                continue
            future = [p for p in points[index + 1:] if number(p.get("snapshot_epoch")) <= start + horizon_seconds and number(p.get("price"))]
            if not future:
                continue
            max_return = max(pct(number(p["price"]), entry) for p in future)
            previous = points[index - 1] if index else None
            features = feature_vector(point, previous)
            examples.append({
                "market": market, "snapshot_epoch": start, "features": features,
                "conditions": predicates(features), "max_return_pct": max_return,
                "labels": {f"hit_{target}": max_return >= target for target in TARGETS},
            })
    return examples, pending


def discover_combinations(examples):
    if not examples:
        return []
    names = sorted(examples[0]["conditions"])
    combinations = []
    for size in (1, 2, 3):
        # Avoid itertools product noise while keeping dependency-free output.
        def choose(start, selected):
            if len(selected) == size:
                matched = [e for e in examples if all(e["conditions"].get(name) for name in selected)]
                if len(matched) < MIN_SAMPLES:
                    return
                row = {"conditions": list(selected), "samples": len(matched), "rates": {}}
                for target in TARGETS:
                    base = sum(e["labels"][f"hit_{target}"] for e in examples) / len(examples)
                    rate = sum(e["labels"][f"hit_{target}"] for e in matched) / len(matched)
                    row["rates"][f"hit_{target}"] = rate
                    row["rates"][f"lift_{target}"] = rate / base if base else None
                combinations.append(row)
                return
            for pos in range(start, len(names)):
                choose(pos + 1, selected + [names[pos]])
        choose(0, [])
    return sorted(combinations, key=lambda r: ((r["rates"].get("lift_10") or 0), r["samples"]), reverse=True)[:50]


def empirical_prediction(conditions, examples):
    active = {name for name, enabled in conditions.items() if enabled}
    scored = []
    for example in examples:
        other = {name for name, enabled in example["conditions"].items() if enabled}
        union = active | other
        similarity = len(active & other) / len(union) if union else 1.0
        scored.append((similarity, example))
    matched = [row for similarity, row in sorted(scored, key=lambda x: x[0], reverse=True) if similarity >= 0.6][:250]
    if len(matched) < MIN_SAMPLES:
        return {"status": "표본 부족", "samples": len(matched), "required_samples": MIN_SAMPLES,
                "p5": None, "p10": None, "p25": None}
    return {"status": "estimated_from_historical_controls", "samples": len(matched), **{
        f"p{target}": sum(e["labels"][f"hit_{target}"] for e in matched) / len(matched) for target in TARGETS
    }}


def calibration(examples):
    # Walk-forward calibration only; every prediction uses strictly older data.
    bins = defaultdict(lambda: {"predictions": 0, "hits": 0})
    ordered = sorted(examples, key=lambda e: e["snapshot_epoch"])
    for index, example in enumerate(ordered):
        training = ordered[:index]
        if len(training) < MIN_SAMPLES:
            continue
        prediction = empirical_prediction(example["conditions"], training)
        probability = prediction.get("p10")
        if probability is None:
            continue
        bucket = f"{int(probability * 10) * 10:02d}-{min(100, int(probability * 10) * 10 + 10):02d}%"
        bins[bucket]["predictions"] += 1
        bins[bucket]["hits"] += int(example["labels"]["hit_10"])
    return {bucket: {**values, "actual_rate": values["hits"] / values["predictions"]} for bucket, values in sorted(bins.items())}


def transition(current, previous):
    if not previous:
        return "신규 포착"
    current_active = sum(predicates(feature_vector(current, previous)).values())
    previous_active = sum(predicates(feature_vector(previous)).values())
    trades = number(current.get("trade_count_15m")) or 0
    value = (number(current.get("bid_trade_value_15m")) or 0) + (number(current.get("ask_trade_value_15m")) or 0)
    if trades < 3 or value <= 0:
        return "제외"
    if current_active >= previous_active + 2:
        return "강화"
    if current_active <= previous_active - 2:
        return "후퇴"
    return "유지"


def run(out=OUT):
    history = read_json(out / "microstructure_history.json", {"markets": {}})
    now = datetime.now(KST)
    now_epoch = int(now.timestamp())
    examples, pending = build_examples(history.get("markets", {}), now_epoch)
    combinations = discover_combinations(examples)
    model = {
        "generated_kst": now.isoformat(), "horizon_minutes": HORIZON_MINUTES,
        "targets_pct": list(TARGETS), "minimum_samples": MIN_SAMPLES,
        "mature_examples": len(examples), "pending_examples": pending,
        "class_balance": {f"hit_{target}": sum(e["labels"][f"hit_{target}"] for e in examples) for target in TARGETS},
        "combinations": combinations, "calibration_p10": calibration(examples),
        "probability_policy": "historical similar-condition hit rate; never launch_score/total_score",
    }
    latest, tracker = [], []
    for market, raw_points in history.get("markets", {}).items():
        points = sorted(raw_points, key=lambda p: number(p.get("snapshot_epoch")) or 0)
        if not points:
            continue
        enriched = [dict(p) for p in points]
        add_derived_fields(enriched)
        current, previous = enriched[-1], enriched[-2] if len(enriched) > 1 else None
        features = feature_vector(current, previous)
        estimate = empirical_prediction(predicates(features), examples)
        latest.append({"market": market, "snapshot_kst": current.get("snapshot_kst"), "features": features,
                       "conditions": predicates(features), "forecast": estimate})
        tracker.append({"market": market, "state": transition(current, previous),
                        "snapshot_kst": current.get("snapshot_kst"), "forecast": estimate})
    (out / "latest_learning_model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "latest_learning_predictions.json").write_text(json.dumps({"generated_kst": now.isoformat(), "rows": latest}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "latest_intraday_transitions.json").write_text(json.dumps({"generated_kst": now.isoformat(), "states": ["신규 포착", "강화", "유지", "후퇴", "제외"], "rows": tracker}, ensure_ascii=False, indent=2), encoding="utf-8")
    return model


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))

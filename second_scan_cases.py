#!/usr/bin/env python3
"""Collect exact alert-cycle windows declared in scan_request.json."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import second_scan_api as ss


OUT = Path("second_scan_output")


def main() -> None:
    request = json.loads(Path("scan_request.json").read_text(encoding="utf-8"))
    cases = request.get("cases") or []
    if not cases:
        raise SystemExit("scan_request.json has no cases")

    OUT.mkdir(exist_ok=True)
    all_rows = []
    manifest = []
    errors = []
    for index, case in enumerate(cases, 1):
        market = ss.normalize_market(case["market"])
        start = ss.parse_kst(case["date"], case["from"])
        end = ss.parse_kst(case["date"], case["to"])
        label = case.get("label") or f"case-{index}"
        print(f"[{index}/{len(cases)}] {label} {market} {start.isoformat()} ~ {end.isoformat()}", flush=True)
        try:
            result = ss.analyze_market(market, start, end, enrich_trades=case.get("raw_trades", True))
            for row in result.get("rows") or []:
                item = dict(row)
                item.update({"case_label": label, "case_from_kst": start.isoformat(), "case_to_kst": end.isoformat()})
                all_rows.append(item)
            manifest.append({
                "case_label": label,
                "market": market,
                "from_kst": start.isoformat(),
                "to_kst": end.isoformat(),
                "tick_size": result.get("tick_size"),
                "seconds_with_trades": result.get("seconds_with_trades"),
                "raw_trade_enrichment": result.get("raw_trade_enrichment"),
            })
        except Exception as exc:
            errors.append({"case_label": label, "market": market, "error": str(exc)})
            print(f"ERROR {label}: {exc}", flush=True)
        time.sleep(0.15)

    if all_rows:
        fields = []
        seen = set()
        for row in all_rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
        with (OUT / "exact_cycle_seconds.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows)
    (OUT / "exact_cycle_manifest.json").write_text(
        json.dumps({"cases": manifest, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"cases": len(manifest), "rows": len(all_rows), "errors": errors}, ensure_ascii=False), flush=True)
    if not manifest:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

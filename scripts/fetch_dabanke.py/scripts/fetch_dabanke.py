#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Dabanke daily review data for the Tiantianban PWA.

The browser cannot fetch dabanke.com directly because the site does not allow
cross-origin requests. This script runs in GitHub Actions (or locally), fetches
the latest daily page, and updates a same-origin ``dabanke_data.json`` file.

Failure policy: existing day records are preserved. A failed fetch never
overwrites good data with an empty record.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


HOME = "https://www.dabanke.com/"
PAGE = "https://www.dabanke.com/index-{date}.html"
UA = "Tiantianban-PWA/1.1 (+https://www.dabanke.com/)"


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def request_text(url: str, timeout: int = 35) -> str:
    result = subprocess.run(
        [
            "curl", "-L", "--compressed", "--fail", "--silent", "--show-error",
            "--max-time", str(timeout),
            "-A", UA,
            "-H", "Accept: text/html,application/xhtml+xml",
            "-H", "Accept-Language: zh-CN,zh;q=0.9",
            url,
        ],
        capture_output=True,
        timeout=timeout + 5,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise OSError(error or f"curl exited {result.returncode}")
    return result.stdout.decode("utf-8", "replace")


def retry_text(url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return request_text(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url}: {last_error}")


def canonical_symbol(source_symbol: str) -> str:
    value = (source_symbol or "").strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", value)
    if match:
        return match.group(2).lower() + match.group(1)
    return source_symbol.strip().lower()


def parse_market(source: str) -> dict:
    market: dict = {}
    index_matches = re.findall(
        r'<div class="home-index-item\s+(?:is-rise|is-fall|is-flat)">\s*'
        r"<span>(.*?)</span>\s*<strong>(.*?)</strong>",
        source,
        re.S,
    )
    market["indices"] = [
        {"name": clean_text(name), "change": clean_text(change)}
        for name, change in index_matches
    ]

    note_match = re.search(
        r'<p class="home-market-overview-note">(.*?)</p>', source, re.S
    )
    if note_match:
        note_parts = re.findall(
            r"<strong>(.*?)</strong>|<span>(.*?)</span>", note_match.group(1), re.S
        )
        pieces = [clean_text(a or b) for a, b in note_parts]
        market["note"] = "；".join(x for x in pieces if x)

    cards: dict[str, dict] = {}
    for match in re.finditer(
        r'<article class="(is-[a-z-]+)">\s*<span>(.*?)</span>\s*'
        r"<strong>(.*?)</strong>\s*(?:<small[^>]*>(.*?)</small>)?",
        source,
        re.S,
    ):
        key, label, value, detail = match.groups()
        cards[key] = {
            "label": clean_text(label),
            "value": clean_text(value),
            "detail": clean_text(detail or ""),
        }
    market["limit"] = cards
    return market


def parse_stocks(source: str) -> dict[str, dict]:
    stocks: dict[str, dict] = {}
    tier_parts = re.split(r'<section class="ladder-view-tier"', source)[1:]
    for part in tier_parts:
        segment = part.split("</section>", 1)[0]
        tier_match = re.search(r"<h3>(.*?)</h3>", segment, re.S)
        ladder = clean_text(tier_match.group(1)) if tier_match else ""
        stock_parts = re.split(r'<div class="stock-line\s+', segment)[1:]
        for stock_part in stock_parts:
            symbol_match = re.search(r'data-symbol="([^"]+)"', stock_part[:800])
            if not symbol_match:
                continue
            source_symbol = symbol_match.group(1).strip().upper()
            symbol = canonical_symbol(source_symbol)
            status_match = re.match(r"([a-z-]+)", stock_part)
            status_class = status_match.group(1) if status_match else ""
            name_match = re.search(
                r'class="stock-detail-link"[^>]*>(.*?)</a>', stock_part, re.S
            )
            change_match = re.search(
                r'<span class="stock-change[^"]*">(.*?)</span>',
                stock_part,
                re.S,
            )
            performance_match = re.search(
                r'<span class="ladder-tag ladder-performance[^"]*"[^>]*>(.*?)</span>',
                stock_part,
                re.S,
            )
            stocks[symbol] = {
                "sourceSymbol": source_symbol,
                "name": clean_text(name_match.group(1)) if name_match else "",
                "ladder": ladder,
                "change": clean_text(change_match.group(1)) if change_match else "",
                "status": (
                    "success"
                    if status_class == "status-success"
                    else "failed"
                    if status_class == "status-failed"
                    else status_class
                ),
                "statusText": (
                    clean_text(performance_match.group(1))
                    if performance_match
                    else ("未晋级" if status_class == "status-failed" else "")
                ),
            }
    return stocks


def parse_day(date: str, source: str) -> dict:
    return {
        "date": date,
        "url": PAGE.format(date=date.replace("-", "")),
        "fetchedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "stockQueryComplete": True,
        "market": parse_market(source),
        "stocks": parse_stocks(source),
    }


def latest_market_date() -> str:
    source = retry_text(HOME)
    match = re.search(
        r'<p class="home-market-meta">\s*<span>(\d{4}-\d{2}-\d{2})</span>',
        source,
        re.S,
    )
    if not match:
        raise RuntimeError("cannot find latest market date on Dabanke homepage")
    return match.group(1)


def load_output(path: Path) -> dict:
    if not path.exists():
        return {"generatedAt": "", "source": HOME, "days": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("days"), dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"generatedAt": "", "source": HOME, "days": {}}


def save_output(path: Path, data: dict) -> None:
    data["generatedAt"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    data["source"] = HOME
    data["days"] = dict(sorted(data["days"].items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", action="append", default=[], help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0, help="fetch latest N market dates")
    parser.add_argument("--refresh", action="store_true", help="replace existing dates")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", default="dabanke_data.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).resolve()
    current = load_output(output)
    days = current.setdefault("days", {})
    latest = latest_market_date()
    dates = set(args.date or [latest])
    if args.limit > 0:
        latest_day = dt.date.fromisoformat(latest)
        probe = latest_day
        found: list[str] = []
        while len(found) < args.limit and probe >= latest_day - dt.timedelta(days=30):
            value = probe.isoformat()
            if value in days or value == latest:
                found.append(value)
            probe -= dt.timedelta(days=1)
        dates.update(found)

    pending = sorted(
        d for d in dates
        if args.refresh or d not in days or not days[d].get("stockQueryComplete")
    )
    if not pending:
        print(f"No dates to fetch. Latest Dabanke market date: {latest}")
        return 0

    print(f"Fetching {len(pending)} Dabanke date(s): {', '.join(pending)}")

    def worker(date: str) -> tuple[str, dict | None, str | None]:
        try:
            page = PAGE.format(date=date.replace("-", ""))
            return date, parse_day(date, retry_text(page)), None
        except Exception as exc:
            return date, None, str(exc)

    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(worker, date) for date in pending]
        for future in concurrent.futures.as_completed(futures):
            date, record, error = future.result()
            if record:
                days[date] = record
                print(f"  ok {date}: {len(record['stocks'])} stock row(s)")
            else:
                failures.append(date)
                print(f"  fail {date}: {error}", file=sys.stderr)

    save_output(output, current)
    print(f"Wrote {output} ({len(days)} day(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

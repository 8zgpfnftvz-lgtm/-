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
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
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

    state_match = re.search(
        r'<strong class="home-market-state[^"]*">(.*?)</strong>', source, re.S
    )
    market["state"] = clean_text(state_match.group(1)) if state_match else ""

    turnover_match = re.search(
        r'<div class="home-turnover-total">\s*<span>(.*?)</span>\s*'
        r'<strong>(.*?)</strong>\s*<small>(.*?)</small>.*?'
        r'<dl>(.*?)</dl>',
        source,
        re.S,
    )
    if turnover_match:
        market["turnover"] = {
            "label": clean_text(turnover_match.group(1)),
            "value": clean_text(turnover_match.group(2)),
            "state": clean_text(turnover_match.group(3)),
            "comparisons": [
                {"label": clean_text(label), "value": clean_text(value),
                 "direction": "rise" if "is-rise" in cls else "fall" if "is-fall" in cls else ""}
                for label, cls, value in re.findall(
                    r"<dt>(.*?)</dt>\s*<dd class=\"([^\"]*)\">(.*?)</dd>",
                    turnover_match.group(4),
                    re.S,
                )
            ],
        }

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
    market["ecosystem"] = cards

    breadth = {}
    for key, body in re.findall(
        r'class="short-pool-bar-row market-bucket-([a-z0-9_]+)"[^>]*>([\s\S]*?)</div>',
        source,
        re.S,
    ):
        label = re.search(r"<span[^>]*class=\"short-pool-bar-label\"[^>]*>(.*?)</span>", body, re.S)
        value = re.search(r"<strong>(.*?)</strong>", body, re.S)
        breadth[key] = {
            "label": clean_text(label.group(1)) if label else "",
            "value": clean_text(value.group(1)) if value else "",
        }
    market["breadth"] = breadth
    market["breadthTotals"] = {
        key: {
            "label": clean_text(label),
            "value": clean_text(value),
        }
        for key, label, value in re.findall(
            r'<span class="market-total-(rise|flat|fall|rate)">(.*?)'
            r"<strong>(.*?)</strong></span>",
            source,
            re.S,
        )
    }
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
                r'<span class="ladder-tag ladder-performance[^"]*"'
                r'[^>]*title="([^"]*)"[^>]*>(.*?)</span>',
                stock_part,
                re.S,
            )
            rank_match = re.search(
                r'<span class="ladder-tag ladder-rank"[^>]*>(.*?)</span>',
                stock_part,
                re.S,
            )
            history_match = re.search(
                r'<span class="ladder-tag ladder-history"[^>]*title="([^"]*)"'
                r"[^>]*>(.*?)</span>",
                stock_part,
                re.S,
            )
            theme_match = re.search(
                r'<details class="ladder-stock-reason">\s*'
                r'<summary[^>]*title="([^"]*)"[^>]*>(.*?)</summary>'
                r"\s*<p>(.*?)</p>",
                stock_part,
                re.S,
            )
            market_match = re.search(
                r'<span class="stock-market"[^>]*>(.*?)</span>', stock_part, re.S
            )
            perf_title = clean_text(performance_match.group(1)) if performance_match else ""
            perf_text = clean_text(performance_match.group(2)) if performance_match else ""
            first_seal = re.search(r"首封\s*(\d{1,2}:\d{2}:\d{2})", perf_title)
            last_seal = re.search(r"最后封板\s*(\d{1,2}:\d{2}:\d{2})", perf_title)
            open_count = re.search(r"开板\s*(\d+)\s*次", perf_title)
            history_title = clean_text(history_match.group(1)) if history_match else ""
            ten_day_boards = re.search(r"近10个交易日\s*(\d+)\s*次", history_title)
            reason_title = clean_text(theme_match.group(1)) if theme_match else ""
            reason_parts = reason_title.split(" · 资料日期 · ")
            reason = clean_text(reason_parts[0]) if reason_parts else ""
            reason_date = clean_text(reason_parts[1]) if len(reason_parts) > 1 else ""
            stocks[symbol] = {
                "sourceSymbol": source_symbol,
                "name": clean_text(name_match.group(1)) if name_match else "",
                "market": clean_text(market_match.group(1)) if market_match else "",
                "ladder": ladder,
                "change": clean_text(change_match.group(1)) if change_match else "",
                "rank": clean_text(rank_match.group(1)) if rank_match else "",
                "performance": perf_text,
                "performanceTitle": perf_title,
                "firstSeal": first_seal.group(1) if first_seal else "",
                "lastSeal": last_seal.group(1) if last_seal else "",
                "openCount": int(open_count.group(1)) if open_count else 0,
                "theme": clean_text(theme_match.group(2)) if theme_match else "",
                "reason": reason,
                "reasonDate": reason_date,
                "tenDayBoards": int(ten_day_boards.group(1)) if ten_day_boards else 0,
                "newHigh60": 'ladder-new-high' in stock_part,
                "status": (
                    "success"
                    if status_class == "status-success"
                    else "failed"
                    if status_class == "status-failed"
                    else status_class
                ),
                "statusText": (
                    perf_text
                    if performance_match
                    else ("未晋级" if status_class == "status-failed" else "")
                ),
            }
    return stocks


def parse_layers(source: str) -> list[dict]:
    layers: list[dict] = []
    tier_parts = re.split(r'<section class="ladder-view-tier"', source)[1:]
    for part in tier_parts:
        segment = part.split("</section>", 1)[0]
        name_match = re.search(r"<h3>(.*?)</h3>", segment, re.S)
        rate_match = re.search(r"</h3>\s*<strong>(.*?)</strong>", segment, re.S)
        count_match = re.search(
            r"<p>\s*(晋级|封住)\s*(\d+)\s*/\s*(\d+)\s*只\s*</p>",
            segment,
            re.S,
        )
        if not name_match:
            continue
        layers.append({
            "name": clean_text(name_match.group(1)),
            "rate": clean_text(rate_match.group(1)) if rate_match else "",
            "label": clean_text(count_match.group(1)) if count_match else "",
            "passed": int(count_match.group(2)) if count_match else 0,
            "total": int(count_match.group(3)) if count_match else 0,
        })
    return layers


def parse_themes(source: str) -> list[dict]:
    table_match = re.search(r'<table class="concept-table">([\s\S]*?)</table>', source)
    if not table_match:
        return []
    themes: list[dict] = []
    for row in re.findall(r"<tr>([\s\S]*?)</tr>", table_match.group(1), re.S):
        name_match = re.search(
            r'class="concept-detail-link"[^>]*>([\s\S]*?)</a>', row, re.S
        )
        if not name_match:
            continue
        summary = clean_text(row)
        totals = re.search(
            r"(\d+)只\s*·\s*活跃(\d+)只\s*·\s*([0-9.]+)%", summary
        )
        sealed = re.search(
            r"封板(\d+)只\s*·\s*中位\s*([+-]?[0-9.]+)%\s*·\s*最高(\d+)板",
            summary,
        )
        alignment = re.search(
            r'class="concept-reason-alignment">([\s\S]*?)</span>', row, re.S
        )
        stocks = [
            {
                "symbol": canonical_symbol(sym),
                "sourceSymbol": sym,
                "name": clean_text(name),
                "change": clean_text(change),
            }
            for sym, name, change in re.findall(
                r'class="concept-stock"[^>]*title="([^"]+)"[\s\S]*?'
                r'class="stock-detail-link"[^>]*>([\s\S]*?)</a>[\s\S]*?'
                r'class="concept-change-number">([^<]+)</span>',
                row,
                re.S,
            )
        ]
        themes.append({
            "name": clean_text(name_match.group(1)),
            "totalStocks": int(totals.group(1)) if totals else 0,
            "activeStocks": int(totals.group(2)) if totals else 0,
            "activeRate": float(totals.group(3)) if totals else 0,
            "sealedStocks": int(sealed.group(1)) if sealed else 0,
            "medianChange": float(sealed.group(2)) if sealed else 0,
            "maxBoard": int(sealed.group(3)) if sealed else 0,
            "alignment": clean_text(alignment.group(1)) if alignment else "",
            "stocks": stocks,
        })
    return themes


def parse_patterns(source: str) -> list[dict]:
    table_match = re.search(r'<table class="overview-table">([\s\S]*?)</table>', source)
    if not table_match:
        return []
    patterns: list[dict] = []
    for row in re.findall(r"<tr>([\s\S]*?)</tr>", table_match.group(1), re.S):
        type_match = re.search(
            r'<th class="overview-type"[^>]*>\s*<span>(.*?)</span>\s*'
            r"<small>(.*?)</small>",
            row,
            re.S,
        )
        if not type_match:
            continue
        stocks = []
        for sym, name, change, reason in re.findall(
            r'<div class="overview-stock" title="([^"]+)">[\s\S]*?'
            r'class="stock-detail-link"[^>]*>([\s\S]*?)</a>[\s\S]*?'
            r'class="overview-change[^"]*">\s*\[\s*<span>([^<]+)</span>[\s\S]*?'
            r'class="overview-reason"[^>]*title="([^"]*)"',
            row,
            re.S,
        ):
            stocks.append({
                "symbol": canonical_symbol(sym),
                "sourceSymbol": sym,
                "name": clean_text(name),
                "change": clean_text(change),
                "reason": clean_text(reason),
            })
        patterns.append({
            "name": clean_text(type_match.group(1)),
            "countText": clean_text(type_match.group(2)),
            "stocks": stocks,
        })
    return patterns


def parse_day(date: str, source: str) -> dict:
    stocks = parse_stocks(source)
    themes = parse_themes(source)
    for theme in themes:
        for item in theme.get("stocks") or []:
            stock = stocks.get(item.get("symbol"))
            if not stock:
                continue
            stock.setdefault("activeThemes", []).append({
                "name": theme["name"],
                "sealedStocks": theme["sealedStocks"],
                "activeStocks": theme["activeStocks"],
                "maxBoard": theme["maxBoard"],
            })
    return {
        "date": date,
        "url": PAGE.format(date=date.replace("-", "")),
        "fetchedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "stockQueryComplete": True,
        "market": parse_market(source),
        "stocks": stocks,
        "layers": parse_layers(source),
        "themes": themes,
        "patterns": parse_patterns(source),
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

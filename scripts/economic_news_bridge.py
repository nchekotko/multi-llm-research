"""Run in the pinned Economic Calendar image. Export public news, no account data."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")  # Pinned Economic Calendar image, not user-provided code.
from news_feed.client import SmartLabClient  # noqa: E402
from news_feed.smartlab_parser import parse_page  # noqa: E402

ROOT = Path("/export")


def now():
    return datetime.now(timezone.utc).isoformat()


def run_once():
    if shutil.disk_usage(ROOT).free < 1024**3:
        raise OSError("disk_reserve")
    state_path = ROOT / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": {}, "initialized": []}
    feeds = {}
    health = {}
    try:
        page = asyncio.run(SmartLabClient().get_news_page(1))
        values = parse_page(page)
        feeds["economic/smartlab"] = [
            dict(
                x,
                published_at=x.get("published_at") if x.get("date_raw") else None,
                publication_precision="day" if x.get("date_raw") else "unknown",
            )
            for x in values
        ]
        health["economic/smartlab"] = "ok" if values else "empty_or_unavailable"
    except Exception as exc:
        health["economic/smartlab"] = type(exc).__name__
    # Reuse the audited public-data collectors in the Economic Calendar image.
    # No web account impersonation, credentials or live application DB mounts.
    for name in ("oil", "macro"):
        key = "economic/" + name
        try:
            if name == "oil":
                from src.services.rss_aggregator import RSS_FEEDS, _fetch_rss

                values = [item for feed in RSS_FEEDS for item in _fetch_rss(feed)]
            else:
                from src.services.bonds_lab.news import get_feed

                values = get_feed(limit=100)["items"]
            feeds[key] = values
            health[key] = "ok" if values else "empty_or_unavailable"
        except Exception as exc:
            health[key] = type(exc).__name__
    items = []
    for feed, values in feeds.items():
        baseline = feed not in state["initialized"]
        for value in values[:100]:
            title = str(value.get("title") or "")[:3000]
            url = str(value.get("url") or value.get("link") or "")[:2000]
            if not title:
                continue
            identity = hashlib.sha256((feed + "|" + (url or title)).encode()).hexdigest()
            if identity not in state["seen"]:
                if len(state["seen"]) >= 50000:
                    raise ValueError("identity_capacity_reached")
                state["seen"][identity] = {"first_seen_at": now(), "baseline": baseline}
            published = value.get("published_at") or value.get("date")
            if isinstance(published, datetime):
                published = published.isoformat()
            items.append(
                {
                    "id": identity,
                    "feed": feed,
                    "source": value.get("source") or feed,
                    "title": title,
                    "url": url,
                    "summary": str(value.get("summary") or "")[:6000],
                    "published_at": published,
                    "publication_precision": value.get(
                        "publication_precision", "source_timestamp_unverified"
                    ),
                    **state["seen"][identity],
                }
            )
        if values and baseline:
            state["initialized"].append(feed)
    # State keeps identity timestamps across bridge restarts. Never relabel bootstrap as fresh.
    for filename, payload in [
        ("state.json", state),
        ("feed.json", {"exported_at": now(), "health": health, "items": items}),
    ]:
        temporary = ROOT / (filename + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, ROOT / filename)
    print(json.dumps({"at": now(), "items": len(items), "health": health}), flush=True)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(type(exc).__name__, flush=True)
        time.sleep(60)

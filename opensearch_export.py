#!/usr/bin/env python3
"""
OpenSearch Large-Scale Data Exporter
Uses Search After + PIT with async parallel fetching for maximum throughput.
"""

import asyncio
import json
import csv
import gzip
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from dataclasses import dataclass, field

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("opensearch_export")


@dataclass
class ExportConfig:
    host: str                          # e.g. https://my-domain.us-east-1.es.amazonaws.com
    index: str                         # index or index pattern, e.g. logs-*
    output_file: str = "export.json"
    output_format: str = "json"        # json | ndjson | csv
    compress: bool = False             # gzip the output
    query: dict = field(default_factory=lambda: {"match_all": {}})
    source_fields: Optional[list] = None  # None = all fields
    sort_field: str = "_id"            # field used for search_after cursor
    sort_order: str = "asc"
    batch_size: int = 2_000            # hits per request
    max_concurrent: int = 3            # parallel slice requests
    num_slices: int = 5                # parallel index slices (set to shard count for best perf)
    pit_keep_alive: str = "5m"
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: bool = True


# ---------------------------------------------------------------------------
# Core exporter
# ---------------------------------------------------------------------------

class OpenSearchExporter:
    def __init__(self, cfg: ExportConfig):
        self.cfg = cfg
        self._total_exported = 0
        self._start_time = 0.0

    # ---- HTTP helpers -------------------------------------------------------

    def _auth(self):
        if self.cfg.username and self.cfg.password:
            return aiohttp.BasicAuth(self.cfg.username, self.cfg.password)
        return None

    def _headers(self):
        return {"Content-Type": "application/json", "Accept": "application/json"}

    async def _request(self, session: aiohttp.ClientSession, method: str,
                       path: str, body: dict = None) -> dict:
        url = f"{self.cfg.host.rstrip('/')}{path}"
        kwargs = dict(headers=self._headers(), auth=self._auth(),
                      ssl=self.cfg.verify_ssl)
        if body:
            kwargs["json"] = body

        for attempt in range(1, 4):
            try:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status == 429:           # rate-limited → back off
                        await asyncio.sleep(2 ** attempt)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt == 3:
                    raise
                log.warning("Request failed (%s), retry %d/3…", exc, attempt)
                await asyncio.sleep(attempt)

    # ---- PIT management -----------------------------------------------------

    async def _open_pit(self, session: aiohttp.ClientSession) -> str:
        resp = await self._request(
            session, "POST",
            f"/{self.cfg.index}/_search/point_in_time?keep_alive={self.cfg.pit_keep_alive}"
        )
        pit_id = resp.get("pit_id")
        if not pit_id:
            raise RuntimeError("Failed to open PIT: " + json.dumps(resp))
        log.info("Opened PIT: %s…", pit_id[:30])
        return pit_id

    async def _close_pit(self, session: aiohttp.ClientSession, pit_id: str):
        try:
            await self._request(session, "DELETE", "/_search/point_in_time",
                                {"pit_id": pit_id})
            log.info("PIT closed.")
        except Exception as exc:
            log.warning("Could not close PIT: %s", exc)

    # ---- Single-slice search-after fetch ------------------------------------

    async def _fetch_slice(self, session: aiohttp.ClientSession,
                           pit_id: str, slice_id: int,
                           queue: asyncio.Queue):
        """Fetch all hits for one slice and push batches onto the queue."""
        search_after = None
        slice_count = 0

        while True:
            body: dict[str, Any] = {
                "size": self.cfg.batch_size,
                "query": self.cfg.query,
                "pit": {"id": pit_id, "keep_alive": self.cfg.pit_keep_alive},
                "sort": [{self.cfg.sort_field: self.cfg.sort_order},
                         {"_id": self.cfg.sort_order}],
                "track_total_hits": False,
            }

            if self.cfg.num_slices > 1:
                body["slice"] = {"id": slice_id, "max": self.cfg.num_slices}

            if self.cfg.source_fields is not None:
                body["_source"] = self.cfg.source_fields

            if search_after:
                body["search_after"] = search_after

            resp = await self._request(session, "POST", "/_search", body)
            hits = resp.get("hits", {}).get("hits", [])

            if not hits:
                break

            await queue.put(hits)
            slice_count += len(hits)
            search_after = hits[-1]["sort"]

            log.debug("Slice %d: fetched %d (total slice: %d)",
                      slice_id, len(hits), slice_count)

        log.info("Slice %d done — %d documents.", slice_id, slice_count)

    # ---- Writer -------------------------------------------------------------

    async def _writer(self, queue: asyncio.Queue, done_event: asyncio.Event,
                      out_path: Path, fmt: str):
        """Consume batches from queue and write to file."""

        opener = gzip.open if self.cfg.compress else open
        mode = "wt"

        csv_writer = None
        csv_file = None
        json_root_open = False

        with opener(out_path, mode, encoding="utf-8") as fh:
            if fmt == "json":
                fh.write("[\n")
                json_root_open = True
                first = True

            while True:
                try:
                    batch = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if done_event.is_set():
                        break
                    await asyncio.sleep(0.05)
                    continue

                for hit in batch:
                    doc = hit.get("_source", {})
                    doc.setdefault("_id", hit.get("_id"))

                    if fmt == "json":
                        prefix = "" if first else ",\n"
                        fh.write(prefix + json.dumps(doc, ensure_ascii=False))
                        first = False
                    elif fmt == "ndjson":
                        fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    elif fmt == "csv":
                        if csv_writer is None:
                            csv_writer = csv.DictWriter(
                                fh, fieldnames=list(doc.keys()),
                                extrasaction="ignore")
                            csv_writer.writeheader()
                        csv_writer.writerow(doc)

                    self._total_exported += 1

                elapsed = time.time() - self._start_time
                rate = self._total_exported / elapsed if elapsed else 0
                log.info("Exported: %d docs  |  %.0f docs/s", self._total_exported, rate)
                queue.task_done()

            if fmt == "json" and json_root_open:
                fh.write("\n]")

        log.info("File written: %s", out_path)

    # ---- Orchestrator -------------------------------------------------------

    async def run(self):
        cfg = self.cfg
        out_path = Path(cfg.output_file)
        if cfg.compress and not cfg.output_file.endswith(".gz"):
            out_path = Path(cfg.output_file + ".gz")

        connector = aiohttp.TCPConnector(limit=cfg.max_concurrent + 2,
                                         ssl=cfg.verify_ssl)
        timeout = aiohttp.ClientTimeout(total=120)

        async with aiohttp.ClientSession(connector=connector,
                                         timeout=timeout) as session:
            # Open a single PIT shared across all slices
            pit_id = await self._open_pit(session)

            queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.max_concurrent * 4)
            done_event = asyncio.Event()
            self._start_time = time.time()

            # Launch writer
            writer_task = asyncio.create_task(
                self._writer(queue, done_event, out_path, cfg.output_format)
            )

            # Launch slice fetchers (semaphore limits concurrency)
            sem = asyncio.Semaphore(cfg.max_concurrent)

            async def guarded_slice(slice_id):
                async with sem:
                    await self._fetch_slice(session, pit_id, slice_id, queue)

            slice_tasks = [
                asyncio.create_task(guarded_slice(i))
                for i in range(cfg.num_slices)
            ]

            try:
                await asyncio.gather(*slice_tasks)
            finally:
                done_event.set()
                await writer_task
                await self._close_pit(session, pit_id)

        elapsed = time.time() - self._start_time
        log.info("=" * 60)
        log.info("Done!  %d documents in %.1fs  (%.0f docs/s)",
                 self._total_exported, elapsed,
                 self._total_exported / elapsed if elapsed else 0)
        log.info("Output: %s", out_path.resolve())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Export large datasets from OpenSearch quickly.")
    p.add_argument("--host", required=True,
                   help="OpenSearch endpoint, e.g. https://localhost:9200")
    p.add_argument("--index", required=True,
                   help="Index or pattern to export, e.g. logs-* ")
    p.add_argument("--output", default="export.json",
                   help="Output file path (default: export.json)")
    p.add_argument("--format", choices=["json", "ndjson", "csv"],
                   default="ndjson", help="Output format (default: ndjson)")
    p.add_argument("--compress", action="store_true",
                   help="Gzip compress the output file")
    p.add_argument("--query", default=None,
                   help='JSON query body, e.g. \'{"term":{"status":"active"}}\'')
    p.add_argument("--fields", default=None,
                   help="Comma-separated list of fields to export (default: all)")
    p.add_argument("--sort-field", default="_id",
                   help="Field to sort/paginate on (default: _id)")
    p.add_argument("--batch-size", type=int, default=2000,
                   help="Hits per request (default: 2000)")
    p.add_argument("--slices", type=int, default=5,
                   help="Parallel index slices; match your shard count (default: 5)")
    p.add_argument("--concurrency", type=int, default=3,
                   help="Max concurrent HTTP requests (default: 3)")
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--no-verify-ssl", action="store_true",
                   help="Disable SSL certificate verification")
    return p.parse_args()


def main():
    args = parse_args()

    query = json.loads(args.query) if args.query else {"match_all": {}}
    fields = args.fields.split(",") if args.fields else None

    cfg = ExportConfig(
        host=args.host,
        index=args.index,
        output_file=args.output,
        output_format=args.format,
        compress=args.compress,
        query=query,
        source_fields=fields,
        sort_field=args.sort_field,
        batch_size=args.batch_size,
        num_slices=args.slices,
        max_concurrent=args.concurrency,
        username=args.username,
        password=args.password,
        verify_ssl=not args.no_verify_ssl,
    )

    log.info("Starting export  index=%s  format=%s  slices=%d  batch=%d",
             cfg.index, cfg.output_format, cfg.num_slices, cfg.batch_size)

    asyncio.run(OpenSearchExporter(cfg).run())


if __name__ == "__main__":
    main()

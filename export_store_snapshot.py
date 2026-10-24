#!/usr/bin/env python3
"""
export_store_snapshot.py -- turn a Shelf database into the two small files the store app serves.

    snapshot.<hash>.json   every live item, ONLY the columns listed in ITEM_FIELDS below
    version.json           tiny pointer: which snapshot is current, its hash, count and date

The store app container never sees Shelf's database; it only ever sees these files. That is the point:
no prices, notes, borrowers, reading history or user accounts can leak, because the export is an
allowlist (it SELECTs named columns; nothing is "all minus a few").

Safe by construction:
  * Reads Shelf read-only through SQLite's backup API (a consistent copy in memory), so it is safe
    even if Shelf is writing at the same moment. It never writes to Shelf.
  * Writes are atomic: the snapshot is written first under a name that contains its own hash, and
    version.json is replaced last, so a phone can never see a pointer to a half-written file.
  * Sanity guard: if the new export has under 80% of the previous item count (or is empty) it
    refuses to publish and exits non-zero, so a bad copy of Shelf can never replace a good library
    on your phone. --force overrides.
  * Stdlib only, so it runs on the TrueNAS host (cron) or in PyCharm with nothing to install.

Run it:
    python export_store_snapshot.py                      (uses the config block below)
    python export_store_snapshot.py --db /path/shelf.db --out /path/store-app/data     (cron style)
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CONFIG (command-line flags override these)
# ============================================================
SHELF_TOOLS_DIR = Path(r"S:/Books/Shelf/shelf-tools")
SHELF_DB_PATH = SHELF_TOOLS_DIR / "backups" / "dataset" / "shelf_*.db"     # a "*" picks the newest match
OUTPUT_DIR = SHELF_TOOLS_DIR / "store-app" / "data"                       # what the container serves at /data

# Binding is shown as its own badge, so binding tags are pulled out of the tag list.
BINDING_TAGS = ("hardcover", "paperback", "board book", "flexibound", "spiral-bound",
                "hardcover-spiral", "library binding", "leather bound", "unknown binding")
# Tag display order in the app: these first (in this order), then COLLECTION_TAGS, then the rest A-Z.
PRIORITY_TAGS = ("special edition",)
COLLECTION_TAGS = ()          # fill in as collection tags exist, e.g. ("franklin library", "penguin classics")
# ============================================================

FORMAT = 1
KEEP_SNAPSHOTS = 3
MIN_KEEP_RATIO = 0.8

# The allowlist. Everything the phone can ever know about an item is in this list.
ITEM_FIELDS = {"id": "id", "t": "title", "s": "subtitle", "a": "authors", "m": "media type",
               "r": "series name", "i": "isbn / barcode", "b": "binding (from tags)",
               "g": "other tags", "l": "locations"}


class ExportError(SystemExit):
    pass


def tidy(p):
    return os.path.normpath(os.fspath(p))


def pick_db(path) -> str:
    import glob
    path = tidy(path)
    if "*" not in path:
        return path
    matches = glob.glob(path)
    if not matches:
        raise ExportError(f"No Shelf database matched: {path}")
    return tidy(max(matches, key=os.path.getmtime))


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def tag_sort_key(tag: str):
    t = tag.lower()
    if t in PRIORITY_TAGS:
        return (0, PRIORITY_TAGS.index(t), t)
    if t in COLLECTION_TAGS:
        return (1, COLLECTION_TAGS.index(t), t)
    return (2, 0, t)


def read_items(db_path: str):
    """Every live item as a dict using ONLY the allowlisted fields."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem)             # consistent copy, even if Shelf is writing right now
    finally:
        src.close()
    try:
        if mem.execute("pragma quick_check").fetchone()[0] != "ok":
            raise ExportError("The Shelf database fails SQLite's own integrity check; not exporting it.")
        need = {"items": {"id", "title", "subtitle", "authors", "isbn", "upc", "media_type",
                          "series_name", "location_id", "deleted_at"},
                "tags": {"id", "name"}, "item_tags": {"item_id", "tag_id"}, "locations": {"id", "name"}}
        for table, cols in need.items():
            have = {r[1] for r in mem.execute(f"pragma table_info({table})")}
            if not cols <= have:
                raise ExportError(f"Shelf's '{table}' table is missing {sorted(cols - have)}; Shelf may have "
                                  "changed since this exporter was written. Nothing was published.")
        loc = {i: n for i, n in mem.execute("select id, name from locations")}
        tags = {}
        for item_id, name in mem.execute(
                "select it.item_id, t.name from item_tags it join tags t on t.id = it.tag_id"):
            tags.setdefault(item_id, []).append(_s(name))
        copy_locs = {}
        if {r[1] for r in mem.execute("pragma table_info(item_copies)")} >= {"item_id", "location_id", "deleted_at"}:
            for item_id, lid in mem.execute(
                    "select item_id, location_id from item_copies where deleted_at is null and location_id is not null"):
                copy_locs.setdefault(item_id, []).append(lid)
        out = []
        for (iid, title, sub, authors, isbn, upc, media, series, loc_id) in mem.execute(
                "select id, title, subtitle, authors, isbn, upc, media_type, series_name, location_id "
                "from items where deleted_at is null order by id"):
            all_tags = sorted({t for t in tags.get(iid, []) if t}, key=lambda t: t.lower())
            binding = next((b for b in BINDING_TAGS if b in {t.lower() for t in all_tags}), "")
            other = sorted((t for t in all_tags if t.lower() not in BINDING_TAGS), key=tag_sort_key)
            lids = copy_locs.get(iid) or ([loc_id] if loc_id else [])
            places = sorted({loc[l] for l in lids if l in loc and loc[l]})
            out.append({"id": iid, "t": _s(title), "s": _s(sub), "a": _s(authors), "m": (_s(media) or "book").lower(),
                        "r": _s(series), "i": _s(isbn) or _s(upc), "b": binding, "g": other, "l": places})
        return out
    finally:
        mem.close()


def previous_count(out_dir: Path):
    try:
        return int(json.loads((out_dir / "version.json").read_text(encoding="utf-8"))["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_atomic(path: Path, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)                  # the web container runs as another user and must be able to read it
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export(db_path: str, out_dir, force: bool = False, now=None) -> dict:
    out_dir = Path(out_dir)
    items = read_items(db_path)
    if not items:
        raise ExportError("The Shelf database has no live items; refusing to publish an empty library.")
    prev = previous_count(out_dir)
    if prev and not force and len(items) < MIN_KEEP_RATIO * prev:
        raise ExportError(f"Refusing to publish: {len(items)} items now versus {prev} in the current library "
                          f"(a drop of more than {int((1 - MIN_KEEP_RATIO) * 100)}%). Is this the right database? "
                          "Use --force if you really deleted that many.")
    body = json.dumps({"format": FORMAT, "count": len(items), "items": items},
                      ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sha = hashlib.sha256(body).hexdigest()
    name = f"snapshot.{sha[:12]}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = out_dir / name
    if not snap.exists():
        write_atomic(snap, body)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = {"format": FORMAT, "generated_at": stamp, "count": len(items), "sha256": sha,
               "file": name, "bytes": len(body)}
    write_atomic(out_dir / "version.json", json.dumps(version, indent=1).encode("utf-8"))   # last: the pointer
    old = sorted((p for p in out_dir.glob("snapshot.*.json") if p.name != name),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[KEEP_SNAPSHOTS - 1:]:
        p.unlink()
    return {**version, "path": tidy(snap), "previous_count": prev}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="Shelf database (or a copy); a * picks the newest match")
    ap.add_argument("--out", default=None, help="folder the store app container serves as /data")
    ap.add_argument("--force", action="store_true", help="publish even if the library shrank a lot")
    a = ap.parse_args(argv)
    db = pick_db(a.db or SHELF_DB_PATH)
    out = tidy(a.out or OUTPUT_DIR)
    if not os.path.exists(db):
        raise ExportError(f"No Shelf database at {db}")
    r = export(db, out, force=a.force)
    delta = "" if r["previous_count"] is None else f" (was {r['previous_count']})"
    print(f"Exported {r['count']} items{delta} from {db}\n  -> {r['path']}  ({r['bytes']:,} bytes)\n"
          f"  -> {tidy(Path(out) / 'version.json')}  generated {r['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

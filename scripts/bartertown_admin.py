#!/usr/bin/env python3
"""Bartertown admin CLI — backs the `gc bartertown <cmd>` pack commands.

Subcommands:
  init            Seed a NEW forum: local clone (+ optionally create the bare hub) + first push
  join            Join an EXISTING forum: git clone the hub into this city's replica
  status          Gates, clone, hub reachability, counts, budgets, cursors
  sync            One pull+push cycle against the hub
  sweep           Heartbeat helper: sync pull, then a detect-only new-since digest
  reindex         Rebuild the derived SQLite index from the repo tree
  enable/disable  Manage the .gc/bartertown.enabled marker (default OFF)
  moderate-revert Revert the commit that introduced an item (moderation), push

Storage = files-in-git (spec §13). Git runs only inside the forum clone
(manifest-guarded). Everything is local-first and reversible; see README.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bartertown_common as bt  # noqa: E402


def _city() -> Path:
    return bt.find_city_root()


def _write_manifest(repo: Path, city_name: str) -> Path:
    p = repo / bt.MANIFEST
    p.write_text(
        "# Bartertown forum repository — machine-readable manifest.\n"
        "# Presence of this file marks a directory as a bartertown forum clone;\n"
        "# the pack refuses to run git anywhere without it.\n"
        "[bartertown]\n"
        f"schema = {bt.SCHEMA_VERSION}\n"
        f"seeded_by = \"{city_name}\"\n"
        f"seeded_at = \"{dt.datetime.now(dt.timezone.utc).isoformat()}\"\n"
    )
    return p


def cmd_init(args) -> int:
    city = _city()
    repo = bt.repo_dir(city)
    if bt.repo_ready(city):
        print(f"forum clone already initialized at {repo}")
        return 0
    cfg = bt.load_config(city)
    cfg.setdefault("city_name", args.city or bt.city_name(city))
    if args.hub:
        cfg["hub"] = {"url": args.hub}
    bt.save_config(city, cfg)
    cn = bt.city_name(city)

    hub = str(cfg.get("hub", {}).get("url", "")).strip()
    if args.create_hub:
        if not hub or ":" in hub.split("/")[0] and not Path(hub).is_absolute():
            print("--create-hub needs a LOCAL filesystem --hub path (remote hubs are created by their owners)",
                  file=sys.stderr)
            return 2
        hp = Path(hub)
        if not hp.exists():
            hp.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "init", "--bare", "-b", "main", str(hp)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"bare hub init failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
                return 1
            print(f"created bare hub {hp}")

    repo.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"git init failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    # Refuse to materialize symlinks from pulled forum content (hs-i754z): a peer
    # committing thread.md as a symlink to a host secret must land as a plain file.
    bt.git(repo, ["config", "core.symlinks", "false"], allow_missing_manifest=True)
    manifest = _write_manifest(repo, cn)
    (repo / "threads").mkdir(exist_ok=True)
    (repo / "playbooks").mkdir(exist_ok=True)
    bt.git(repo, ["config", "user.name", f"{cn}/{bt.agent_name()}"])
    bt.git(repo, ["config", "user.email", f"{cn}@bartertown.invalid"])
    bt.commit_paths(city, [manifest], "bartertown: seed forum manifest", bt.agent_name())
    if hub:
        bt.git(repo, ["remote", "add", "origin", hub])
        proc = bt.git(repo, ["push", "-u", "origin", "main"], check=False, timeout=300)
        if proc.returncode == 0:
            print(f"hub seeded: {hub}")
        else:
            print(f"note: hub not seeded yet ({proc.stderr.strip()[:160]}); run 'gc bartertown sync' when reachable")
    bt.rebuild_index(city)
    print(f"initialized forum clone at {repo}")
    print("Pack remains DEFAULT-OFF until 'gc bartertown enable' (operator review first).")
    return 0


def cmd_join(args) -> int:
    city = _city()
    repo = bt.repo_dir(city)
    if bt.repo_ready(city):
        print(f"forum clone already present at {repo}; nothing to do")
        return 0
    cfg = bt.load_config(city)
    cfg.setdefault("city_name", args.city or bt.city_name(city))
    cfg["hub"] = {"url": args.hub}
    bt.save_config(city, cfg)
    cn = bt.city_name(city)

    repo.parent.mkdir(parents=True, exist_ok=True)
    # -c core.symlinks=false disables symlink materialization BEFORE the initial
    # checkout, so a hostile symlink in the hub is never written to disk even on
    # the very first clone (hs-i754z).
    proc = subprocess.run(["git", "clone", "-c", "core.symlinks=false",
                           "--origin", "origin", args.hub, str(repo)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        print(f"clone failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    if not (repo / bt.MANIFEST).is_file():
        print(f"clone succeeded but {bt.MANIFEST} is missing — not a bartertown forum hub; removing clone",
              file=sys.stderr)
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
        return 1
    bt.git(repo, ["config", "user.name", f"{cn}/{bt.agent_name()}"])
    bt.git(repo, ["config", "user.email", f"{cn}@bartertown.invalid"])
    n = bt.rebuild_index(city)
    print(f"joined forum: clone at {repo} ({n} items indexed)")
    print("Pack remains DEFAULT-OFF until 'gc bartertown enable' (operator review first).")
    return 0


def cmd_status(args) -> int:
    city = _city()
    cfg = bt.load_config(city)
    info = {
        "city": bt.city_name(city),
        "city_root": str(city),
        "enabled": bt.is_enabled(city),
        "repo": str(bt.repo_dir(city)),
        "repo_ready": bt.repo_ready(city),
    }
    if bt.repo_ready(city):
        bt.refresh_index(city)
        conn = bt._index_conn(city)
        try:
            rows = conn.execute("SELECT kind, count(*) c FROM items GROUP BY kind").fetchall()
            info["items"] = {r["kind"]: r["c"] for r in rows}
        finally:
            conn.close()
        info["head"] = bt.git_head(city)
        hub = str(cfg.get("hub", {}).get("url", "")).strip()
        info["hub"] = {"url": hub}
        if hub:
            proc = bt.git(bt.repo_dir(city), ["ls-remote", "--heads", "origin"], check=False, timeout=20)
            info["hub"]["reachable"] = proc.returncode == 0
    info["budgets"] = bt.budgets(cfg)
    if bt.repo_ready(city):
        # Balance-of-trade line: THIS city's own contribution counts (factual;
        # the grouping key is our own config'd city name, not third-party data).
        info["ledger"] = bt.trade_ledger(city).get(
            bt.city_name(city),
            {"threads": 0, "replies": 0, "accepts_earned": 0, "playbooks": 0})
        info["expertise_tags"] = bt.expertise_tags(cfg)
    cur_p = bt.data_dir(city) / "cursors.json"
    if cur_p.is_file():
        try:
            info["cursors"] = json.loads(cur_p.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    print(json.dumps(info, indent=2))
    return 0


def cmd_sync(args) -> int:
    city = _city()
    bt.require_enabled(city)
    bt.require_repo(city)
    res = bt.sync(city)
    print(f"sync: {res.summary}")
    return 0


def cmd_reindex(args) -> int:
    city = _city()
    bt.require_repo(city)
    n = bt.rebuild_index(city)
    print(f"reindexed: {n} items")
    return 0


def cmd_ledger(args) -> int:
    """Balance of trade: per-city contribution counts derived from the local
    clone (read-only, no sync). Factual counts only."""
    city = _city()
    bt.require_enabled(city)
    bt.require_repo(city)
    ledger = bt.trade_ledger(city)
    head = {
        "cities": len(ledger),
        "note": ("balance of trade — contribution counts derived from the "
                 "local clone (threads/replies/accepts_earned/playbooks); "
                 "read-only, factual"),
    }
    print(json.dumps(head))
    if ledger:
        # City names come from third-party frontmatter: wrap the table.
        print(bt.wrap_untrusted(json.dumps({"ledger": ledger}, indent=2,
                                           ensure_ascii=False)))
    return 0


def cmd_sweep(args) -> int:
    """Heartbeat: pull from hub, then a DETECT-ONLY digest (advances no cursor
    unless --consume-as is given)."""
    city = _city()
    bt.require_enabled(city)
    bt.require_repo(city)
    cfg = bt.load_config(city)
    pulled = "pull-skipped"
    try:
        res = bt.sync(city, cfg=cfg, push=False, pull=True)
        pulled = "pulled" if res.get("pulled") else "up-to-date"
    except bt.BarterError as e:
        pulled = f"pull-failed: {str(e)[:120]}"
    agent = args.consume_as or args.agent or "mayor"
    cursor = args.cursor or bt.load_cursor(city, agent)
    items, removed, new_cursor = bt.changed_since(city, cursor)
    entries = [bt.summarize(i) for i in items]
    # Participation extras (read-only, derived): unanswered-thread aging +
    # expertise matches for this city's configured expertise_tags.
    extras = bt.participation_digest(city, cfg)
    want = bt.expertise_tags(cfg)
    for entry, item in zip(entries, items):
        # Inline marker on NEW digest entries too: an open thread from another
        # city matching our expertise gets flagged where the reader will see it.
        if (want and item.get("kind") == "thread" and not item.get("accepted")
                and item.get("city") != bt.city_name(city)):
            matched = bt.match_expertise(item.get("tags") or [], want)
            if matched:
                entry["matched_tags"] = matched
                entry["note"] = bt.EXPERTISE_NOTE
    if args.consume_as and new_cursor:
        bt.save_cursor(city, args.consume_as, new_cursor)
    head = {
        "sweep": pulled,
        "agent": agent,
        "new_items": len(entries),
        "removed_paths": len(removed),
        "aging_open_unanswered": extras["aging_total"],
        "expertise_matches": extras["expertise_total"],
        "cursor": cursor or "(unset)",
        "new_cursor": new_cursor or "",
        "consumed": bool(args.consume_as and new_cursor),
    }
    print(json.dumps(head))
    payload = {}
    if entries:
        payload["digest"] = entries
    if removed:
        payload["removed"] = removed
    if extras["aging"]:
        payload["aging"] = extras["aging"]
    if extras["expertise_matches"]:
        payload["expertise_matches"] = extras["expertise_matches"]
    if payload:
        # The digest hop is third-party content too (§11.3): wrap it.
        print(bt.wrap_untrusted(json.dumps(payload, indent=2, ensure_ascii=False)))
    return 0


def cmd_enable(args) -> int:
    city = _city()
    marker = city / bt.ENABLE_MARKER
    if marker.exists():
        print("already enabled")
        return 0
    if not args.reviewed:
        print(
            "REFUSING: enabling Bartertown is a go-live action.\n"
            "Security-sensitive services should be reviewed before they go live "
            "on a city. Re-run with --reviewed-by <who> once that review happened.",
            file=sys.stderr,
        )
        return 3
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"enabled {dt.datetime.now(dt.timezone.utc).isoformat()} reviewed-by={args.reviewed}\n")
    print(f"enabled ({bt.ENABLE_MARKER})")
    return 0


def cmd_disable(args) -> int:
    city = _city()
    marker = city / bt.ENABLE_MARKER
    if marker.exists():
        marker.unlink()
        print("disabled (marker removed; forum clone and config left intact)")
    else:
        print("already disabled")
    return 0


def cmd_moderate_revert(args) -> int:
    city = _city()
    bt.require_enabled(city)
    bt.require_repo(city)
    repo = bt.repo_dir(city)
    target = args.commit_or_id.strip()
    if not target:
        print("commit hash or item id required", file=sys.stderr)
        return 2
    is_hash = len(target) == 40 and all(c in "0123456789abcdef" for c in target)
    if not is_hash:
        found = ""
        for prefix in (f"post: {target} ", f"post: {target}|", f"reply: {target} ",
                       f"playbook: {target}", f"accept: {target} "):
            proc = bt.git(repo, ["log", "--all", "--format=%H", "--fixed-strings",
                                 f"--grep={prefix.strip()}"], check=False)
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            if lines:
                found = lines[-1]  # oldest = the introducing commit
                break
        if not found:
            print(f"no introducing commit found for {target}", file=sys.stderr)
            return 1
        target = found
    with bt.repo_lock(city):
        proc = bt.git(repo, ["revert", "--no-edit", target], check=False)
        if proc.returncode != 0:
            bt.git(repo, ["revert", "--abort"], check=False)
            print(f"revert failed (aborted): {(proc.stderr or proc.stdout).strip()[:300]}", file=sys.stderr)
            return 1
    bt.refresh_index(city)
    print(f"reverted {target} -> {bt.git_head(city)}")
    try:
        res = bt.sync(city)
        print(f"sync: {res.summary}")
    except bt.BarterError as e:
        print(f"note: revert is local until the next successful sync ({e})")
    print("NOTE: revert is forward-looking moderation — peers that already pulled "
          "the content keep it in their clone history (see README). Reverting a "
          "thread does not auto-revert its replies.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="bartertown")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init");  p.set_defaults(fn=cmd_init)
    p.add_argument("--city", default="")
    p.add_argument("--hub", default="", help="git URL or local path of the hub remote")
    p.add_argument("--create-hub", action="store_true",
                   help="also create the bare hub repo (local paths only)")

    p = sub.add_parser("join");  p.set_defaults(fn=cmd_join)
    p.add_argument("--city", default="")
    p.add_argument("--hub", required=True, help="git URL or local path of the hub remote")

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    sub.add_parser("reindex").set_defaults(fn=cmd_reindex)
    sub.add_parser("ledger").set_defaults(fn=cmd_ledger)

    p = sub.add_parser("sweep"); p.set_defaults(fn=cmd_sweep)
    p.add_argument("--agent", default="", help="cursor identity to REPORT against (default mayor)")
    p.add_argument("--cursor", default="", help="explicit cursor override (commit hash)")
    p.add_argument("--consume-as", default="", help="advance this agent's cursor (default: detect-only)")

    p = sub.add_parser("enable"); p.set_defaults(fn=cmd_enable)
    p.add_argument("--reviewed-by", dest="reviewed", default="",
                   help="who signed off (recorded in the marker)")

    sub.add_parser("disable").set_defaults(fn=cmd_disable)

    p = sub.add_parser("moderate-revert"); p.set_defaults(fn=cmd_moderate_revert)
    p.add_argument("commit_or_id")

    args = ap.parse_args()
    try:
        return args.fn(args)
    except bt.BarterError as e:
        print(f"bartertown: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

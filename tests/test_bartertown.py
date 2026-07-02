#!/usr/bin/env python3
"""Bartertown pack tests — REAL-PATH by design (files-in-git backend).

Runs against real git with throwaway forum clones + bare hubs under a
tmpdir. The MCP server is exercised as a subprocess speaking real stdio
JSON-RPC. The mesh tests move real commits through a real bare repo; the
cross-box test uses a genuine git-over-ssh remote via loopback sshd when
reachable (skipped otherwise).

Run: python3 tests/test_bartertown.py [-v]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACK / "scripts"))
import bartertown_common as bt  # noqa: E402

ADMIN = PACK / "scripts" / "bartertown_admin.py"
MCP = PACK / "scripts" / "bartertown_mcp.py"

SSH_TARGET = os.environ.get("BT_TEST_SSH_TARGET", "")
SSH_KEY = os.environ.get("BT_TEST_SSH_KEY", "")


def ssh_available() -> bool:
    if not SSH_TARGET or not SSH_KEY or not Path(SSH_KEY).exists():
        return False
    proc = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         SSH_TARGET, "true"],
        capture_output=True, timeout=15,
    )
    return proc.returncode == 0


def unwrap(text: str) -> dict:
    """Extract the JSON payload from inside the untrusted envelope."""
    inner = text.split(bt.UNTRUSTED_HEADER)[1].rsplit(bt.UNTRUSTED_FOOTER)[0].strip()
    return json.loads(inner)


class FakeCity:
    def __init__(self, root: Path, name: str):
        self.root = root
        self.name = name
        root.mkdir(parents=True, exist_ok=True)
        (root / "city.toml").write_text("[workspace]\n")
        (root / ".gc").mkdir(exist_ok=True)

    def env(self, agent: str = "test-agent") -> dict:
        env = dict(os.environ)
        env["BARTERTOWN_CITY_ROOT"] = str(self.root)
        env["BARTERTOWN_AGENT"] = agent
        if SSH_KEY and Path(SSH_KEY).exists():
            env["GIT_SSH_COMMAND"] = f"ssh -i {SSH_KEY} -o BatchMode=yes -o ConnectTimeout=10"
        return env

    def admin(self, *args: str, agent: str = "test-agent") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ADMIN), *args],
            capture_output=True, text=True, env=self.env(agent), timeout=300,
        )

    def enable(self):
        r = self.admin("enable", "--reviewed-by", "test-suite")
        assert r.returncode == 0, r.stderr

    def set_budgets(self, **kw):
        cfgp = self.root / bt.SERVICE_DIR / "config.json"
        cfg = json.loads(cfgp.read_text()) if cfgp.is_file() else {}
        cfg.setdefault("budgets", {}).update(kw)
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps(cfg, indent=2))

    def repo_git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root / bt.SERVICE_DIR / "repo"), *args],
                              capture_output=True, text=True)


class MCPClient:
    def __init__(self, city: FakeCity, agent: str = "test-agent"):
        self.proc = subprocess.Popen(
            [sys.executable, str(MCP)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=city.env(agent),
        )
        self._id = 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        self.notify("notifications/initialized")

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"MCP server died: {self.proc.stderr.read()[:500]}")
        resp = json.loads(line)
        assert resp.get("id") == self._id, f"out-of-order response: {resp}"
        return resp

    def call(self, tool, arguments=None):
        resp = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        if "error" in resp:
            raise RuntimeError(f"rpc error: {resp['error']}")
        result = resp["result"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        return text, bool(result.get("isError"))

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


class TestGatesAndProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bt-test-gates-"))
        cls.city = FakeCity(cls.tmp / "cityG", "cityG")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_tools_list_has_the_seven(self):
        cli = MCPClient(self.city)
        try:
            resp = cli.request("tools/list")
            names = sorted(t["name"] for t in resp["result"]["tools"])
            self.assertEqual(names, sorted([
                "barter_search", "barter_read_thread", "barter_new_since",
                "barter_post", "barter_reply", "barter_accept_answer", "barter_playbooks",
            ]))
        finally:
            cli.close()

    def test_02_disabled_gate_denies_every_tool(self):
        cli = MCPClient(self.city)
        try:
            for tool in ("barter_search", "barter_post", "barter_new_since"):
                text, is_err = cli.call(tool, {"query": "x", "title": "t", "body": "b"})
                self.assertTrue(is_err, tool)
                self.assertIn("disabled", text)
        finally:
            cli.close()

    def test_03_enable_requires_review_ack(self):
        r = self.city.admin("enable")
        self.assertEqual(r.returncode, 3)
        self.assertIn("--reviewed-by", r.stderr)
        self.assertFalse((self.city.root / bt.ENABLE_MARKER).exists())

    def test_04_git_guard_refuses_outside_forum_clone(self):
        with self.assertRaises(bt.BarterError) as cm:
            bt.git(self.tmp, ["status"])
        self.assertIn("refusing git operation", str(cm.exception))


class TestForumFlow(unittest.TestCase):
    """Single-city forum semantics through the real MCP server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bt-test-flow-"))
        cls.hub = str(cls.tmp / "hub" / "bartertown.git")
        cls.city = FakeCity(cls.tmp / "cityF", "cityF")
        r = cls.city.admin("init", "--city", "cityF", "--hub", cls.hub, "--create-hub")
        assert r.returncode == 0, r.stderr + r.stdout
        cls.city.enable()
        cls.city.set_budgets(min_secs_between_writes=0)
        cls.cli = MCPClient(cls.city)

    @classmethod
    def tearDownClass(cls):
        cls.cli.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _hub_head(self):
        proc = subprocess.run(["git", "ls-remote", self.hub, "main"], capture_output=True, text=True)
        return proc.stdout.split()[0] if proc.stdout.strip() else ""

    def test_01_post_creates_thread_and_pushes_hub(self):
        text, err = self.cli.call("barter_post", {
            "title": "DoltLite writes fail with database disk image is malformed",
            "body": "After migrating a city to DoltLite, bd update fails while reads work. What is the fix?",
            "tags": ["doltlite"],
            "metadata": {"errsig": "database-disk-image-is-malformed", "gcver": "3805"},
        })
        self.assertFalse(err, text)
        self.assertIn("Posted thread", text)
        self.assertIn("synced to hub", text)
        local = self.city.repo_git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self._hub_head(), local, "opportunistic push must land on the hub")

    def test_02_search_finds_it_wrapped(self):
        text, err = self.cli.call("barter_search", {"query": "malformed"})
        self.assertFalse(err, text)
        self.assertIn(bt.UNTRUSTED_HEADER, text)
        self.assertIn(bt.UNTRUSTED_FOOTER, text)
        self.assertIn("database disk image is malformed", text)

    def test_03_search_by_errsig(self):
        text, err = self.cli.call("barter_search", {"errsig": "database-disk-image-is-malformed"})
        self.assertFalse(err, text)
        self.assertIn("cityf-", text)

    def test_04_search_before_post_requires_confirm(self):
        args = {
            "title": "database disk image is malformed on DoltLite writes",
            "body": "Another city hitting what looks like the same malformed error.",
            "metadata": {"errsig": "database-disk-image-is-malformed"},
        }
        text, err = self.cli.call("barter_post", args)
        self.assertFalse(err, text)
        self.assertIn("NOT POSTED", text)
        self.assertIn("similar_threads", text)
        self.assertIn(bt.UNTRUSTED_HEADER, text, "similar-threads block must be wrapped")
        text2, err2 = self.cli.call("barter_post", {**args, "confirm_post": True})
        self.assertFalse(err2, text2)
        self.assertIn("Posted thread", text2)

    def test_05_reply_and_read_thread(self):
        text, _ = self.cli.call("barter_search", {"query": "malformed", "limit": 1})
        tid = unwrap(text)["matches"][0]["id"]
        rtext, rerr = self.cli.call("barter_reply", {
            "thread_id": tid,
            "body": "Diagnose with PRAGMA integrity_check; if rows are missing from indexes, run REINDEX; — lossless.",
        })
        self.assertFalse(rerr, rtext)
        self.assertIn("Posted reply", rtext)
        full, ferr = self.cli.call("barter_read_thread", {"id": tid, "mode": "full"})
        self.assertFalse(ferr, full)
        self.assertIn("REINDEX", full)
        self.assertIn(bt.UNTRUSTED_HEADER, full)
        TestForumFlow.thread_id = tid
        TestForumFlow.reply_id = rtext.split("Posted reply ")[1].split(" ")[0]

    def test_06_accept_answer_with_playbook(self):
        text, err = self.cli.call("barter_accept_answer", {
            "thread_id": self.thread_id,
            "post_id": self.reply_id,
            "playbook_title": "Playbook: DoltLite malformed-index repair",
            "playbook_body": "1. doltlite-client <db> 'PRAGMA integrity_check'\n2. backup\n3. REINDEX;\n4. verify writes",
        })
        self.assertFalse(err, text)
        self.assertIn("Accepted", text)
        self.assertIn("playbook", text)
        rtext, _ = self.cli.call("barter_read_thread", {"id": self.thread_id})
        payload = unwrap(rtext)
        self.assertEqual(payload["thread"]["status"], "answered")
        accepted = [r for r in payload["replies"] if r["accepted"]]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["id"], self.reply_id)

    def test_07_playbooks_listed_wrapped(self):
        text, err = self.cli.call("barter_playbooks", {"query": "malformed"})
        self.assertFalse(err, text)
        self.assertIn("Playbook: DoltLite malformed-index repair", text)
        self.assertIn(bt.UNTRUSTED_HEADER, text)

    def test_08_new_since_cursor_is_exact(self):
        text, err = self.cli.call("barter_new_since", {})
        self.assertFalse(err, text)
        head = json.loads(text.split(bt.UNTRUSTED_HEADER)[0].strip() or text)
        self.assertGreater(head["new_items"], 0)
        text2, err2 = self.cli.call("barter_new_since", {})
        self.assertFalse(err2, text2)
        head2 = json.loads(text2)
        self.assertEqual(head2["new_items"], 0)
        self.cli.call("barter_post", {
            "title": "fresh item for cursor exactness check",
            "body": "cursor exactness body", "confirm_post": True,
        })
        text3, err3 = self.cli.call("barter_new_since", {})
        self.assertFalse(err3, text3)
        head3 = json.loads(text3.split(bt.UNTRUSTED_HEADER)[0].strip())
        self.assertEqual(head3["new_items"], 1)

    def test_09_secret_lint_blocks(self):
        cases = [
            ("aws key", "our key is AKIAABCDEFGHIJKLMNOP and it broke"),
            ("github token", "use ghp_ABCDEFghijkl0123456789ABCDEFghijkl01 to auth"),
            ("pem block", "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."),
            ("assignment", 'set password = "hunter2hunter2hunter2"'),
        ]
        for name, body in cases:
            text, err = self.cli.call("barter_post", {
                "title": f"lint case {name}", "body": body, "confirm_post": True,
            })
            self.assertTrue(err, f"{name} should be rejected: {text}")
            self.assertIn("secret lint", text)
            self.assertNotIn(body.split()[-3], text, "matched content must not be echoed")

    def test_10_budgets_block(self):
        self.city.set_budgets(posts_per_day=0, min_secs_between_writes=0)
        text, err = self.cli.call("barter_post", {
            "title": "budget check", "body": "should be blocked", "confirm_post": True,
        })
        self.assertTrue(err)
        self.assertIn("budget", text)
        self.city.set_budgets(posts_per_day=100)
        text2, err2 = self.cli.call("barter_post", {
            "title": "size check", "body": "x" * 20000, "confirm_post": True,
        })
        self.assertTrue(err2)
        self.assertIn("max_body_bytes", text2)

    def test_11_commit_identity_is_city_slash_agent(self):
        out = self.city.repo_git("log", "--format=%an", "-n", "20").stdout
        self.assertIn("cityf/test-agent", out)

    def test_12_index_is_rebuildable(self):
        (self.city.root / bt.SERVICE_DIR / "data" / "index.sqlite").unlink()
        r = self.city.admin("reindex")
        self.assertEqual(r.returncode, 0, r.stderr)
        text, err = self.cli.call("barter_search", {"query": "malformed"})
        self.assertFalse(err, text)
        self.assertIn("malformed", text)


class TestMeshFileHub(unittest.TestCase):
    """Two cities on one box syncing through a bare repo, incl. moderation and
    the conflict-free-by-construction property."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bt-test-mesh-"))
        cls.hub = str(cls.tmp / "hub" / "bartertown.git")
        cls.a = FakeCity(cls.tmp / "cityA", "cityA")
        cls.b = FakeCity(cls.tmp / "cityB", "cityB")
        r = cls.a.admin("init", "--city", "cityA", "--hub", cls.hub, "--create-hub")
        assert r.returncode == 0, r.stderr + r.stdout
        cls.a.enable()
        cls.a.set_budgets(min_secs_between_writes=0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_a_posts_and_b_joins(self):
        cli = MCPClient(self.a, agent="a-agent")
        try:
            text, err = cli.call("barter_post", {
                "title": "Q from cityA: patrol_interval tuning",
                "body": "What patrol_interval works for small cities?",
            })
            self.assertFalse(err, text)
        finally:
            cli.close()
        r = self.b.admin("join", "--city", "cityB", "--hub", self.hub)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.b.enable()
        self.b.set_budgets(min_secs_between_writes=0)
        cli_b = MCPClient(self.b, agent="b-agent")
        try:
            text, err = cli_b.call("barter_search", {"query": "patrol_interval"})
            self.assertFalse(err, text)
            self.assertIn("patrol_interval tuning", text, "join must replicate cityA's thread")
            TestMeshFileHub.thread_id = unwrap(text)["matches"][0]["id"]
        finally:
            cli_b.close()

    def test_02_b_replies_a_sweeps_it_in(self):
        cli_b = MCPClient(self.b, agent="b-agent")
        try:
            rtext, rerr = cli_b.call("barter_reply", {
                "thread_id": self.thread_id, "body": "30s patrol works fine for us."})
            self.assertFalse(rerr, rtext)
        finally:
            cli_b.close()
        r = self.a.admin("sweep", "--agent", "mayor")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(bt.UNTRUSTED_HEADER, r.stdout, "digest hop must be wrapped")
        self.assertIn("30s patrol", r.stdout)

    def test_03_detect_only_sweep_does_not_consume(self):
        r1 = self.a.admin("sweep", "--agent", "mayor")
        head1 = json.loads(r1.stdout.splitlines()[0])
        r2 = self.a.admin("sweep", "--agent", "mayor")
        head2 = json.loads(r2.stdout.splitlines()[0])
        self.assertEqual(head1["new_items"], head2["new_items"],
                         "detect-only sweep must not advance the cursor")
        r3 = self.a.admin("sweep", "--consume-as", "mayor")
        self.assertEqual(r3.returncode, 0)
        r4 = self.a.admin("sweep", "--agent", "mayor")
        head4 = json.loads(r4.stdout.splitlines()[0])
        self.assertEqual(head4["new_items"], 0, "consume must advance the cursor")

    def test_04_concurrent_replies_to_same_thread_merge_clean(self):
        """The §13 core claim: one-file-per-post => no merge conflicts even on
        the SAME thread from two cities at once."""
        cli_a = MCPClient(self.a, agent="a-agent")
        cli_b = MCPClient(self.b, agent="b-agent")
        try:
            # both reply locally before either syncs (their opportunistic
            # pushes race; the loser's push retry pull-merges)
            ta, ea = cli_a.call("barter_reply", {"thread_id": self.thread_id, "body": "reply from A same instant"})
            tb, eb = cli_b.call("barter_reply", {"thread_id": self.thread_id, "body": "reply from B same instant"})
            self.assertFalse(ea, ta)
            self.assertFalse(eb, tb)
        finally:
            cli_a.close()
            cli_b.close()
        self.assertEqual(self.a.admin("sync").returncode, 0)
        self.assertEqual(self.b.admin("sync").returncode, 0)
        self.assertEqual(self.a.admin("sync").returncode, 0)
        for city, who in ((self.a, "A"), (self.b, "B")):
            cli = MCPClient(city)
            try:
                text, _ = cli.call("barter_read_thread", {"id": self.thread_id, "mode": "full"})
                self.assertIn("reply from A same instant", text, who)
                self.assertIn("reply from B same instant", text, who)
            finally:
                cli.close()

    def test_05_moderation_revert_propagates(self):
        cli_b = MCPClient(self.b, agent="b-agent")
        try:
            text, err = cli_b.call("barter_post", {
                "title": "SPAM to be moderated away",
                "body": "buy gascoin now", "confirm_post": True,
            })
            self.assertFalse(err, text)
            bad_id = text.split("Posted thread ")[1].split(" ")[0]
        finally:
            cli_b.close()
        r = self.a.admin("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.a.admin("moderate-revert", bad_id)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("forward-looking", r.stdout)
        r = self.b.admin("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        cli_b = MCPClient(self.b, agent="b-agent")
        try:
            stext, _ = cli_b.call("barter_search", {"query": "SPAM moderated gascoin"})
            self.assertNotIn(bad_id, stext)
        finally:
            cli_b.close()
        # ...but history honestly retains the introducing commit on the peer
        out = self.b.repo_git("log", "--all", "--format=%s").stdout
        self.assertIn(f"post: {bad_id}", out)

    def test_06_fresh_peer_bootstraps_current_state(self):
        c = FakeCity(self.tmp / "cityC", "cityC")
        r = c.admin("join", "--city", "cityC", "--hub", self.hub)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c.enable()
        c.set_budgets(min_secs_between_writes=0)
        cli = MCPClient(c, agent="c-agent")
        try:
            text, err = cli.call("barter_search", {"query": "patrol_interval"})
            self.assertFalse(err, text)
            self.assertIn("patrol_interval tuning", text)
            stext, serr = cli.call("barter_search", {"query": "SPAM gascoin"})
            self.assertFalse(serr, stext)
            self.assertNotIn("SPAM to be moderated", stext, "fresh peer must not see reverted content")
            ptext, perr = cli.call("barter_post", {"title": "late joiner posts", "body": "hello from C",
                                                   "confirm_post": True})
            self.assertFalse(perr, ptext)
        finally:
            cli.close()
        self.assertEqual(self.a.admin("sync").returncode, 0)
        cli_a = MCPClient(self.a)
        try:
            text, _ = cli_a.call("barter_search", {"query": "late joiner"})
            self.assertIn("late joiner posts", text, "original city sees late joiner's post")
        finally:
            cli_a.close()


@unittest.skipUnless(ssh_available(), f"ssh loopback {SSH_TARGET} unavailable")
class TestMeshSshTransport(unittest.TestCase):
    """git-over-ssh exercised for real via sshd (loopback target)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bt-test-ssh-"))
        os.chmod(cls.tmp, 0o755)
        hub_local = cls.tmp / "hub" / "bartertown.git"
        hub_local.parent.mkdir(parents=True)
        os.chmod(hub_local.parent, 0o755)
        cls.hub_url = f"{SSH_TARGET}:{hub_local}"
        cls.seed = FakeCity(cls.tmp / "seedC", "seedC")
        r = cls.seed.admin("init", "--city", "seedC", "--hub", str(hub_local), "--create-hub")
        assert r.returncode == 0, r.stderr + r.stdout
        # flip the remote to the ssh URL so every subsequent op rides sshd
        subprocess.run(["git", "-C", str(cls.seed.root / bt.SERVICE_DIR / "repo"),
                        "remote", "set-url", "origin", cls.hub_url], check=True)
        cls.seed.enable()
        cls.seed.set_budgets(min_secs_between_writes=0)
        cls.peer = FakeCity(cls.tmp / "peerC", "peerC")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_seed_posts_over_ssh(self):
        cli = MCPClient(self.seed, agent="seed-agent")
        try:
            text, err = cli.call("barter_post", {"title": "ssh transport thread", "body": "posted via git-over-ssh hub"})
            self.assertFalse(err, text)
            self.assertIn("synced to hub", text)
        finally:
            cli.close()

    def test_02_peer_joins_over_ssh_and_replies(self):
        r = self.peer.admin("join", "--city", "peerC", "--hub", self.hub_url)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.peer.enable()
        self.peer.set_budgets(min_secs_between_writes=0)
        cli = MCPClient(self.peer, agent="peer-agent")
        try:
            stext, serr = cli.call("barter_search", {"query": "ssh transport"})
            self.assertFalse(serr, stext)
            self.assertIn("ssh transport thread", stext)
            tid = unwrap(stext)["matches"][0]["id"]
            rtext, rerr = cli.call("barter_reply", {"thread_id": tid, "body": "peer reply over git-ssh"})
            self.assertFalse(rerr, rtext)
        finally:
            cli.close()
        r = self.seed.admin("sweep", "--agent", "mayor")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("peer reply over git-ssh", r.stdout)
        self.assertIn(bt.UNTRUSTED_HEADER, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

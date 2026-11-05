"""Tests for jumpd core logic."""

import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jumpd import (
    Entry,
    Database,
    _match_query,
    query,
    _split_components,
    _smart_case_eq,
    _smart_case_contains,
    _has_uppercase,
    _time_ago,
    DEFAULT_MAXAGE,
    RECENCY_MULTIPLIERS,
)


class TestEntry(unittest.TestCase):
    def test_entry_creation(self):
        e = Entry(path="/home/user/project", score=5.0, last_seen=time.time())
        self.assertEqual(e.path, "/home/user/project")
        self.assertEqual(e.score, 5.0)

    def test_entry_to_line_roundtrip(self):
        e = Entry(path="/foo/bar", score=3.5, last_seen=1700000000.0)
        line = e.to_line()
        self.assertEqual(line, "3.5\t1700000000.0\t/foo/bar")
        e2 = Entry.from_line(line)
        self.assertIsNotNone(e2)
        self.assertEqual(e2.path, "/foo/bar")
        self.assertEqual(e2.score, 3.5)

    def test_entry_from_line_invalid(self):
        self.assertIsNone(Entry.from_line(""))
        self.assertIsNone(Entry.from_line("garbage"))
        self.assertIsNone(Entry.from_line("1\t2"))  # missing path
        self.assertIsNone(Entry.from_line("abc\tdef\tghi"))  # non-numeric

    def test_entry_from_line_with_spaces_in_path(self):
        e = Entry.from_line("2.0\t1700000000.0\t/home/user/my project")
        self.assertIsNotNone(e)
        self.assertEqual(e.path, "/home/user/my project")

    def test_frecency_recent(self):
        """Within the last hour = score * 4."""
        e = Entry(path="/foo", score=10.0, last_seen=time.time() - 100)
        self.assertAlmostEqual(e.frecency, 40.0, places=1)

    def test_frecency_within_day(self):
        e = Entry(path="/foo", score=10.0, last_seen=time.time() - 7200)
        self.assertAlmostEqual(e.frecency, 20.0, places=1)

    def test_frecency_within_week(self):
        e = Entry(path="/foo", score=10.0, last_seen=time.time() - 200000)
        self.assertAlmostEqual(e.frecency, 5.0, places=1)

    def test_frecency_old(self):
        e = Entry(path="/foo", score=10.0, last_seen=time.time() - 2000000)
        self.assertAlmostEqual(e.frecency, 2.5, places=1)


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "db.tsv")

    def test_load_empty(self):
        db = Database.load(self.db_path)
        self.assertEqual(len(db.entries), 0)

    def test_add_and_save(self):
        db = Database.load(self.db_path)
        db.add("/foo/bar")
        db.save(self.db_path)
        self.assertTrue(os.path.isfile(self.db_path))

        db2 = Database.load(self.db_path)
        self.assertEqual(len(db2.entries), 1)
        e = db2.entries[0]
        self.assertEqual(e.path, os.path.abspath("/foo/bar"))
        self.assertEqual(e.score, 1.0)

    def test_add_increments(self):
        db = Database.load(self.db_path)
        db.add("/foo/bar")
        db.add("/foo/bar")
        db.add("/foo/bar")
        self.assertEqual(len(db.entries), 1)
        self.assertEqual(db.entries[0].score, 3.0)

    def test_remove(self):
        db = Database.load(self.db_path)
        db.add("/foo/bar")
        self.assertTrue(db.remove("/foo/bar"))
        self.assertEqual(len(db.entries), 0)
        self.assertFalse(db.remove("/foo/bar"))

    def test_age_no_change_under_maxage(self):
        db = Database.load(self.db_path)
        db.add("/foo")
        removed = db.age(maxage=1000)
        self.assertEqual(removed, 0)
        self.assertEqual(len(db.entries), 1)

    def test_age_scales_and_removes(self):
        db = Database.load(self.db_path)
        # Add entries with high scores
        now = time.time()
        db._entries["/a"] = Entry(path="/a", score=5000.0, last_seen=now)
        db._entries["/b"] = Entry(path="/b", score=5000.0, last_seen=now)
        db._entries["/c"] = Entry(path="/c", score=0.5, last_seen=now)
        removed = db.age(maxage=1000)
        # Total was 10000.5, k = 10000.5 / 900 ~= 11.11
        # /a: 5000/11.11 ~= 450, /b: same, /c: 0.5/11.11 ~= 0.045 -> removed
        self.assertEqual(removed, 1)
        self.assertIn("/a", db._entries)
        self.assertIn("/b", db._entries)
        self.assertNotIn("/c", db._entries)

    def test_prune_stale_missing_dirs(self):
        db = Database.load(self.db_path)
        old_time = time.time() - (91 * 86400)  # 91 days ago
        db._entries["/nonexistent/old"] = Entry(
            path="/nonexistent/old", score=5.0, last_seen=old_time
        )
        # A recent missing dir should NOT be pruned
        db._entries["/nonexistent/recent"] = Entry(
            path="/nonexistent/recent", score=5.0, last_seen=time.time()
        )
        removed = db.prune_stale()
        self.assertEqual(removed, 1)
        self.assertNotIn("/nonexistent/old", db._entries)
        self.assertIn("/nonexistent/recent", db._entries)


class TestMatching(unittest.TestCase):
    def test_simple_match(self):
        matched, quality, exact, _ = _match_query(
            "/home/user/project", ["project"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)
        self.assertEqual(quality, 1.0)

    def test_substring_match(self):
        matched, quality, exact, _ = _match_query(
            "/home/user/my-project-dir", ["project"]
        )
        self.assertTrue(matched)
        self.assertFalse(exact)
        self.assertGreater(quality, 0)
        self.assertLess(quality, 1.0)

    def test_multiple_keywords_ordered(self):
        matched, _, _, _ = _match_query(
            "/home/user/foo/bar", ["foo", "bar"]
        )
        self.assertTrue(matched)

    def test_multiple_keywords_wrong_order(self):
        matched, _, _, _ = _match_query(
            "/home/user/foo/bar", ["bar", "foo"]
        )
        self.assertFalse(matched)

    def test_keyword_with_slash_boundary(self):
        # "foo / bar" means foo and bar must be in different components
        matched, _, _, _ = _match_query(
            "/foo/bar", ["foo", "/", "bar"]
        )
        self.assertTrue(matched)

    def test_no_slash_boundary_in_same_component(self):
        matched, _, _, _ = _match_query(
            "/foobar", ["foo", "/", "bar"]
        )
        self.assertFalse(matched)

    def test_last_component_must_match(self):
        # "bar" must match last component
        matched, _, _, _ = _match_query(
            "/bar/foo", ["bar"]
        )
        self.assertFalse(matched)

    def test_last_component_matches(self):
        matched, _, exact, _ = _match_query(
            "/foo/bar", ["bar"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_trailing_slash_exact_last(self):
        # "bar/" means last component must be exactly "bar"
        matched, _, exact, _ = _match_query(
            "/foo/bar", ["bar/"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_trailing_slash_rejects_partial(self):
        # "bar/" should NOT match "barbaz" as last component
        matched, _, _, _ = _match_query(
            "/foo/barbaz", ["bar/"]
        )
        self.assertFalse(matched)

    def test_case_insensitive_default(self):
        matched, _, exact, _ = _match_query(
            "/home/user/Project", ["project"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_case_sensitive_with_uppercase(self):
        # Query with uppercase should be case-sensitive
        matched, _, exact, _ = _match_query(
            "/home/user/project", ["Project"]
        )
        self.assertFalse(matched)

    def test_case_sensitive_with_uppercase_match(self):
        matched, _, exact, _ = _match_query(
            "/home/user/Project", ["Project"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_smart_case_eq_lowercase(self):
        self.assertTrue(_smart_case_eq("Project", "project"))
        self.assertTrue(_smart_case_eq("project", "project"))

    def test_smart_case_eq_uppercase(self):
        self.assertFalse(_smart_case_eq("project", "Project"))
        self.assertTrue(_smart_case_eq("Project", "Project"))

    def test_smart_case_contains_lowercase(self):
        self.assertTrue(_smart_case_contains("MyProject", "proj"))
        self.assertTrue(_smart_case_contains("myproject", "proj"))  # lowercase query, case-insensitive
        self.assertFalse(_smart_case_contains("myproject", "PROJ"))  # uppercase query, case-sensitive

    def test_smart_case_contains_uppercase(self):
        self.assertFalse(_smart_case_contains("myproject", "Proj"))
        self.assertTrue(_smart_case_contains("MyProject", "Proj"))

    def test_no_match(self):
        matched, _, _, _ = _match_query(
            "/home/user/foo", ["xyz"]
        )
        self.assertFalse(matched)

    def test_empty_path(self):
        matched, _, _, _ = _match_query("", ["foo"])
        self.assertFalse(matched)

    def test_windows_path(self):
        matched, _, exact, _ = _match_query(
            "C:\\Users\\admin\\projects\\foo", ["foo"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)

    def test_match_quality_exact_is_1(self):
        matched, quality, exact, _ = _match_query(
            "/a/b/c/proj", ["proj"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)
        self.assertEqual(quality, 1.0)

    def test_match_quality_partial(self):
        matched, quality, exact, _ = _match_query(
            "/a/b/c/my-project-v2", ["proj"]
        )
        self.assertTrue(matched)
        self.assertFalse(exact)
        self.assertGreater(quality, 0)
        self.assertLess(quality, 1.0)

    def test_multiple_keywords_last_exact(self):
        matched, quality, exact, _ = _match_query(
            "/home/user/foo/bar", ["foo", "bar"]
        )
        self.assertTrue(matched)
        self.assertTrue(exact)
        self.assertEqual(quality, 1.0)


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "db.tsv")
        self.db = Database.load(self.db_path)
        now = time.time()

        # Create entries simulating a real database
        self.db._entries["/home/user/projects/web"] = Entry(
            path="/home/user/projects/web", score=10.0, last_seen=now
        )
        self.db._entries["/home/user/projects/api"] = Entry(
            path="/home/user/projects/api", score=8.0, last_seen=now - 100
        )
        self.db._entries["/home/user/work/web-frontend"] = Entry(
            path="/home/user/work/web-frontend", score=5.0, last_seen=now - 7200
        )
        self.db._entries["/home/user/work/old-stuff"] = Entry(
            path="/home/user/work/old-stuff", score=2.0, last_seen=now - 2000000
        )

    def test_query_returns_matches(self):
        results = query(self.db, ["web"])
        self.assertEqual(len(results), 2)
        # /home/user/projects/web has higher frecency AND exact match
        self.assertEqual(results[0].entry.path, "/home/user/projects/web")

    def test_query_exact_match_wins_over_higher_frecency(self):
        """Issue #247: exact match should beat more frequent partial match."""
        # Give web-frontend a higher score than the exact match
        now = time.time()
        self.db._entries["/home/user/work/web-frontend"].score = 100.0
        self.db._entries["/home/user/work/web-frontend"].last_seen = now
        self.db._entries["/home/user/projects/web"].score = 10.0
        self.db._entries["/home/user/projects/web"].last_seen = now

        results = query(self.db, ["web"])
        # Even though web-frontend has higher frecency, "web" is an exact
        # match for /home/user/projects/web, so it should win.
        self.assertEqual(results[0].entry.path, "/home/user/projects/web")

    def test_query_no_match(self):
        results = query(self.db, ["nonexistent"])
        self.assertEqual(len(results), 0)

    def test_query_multiple_keywords(self):
        results = query(self.db, ["projects", "web"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].entry.path, "/home/user/projects/web")

    def test_query_exclude(self):
        results = query(
            self.db, ["web"], exclude_dirs=["/home/user/projects/web"]
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].entry.path, "/home/user/work/web-frontend")

    def test_query_trailing_slash(self):
        """Issue #876: trailing slash means exact last component."""
        results = query(self.db, ["web/"])
        # Only /home/user/projects/web has "web" as exact last component
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].entry.path, "/home/user/projects/web")

    def test_query_sorted_by_total(self):
        results = query(self.db, ["web"])
        self.assertGreater(results[0].total, results[1].total)

    def test_query_explain_has_breakdown(self):
        results = query(self.db, ["web"])
        lines = results[0].breakdown_lines()
        self.assertTrue(any("frecency" in l for l in lines))
        self.assertTrue(any("total" in l for l in lines))

    def test_smart_case_search(self):
        """Issue #224: smart case - uppercase should be case-sensitive."""
        # "Web" with uppercase W should only match "Web", not "web"
        # But in our test data, "web" is lowercase, so "Web" shouldn't match
        results = query(self.db, ["Web"])
        # No path has "Web" with capital W
        self.assertEqual(len(results), 0)

    def test_match_quality_affects_ranking(self):
        """Issue #260: match closeness should participate in ranking."""
        now = time.time()
        db = Database.load(self.db_path)
        db._entries["/a/web"] = Entry(path="/a/web", score=10.0, last_seen=now)
        db._entries["/b/my-web-app-v2"] = Entry(
            path="/b/my-web-app-v2", score=10.0, last_seen=now
        )
        results = query(db, ["web"])
        # /a/web has exact match (quality=1.0), /b/my-web-app-v2 has partial
        # Both have same frecency, so exact should rank first
        self.assertEqual(results[0].entry.path, "/a/web")
        self.assertEqual(results[1].entry.path, "/b/my-web-app-v2")


class TestHelpers(unittest.TestCase):
    def test_split_components_unix(self):
        self.assertEqual(_split_components("/home/user/project"), ["home", "user", "project"])

    def test_split_components_windows(self):
        self.assertEqual(
            _split_components("C:\\Users\\admin\\project"),
            ["C:", "Users", "admin", "project"],
        )

    def test_split_components_trailing_slash(self):
        self.assertEqual(_split_components("/home/user/"), ["home", "user"])

    def test_has_uppercase(self):
        self.assertTrue(_has_uppercase("Hello"))
        self.assertFalse(_has_uppercase("hello"))
        self.assertFalse(_has_uppercase(""))

    def test_time_ago_seconds(self):
        ago = _time_ago(time.time() - 30)
        self.assertIn("s ago", ago)

    def test_time_ago_minutes(self):
        ago = _time_ago(time.time() - 300)
        self.assertIn("m ago", ago)

    def test_time_ago_hours(self):
        ago = _time_ago(time.time() - 7200)
        self.assertIn("h ago", ago)

    def test_time_ago_days(self):
        ago = _time_ago(time.time() - 200000)
        self.assertIn("d ago", ago)

    def test_time_ago_weeks(self):
        ago = _time_ago(time.time() - 2000000)
        self.assertIn("w ago", ago)


class TestImport(unittest.TestCase):
    def test_import_z_format(self):
        tmpdir = tempfile.mkdtemp()
        z_file = os.path.join(tmpdir, "z")
        with open(z_file, "w") as f:
            f.write("/home/user/project|10\n")
            f.write("/home/user/work|5\n")
            f.write("# comment line\n")

        db_path = os.path.join(tmpdir, "db.tsv")
        db = Database.load(db_path)

        # Simulate import
        count = 0
        with open(z_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    path = parts[0]
                    score = float(parts[1])
                    db._entries[os.path.abspath(path)] = Entry(
                        path=os.path.abspath(path),
                        score=score,
                        last_seen=time.time(),
                    )
                    count += 1

        self.assertEqual(count, 2)
        self.assertEqual(len(db.entries), 2)

    def test_import_autojump_format(self):
        tmpdir = tempfile.mkdtemp()
        aj_file = os.path.join(tmpdir, "autojump")
        with open(aj_file, "w") as f:
            f.write("10.0\t/home/user/project\n")
            f.write("5.0\t/home/user/work\n")

        db_path = os.path.join(tmpdir, "db.tsv")
        db = Database.load(db_path)

        count = 0
        with open(aj_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    score = float(parts[0])
                    path = parts[-1]
                    db._entries[os.path.abspath(path)] = Entry(
                        path=os.path.abspath(path),
                        score=score,
                        last_seen=time.time(),
                    )
                    count += 1

        self.assertEqual(count, 2)


class TestCLISmoke(unittest.TestCase):
    """Smoke tests for the CLI interface."""

    def test_main_no_args(self):
        from jumpd import main
        rc = main([])
        self.assertEqual(rc, 1)

    def test_main_version(self):
        from jumpd import main
        with self.assertRaises(SystemExit):
            main(["--version"])


if __name__ == "__main__":
    unittest.main()

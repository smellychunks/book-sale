"""Tests for export_store_snapshot.py, run against a copy of the REAL Shelf schema (your backup)."""
import json, os, shutil, sqlite3, tempfile, unittest, hashlib
from pathlib import Path

import export_store_snapshot as ex

REAL_SHELF = "/mnt/user-data/uploads/upload_nopass_shelf_backup_20260920_1254.db"


@unittest.skipUnless(os.path.exists(REAL_SHELF), "Shelf schema sample not available")
class Export(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "shelf.db")
        self.out = os.path.join(self.d, "data")
        shutil.copy(REAL_SHELF, self.db)
        c = sqlite3.connect(self.db)
        for t in ("item_tags", "tags", "items", "item_copies"):
            c.execute(f"delete from {t}")
        for i in range(1, 11):
            c.execute("insert into items (id,title,subtitle,authors,isbn,media_type,series_name,location_id,notes,"
                      "estimated_value,manual_value,description) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (i, f"Book {i}", "Sub" if i == 1 else None, f"Author {i}", f"97800000000{i:02d}", "book",
                       "Series X" if i == 2 else None, 1, "PRIVATE NOTE", 99.5, 12.0, "PRIVATE DESCRIPTION"))
        c.execute("insert into items (id,title,media_type,upc,deleted_at) values (11,'Gone','book','1','2026-01-01')")
        c.execute("insert into items (id,title,media_type,upc) values (12,'A Film','dvd','012345678905')")
        c.executemany("insert into tags (id,name) values (?,?)", [(1, "hardcover"), (2, "special edition"),
                      (3, "botm"), (4, "gothic"), (5, "board book")])
        c.executemany("insert into item_tags values (?,?)", [(1, 1), (1, 3), (1, 2), (1, 4), (2, 5), (3, 1), (3, 5)])
        c.execute("insert into item_copies (item_id, copy_number, location_id) values (1,1,1)")
        c.execute("insert into item_copies (item_id, copy_number, location_id) values (1,2,2)")
        c.commit(); c.close()

    def tearDown(self):
        shutil.rmtree(self.d)

    def snap(self):
        v = json.load(open(os.path.join(self.out, "version.json")))
        return v, json.load(open(os.path.join(self.out, v["file"]), encoding="utf-8"))

    def test_exports_live_items_only_with_binding_and_tags_split(self):
        ex.export(self.db, self.out)
        v, s = self.snap()
        self.assertEqual((v["count"], s["count"], len(s["items"])), (11, 11, 11))      # 10 books + 1 film, not 'Gone'
        by = {i["id"]: i for i in s["items"]}
        self.assertNotIn(11, by)
        self.assertEqual(by[1]["b"], "hardcover")
        self.assertEqual(by[1]["g"], ["special edition", "botm", "gothic"], "special edition first, then A-Z")
        self.assertEqual(by[2]["b"], "board book")
        self.assertEqual(by[3]["b"], "hardcover", "first binding in vocabulary order wins")
        self.assertEqual(by[4]["b"], "")
        self.assertEqual((by[12]["m"], by[12]["i"]), ("dvd", "012345678905"))
        self.assertEqual(by[1]["s"], "Sub")
        self.assertEqual(by[2]["r"], "Series X")

    def test_locations_come_from_copies_when_there_are_any(self):
        ex.export(self.db, self.out)
        _, s = self.snap()
        by = {i["id"]: i for i in s["items"]}
        self.assertEqual(by[1]["l"], ["Elm Street", "Nathan Pratt Drive"])
        self.assertEqual(by[4]["l"], ["Elm Street"])

    def test_only_allowlisted_fields_ever_leave_shelf(self):
        ex.export(self.db, self.out)
        raw = open(os.path.join(self.out, self.snap()[0]["file"]), encoding="utf-8").read()
        for secret in ("PRIVATE NOTE", "PRIVATE DESCRIPTION", "99.5", "estimated_value", "notes"):
            self.assertNotIn(secret, raw)
        _, s = self.snap()
        for item in s["items"]:
            self.assertLessEqual(set(item), set(ex.ITEM_FIELDS))

    def test_version_points_at_a_file_whose_hash_matches(self):
        ex.export(self.db, self.out)
        v, _ = self.snap()
        data = open(os.path.join(self.out, v["file"]), "rb").read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), v["sha256"])
        self.assertEqual(len(data), v["bytes"])
        self.assertTrue(v["file"].startswith("snapshot.") and v["sha256"].startswith(v["file"][9:21]))

    def test_unchanged_library_gives_the_same_hash_so_phones_skip_the_download(self):
        a = ex.export(self.db, self.out)
        b = ex.export(self.db, self.out)
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(len([f for f in os.listdir(self.out) if f.startswith("snapshot.")]), 1)

    def test_shrinking_library_is_refused_unless_forced(self):
        ex.export(self.db, self.out)
        before = open(os.path.join(self.out, "version.json")).read()
        c = sqlite3.connect(self.db)
        c.execute("update items set deleted_at='2026-02-02' where id > 3"); c.commit(); c.close()
        with self.assertRaises(SystemExit) as cm:
            ex.export(self.db, self.out)
        self.assertIn("Refusing", str(cm.exception))
        self.assertEqual(open(os.path.join(self.out, "version.json")).read(), before, "old library untouched")
        ex.export(self.db, self.out, force=True)
        self.assertEqual(self.snap()[0]["count"], 3)

    def test_empty_database_is_refused(self):
        c = sqlite3.connect(self.db); c.execute("update items set deleted_at='x'"); c.commit(); c.close()
        with self.assertRaises(SystemExit):
            ex.export(self.db, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "version.json")))

    def test_old_snapshots_are_pruned(self):
        for n in range(5):
            c = sqlite3.connect(self.db); c.execute("update items set title=? where id=1", (f"T{n}",)); c.commit(); c.close()
            ex.export(self.db, self.out)
        self.assertEqual(len([f for f in os.listdir(self.out) if f.startswith("snapshot.")]), ex.KEEP_SNAPSHOTS)
        v, _ = self.snap()
        self.assertTrue(os.path.exists(os.path.join(self.out, v["file"])), "current snapshot is never pruned")

    def test_never_writes_to_shelf_and_leaves_no_temp_files(self):
        h = hashlib.sha256(open(self.db, "rb").read()).hexdigest()
        ex.export(self.db, self.out)
        self.assertEqual(hashlib.sha256(open(self.db, "rb").read()).hexdigest(), h)
        self.assertFalse([f for f in os.listdir(self.out) if f.endswith(".tmp")])

    def test_published_files_are_world_readable_for_the_web_container(self):
        ex.export(self.db, self.out)
        for f in os.listdir(self.out):
            self.assertEqual(os.stat(os.path.join(self.out, f)).st_mode & 0o777, 0o644, f)

    def test_schema_drift_is_refused(self):
        c = sqlite3.connect(self.db); c.execute("alter table tags rename column name to label"); c.commit(); c.close()
        with self.assertRaises(SystemExit) as cm:
            ex.export(self.db, self.out)
        self.assertIn("tags", str(cm.exception))

    def test_cli_with_flags(self):
        self.assertEqual(ex.main(["--db", self.db, "--out", self.out]), 0)
        self.assertEqual(self.snap()[0]["count"], 11)


if __name__ == "__main__":
    unittest.main()

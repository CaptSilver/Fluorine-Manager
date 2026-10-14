from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_views_tests")
views = support.module(PACKAGE, "views")
merge_engine = support.module(PACKAGE, "merge_engine")
state = support.module(PACKAGE, "state")


def tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        for p in sorted(root.rglob("*"))
    }


class ViewsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.mod = self.organizer.modsPath_ / "pack"
        for rel, data in {
            "meta.ini": "[General]\n",
            "regulation.bin": b"reg",
            "msg/engUS/item.msgbnd.dcx": b"text",
            "msg/engUS/menu.msgbnd.dcx": b"menu",
            "param/systemparam.parambnd.dcx": b"sys",
            "param/hidden.bin.mohidden": b"hidden",
        }.items():
            target = self.mod / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())

    def prunes(self, *paths, mod="pack"):
        return [{"mod": mod, "paths": list(paths)}]

    def renames(self, mapping, mod="pack"):
        return [{"mod": mod, "paths": dict(mapping)}]

    def view_names(self, view: Path) -> list[str]:
        return sorted(p.relative_to(view).as_posix() for p in view.rglob("*") if p.is_file())


class ViewFilesTests(ViewsCase):
    def test_no_rules_lists_the_game_files_without_meta_ini_or_hidden_ones(self) -> None:
        files = views.view_files("pack", self.mod, [], [])
        self.assertEqual(
            sorted(files),
            ["msg/engUS/item.msgbnd.dcx", "msg/engUS/menu.msgbnd.dcx", "param/systemparam.parambnd.dcx", "regulation.bin"],
        )

    def test_a_prune_removes_a_file_and_a_directory(self) -> None:
        files = views.view_files("pack", self.mod, self.prunes("param/systemparam.parambnd.dcx", "msg/engUS"), [])
        self.assertEqual(sorted(files), ["regulation.bin"])

    def test_a_rename_places_the_file_at_its_destination(self) -> None:
        renames = self.renames({"msg/engUS/item.msgbnd.dcx": "msg/engus/item.msgbnd.dcx"})
        files = views.view_files("pack", self.mod, [], renames)
        self.assertIn("msg/engus/item.msgbnd.dcx", files)
        self.assertNotIn("msg/engUS/item.msgbnd.dcx", files)
        self.assertEqual(files["msg/engus/item.msgbnd.dcx"], self.mod / "msg/engUS/item.msgbnd.dcx")

    def test_a_prune_names_the_path_after_the_rename(self) -> None:
        renames = self.renames({"msg/engUS/item.msgbnd.dcx": "msg/engus/item.msgbnd.dcx"})
        files = views.view_files("pack", self.mod, self.prunes("msg/engus/item.msgbnd.dcx"), renames)
        self.assertNotIn("msg/engus/item.msgbnd.dcx", files)

    def test_rules_for_another_mod_are_ignored(self) -> None:
        files = views.view_files("pack", self.mod, self.prunes("regulation.bin", mod="other"), [])
        self.assertIn("regulation.bin", files)

    def test_a_rename_over_a_file_the_mod_ships_is_refused(self) -> None:
        renames = self.renames({"msg/engUS/item.msgbnd.dcx": "msg/engUS/menu.msgbnd.dcx"})
        with self.assertRaises(merge_engine.ConflictError):
            views.view_files("pack", self.mod, [], renames)

    def test_a_rename_onto_a_folder_the_mod_ships_is_refused(self) -> None:
        renames = self.renames({"regulation.bin": "msg/engUS"})
        with self.assertRaises(merge_engine.ConflictError):
            views.view_files("pack", self.mod, [], renames)

    def test_a_rename_into_a_file_the_mod_ships_is_refused(self) -> None:
        renames = self.renames({"msg/engUS/item.msgbnd.dcx": "regulation.bin/item.msgbnd.dcx"})
        with self.assertRaises(merge_engine.ConflictError):
            views.view_files("pack", self.mod, [], renames)

    def test_unsafe_paths_are_refused_whichever_rule_carries_them(self) -> None:
        for prunes, renames in (
            (self.prunes("../x"), []),
            (self.prunes("/etc/passwd"), []),
            ([], self.renames({"../x": "y"})),
            ([], self.renames({"regulation.bin": "../y"})),
        ):
            with self.subTest(prunes=prunes, renames=renames), self.assertRaises(merge_engine.ConflictError):
                views.view_files("pack", self.mod, prunes, renames)

    def test_a_rename_of_a_missing_source_is_skipped_as_already_fixed(self) -> None:
        files = views.view_files("pack", self.mod, [], self.renames({"nothing": "somewhere"}))
        self.assertNotIn("somewhere", files)


class EnsureViewTests(ViewsCase):
    def test_the_view_is_a_link_tree_and_the_mod_folder_is_untouched(self) -> None:
        before = tree(self.mod)
        view = views.ensure_view(self.paths, "pack", self.mod, self.prunes("param/systemparam.parambnd.dcx"), [])
        self.assertEqual(view, self.paths.view_dir("pack"))
        self.assertEqual(
            self.view_names(view), ["msg/engUS/item.msgbnd.dcx", "msg/engUS/menu.msgbnd.dcx", "regulation.bin"]
        )
        self.assertTrue((view / "regulation.bin").is_symlink())
        self.assertEqual((view / "regulation.bin").read_bytes(), b"reg")
        self.assertEqual(tree(self.mod), before)

    def test_the_state_file_sits_beside_the_view_and_not_inside_it(self) -> None:
        view = views.ensure_view(self.paths, "pack", self.mod, self.prunes("regulation.bin"), [])
        self.assertTrue(self.paths.view_state("pack").is_file())
        self.assertEqual(self.paths.view_state("pack").parent, view.parent)
        self.assertNotIn("pack.state.json", self.view_names(view))

    def test_an_unchanged_mod_and_rules_are_not_rebuilt(self) -> None:
        prunes = self.prunes("regulation.bin")
        views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        with mock.patch.object(views, "build_view", side_effect=AssertionError("rebuilt")):
            views.ensure_view(self.paths, "pack", self.mod, prunes, [])

    def test_a_changed_listing_or_changed_rules_rebuild(self) -> None:
        prunes = self.prunes("regulation.bin")
        view = views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        (self.mod / "new/file.bin").parent.mkdir()
        (self.mod / "new/file.bin").write_bytes(b"new")
        views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        self.assertIn("new/file.bin", self.view_names(view))
        views.ensure_view(self.paths, "pack", self.mod, self.prunes("regulation.bin", "new"), [])
        self.assertNotIn("new/file.bin", self.view_names(view))

    def test_a_build_that_fails_leaves_no_state_so_the_next_call_builds_again(self) -> None:
        with self.assertRaises(merge_engine.ConflictError):
            views.ensure_view(self.paths, "pack", self.mod, self.prunes("../x"), [])
        self.assertFalse(self.paths.view_state("pack").exists())
        self.assertFalse(self.paths.view_dir("pack").exists())

    def test_a_view_that_vanished_is_rebuilt_even_with_a_matching_state(self) -> None:
        prunes = self.prunes("regulation.bin")
        view = views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        shutil.rmtree(view)
        self.assertTrue(views.ensure_view(self.paths, "pack", self.mod, prunes, []).is_dir())

    def test_hardlinks_are_the_fallback_when_the_constant_says_so(self) -> None:
        with mock.patch.object(views, "LINK", os.link):
            view = views.ensure_view(self.paths, "pack", self.mod, self.prunes("param"), [])
        target = view / "regulation.bin"
        self.assertFalse(target.is_symlink())
        self.assertEqual(os.stat(target).st_ino, os.stat(self.mod / "regulation.bin").st_ino)

    def test_changing_the_link_kind_rebuilds(self) -> None:
        prunes = self.prunes("param")
        views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        with mock.patch.object(views, "LINK", os.link):
            view = views.ensure_view(self.paths, "pack", self.mod, prunes, [])
        self.assertFalse((view / "regulation.bin").is_symlink())

    def test_has_rules_is_false_for_a_mod_no_rule_names(self) -> None:
        self.assertTrue(views.has_rules("pack", self.prunes("x"), []))
        self.assertFalse(views.has_rules("pack", self.prunes("x", mod="other"), self.renames({"a": "b"}, mod="other")))


if __name__ == "__main__":
    unittest.main()

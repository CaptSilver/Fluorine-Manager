from __future__ import annotations

import hashlib
import shutil
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_merge_engine_tests")
merge_engine = support.module(PACKAGE, "merge_engine")
state = support.module(PACKAGE, "state")
kinds = support.module(PACKAGE, "kinds")
errors = support.module(PACKAGE, "ermlib.errors")
heal = support.module(PACKAGE, "ermlib.heal")
merge = support.module(PACKAGE, "ermlib.merge")
bhd5 = support.module(PACKAGE, "ermlib.formats.bhd5")

CONCAT = {"concat": lambda out, extra, notes=None: out + b"|" + extra}
RULE = {"path": "msg/x.dcx", "strategy": "concat", "mods": ["a", "b", "c"], "prefer": "a"}


def tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def no_vanilla(mod_id, member):
    raise AssertionError("no vanilla expected")


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)

    def folder(self, name: str, files: dict) -> merge_engine.Contributor:
        mod = self.organizer.mod_list.add(name, kind="package", files=files)
        return merge_engine.Contributor(name, Path(mod.absolutePath()))

    def plan(self, contributors, merges, **kwargs):
        provided = merge_engine.index(contributors)
        return merge_engine.plan(
            provided, contributors, merges, kwargs.pop("loader", no_vanilla), strategies=CONCAT, **kwargs
        )


class IndexTests(EngineCase):
    def test_providers_are_listed_in_contributor_order(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A", "only-a.bin": "A"})
        b = self.folder("b", {"msg/x.dcx": "B"})
        self.assertEqual(
            merge_engine.index([a, b]), {"msg/x.dcx": ["a", "b"], "only-a.bin": ["a"]}
        )
        self.assertEqual(merge_engine.index([b, a])["msg/x.dcx"], ["b", "a"])

    def test_meta_ini_and_hidden_files_are_not_provided(self) -> None:
        a = self.folder("a", {"meta.ini": "x", "regulation.bin": "A", "gone.bin.mohidden": "x", "d.mohidden/z.bin": "x"})
        self.assertEqual(merge_engine.index([a]), {"regulation.bin": ["a"]})

    def test_a_view_is_indexed_instead_of_the_mod_folder(self) -> None:
        a = self.folder("a", {"keep.bin": "A", "drop.bin": "A"})
        view = self.tmp / "view"
        view.mkdir()
        (view / "keep.bin").symlink_to(a.source / "keep.bin")
        self.assertEqual(merge_engine.index([merge_engine.Contributor("a", view)]), {"keep.bin": ["a"]})

    def test_paths_that_differ_only_in_case_are_refused_naming_both_mods(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        b = self.folder("b", {"msg/engus/x.dcx": "B"})
        with self.assertRaises(merge_engine.ConflictError) as caught:
            merge_engine.index([a, b])
        self.assertIn("a", str(caught.exception))
        self.assertIn("engUS", str(caught.exception))


class ContributorsTests(EngineCase):
    def test_only_enabled_packages_contribute_in_priority_order(self) -> None:
        modlist = self.organizer.mod_list
        modlist.add("first", kind="package", files={"a": "x"})
        modlist.add("a native", kind="native", files={"mods/n.dll": "x"})
        modlist.add("off", enabled=False, kind="package", files={"a": "x"})
        modlist.add("ersc", kind="ersc", files={"SeamlessCoop/ersc.dll": "x"})
        modlist.add("Elden Ring Generated", kind="generated")
        modlist.add("second", kind="package", files={"a": "x"})
        got = merge_engine.contributors_of(self.organizer, {"second": self.tmp / "second view"})
        self.assertEqual([c.mod for c in got], ["first", "second"])
        self.assertEqual(got[1].source, self.tmp / "second view")
        self.assertEqual(got[0].source, self.organizer.modsPath_ / "first")

    def test_the_generated_mod_never_contributes_even_when_its_recorded_kind_is_a_package(self) -> None:
        modlist = self.organizer.mod_list
        modlist.add("first", kind="package", files={"regulation.bin": "x"})
        modlist.add("Elden Ring Generated", kind="package", files={"regulation.bin": "merged"})
        got = merge_engine.contributors_of(self.organizer, {})
        self.assertEqual([c.mod for c in got], ["first"])


class PlanTests(EngineCase):
    def providers(self):
        return [
            self.folder("a", {"msg/x.dcx": "A", "only-a.bin": "A"}),
            self.folder("b", {"msg/x.dcx": "B"}),
            self.folder("c", {"msg/x.dcx": "C"}),
        ]

    def test_a_declared_merge_folds_prefer_first_then_the_rest_in_contributor_order(self) -> None:
        planned = self.plan(self.providers(), [RULE])
        self.assertEqual(planned, [("msg/x.dcx", b"A|B|C")])

    def test_swapping_two_contributors_changes_the_bytes(self) -> None:
        a, b, c = self.providers()
        rule = {**RULE, "prefer": "c"}
        one = self.plan([a, b, c], [rule])
        two = self.plan([b, a, c], [rule])
        self.assertEqual(one, [("msg/x.dcx", b"C|A|B")])
        self.assertEqual(two, [("msg/x.dcx", b"C|B|A")])

    def test_outputs_exist_only_at_declared_paths_and_sources_are_untouched(self) -> None:
        contributors = self.providers()
        before = [tree(c.source) for c in contributors]
        planned = self.plan(contributors, [RULE])
        self.assertEqual([rel for rel, _ in planned], ["msg/x.dcx"])
        self.assertEqual([tree(c.source) for c in contributors], before)

    def test_an_undeclared_collision_is_refused_with_both_names(self) -> None:
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [])
        self.assertIn("a, b, c", str(caught.exception))
        self.assertIn("Declare a [[merges]] entry", str(caught.exception))

    def test_a_provider_outside_the_declaration_is_refused(self) -> None:
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [{**RULE, "mods": ["a", "b"]}])
        self.assertIn("also ship this path", str(caught.exception))

    def test_a_declared_mod_that_is_not_installed_is_fine(self) -> None:
        a, b, _c = self.providers()
        planned = self.plan([a, b], [{**RULE, "mods": ["a", "b", "ghost"]}])
        self.assertEqual(planned, [("msg/x.dcx", b"A|B")])

    def test_prefer_must_be_a_provider(self) -> None:
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [{**RULE, "prefer": "ghost"}])
        self.assertIn("prefer", str(caught.exception))

    def test_an_unknown_strategy_is_refused_listing_the_known_ones(self) -> None:
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [{**RULE, "strategy": "nope"}])
        self.assertIn("concat", str(caught.exception))

    def test_two_declarations_for_one_path_that_disagree_are_refused(self) -> None:
        with self.assertRaises(merge_engine.ConflictError):
            self.plan(self.providers(), [RULE, {**RULE, "prefer": "b"}])

    def test_an_identical_repeat_of_a_declaration_is_fine(self) -> None:
        self.assertEqual(len(self.plan(self.providers(), [RULE, dict(RULE)])), 1)

    def test_a_declared_contributor_that_stopped_shipping_the_path_is_noted(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A"})
        b = self.folder("b", {"other.bin": "B"})
        notes = []
        planned = self.plan([a, b], [{**RULE, "mods": ["a", "b"]}], notes=notes)
        self.assertEqual(planned, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "msg/x.dcx")
        self.assertEqual(notes[0][1].mods, ("b",))

    def test_a_base_makes_a_single_provider_a_merge_and_leads_the_fold(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A"})
        planned = self.plan([a], [{**RULE, "mods": ["a"]}], bases={"msg/x.dcx": b"GAME"})
        self.assertEqual(planned, [("msg/x.dcx", b"GAME|A")])

    def test_only_the_first_fold_step_is_told_its_base_is_the_game(self) -> None:
        calls = []

        def strategy(out, extra, base_is_game=False, notes=None):
            calls.append((out, extra, base_is_game))
            return out + b"+" + extra

        a, b, _c = self.providers()
        provided = merge_engine.index([a, b])
        with mock.patch.object(merge, "TAKES_GAME_BASE", frozenset({"takes"})):
            merge_engine.plan(
                provided, [a, b], [{**RULE, "strategy": "takes", "mods": ["a", "b"]}], no_vanilla,
                bases={"msg/x.dcx": b"G"}, strategies={"takes": strategy},
            )
        self.assertEqual(calls, [(b"G", b"A", True), (b"G+A", b"B", False)])


class VanillaTests(EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.archive = self.organizer.downloads / "ancestor.zip"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("sub/regulation.bin", b"ANCESTOR")
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.sources = {"ancestor-mod": {"archive": "ancestor.zip", "sha256": self.sha}}
        self.loader = merge_engine.make_vanilla_loader(self.paths, self.organizer.downloads, self.sources)

    def three_way(self, **vanilla):
        rule = {"path": "msg/x.dcx", "strategy": "tri", "mods": ["a", "b"], "prefer": "a"}
        if vanilla:
            rule["vanilla"] = vanilla
        return rule

    def plan_tri(self, rule, loader=None):
        a = self.folder("a", {"msg/x.dcx": "A"})
        b = self.folder("b", {"msg/x.dcx": "B"})
        provided = merge_engine.index([a, b])
        strategies = {"tri": lambda out, extra, vanilla, notes=None: out + b"|" + extra + b"|" + vanilla}
        with mock.patch.object(merge, "NEEDS_VANILLA", frozenset({"tri"})):
            return merge_engine.plan(provided, [a, b], [rule], loader or self.loader, strategies=strategies)

    def test_the_ancestor_is_read_out_of_the_pinned_archive(self) -> None:
        rule = self.three_way(mod="ancestor-mod", member="sub/regulation.bin")
        self.assertEqual(self.plan_tri(rule), [("msg/x.dcx", b"A|B|ANCESTOR")])

    def test_each_way_the_ancestor_can_be_missing_is_a_conflict_error(self) -> None:
        cases = {
            "no vanilla declared": self.three_way(),
            "no member": self.three_way(mod="ancestor-mod"),
            "unsafe member": self.three_way(mod="ancestor-mod", member="../x"),
            "mod not in the table": self.three_way(mod="ghost", member="sub/regulation.bin"),
            "member not in archive": self.three_way(mod="ancestor-mod", member="nope.bin"),
        }
        for label, rule in cases.items():
            with self.subTest(label), self.assertRaises(merge_engine.ConflictError):
                self.plan_tri(rule)

    def test_an_archive_that_is_not_in_downloads_is_vanilla_archive_missing(self) -> None:
        self.archive.unlink()
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan_tri(self.three_way(mod="ancestor-mod", member="sub/regulation.bin"))
        self.assertIn("vanilla archive missing", str(caught.exception))

    def test_an_archive_whose_sha_differs_from_the_pin_is_refused(self) -> None:
        self.sources["ancestor-mod"]["sha256"] = "0" * 64
        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan_tri(self.three_way(mod="ancestor-mod", member="sub/regulation.bin"))
        self.assertIn("sha256", str(caught.exception))

    def test_a_corrupt_archive_is_a_conflict_error(self) -> None:
        self.archive.write_bytes(b"not a zip")
        self.sources["ancestor-mod"]["sha256"] = hashlib.sha256(b"not a zip").hexdigest()
        with self.assertRaises(merge_engine.ConflictError):
            self.plan_tri(self.three_way(mod="ancestor-mod", member="sub/regulation.bin"))

    def test_declared_ancestor_is_none_for_a_two_way_or_undeclared_path(self) -> None:
        self.assertIsNone(merge_engine.declared_ancestor([], "regulation.bin", self.loader))
        two_way = {"path": "regulation.bin", "strategy": "concat", "mods": ["a"], "prefer": "a"}
        self.assertIsNone(merge_engine.declared_ancestor([two_way], "regulation.bin", self.loader))

    def test_declared_ancestor_reads_it_for_a_three_way_path(self) -> None:
        rule = {
            "path": "regulation.bin", "strategy": "param-rows", "mods": ["a", "b"], "prefer": "a",
            "vanilla": {"mod": "ancestor-mod", "member": "sub/regulation.bin"},
        }
        self.assertEqual(merge_engine.declared_ancestor([rule], "regulation.bin", self.loader), b"ANCESTOR")


class GameBaseTests(unittest.TestCase):
    def test_only_merges_that_ask_for_the_game_copy_read_it(self) -> None:
        merges = [{"path": "a", "base": "game"}, {"path": "b"}]
        with mock.patch.object(bhd5, "read_file", return_value=b"GAMEBYTES") as read:
            self.assertEqual(merge_engine.load_game_bases(merges, "/game"), {"a": b"GAMEBYTES"})
        read.assert_called_once_with("/game", "a")

    def test_an_unreadable_game_copy_is_fatal(self) -> None:
        with mock.patch.object(bhd5, "read_file", side_effect=bhd5.Bhd5Error("no archive")):
            with self.assertRaises(merge_engine.ConflictError) as caught:
                merge_engine.load_game_bases([{"path": "a", "base": "game"}], "/game")
        self.assertIn("no archive", str(caught.exception))


class RebaseTests(EngineCase):
    LIVE = types.SimpleNamespace(regulation="11711000", regulation_sha="s" * 64)

    def setUp(self) -> None:
        super().setUp()
        self.a = self.folder("a", {"regulation.bin": "RA"})
        self.b = self.folder("b", {"regulation.bin": "RB"})
        self.c = self.folder("c", {"other.bin": "x"})
        self.rule = {
            "path": "regulation.bin", "strategy": "param-rows", "mods": ["a", "b"], "prefer": "a",
            "vanilla": {"mod": "anc", "member": "r.bin"},
        }

    def test_the_regulations_of_the_contributors_and_the_ancestor_go_to_prepare_rebase(self) -> None:
        loader = lambda mod_id, member: b"ANC"  # noqa: E731
        with mock.patch.object(heal, "prepare_rebase", return_value={"regulation.bin": b"BASE"}) as prepare:
            result = merge_engine.rebase_bases(
                "/game", self.LIVE, [self.rule], [self.a, self.b, self.c], self.paths, loader
            )
        args, kwargs = prepare.call_args
        self.assertEqual(args[:3], ("/game", self.LIVE, b"ANC"))
        self.assertEqual(args[3], [("a", b"RA"), ("b", b"RB")])
        self.assertEqual(kwargs, {"base": self.paths.baselines, "cache_path": self.paths.regulation_cache})
        self.assertTrue(result.rebased)
        self.assertEqual(result.bases, {"regulation.bin": b"BASE"})
        self.assertEqual(result.baseline, b"BASE")

    def test_a_single_regulation_asks_for_no_ancestor(self) -> None:
        loader = mock.Mock(side_effect=AssertionError("ancestor read"))
        with mock.patch.object(heal, "prepare_rebase", return_value={}) as prepare:
            result = merge_engine.rebase_bases("/game", self.LIVE, [self.rule], [self.a, self.c], self.paths, loader)
        self.assertIsNone(prepare.call_args.args[2])
        self.assertFalse(result.rebased)
        self.assertEqual(result.bases, {})

    def test_game_bases_are_merged_in_and_do_not_count_as_a_rebase(self) -> None:
        merges = [{"path": "msg/y.dcx", "base": "game"}]
        with mock.patch.object(heal, "prepare_rebase", return_value={}), \
                mock.patch.object(bhd5, "read_file", return_value=b"G"):
            result = merge_engine.rebase_bases("/game", self.LIVE, merges, [self.a], self.paths, no_vanilla)
        self.assertEqual(result.bases, {"msg/y.dcx": b"G"})
        self.assertFalse(result.rebased)

    def test_verification_runs_only_after_a_rebase(self) -> None:
        plain = merge_engine.Rebase({}, False, None, [])
        with mock.patch.object(heal, "verify_rebase", side_effect=AssertionError("verified")):
            merge_engine.verify_rebase([], plain, self.LIVE)

    def test_a_rebase_with_no_merged_regulation_is_refused(self) -> None:
        rebase = merge_engine.Rebase({"regulation.bin": b"B"}, True, b"B", [("a", b"RA")])
        with self.assertRaises(heal.HealError):
            merge_engine.verify_rebase([("other", b"x")], rebase, self.LIVE)

    def test_problems_found_by_verify_rebase_are_refused_and_listed(self) -> None:
        rebase = merge_engine.Rebase({"regulation.bin": b"B"}, True, b"B", [("a", b"RA")])
        with mock.patch.object(heal, "verify_rebase", return_value=("row 5 dropped",)) as verify:
            with self.assertRaises(heal.HealError) as caught:
                merge_engine.verify_rebase([("regulation.bin", b"M")], rebase, self.LIVE)
        self.assertIn("row 5 dropped", str(caught.exception))
        verify.assert_called_once_with(b"M", b"B", [("a", b"RA")], self.LIVE)


if __name__ == "__main__":
    unittest.main()

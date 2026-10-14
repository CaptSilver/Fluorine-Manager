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
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

CONCAT = {"concat": lambda out, extra, notes=None: out + b"|" + extra}


def tri(out, extra, vanilla, notes=None):
    """A three-way strategy that only concatenates, so a test can read the fold order off the bytes."""
    return out + b"|" + extra


def refuse(out, extra, vanilla, notes=None):
    raise merge.MergeError("entry 1 row 5: both sides changed it and differ")


def crash(out, extra, vanilla, notes=None):
    raise IndexError("list index out of range")


def keep_lead(out, extra, vanilla, notes=None):
    """Settles a tie the way param_rows settles a rival row: the lead keeps its row."""
    return extra if out == vanilla else out
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

    def decide(self, contributors, merges, **kwargs):
        provided = merge_engine.index(contributors)
        return merge_engine.plan(
            provided, contributors, merges, kwargs.pop("loader", no_vanilla),
            strategies=kwargs.pop("strategies", CONCAT), **kwargs,
        )

    def plan(self, contributors, merges, **kwargs):
        return self.decide(contributors, merges, **kwargs).outputs


def spelled(provided: dict) -> dict:
    """{path: [(mod, spelling)]}: an index without the file locations."""
    return {key: [(p.mod, p.rel) for p in providers] for key, providers in provided.items()}


class IndexTests(EngineCase):
    def test_providers_are_listed_in_contributor_order(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A", "only-a.bin": "A"})
        b = self.folder("b", {"msg/x.dcx": "B"})
        self.assertEqual(
            spelled(merge_engine.index([a, b])),
            {"msg/x.dcx": [("a", "msg/x.dcx"), ("b", "msg/x.dcx")], "only-a.bin": [("a", "only-a.bin")]},
        )
        self.assertEqual([p.mod for p in merge_engine.index([b, a])["msg/x.dcx"]], ["b", "a"])

    def test_meta_ini_and_hidden_files_are_not_provided(self) -> None:
        a = self.folder("a", {"meta.ini": "x", "regulation.bin": "A", "gone.bin.mohidden": "x", "d.mohidden/z.bin": "x"})
        self.assertEqual(spelled(merge_engine.index([a])), {"regulation.bin": [("a", "regulation.bin")]})

    def test_a_view_is_indexed_instead_of_the_mod_folder(self) -> None:
        a = self.folder("a", {"keep.bin": "A", "drop.bin": "A"})
        view = self.tmp / "view"
        view.mkdir()
        (view / "keep.bin").symlink_to(a.source / "keep.bin")
        provided = merge_engine.index([merge_engine.Contributor("a", view)])
        self.assertEqual(spelled(provided), {"keep.bin": [("a", "keep.bin")]})
        self.assertEqual(provided["keep.bin"][0].path, view / "keep.bin")

    def test_paths_that_differ_only_in_case_are_one_path_in_the_games_spelling(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        b = self.folder("b", {"msg/engus/x.dcx": "B"})
        self.assertEqual(
            spelled(merge_engine.index([a, b])),
            {"msg/engus/x.dcx": [("a", "msg/engUS/x.dcx"), ("b", "msg/engus/x.dcx")]},
        )

    def test_one_mod_shipping_two_spellings_is_one_provider_and_the_other_spelling_is_dropped(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "upper", "msg/engus/x.dcx": "lower"})
        warnings, dropped = [], {}
        provided = merge_engine.index([a], warnings=warnings, dropped=dropped)
        self.assertEqual(spelled(provided), {"msg/engus/x.dcx": [("a", "msg/engus/x.dcx")]})
        self.assertEqual(provided["msg/engus/x.dcx"][0].path.read_bytes(), b"lower")
        self.assertEqual(dropped, {"a": ["msg/engUS/x.dcx"]})
        self.assertEqual(len(warnings), 1)
        for text in ("a", "msg/engUS/x.dcx", "msg/engus/x.dcx"):
            self.assertIn(text, warnings[0])

    def test_when_neither_spelling_is_the_games_the_first_in_sorted_order_is_kept(self) -> None:
        a = self.folder("a", {"Msg/x.dcx": "1", "MSG/x.dcx": "2"})
        dropped = {}
        provided = merge_engine.index([a], dropped=dropped)
        self.assertEqual(spelled(provided), {"msg/x.dcx": [("a", "MSG/x.dcx")]})
        self.assertEqual(dropped, {"a": ["Msg/x.dcx"]})

    def test_a_mod_with_one_spelling_adds_no_warning_and_drops_nothing(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A"})
        warnings, dropped = [], {}
        merge_engine.index([a], warnings=warnings, dropped=dropped)
        self.assertEqual((warnings, dropped), ([], {}))


class CanonicalTests(unittest.TestCase):
    def test_the_games_spelling_is_lowercase(self) -> None:
        self.assertEqual(merge_engine.canonical("msg/engUS/Item_DLC02.msgbnd.dcx"), "msg/engus/item_dlc02.msgbnd.dcx")
        self.assertEqual(merge_engine.canonical("regulation.bin"), "regulation.bin")

    def test_it_is_the_spelling_the_archive_index_hashes(self) -> None:
        for rel in ("msg/engUS/Item.msgbnd.dcx", "Regulation.BIN"):
            self.assertEqual("/" + merge_engine.canonical(rel), bhd5.normalise(rel))


class DeclareTests(unittest.TestCase):
    RULE = {"path": "msg/x.dcx", "strategy": "concat", "mods": ["a"], "prefer": "a"}

    def test_declarations_are_keyed_in_the_games_spelling(self) -> None:
        declared = merge_engine.declare_merges([{**self.RULE, "path": "MSG/X.dcx"}])
        self.assertEqual(list(declared), ["msg/x.dcx"])
        self.assertEqual(declared["msg/x.dcx"]["path"], "MSG/X.dcx")

    def test_the_same_rule_written_in_two_spellings_of_the_path_agrees(self) -> None:
        declared = merge_engine.declare_merges(
            [{**self.RULE, "path": "msg/engUS/x.dcx"}, {**self.RULE, "path": "msg/engus/x.dcx"}]
        )
        self.assertEqual(list(declared), ["msg/engus/x.dcx"])

    def test_two_declarations_that_differ_only_in_the_spelling_of_the_path_still_clash_when_they_disagree(self) -> None:
        with self.assertRaises(merge_engine.ConflictError):
            merge_engine.declare_merges([{**self.RULE, "path": "MSG/X.dcx"}, {**self.RULE, "prefer": "b"}])


class ShadowTests(EngineCase):
    def test_a_collision_under_another_spelling_shadows_that_mods_file(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        b = self.folder("b", {"msg/engus/x.dcx": "B"})
        provided = merge_engine.index([a, b])
        self.assertEqual(merge_engine.shadowed(provided, []), {"a": ["msg/engUS/x.dcx"]})

    def test_a_path_with_one_provider_is_never_shadowed(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        self.assertEqual(merge_engine.shadowed(merge_engine.index([a]), []), {})

    def test_a_declaration_that_leads_with_the_game_makes_a_single_provider_a_merge(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        merges = [{"path": "msg/engus/x.dcx", "strategy": "concat", "mods": ["a"], "prefer": "a", "base": "game"}]
        self.assertEqual(merge_engine.shadowed(merge_engine.index([a]), merges), {"a": ["msg/engUS/x.dcx"]})

    def test_dropped_spellings_are_shadowed_whether_or_not_anything_collides(self) -> None:
        a = self.folder("a", {"only.bin": "A"})
        provided = merge_engine.index([a])
        self.assertEqual(
            merge_engine.shadowed(provided, [], {"a": ["Z/y.bin"], "b": ["Q.bin"]}),
            {"a": ["Z/y.bin"], "b": ["Q.bin"]},
        )

    def test_the_result_is_sorted_so_it_is_stable(self) -> None:
        a = self.folder("a", {"B/x.dcx": "A", "A/x.dcx": "A"})
        b = self.folder("b", {"b/x.dcx": "B", "a/x.dcx": "B"})
        self.assertEqual(merge_engine.shadowed(merge_engine.index([a, b]), []), {"a": ["A/x.dcx", "B/x.dcx"]})


class CollisionMapTests(EngineCase):
    def test_it_names_providers_with_their_spellings_and_the_strategy_that_will_be_tried(self) -> None:
        a = self.folder("a", {"regulation.bin": "A", "msg/engUS/item.msgbnd.dcx": "A", "plain.bin": "A", "solo.bin": "A"})
        b = self.folder("b", {"regulation.bin": "B", "msg/engus/item.msgbnd.dcx": "B", "plain.bin": "B"})
        got = merge_engine.collision_map(merge_engine.index([a, b]), [])
        self.assertEqual(
            got,
            {
                "paths": {
                    "msg/engus/item.msgbnd.dcx": {
                        "providers": [["a", "msg/engUS/item.msgbnd.dcx"], ["b", "msg/engus/item.msgbnd.dcx"]],
                        "strategy": "fmg-3way",
                    },
                    "plain.bin": {"providers": [["a", "plain.bin"], ["b", "plain.bin"]], "strategy": None},
                    "regulation.bin": {
                        "providers": [["a", "regulation.bin"], ["b", "regulation.bin"]],
                        "strategy": "param-rows",
                    },
                },
                "dropped": {},
                "archives": [],
            },
        )

    def test_a_declared_strategy_replaces_the_automatic_one(self) -> None:
        a = self.folder("a", {"regulation.bin": "A"})
        b = self.folder("b", {"regulation.bin": "B"})
        merges = [{"path": "regulation.bin", "strategy": "concat", "mods": ["a", "b"], "prefer": "a"}]
        got = merge_engine.collision_map(merge_engine.index([a, b]), merges)
        self.assertEqual(got["paths"]["regulation.bin"]["strategy"], "concat")

    def test_it_moves_with_the_order_and_with_a_dropped_spelling(self) -> None:
        a = self.folder("a", {"x.bin": "A"})
        b = self.folder("b", {"x.bin": "B"})
        forward = merge_engine.collision_map(merge_engine.index([a, b]), [])
        backward = merge_engine.collision_map(merge_engine.index([b, a]), [])
        dropped = merge_engine.collision_map(merge_engine.index([a, b]), [], {"a": ["X.bin"]})
        self.assertEqual(len({str(forward), str(backward), str(dropped)}), 3)

    def test_it_moves_with_the_game_archives_present(self) -> None:
        a = self.folder("a", {"msg/engus/item_dlc02.msgbnd.dcx": "A"})
        b = self.folder("b", {"msg/engus/item_dlc02.msgbnd.dcx": "B"})
        provided = merge_engine.index([a, b])
        base = merge_engine.collision_map(provided, [], None, ["Data0.bdt", "Data0.bhd"])
        with_dlc = merge_engine.collision_map(provided, [], None, ["DLC.bdt", "DLC.bhd", "Data0.bdt", "Data0.bhd"])
        self.assertNotEqual(base, with_dlc)
        self.assertEqual(with_dlc["archives"], ["DLC.bdt", "DLC.bhd", "Data0.bdt", "Data0.bhd"])


class GameArchivesTests(EngineCase):
    def test_it_lists_the_archive_files_present_in_sorted_order(self) -> None:
        game = self.folder("game", {"Data0.bhd": "x", "Data0.bdt": "x", "DLC.bhd": "x", "regulation.bin": "x"})
        self.assertEqual(merge_engine.game_archives(game.source), ["DLC.bhd", "Data0.bdt", "Data0.bhd"])

    def test_a_folder_without_archives_lists_none(self) -> None:
        self.assertEqual(merge_engine.game_archives(self.folder("game", {"regulation.bin": "x"}).source), [])

    def test_the_automatic_strategies_are_chosen_by_the_games_file_kinds(self) -> None:
        self.assertEqual(merge_engine.auto_strategy("regulation.bin"), "param-rows")
        self.assertIsNone(merge_engine.auto_strategy("sub/regulation.bin"))
        self.assertEqual(merge_engine.auto_strategy("msg/engus/item_dlc02.msgbnd.dcx"), "fmg-3way")
        self.assertEqual(merge_engine.auto_strategy("script/talk/t100.talkesdbnd.dcx"), "esd-3way")
        self.assertIsNone(merge_engine.auto_strategy("menu/hi/01_common.tpf.dcx"))


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

    def test_a_declared_mod_that_is_not_installed_is_fine(self) -> None:
        a, b, _c = self.providers()
        planned = self.plan([a, b], [{**RULE, "mods": ["a", "b", "ghost"]}])
        self.assertEqual(planned, [("msg/x.dcx", b"A|B")])

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

    def test_an_undeclared_collision_without_a_strategy_goes_to_the_highest_priority_mod(self) -> None:
        plan = self.decide(self.providers(), [])
        [(rel, source)] = plan.outputs
        self.assertEqual(rel, "msg/x.dcx")
        self.assertEqual(Path(source).read_bytes(), b"C")
        [decision] = plan.decisions
        self.assertEqual(
            (decision.path, decision.outcome, decision.providers, decision.winner, decision.declared),
            ("msg/x.dcx", merge_engine.WINNER, ("a", "b", "c"), "c", False),
        )
        self.assertIn("no way to merge", decision.detail)

    def test_a_provider_outside_the_declaration_is_folded_in_and_named(self) -> None:
        plan = self.decide(self.providers(), [{**RULE, "mods": ["a", "b"]}])
        self.assertEqual(plan.outputs, [("msg/x.dcx", b"A|B|C")])
        [decision] = plan.decisions
        self.assertEqual((decision.outcome, decision.declared), (merge_engine.MERGED, True))
        self.assertIn("does not name c", decision.detail)
        self.assertIn("does not name c", merge_engine.warning(decision))

    def test_a_prefer_that_is_not_a_provider_hands_the_lead_to_the_highest_priority_mod(self) -> None:
        plan = self.decide(self.providers(), [{**RULE, "prefer": "ghost"}])
        self.assertEqual(plan.outputs, [("msg/x.dcx", b"C|A|B")])
        self.assertIn("ghost", plan.decisions[0].detail)
        self.assertIn("c, the highest priority", plan.decisions[0].detail)

    def test_a_declaration_in_another_spelling_applies_to_providers_in_any_spelling(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        b = self.folder("b", {"msg/engus/x.dcx": "B"})
        plan = self.decide([a, b], [{**RULE, "path": "MSG/ENGUS/X.dcx", "mods": ["a", "b"]}])
        self.assertEqual(plan.outputs, [("msg/engus/x.dcx", b"A|B")])

    def test_a_declared_merge_that_fails_stops_with_the_file_the_mods_and_the_way_out(self) -> None:
        def refuse_two(out, extra, notes=None):
            raise merge.MergeError("entry 1 row 5: both sides changed it and differ")

        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [{**RULE, "strategy": "refuse"}], strategies={"refuse": refuse_two})
        text = str(caught.exception)
        for expected in ("msg/x.dcx", "a, b, c", "row 5", "delete the entry"):
            self.assertIn(expected, text)

    def test_a_declared_merge_over_a_malformed_file_stops_with_the_file_and_the_error_named(self) -> None:
        def crash_two(out, extra, notes=None):
            raise IndexError("list index out of range")

        with self.assertRaises(merge_engine.ConflictError) as caught:
            self.plan(self.providers(), [{**RULE, "strategy": "crash"}], strategies={"crash": crash_two})
        for expected in ("msg/x.dcx", "IndexError", "a, b, c"):
            self.assertIn(expected, str(caught.exception))

    def test_a_declared_merge_takes_precedence_over_the_automatic_one(self) -> None:
        def unused(rel):
            raise AssertionError("the automatic merge ran")

        auto = merge_engine.AutoResolver(unused, lambda *a: None, lambda *a: None)
        with mock.patch.dict(merge_engine.AUTO_SUFFIX, {".dcx": "tri"}, clear=True), \
                mock.patch.object(merge, "NEEDS_VANILLA", frozenset({"tri"})):
            plan = self.decide(self.providers(), [RULE], auto=auto, strategies={**CONCAT, "tri": tri})
        self.assertEqual(plan.outputs, [("msg/x.dcx", b"A|B|C")])


class AutomaticTests(EngineCase):
    """Colliding files the profile declares nothing for."""

    GAME = b"G"

    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch.dict(merge_engine.AUTO_EXACT, {}, clear=True),
            mock.patch.dict(merge_engine.AUTO_SUFFIX, {".dcx": "tri"}, clear=True),
            mock.patch.object(merge, "NEEDS_VANILLA", frozenset({"tri", "takes", "keep", "refuse", "crash", "noting"})),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.auto = merge_engine.AutoResolver(lambda rel: self.GAME, lambda *a: None, lambda *a: None)

    def providers(self, names="abc"):
        return [self.folder(name, {"msg/x.dcx": name.upper()}) for name in names]

    def run_auto(self, contributors, strategy_name="tri", strategy=tri, auto=None, **kwargs):
        with mock.patch.dict(merge_engine.AUTO_SUFFIX, {".dcx": strategy_name}, clear=True):
            return self.decide(
                contributors, [], strategies={strategy_name: strategy}, auto=auto or self.auto, **kwargs
            )

    def test_the_game_leads_and_the_providers_fold_in_highest_priority_first(self) -> None:
        plan = self.run_auto(self.providers())
        self.assertEqual(plan.outputs, [("msg/x.dcx", b"G|C|B|A")])
        [decision] = plan.decisions
        self.assertEqual(
            (decision.outcome, decision.providers, decision.strategy, decision.declared, decision.winner),
            (merge_engine.MERGED, ("a", "b", "c"), "tri", False, None),
        )

    def test_every_step_is_three_way_against_the_game_copy(self) -> None:
        seen = []

        def spy(out, extra, vanilla, notes=None):
            seen.append(vanilla)
            return out + extra

        self.run_auto(self.providers(), "tri", spy)
        self.assertEqual(seen, [b"G", b"G", b"G"])

    def test_only_the_first_step_is_told_its_base_is_the_game(self) -> None:
        calls = []

        def takes(out, extra, vanilla, base_is_game=False, notes=None):
            calls.append((out, extra, base_is_game))
            return out + b"+" + extra

        with mock.patch.object(merge, "TAKES_GAME_BASE", frozenset({"takes"})):
            self.run_auto(self.providers("ab"), "takes", takes)
        self.assertEqual(calls, [(b"G", b"B", True), (b"G+B", b"A", False)])

    def test_a_tie_the_strategy_settles_for_its_lead_goes_to_the_mod_that_wins_when_the_merge_fails(self) -> None:
        contributors = self.providers()
        merged = self.run_auto(contributors, "keep", keep_lead)
        fallback = self.run_auto(contributors, "refuse", refuse)
        self.assertEqual(merged.outputs[0][1], b"C")
        self.assertEqual(Path(fallback.outputs[0][1]).read_bytes(), b"C")

    def test_a_strategy_that_refuses_hands_the_whole_file_to_the_highest_priority_mod(self) -> None:
        plan = self.run_auto(self.providers(), "refuse", refuse)
        [(rel, source)] = plan.outputs
        self.assertEqual((rel, Path(source).read_bytes()), ("msg/x.dcx", b"C"))
        [decision] = plan.decisions
        self.assertEqual((decision.outcome, decision.winner, decision.providers), (merge_engine.WINNER, "c", ("a", "b", "c")))
        self.assertIn("row 5", decision.detail)

    def test_a_strategy_that_crashes_on_a_malformed_file_is_a_fallback_with_the_error_named(self) -> None:
        plan = self.run_auto(self.providers(), "crash", crash)
        self.assertEqual(plan.decisions[0].outcome, merge_engine.WINNER)
        self.assertIn("IndexError", plan.decisions[0].detail)
        self.assertEqual(Path(plan.outputs[0][1]).read_bytes(), b"C")

    def test_a_file_with_no_strategy_is_won_by_the_highest_priority_mod(self) -> None:
        a = self.folder("a", {"menu/pic.bin": "A"})
        b = self.folder("b", {"menu/pic.bin": "B"})
        plan = self.run_auto([a, b])
        self.assertEqual(Path(plan.outputs[0][1]).read_bytes(), b"B")
        self.assertIn("no way to merge", plan.decisions[0].detail)
        self.assertIsNone(plan.decisions[0].strategy)

    def test_a_game_without_the_file_is_a_fallback_with_the_reason(self) -> None:
        def missing(rel):
            raise merge_engine.ConflictError("the game's own copy of msg/x.dcx could not be read: not in any of Data0")

        auto = merge_engine.AutoResolver(missing, lambda *a: None, lambda *a: None)
        plan = self.run_auto(self.providers(), auto=auto)
        self.assertEqual(plan.decisions[0].outcome, merge_engine.WINNER)
        self.assertIn("not in any of Data0", plan.decisions[0].detail)

    def test_no_resolver_at_all_is_a_fallback_too(self) -> None:
        with mock.patch.dict(merge_engine.AUTO_SUFFIX, {".dcx": "tri"}, clear=True):
            plan = self.decide(self.providers(), [], strategies={"tri": tri}, auto=None)
        self.assertEqual(plan.decisions[0].outcome, merge_engine.WINNER)
        self.assertIn("game's own copy is not available", plan.decisions[0].detail)

    def test_a_check_that_refuses_the_inputs_or_the_output_is_a_fallback(self) -> None:
        def refuse_inputs(rel, blobs, game):
            raise merge_engine.ConflictError("built for another build")

        def refuse_output(rel, merged, blobs, game):
            raise merge_engine.ConflictError("a row was lost")

        for label, auto, reason in (
            ("inputs", merge_engine.AutoResolver(lambda rel: b"G", refuse_inputs, lambda *a: None), "another build"),
            ("output", merge_engine.AutoResolver(lambda rel: b"G", lambda *a: None, refuse_output), "row was lost"),
        ):
            with self.subTest(label):
                plan = self.run_auto(self.providers(), auto=auto)
                self.assertEqual(plan.decisions[0].outcome, merge_engine.WINNER)
                self.assertIn(reason, plan.decisions[0].detail)

    def test_the_providers_blobs_reach_the_checks_highest_priority_first(self) -> None:
        got = []
        auto = merge_engine.AutoResolver(
            lambda rel: b"G", lambda rel, blobs, game: got.append((rel, blobs, game)), lambda *a: None
        )
        self.run_auto(self.providers("ab"), auto=auto)
        self.assertEqual(got, [("msg/x.dcx", [("b", b"B"), ("a", b"A")], b"G")])

    def test_providers_with_the_same_bytes_need_no_merge_and_no_output(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "SAME"})
        b = self.folder("b", {"msg/x.dcx": "SAME"})
        plan = self.run_auto([a, b], "refuse", refuse)
        self.assertEqual(plan.outputs, [])
        self.assertEqual((plan.decisions[0].outcome, plan.decisions[0].winner), (merge_engine.IDENTICAL, None))
        self.assertIsNone(merge_engine.warning(plan.decisions[0]))

    def test_same_bytes_under_another_spelling_still_leave_one_copy_in_the_games_spelling(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "SAME"})
        b = self.folder("b", {"msg/engus/x.dcx": "SAME"})
        plan = self.run_auto([a, b], "refuse", refuse)
        [(rel, source)] = plan.outputs
        self.assertEqual((rel, Path(source).read_bytes()), ("msg/engus/x.dcx", b"SAME"))
        self.assertEqual(plan.decisions[0].outcome, merge_engine.IDENTICAL)

    def test_same_size_different_bytes_are_not_identical(self) -> None:
        a = self.folder("a", {"menu/pic.bin": "AAAA"})
        b = self.folder("b", {"menu/pic.bin": "AAAB"})
        self.assertEqual(self.run_auto([a, b]).decisions[0].outcome, merge_engine.WINNER)

    def test_a_case_variant_pair_merges_into_the_games_spelling(self) -> None:
        a = self.folder("a", {"msg/engUS/x.dcx": "A"})
        b = self.folder("b", {"msg/engus/x.dcx": "B"})
        plan = self.run_auto([a, b])
        self.assertEqual(plan.outputs, [("msg/engus/x.dcx", b"G|B|A")])

    def test_what_a_strategy_could_not_carry_over_is_reported_with_the_path(self) -> None:
        def noting(out, extra, vanilla, notes=None):
            notes.append("N:" + extra.decode())
            return out + extra

        notes = []
        with mock.patch.object(merge, "NEEDS_NOTES", frozenset({"noting"})):
            self.run_auto(self.providers("ab"), "noting", noting, notes=notes)
        self.assertEqual(notes, [("msg/x.dcx", "N:B"), ("msg/x.dcx", "N:A")])

    def test_the_notes_of_a_fold_that_failed_are_not_reported(self) -> None:
        def noting_then_refusing(out, extra, vanilla, notes=None):
            notes.append("N")
            if extra == b"A":
                raise merge.MergeError("boom")
            return out + extra

        notes = []
        with mock.patch.object(merge, "NEEDS_NOTES", frozenset({"noting"})):
            plan = self.run_auto(self.providers("ab"), "noting", noting_then_refusing, notes=notes)
        self.assertEqual(plan.decisions[0].outcome, merge_engine.WINNER)
        self.assertEqual(notes, [])

    def test_the_providers_are_never_modified(self) -> None:
        contributors = self.providers()
        before = [tree(c.source) for c in contributors]
        self.run_auto(contributors, "refuse", refuse)
        self.assertEqual([tree(c.source) for c in contributors], before)

    def test_a_single_provider_is_left_alone(self) -> None:
        a = self.folder("a", {"msg/x.dcx": "A"})
        plan = self.run_auto([a])
        self.assertEqual((plan.outputs, plan.decisions), ([], []))


class DecisionWordingTests(unittest.TestCase):
    WON = merge_engine.Decision(
        "msg/engus/item_dlc02.msgbnd.dcx", merge_engine.WINNER,
        ("great-rune-overhaul", "boss-resurrection-lite", "nofalldead"), "nofalldead", "fmg-3way", False,
        "entry 1 row 5: both sides changed it and differ",
    )

    def test_a_win_names_the_file_every_provider_the_winner_and_the_reason(self) -> None:
        text = merge_engine.warning(self.WON)
        for expected in (
            "msg/engus/item_dlc02.msgbnd.dcx", "great-rune-overhaul", "boss-resurrection-lite", "nofalldead",
            "row 5", "highest priority", "not in the game",
        ):
            self.assertIn(expected, text)

    def test_a_clean_merge_has_no_warning_but_is_described(self) -> None:
        merged = self.WON._replace(outcome=merge_engine.MERGED, winner=None, detail="")
        self.assertIsNone(merge_engine.warning(merged))
        self.assertIn("merged from great-rune-overhaul, boss-resurrection-lite, nofalldead", merge_engine.describe(merged))

    def test_a_merge_with_something_to_say_has_a_warning(self) -> None:
        merged = self.WON._replace(outcome=merge_engine.MERGED, winner=None, detail="the entry does not name x")
        self.assertEqual(merge_engine.warning(merged), "msg/engus/item_dlc02.msgbnd.dcx: the entry does not name x")

    def test_warnings_for_keeps_only_the_decisions_that_have_one(self) -> None:
        merged = self.WON._replace(outcome=merge_engine.MERGED, winner=None, detail="")
        same = self.WON._replace(outcome=merge_engine.IDENTICAL, winner=None, detail="")
        self.assertEqual(len(merge_engine.warnings_for([merged, self.WON, same])), 1)

    def test_a_decision_survives_the_state_file(self) -> None:
        import json

        again = merge_engine.decisions_from_state(json.loads(json.dumps([self.WON.to_json()])))
        self.assertEqual(again, [self.WON])

    def test_whatever_in_the_state_does_not_read_back_as_a_decision_is_dropped(self) -> None:
        good = self.WON.to_json()
        junk = [None, 3, "x", {}, {"outcome": "nonsense", "path": "p", "providers": []}, {"outcome": "winner"},
                {**good, "providers": 7}, good]
        self.assertEqual(merge_engine.decisions_from_state(junk), [self.WON])
        for raw in (None, {}, "winner", 5):
            self.assertEqual(merge_engine.decisions_from_state(raw), [])


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
            return merge_engine.plan(provided, [a, b], [rule], loader or self.loader, strategies=strategies).outputs

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

    def test_a_vanilla_that_is_not_a_table_is_a_conflict_error(self) -> None:
        rule = {"path": "regulation.bin", "strategy": "param-rows", "mods": ["a"], "prefer": "a", "vanilla": "oops"}
        with self.assertRaises(merge_engine.ConflictError) as caught:
            merge_engine.declared_ancestor([rule], "regulation.bin", self.loader)
        self.assertIn("table", str(caught.exception))

    def test_an_archive_name_that_is_not_text_is_a_conflict_error(self) -> None:
        loader = merge_engine.make_vanilla_loader(self.paths, self.organizer.downloads, {"m": {"archive": 5}})
        with self.assertRaises(merge_engine.ConflictError):
            loader("m", "x")


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

    def test_a_missing_archive_file_is_fatal_too(self) -> None:
        with mock.patch.object(bhd5, "read_file", side_effect=FileNotFoundError("Data0.bdt")):
            with self.assertRaises(merge_engine.ConflictError) as caught:
                merge_engine.load_game_bases([{"path": "a", "base": "game"}], "/game")
        self.assertIn("Data0.bdt", str(caught.exception))


class ResolverTests(EngineCase):
    LIVE = types.SimpleNamespace(regulation="11711000", regulation_sha="s" * 64)

    def resolver(self):
        return merge_engine.make_auto_resolver("/game", self.LIVE, self.paths)

    def test_a_game_copy_is_read_out_of_the_archives_once(self) -> None:
        with mock.patch.object(bhd5, "read_file", return_value=b"G") as read:
            resolver = self.resolver()
            self.assertEqual(resolver.game_copy("msg/engus/x.msgbnd.dcx"), b"G")
            self.assertEqual(resolver.game_copy("msg/engus/x.msgbnd.dcx"), b"G")
        read.assert_called_once_with("/game", "msg/engus/x.msgbnd.dcx")

    def test_a_path_no_archive_holds_is_a_conflict_error_with_the_reason(self) -> None:
        with mock.patch.object(bhd5, "read_file", side_effect=bhd5.Bhd5Error("is not in any of Data0")):
            with self.assertRaises(merge_engine.ConflictError) as caught:
                self.resolver().game_copy("msg/engus/x.msgbnd.dcx")
        self.assertIn("is not in any of Data0", str(caught.exception))
        self.assertIn("msg/engus/x.msgbnd.dcx", str(caught.exception))

    def test_a_missing_archive_file_is_a_conflict_error_too(self) -> None:
        with mock.patch.object(bhd5, "read_file", side_effect=FileNotFoundError("Data0.bdt")):
            with self.assertRaises(merge_engine.ConflictError):
                self.resolver().game_copy("msg/engus/x.msgbnd.dcx")

    def test_regulation_bin_is_the_kept_baseline_not_an_archive_member(self) -> None:
        kept = self.tmp / "baseline.bin"
        kept.write_bytes(b"REG")
        with mock.patch.object(heal, "adopt_baseline", return_value=kept) as adopt, \
                mock.patch.object(bhd5, "read_file", side_effect=AssertionError("archive read")):
            self.assertEqual(self.resolver().game_copy("regulation.bin"), b"REG")
        adopt.assert_called_once_with("/game", self.LIVE, base=self.paths.baselines)

    def test_a_baseline_that_cannot_be_adopted_is_a_conflict_error(self) -> None:
        with mock.patch.object(heal, "adopt_baseline", side_effect=heal.HealError("kept baseline differs")):
            with self.assertRaises(merge_engine.ConflictError) as caught:
                self.resolver().game_copy("regulation.bin")
        self.assertIn("kept baseline differs", str(caught.exception))

    def test_a_regulation_built_for_another_build_is_refused_naming_the_mod_and_both_builds(self) -> None:
        with mock.patch.object(gamebuild, "cached_regulation_version", return_value="11611000"):
            with self.assertRaises(merge_engine.ConflictError) as caught:
                self.resolver().check_inputs("regulation.bin", [("old-mod", b"x")], b"G")
        for text in ("old-mod", "11611000", "11711000"):
            self.assertIn(text, str(caught.exception))

    def test_the_layout_gate_decides_whether_rows_can_be_transplanted(self) -> None:
        problems = ("old-mod: EquipParamWeapon.param row stride 12 != baseline 8",)
        with mock.patch.object(gamebuild, "cached_regulation_version", return_value="11711000"), \
                mock.patch.object(heal, "layout_gate", return_value=problems) as gate:
            with self.assertRaises(merge_engine.ConflictError) as caught:
                self.resolver().check_inputs("regulation.bin", [("old-mod", b"x")], b"G")
        self.assertIn("row stride 12", str(caught.exception))
        gate.assert_called_once_with(b"G", [("old-mod", b"x")])

    def test_regulations_on_the_installed_build_with_a_matching_layout_pass(self) -> None:
        with mock.patch.object(gamebuild, "cached_regulation_version", return_value="11711000"), \
                mock.patch.object(heal, "layout_gate", return_value=()):
            self.resolver().check_inputs("regulation.bin", [("a", b"x"), ("b", b"y")], b"G")

    def test_a_merged_regulation_that_loses_rows_is_refused(self) -> None:
        with mock.patch.object(heal, "verify_rebase", return_value=("a: SpEffectParam row 5 did not survive the rebase",)) as verify:
            with self.assertRaises(merge_engine.ConflictError) as caught:
                self.resolver().check_output("regulation.bin", b"M", [("a", b"x")], b"G")
        self.assertIn("row 5", str(caught.exception))
        verify.assert_called_once_with(b"M", b"G", [("a", b"x")], self.LIVE)

    def test_other_files_are_not_checked(self) -> None:
        with mock.patch.object(gamebuild, "cached_regulation_version", side_effect=AssertionError("checked")), \
                mock.patch.object(heal, "layout_gate", side_effect=AssertionError("checked")), \
                mock.patch.object(heal, "verify_rebase", side_effect=AssertionError("checked")):
            resolver = self.resolver()
            resolver.check_inputs("msg/engus/x.msgbnd.dcx", [("a", b"x")], b"G")
            resolver.check_output("msg/engus/x.msgbnd.dcx", b"M", [("a", b"x")], b"G")


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
                "/game", self.LIVE, [self.rule], merge_engine.index([self.a, self.b, self.c]), self.paths, loader
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
            result = merge_engine.rebase_bases("/game", self.LIVE, [self.rule], merge_engine.index([self.a, self.c]), self.paths, loader)
        self.assertIsNone(prepare.call_args.args[2])
        self.assertFalse(result.rebased)
        self.assertEqual(result.bases, {})

    def test_game_bases_are_merged_in_and_do_not_count_as_a_rebase(self) -> None:
        merges = [{"path": "msg/y.dcx", "base": "game"}]
        with mock.patch.object(heal, "prepare_rebase", return_value={}), \
                mock.patch.object(bhd5, "read_file", return_value=b"G"):
            result = merge_engine.rebase_bases("/game", self.LIVE, merges, merge_engine.index([self.a]), self.paths, no_vanilla)
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

    def test_a_regulation_in_another_spelling_is_still_found(self) -> None:
        odd = self.folder("odd", {"Regulation.bin": "RO"})
        with mock.patch.object(heal, "prepare_rebase", return_value={}) as prepare:
            merge_engine.rebase_bases(
                "/game", self.LIVE, [self.rule], merge_engine.index([odd, self.b]), self.paths, lambda mod_id, member: b"ANC"
            )
        self.assertEqual(prepare.call_args.args[3], [("odd", b"RO"), ("b", b"RB")])

    def test_a_winner_copied_by_path_is_verified_after_a_rebase_like_bytes(self) -> None:
        source = self.tmp / "winner.bin"
        source.write_bytes(b"M")
        rebase = merge_engine.Rebase({"regulation.bin": b"B"}, True, b"B", [("a", b"RA")])
        with mock.patch.object(heal, "verify_rebase", return_value=()) as verify:
            merge_engine.verify_rebase([("regulation.bin", source)], rebase, self.LIVE)
        verify.assert_called_once_with(b"M", b"B", [("a", b"RA")], self.LIVE)


if __name__ == "__main__":
    unittest.main()

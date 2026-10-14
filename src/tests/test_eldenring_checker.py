from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


class _CheckReturn:
    INVALID, FIXABLE, VALID = "INVALID", "FIXABLE", "VALID"


class _ModDataChecker:
    INVALID, FIXABLE, VALID = _CheckReturn.INVALID, _CheckReturn.FIXABLE, _CheckReturn.VALID
    CheckReturn = _CheckReturn

    def __init__(self) -> None:
        pass


_mobase = types.ModuleType("mobase")
_mobase.ModDataChecker = _ModDataChecker
sys.modules["mobase"] = _mobase

import eldenring_testsupport as support  # noqa: E402  (after the stub on purpose)

PACKAGE = support.load_support("_eldenring_checker_tests")
kinds = support.module(PACKAGE, "kinds")
checker = support.module(PACKAGE, "checker")


class Entry:
    def __init__(self, name: str, parent: "Tree | None" = None) -> None:
        self._name = name
        self._parent = parent

    def name(self) -> str:
        return self._name

    def isDir(self) -> bool:  # noqa: N802
        return False

    def parent(self):
        return self._parent

    def detach(self) -> None:
        self._parent.children.remove(self)
        self._parent = None


class Tree(Entry):
    """An IFileTree: iterable entries, merge and detach."""

    def __init__(self, name: str = "", parent: "Tree | None" = None) -> None:
        super().__init__(name, parent)
        self.children: list[Entry] = []

    def isDir(self) -> bool:  # noqa: N802
        return True

    def __iter__(self):
        return iter(list(self.children))

    def __len__(self) -> int:
        return len(self.children)

    def at(self, index):
        return self.children[index]

    def merge(self, other: "Tree") -> None:
        # Like IFileTree.merge: an entry of the same name is replaced.
        for child in list(other.children):
            for clash in [c for c in self.children if c.name() == child.name() and c is not other]:
                self.children.remove(clash)
            child._parent = self
            self.children.append(child)
        other.children.clear()


def build(layout: dict, name: str = "", parent: Tree | None = None) -> Tree:
    """{name: None (file) | {...} (directory)} as a tree."""
    tree = Tree(name, parent)
    for child, value in layout.items():
        tree.children.append(Entry(child, tree) if value is None else build(value, child, tree))
    return tree


def names(tree: Tree) -> list[str]:
    return sorted(entry.name() for entry in tree)


def simple_archive_base(tree: Tree, check) -> Tree | None:
    """InstallerQuick::getSimpleArchiveBase, installerquick.cpp:94-112."""
    while True:
        if check.dataLooksValid(tree) == _ModDataChecker.VALID:
            return tree
        children = list(tree)
        if len(children) == 1 and children[0].isDir():
            tree = children[0]
        else:
            return None


def quick_install(tree: Tree, check) -> Tree | None:
    """InstallerQuick::install's base-or-fix branch, installerquick.cpp:128-141."""
    base = simple_archive_base(tree, check)
    if base is None and check.dataLooksValid(tree) == _ModDataChecker.FIXABLE:
        return check.fix(tree)
    return base


class InteractiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = checker.EldenRingModDataChecker()

    def test_a_package_root_is_valid_and_a_single_wrapper_is_unfolded_by_the_descent(self) -> None:
        tree = build({"Wrapper": {"regulation.bin": None, "msg": {"a": None}}})
        self.assertEqual(self.check.dataLooksValid(tree), _ModDataChecker.FIXABLE)
        base = quick_install(tree, self.check)
        self.assertEqual(names(base), ["msg", "regulation.bin"])

    def test_a_wrapper_with_a_readme_sibling_is_fixable_and_fixed(self) -> None:
        tree = build({"Wrapper": {"regulation.bin": None}, "readme.txt": None})
        self.assertEqual(self.check.dataLooksValid(tree), _ModDataChecker.FIXABLE)
        fixed = quick_install(tree, self.check)
        self.assertEqual(names(fixed), ["readme.txt", "regulation.bin"])

    def test_a_document_next_to_the_wrapper_with_the_same_name_as_one_inside_it_gives_way(self) -> None:
        tree = build({"Wrapper": {"regulation.bin": None, "readme.txt": None}, "readme.txt": None})
        fixed = quick_install(tree, self.check)
        self.assertEqual(names(fixed), ["readme.txt", "regulation.bin"])

    def test_a_files_directory_holding_two_option_directories_is_refused(self) -> None:
        tree = build({"Files": {"mod": {"regulation.bin": None}, "CSV 4 Merge": {"a.csv": None}}})
        self.assertEqual(self.check.dataLooksValid(tree), _ModDataChecker.INVALID)
        self.assertIsNone(quick_install(tree, self.check))

    def test_a_mods_only_archive_and_a_wrapped_dll_are_valid(self) -> None:
        self.assertEqual(
            self.check.dataLooksValid(build({"mods": {"A.dll": None, "A": {"config.ini": None}}})),
            _ModDataChecker.VALID,
        )
        self.assertEqual(
            self.check.dataLooksValid(build({"RadialMenu": {"RadialMenu.dll": None}})),
            _ModDataChecker.VALID,
        )

    def test_an_unclassifiable_root_is_invalid(self) -> None:
        self.assertEqual(self.check.dataLooksValid(build({"readme.txt": None})), _ModDataChecker.INVALID)


class ImportModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = checker.EldenRingModDataChecker()

    def test_the_root_is_valid_whatever_it_holds_and_the_installer_never_calls_fix(self) -> None:
        roots = [
            build({"Wrapper": {"regulation.bin": None}}),
            build({"Files": {"mod": {"regulation.bin": None}, "CSV 4 Merge": {"a.csv": None}}}),
            build({"weird": {"thing.bin": None}, "other.dat": None}),
        ]
        for tree in roots:
            with self.subTest(names(tree)):
                fixes = []
                original_fix = self.check.fix
                self.check.fix = lambda t, f=fixes, o=original_fix: (f.append(1), o(t))[1]
                with kinds.importing():
                    base = simple_archive_base(tree, self.check)
                    self.assertIs(base, tree)
                    self.assertIs(quick_install(tree, self.check), tree)
                self.check.fix = original_fix
                self.assertEqual(fixes, [])

    def test_every_subtree_is_invalid_in_import_mode(self) -> None:
        tree = build({"Wrapper": {"regulation.bin": None}})
        with kinds.importing():
            self.assertEqual(self.check.dataLooksValid(next(iter(tree))), _ModDataChecker.INVALID)

    def test_outside_import_mode_an_unclassifiable_root_is_invalid_again(self) -> None:
        tree = build({"weird": {"thing.bin": None}})
        with kinds.importing():
            self.assertEqual(self.check.dataLooksValid(tree), _ModDataChecker.VALID)
        self.assertEqual(self.check.dataLooksValid(tree), _ModDataChecker.INVALID)


if __name__ == "__main__":
    unittest.main()

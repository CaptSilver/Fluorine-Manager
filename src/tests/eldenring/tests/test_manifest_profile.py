"""load_profile on profiles built in tmp_path: includes, overrides, and how
merge, prune and rename tables are merged across an include chain."""
import pytest

from ermlib.errors import PathError
from ermlib.manifest import load_profile


def _mod(base, name, includes=None, mods=None, excludes=None):
    lines = [f'name = "{name}"']
    if includes:
        lines.append("includes = [" + ", ".join(f'"{i}"' for i in includes) + "]")
    if excludes:
        lines.append("excludes = [" + ", ".join(f'"{e}"' for e in excludes) + "]")
    for m in mods or []:
        lines += ["", "[[mods]]"] + [f'{k} = {v!r}' if not isinstance(v, str) else f'{k} = "{v}"'
                                     for k, v in m.items()]
    (base / f"{name}.toml").write_text("\n".join(lines) + "\n")


def test_profile_includes_composes_mods_included_first(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1}])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "b", "source": "nexus", "nexus_id": 2}])
    prof = load_profile("parent", base=tmp_path)
    assert [m["id"] for m in prof["mods"]] == ["a", "b"]


def test_profile_includes_own_entry_overrides_included(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1, "install": "game"}])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "a", "source": "nexus", "nexus_id": 1, "install": "mods"}])
    prof = load_profile("parent", base=tmp_path)
    assert [m["id"] for m in prof["mods"]] == ["a"]        # deduped
    assert prof["mods"][0]["install"] == "mods"


def test_profile_includes_cycle_raises(tmp_path):
    _mod(tmp_path, "x", includes=["y"])
    _mod(tmp_path, "y", includes=["x"])
    with pytest.raises(PathError):
        load_profile("x", base=tmp_path)


def test_profile_includes_unknown_raises(tmp_path):
    _mod(tmp_path, "p", includes=["nope-xyz"])
    with pytest.raises(PathError):
        load_profile("p", base=tmp_path)


def test_profile_includes_merges_excludes_from_included_profile(tmp_path):
    _mod(tmp_path, "child", mods=[{"id": "a", "source": "nexus", "nexus_id": 1}],
         excludes=["z"])
    _mod(tmp_path, "parent", includes=["child"],
         mods=[{"id": "b", "source": "nexus", "nexus_id": 2}], excludes=["w"])
    prof = load_profile("parent", base=tmp_path)
    assert set(prof["excludes"]) == {"z", "w"}
    assert len(prof["excludes"]) == len(set(prof["excludes"]))


def test_merges_and_prunes_resolve_through_includes(tmp_path):
    """A merge declared in an included profile is inherited, the same way
    excludes are — otherwise every composing profile would have to repeat it."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[merges]]\n'
        'path = "msg/engus/menu_dlc02.msgbnd.dcx"\n'
        'strategy = "fmg-union"\n'
        'mods = ["a", "b"]\n'
        'prefer = "a"\n'
        '[[prunes]]\n'
        'mod = "b"\n'
        'paths = ["msg/engus/item_dlc02.msgbnd.dcx"]\n')
    (tmp_path / "top.toml").write_text('name = "top"\nincludes = ["base"]\n')

    prof = load_profile("top", base=tmp_path)
    assert [m["path"] for m in prof["merges"]] == ["msg/engus/menu_dlc02.msgbnd.dcx"]
    assert prof["merges"][0]["prefer"] == "a"
    assert prof["prunes"][0]["paths"] == ["msg/engus/item_dlc02.msgbnd.dcx"]


def test_renames_resolve_through_includes(tmp_path):
    """A rename is packaging metadata like a prune, so it has to travel with the
    mod through the include chain. Declared ONLY in the included profile, so the
    raw TOML key on the top profile can't make this pass on its own."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[renames]]\n'
        'mod = "b"\n'
        'paths = { "msg/engUS/item.dcx" = "msg/engus/item.dcx" }\n')
    (tmp_path / "top.toml").write_text('name = "top"\nincludes = ["base"]\n')

    prof = load_profile("top", base=tmp_path)
    assert len(prof["renames"]) == 1
    assert prof["renames"][0]["paths"] == {"msg/engUS/item.dcx": "msg/engus/item.dcx"}


def test_a_rename_declared_twice_is_deduplicated(tmp_path):
    """Running one twice would raise the second time round: the source has
    already moved, and its destination is now occupied by the mod's own file."""
    body = ('[[renames]]\n'
            'mod = "b"\n'
            'paths = { "msg/engUS/item.dcx" = "msg/engus/item.dcx" }\n')
    (tmp_path / "one.toml").write_text('name = "one"\n' + body)
    (tmp_path / "two.toml").write_text('name = "two"\nincludes = ["one"]\n' + body)

    assert len(load_profile("two", base=tmp_path)["renames"]) == 1


def test_profiles_without_renames_get_an_empty_list(tmp_path):
    (tmp_path / "bare.toml").write_text('name = "bare"\n')
    assert load_profile("bare", base=tmp_path)["renames"] == []


def test_a_merge_declared_twice_is_deduplicated(tmp_path):
    """Two profiles in the include graph may both declare the same merge.
    Applying it twice would merge an already-merged file into itself."""
    body = ('[[merges]]\n'
            'path = "p"\nstrategy = "fmg-union"\nmods = ["a", "b"]\nprefer = "a"\n')
    (tmp_path / "one.toml").write_text('name = "one"\n' + body)
    (tmp_path / "two.toml").write_text('name = "two"\nincludes = ["one"]\n' + body)

    prof = load_profile("two", base=tmp_path)
    assert len(prof["merges"]) == 1


def test_a_merge_declared_twice_with_different_prefer_is_not_deduplicated(tmp_path):
    """Two profiles in an include chain can name the same path and mods but
    disagree on `prefer` (or strategy). Deduping on (path, mods) alone would
    silently keep whichever loaded first and drop the other's `prefer` --
    exactly the kind of silent override load_profile's own docstring promises
    LATEST wins for. Both entries must survive so conflicts._declare_merges
    gets the chance to raise on the disagreement."""
    (tmp_path / "base.toml").write_text(
        'name = "base"\n'
        '[[merges]]\n'
        'path = "msg/x.dcx"\nstrategy = "fmg-union"\nmods = ["mod-x", "mod-y"]\n'
        'prefer = "mod-x"\n')
    (tmp_path / "top.toml").write_text(
        'name = "top"\nincludes = ["base"]\n'
        '[[merges]]\n'
        'path = "msg/x.dcx"\nstrategy = "fmg-union"\nmods = ["mod-x", "mod-y"]\n'
        'prefer = "mod-y"\n')

    prof = load_profile("top", base=tmp_path)
    assert len(prof["merges"]) == 2
    assert {m["prefer"] for m in prof["merges"]} == {"mod-x", "mod-y"}


def test_profiles_without_merges_get_empty_lists(tmp_path):
    (tmp_path / "plain.toml").write_text('name = "plain"\n')
    prof = load_profile("plain", base=tmp_path)
    assert prof["merges"] == []
    assert prof["prunes"] == []


def test_a_merge_entry_with_a_nested_table_is_deduplicated(tmp_path):
    """Three-way strategies declare `vanilla = { mod = ..., member = ... }`, a
    nested TOML table. The dedup key handled lists but not dicts, so composing
    two profiles that both declared such a merge crashed before any of the
    disagreement checks could run."""
    base = tmp_path / "profiles"
    base.mkdir()
    merge_toml = (
        '[[merges]]\n'
        'path = "regulation.bin"\n'
        'strategy = "param-rows"\n'
        'mods = ["a", "b"]\n'
        'prefer = "a"\n'
        'vanilla = { mod = "rando", member = "Vanilla/regulation.bin" }\n'
    )
    (base / "leaf.toml").write_text('name = "leaf"\ndescription = "d"\n\n' + merge_toml)
    (base / "top.toml").write_text(
        'name = "top"\ndescription = "d"\nincludes = ["leaf"]\n\n' + merge_toml)

    prof = load_profile("top", base=base)
    assert len(prof["merges"]) == 1
    assert prof["merges"][0]["vanilla"]["member"] == "Vanilla/regulation.bin"


def test_nested_tables_that_differ_are_kept_as_separate_entries(tmp_path):
    # Two declarations for the same path that disagree must both survive here,
    # so conflicts._declare_merges can refuse them by name rather than one
    # being silently dropped at load time.
    base = tmp_path / "profiles"
    base.mkdir()
    def merge_for(member):
        return ('[[merges]]\n'
                'path = "regulation.bin"\n'
                'strategy = "param-rows"\n'
                'mods = ["a", "b"]\n'
                'prefer = "a"\n'
                f'vanilla = {{ mod = "rando", member = "{member}" }}\n')
    (base / "leaf.toml").write_text('name = "leaf"\ndescription = "d"\n\n' + merge_for("one.bin"))
    (base / "top.toml").write_text(
        'name = "top"\ndescription = "d"\nincludes = ["leaf"]\n\n' + merge_for("two.bin"))
    assert len(load_profile("top", base=base)["merges"]) == 2

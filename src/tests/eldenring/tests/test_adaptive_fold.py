"""The fold order the engine's automatic regulation.bin merge relies on.

The engine leads with the game's file and folds the providers highest priority
first, with the game's file as the ancestor of every step. These tests replay that
order against the real param_rows, so a change in how it settles ties shows up here.
"""
from ermlib import merge
from ermlib.formats import param, regulation

from tests.build_fixtures import _regulation

SP = b"SP_EFFECT_PARAM_ST"


def _rows(blob, eid):
    entry = next(e for e in regulation.entries(blob) if e.id == eid)
    return {r.id: r.data for r in param.read(entry.data).rows}


def _fold(game, providers):
    """providers: [(mod, regulation blob)] lowest priority first, as the engine receives them."""
    out, notes = game, []
    for step, (_mod, blob) in enumerate(reversed(providers)):
        out = merge.param_rows(out, blob, game, notes=notes, base_is_game=(step == 0))
    return out, notes


def test_a_row_every_mod_added_under_one_id_ends_as_the_highest_priority_mods():
    game = _regulation({1: (SP, {1: b"\x01" * 8}, 8)})
    low = _regulation({1: (SP, {1: b"\x01" * 8, 77: b"\xcc" * 8}, 8)})
    mid = _regulation({1: (SP, {1: b"\x01" * 8, 77: b"\xbb" * 8}, 8)})
    high = _regulation({1: (SP, {1: b"\x01" * 8, 77: b"\xaa" * 8}, 8)})
    out, notes = _fold(game, [("low", low), ("mid", mid), ("high", high)])
    assert _rows(out, 1)[77] == b"\xaa" * 8
    assert [type(note) for note in notes] == [merge.RivalRow, merge.RivalRow]


def test_edits_to_different_rows_all_survive_whatever_the_order():
    game = _regulation({1: (SP, {1: b"\x01" * 8, 2: b"\x02" * 8, 3: b"\x03" * 8}, 8)})
    one = _regulation({1: (SP, {1: b"\x11" * 8, 2: b"\x02" * 8, 3: b"\x03" * 8}, 8)})
    two = _regulation({1: (SP, {1: b"\x01" * 8, 2: b"\x22" * 8, 3: b"\x03" * 8}, 8)})
    three = _regulation({1: (SP, {1: b"\x01" * 8, 2: b"\x02" * 8, 3: b"\x33" * 8}, 8)})
    forward, _ = _fold(game, [("one", one), ("two", two), ("three", three)])
    backward, _ = _fold(game, [("three", three), ("two", two), ("one", one)])
    wanted = {1: b"\x11" * 8, 2: b"\x22" * 8, 3: b"\x33" * 8}
    assert _rows(forward, 1) == wanted
    assert _rows(backward, 1) == wanted


def test_a_mods_edit_to_a_row_the_game_also_has_wins_over_the_game():
    game = _regulation({1: (SP, {1: b"\x01" * 8, 9: b"\x99" * 8}, 8)})
    mod = _regulation({1: (SP, {1: b"\x01" * 8, 9: b"\x55" * 8}, 8)})
    out, notes = _fold(game, [("mod", mod)])
    assert _rows(out, 1)[9] == b"\x55" * 8
    assert notes == []

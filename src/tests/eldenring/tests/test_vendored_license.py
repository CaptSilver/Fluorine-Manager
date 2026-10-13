import pathlib

SUPPORT = (
    pathlib.Path(__file__).resolve().parents[4]
    / "libs/basic_games/games/eldenring_support"
)


def test_the_notice_for_the_vendored_code_is_in_the_package_init():
    # A .py file, so the bundle build's strip of LICENSE/*.txt/*.md keeps it.
    text = (SUPPORT / "__init__.py").read_text()
    assert "GPL-2.0-or-later" in text
    assert "GPL-3.0" in text
    assert "github.com/CaptSilver/elden_ring_mods" in text


def test_no_gpl2_licence_text_sits_inside_the_gpl3_tree():
    # Fluorine is GPL-3.0; erm's GPLv2 LICENSE would contradict LICENSE.txt.
    for name in ("LICENSE", "COPYING", "NOTICE"):
        assert not (SUPPORT / name).exists(), name
        assert not (SUPPORT / "ermlib" / name).exists(), name

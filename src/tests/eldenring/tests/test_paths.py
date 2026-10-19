import os

import pytest

from ermlib.errors import PathError
from ermlib.paths import (
    APPID,
    MODDED_SAVE_NAME,
    SAVE_NAMES,
    find_prefix,
    find_save_dir,
    is_safe_relpath,
    reshade_active,
)

# What a me3 launch with `savefile = "ER0000_modded"` writes: the name as given,
# no extension, with a .bak beside it. Read from a listing of the save folder
# after an in-game launch; a different name there means changing this and
# paths.MODDED_SAVE_NAME together.
EXPECTED_MODDED_SAVE = "ER0000_modded"


def _save_root(library):
    return (
        library
        / "steamapps"
        / "compatdata"
        / APPID
        / "pfx"
        / "drive_c/users/steamuser/AppData/Roaming/EldenRing"
    )


def test_reshade_active_true_for_reshade_symlink(tmp_path):
    # reshade-steam-proton installs ReShade as Game/dxgi.dll -> .../ReShade64.dll
    game = tmp_path / "Game"
    game.mkdir()
    real = tmp_path / "reshade" / "ReShade64.dll"
    real.parent.mkdir()
    real.write_bytes(b"MZ")
    (game / "dxgi.dll").symlink_to(real)
    assert reshade_active(game) is True


def test_reshade_active_false_without_the_symlink(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    # no dxgi.dll at all
    assert reshade_active(game) is False
    # a plain (non-symlink) dxgi.dll is not a ReShade install we placed
    (game / "dxgi.dll").write_bytes(b"MZ")
    assert reshade_active(game) is False


def test_reshade_active_false_for_non_reshade_symlink(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    other = tmp_path / "dxvk" / "dxgi.dll"
    other.parent.mkdir()
    other.write_bytes(b"MZ")
    (game / "dxgi.dll").symlink_to(other)   # points at DXVK, not ReShade
    assert reshade_active(game) is False


def test_appid_constant():
    assert APPID == "1245620"


def test_is_safe_relpath():
    assert is_safe_relpath("SeamlessCoop/ersc.dll") is True
    assert is_safe_relpath("ersc_launcher.exe") is True
    assert is_safe_relpath("/etc/x") is False        # absolute
    assert is_safe_relpath("../x") is False           # leading traversal
    assert is_safe_relpath("a/../../x") is False       # embedded traversal


def test_find_prefix_locates_and_raises(tmp_path):
    pfx = tmp_path / "steamapps" / "compatdata" / APPID / "pfx"
    pfx.mkdir(parents=True)
    assert find_prefix(tmp_path).samefile(pfx)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PathError):
        find_prefix(empty)


def test_find_save_dir_finds_steamid_subfolder(tmp_path):
    save_dir = _save_root(tmp_path) / "76561198000000000"
    save_dir.mkdir(parents=True)
    (save_dir / "ER0000.sl2").write_bytes(b"save")
    assert find_save_dir(tmp_path).samefile(save_dir)


def test_find_save_dir_finds_co2_only_save(tmp_path):
    # After `erm quarantine` moves ER0000.sl2 out, Seamless Co-op writes
    # ER0000.co2 in its place — find_save_dir must still locate the folder.
    save_dir = _save_root(tmp_path) / "76561198000000000"
    save_dir.mkdir(parents=True)
    (save_dir / "ER0000.co2").write_bytes(b"save")
    assert find_save_dir(tmp_path).samefile(save_dir)


def test_find_save_dir_raises_without_save(tmp_path):
    # Prefix exists but no ER0000.sl2 anywhere -> PathError, not empty return.
    (tmp_path / "steamapps" / "compatdata" / APPID / "pfx").mkdir(parents=True)
    with pytest.raises(PathError):
        find_save_dir(tmp_path)


def test_libraryfolders_key_is_matched_ignoring_case(tmp_path):
    steam = tmp_path / "steam"
    (steam / "steamapps").mkdir(parents=True)
    lib2 = tmp_path / "lib2"
    pfx = lib2 / "steamapps" / "compatdata" / APPID / "pfx"
    pfx.mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % lib2
    )
    assert find_prefix(steam).samefile(pfx)


def test_find_prefix_finds_a_prefix_in_a_secondary_library(tmp_path):
    # find_prefix, find_save_dir and saves.save_dir all go
    # through _library_dirs, so this is the one test of its libraryfolders.vdf
    # parse. Ported from erm's find_game_dir case for a secondary library.
    steam = tmp_path / "steam"
    (steam / "steamapps").mkdir(parents=True)
    lib2 = tmp_path / "lib2"
    pfx = lib2 / "steamapps" / "compatdata" / APPID / "pfx"
    pfx.mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % lib2
    )
    assert find_prefix(steam).samefile(pfx)


def test_find_prefix_survives_a_malformed_libraryfolders(tmp_path):
    steam = tmp_path / "steam"
    pfx = steam / "steamapps" / "compatdata" / APPID / "pfx"
    pfx.mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders" { "0" } } }')
    assert find_prefix(steam).samefile(pfx)


def test_find_save_dir_skips_unreadable_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions; can't simulate EACCES")
    root = _save_root(tmp_path)
    root.mkdir(parents=True)
    os.chmod(root, 0o000)
    try:
        # iterdir() on the unreadable root raises PermissionError internally;
        # discovery must swallow it and raise PathError, never leak OSError.
        with pytest.raises(PathError):
            find_save_dir(tmp_path)
    finally:
        os.chmod(root, 0o755)


def test_save_names_cover_steams_save_the_coop_save_and_the_modded_save():
    assert SAVE_NAMES == ("ER0000.sl2", "ER0000.co2", MODDED_SAVE_NAME)
    assert MODDED_SAVE_NAME == EXPECTED_MODDED_SAVE


def test_find_save_dir_finds_a_folder_holding_only_the_modded_save(tmp_path):
    # A prefix that only ever ran me3 without Seamless Co-op has no .sl2 or .co2.
    save_dir = _save_root(tmp_path) / "76561198000000000"
    save_dir.mkdir(parents=True)
    (save_dir / EXPECTED_MODDED_SAVE).write_bytes(b"save")
    assert find_save_dir(tmp_path).samefile(save_dir)


def test_find_save_dir_ignores_a_folder_with_no_save_in_it(tmp_path):
    # Steam drops steam_autocloud.vdf into the same folder; it is not a save.
    save_dir = _save_root(tmp_path) / "76561198000000000"
    save_dir.mkdir(parents=True)
    (save_dir / "steam_autocloud.vdf").write_text("x")
    with pytest.raises(PathError):
        find_save_dir(tmp_path)

from ermlib.me3pkg import find_package_root, list_option_dirs


def _tree(base, *rels):
    for r in rels:
        p = base / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_bare_tree_at_root(tmp_path):
    _tree(tmp_path, "parts/wp_a.dcx", "menu/hud.dds")
    assert find_package_root(tmp_path) == tmp_path


def test_single_wrapper_folder_is_descended(tmp_path):
    _tree(tmp_path, "My Cool Mod/parts/wp_a.dcx")
    assert find_package_root(tmp_path) == tmp_path / "My Cool Mod"


def test_modengine_mod_folder_convention(tmp_path):
    _tree(tmp_path, "mod/chr/c0000.dcx")
    assert find_package_root(tmp_path) == tmp_path / "mod"


def test_tree_beside_a_readme(tmp_path):
    _tree(tmp_path, "parts/wp_a.dcx", "README.txt", "preview.png")
    assert find_package_root(tmp_path) == tmp_path


def test_modengine2_launcher_files_dont_block_descent(tmp_path):
    _tree(tmp_path, "modengine2_launcher.exe", "config_eldenring.toml", "mod/parts/wp.dcx")
    assert find_package_root(tmp_path) == tmp_path / "mod"


def test_no_asset_dirs_returns_none(tmp_path):
    _tree(tmp_path, "random/thing.bin", "notes.docx")
    assert find_package_root(tmp_path) is None


def test_regulation_only_is_a_valid_root(tmp_path):
    (tmp_path / "regulation.bin").write_bytes(b"x")
    assert find_package_root(tmp_path) == tmp_path


def test_regulation_bin_is_matched_case_insensitively(tmp_path):
    (tmp_path / "Regulation.BIN").write_bytes(b"x")
    assert find_package_root(tmp_path) == tmp_path


# Some Nexus mods (e.g. Minimal HUD #148) ship multiple complete variant
# folders at the archive root — find_package_root correctly refuses to guess
# between them, so a profile entry can name the one to use via `subdir`.
def test_list_option_dirs_finds_each_self_contained_option(tmp_path):
    _tree(tmp_path, "OPTION 1/menu/x.gfx", "OPTION 2/menu/y.gfx")
    assert list_option_dirs(tmp_path) == ["OPTION 1", "OPTION 2"]


def test_list_option_dirs_empty_for_a_normal_single_root_tree(tmp_path):
    # A bare asset dir at the root is a single clear package root, not a set
    # of options — list_option_dirs must not misreport it as one.
    _tree(tmp_path, "parts/x.dcx")
    assert list_option_dirs(tmp_path) == []

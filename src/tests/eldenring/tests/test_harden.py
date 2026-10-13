import pytest

from ermlib import harden
from ermlib.errors import ErmError, PathError

EAC_BYTES = b"EAC-LAUNCHER"
GAME_BYTES = b"GAME"


@pytest.fixture
def game(tmp_game):
    # tmp_game seeds both files with placeholder bytes; give them distinct
    # content so a test can tell "real EAC launcher" and "eldenring copy"
    # apart just by reading bytes back.
    (tmp_game / "start_protected_game.exe").write_bytes(EAC_BYTES)
    (tmp_game / "eldenring.exe").write_bytes(GAME_BYTES)
    return tmp_game


def test_harden_swap_replaces_spg_with_eldenring_copy_and_backs_up_original(game):
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)

    assert spg.read_bytes() == GAME_BYTES
    assert backup.read_bytes() == EAC_BYTES


def test_harden_swap_twice_does_not_clobber_the_real_backup(game):
    # THE critical invariant: on the second call, start_protected_game.exe is
    # already the eldenring copy. If harden_swap backed it up again, the
    # backup would end up holding GAME_BYTES instead of the real EAC launcher
    # -- destroying the only copy of the real launcher. This is the exact bug
    # that made community exe-swap scripts unsafe.
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)
    harden.harden_swap(game)

    assert backup.read_bytes() == EAC_BYTES


def test_harden_swap_raises_patherror_when_eldenring_missing(game):
    (game / "eldenring.exe").unlink()

    with pytest.raises(PathError):
        harden.harden_swap(game)


def test_unharden_restore_puts_back_original_bytes_and_removes_backup(game):
    spg = game / "start_protected_game.exe"
    backup = game / "start_protected_game.exe.erm-backup"

    harden.harden_swap(game)
    harden.unharden_restore(game)

    assert spg.read_bytes() == EAC_BYTES
    assert not backup.exists()


def test_unharden_restore_raises_patherror_when_not_hardened(game):
    with pytest.raises(PathError):
        harden.unharden_restore(game)


def test_is_hardened_true_after_swap_false_after_restore(game):
    assert harden.is_hardened(game) is False
    harden.harden_swap(game)
    assert harden.is_hardened(game) is True
    harden.unharden_restore(game)
    assert harden.is_hardened(game) is False


def test_set_immutable_raises_ermerror_on_nonzero_rc(monkeypatch, tmp_path):
    import subprocess

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ErmError):
        harden.set_immutable(tmp_path / "x", True)


def test_harden_swap_wraps_oserror_as_patherror(game, monkeypatch):
    import shutil

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", _boom)
    with pytest.raises(PathError):
        harden.harden_swap(game)


def test_unharden_restore_wraps_oserror_as_patherror(game, monkeypatch):
    import shutil

    harden.harden_swap(game)

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(shutil, "move", _boom)
    with pytest.raises(PathError):
        harden.unharden_restore(game)


def test_set_immutable_runs_chattr_through_pkexec(monkeypatch, tmp_path):
    import subprocess

    calls = []

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(subprocess, "run",
                        lambda argv, *a, **k: calls.append(argv) or FakeResult())
    target = tmp_path / "spg.exe"
    harden.set_immutable(target, True)
    harden.set_immutable(target, False)
    assert calls == [["pkexec", "chattr", "+i", str(target)],
                     ["pkexec", "chattr", "-i", str(target)]]


def test_set_immutable_raises_ermerror_on_missing_pkexec(monkeypatch, tmp_path):
    import subprocess

    def _raise(*a, **k):
        raise FileNotFoundError("pkexec not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(ErmError, match="pkexec/chattr not available"):
        harden.set_immutable(tmp_path / "x", True)


@pytest.mark.parametrize("code", [126, 127])
def test_set_immutable_names_pkexecs_exit_codes_in_its_error(monkeypatch, tmp_path, code):
    import subprocess

    class FakeResult:
        returncode = code

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ErmError) as caught:
        harden.set_immutable(tmp_path / "x", True)
    message = str(caught.value)
    assert f"rc {code}" in message
    assert f"{code} when" in message

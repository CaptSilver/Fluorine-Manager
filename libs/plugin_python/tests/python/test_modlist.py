import pytest
from PyQt6.QtWidgets import QWidget

import mobase

m = pytest.importorskip("mobase_tests.modlist")


@pytest.fixture(autouse=True)
def release_registrations():
    # The C++ side keeps the Python callables alive between tests; drop them while
    # the interpreter is certainly running.
    yield
    m.reset()


def test_method_is_bound():
    assert hasattr(mobase.IModList, "addContextMenuAction")


def test_registration_without_visible_is_always_shown():
    modlist: mobase.IModList = m.modlist()
    mod = m.mod()

    modlist.addContextMenuAction("Plain", lambda mod, parent: None)

    assert m.labels() == ["Plain"]
    assert not m.has_visible(0)
    assert m.visible(0, mod)


def test_visible_and_triggered_get_mod_and_widget():
    modlist: mobase.IModList = m.modlist()
    mod = m.mod()
    widget = QWidget()
    seen = {}

    def visible(arg: mobase.IModInterface) -> bool:
        seen["visible"] = arg
        return False

    def triggered(arg: mobase.IModInterface, parent: QWidget) -> None:
        seen["triggered"] = arg
        seen["parent"] = parent

    modlist.addContextMenuAction("Filtered", triggered, visible)

    assert m.has_visible(0)
    assert m.visible(0, mod) is False
    assert seen["visible"] is mod

    m.trigger(0, mod, widget)
    assert seen["triggered"] is mod
    assert seen["parent"] is widget


def test_visible_keyword_argument():
    modlist: mobase.IModList = m.modlist()
    mod = m.mod()

    modlist.addContextMenuAction("Kw", lambda mod, parent: None, visible=lambda mod: True)

    assert m.has_visible(0)
    assert m.visible(0, mod)


def test_none_visible_means_always_shown():
    modlist: mobase.IModList = m.modlist()

    modlist.addContextMenuAction("None", lambda mod, parent: None, None)

    assert not m.has_visible(0)


def test_visible_that_forgets_to_return_hides_the_entry():
    modlist: mobase.IModList = m.modlist()
    mod = m.mod()

    modlist.addContextMenuAction("Forgot", lambda mod, parent: None, lambda mod: None)

    assert m.visible(0, mod) is False


def test_python_errors_reach_cpp_as_exceptions():
    modlist: mobase.IModList = m.modlist()
    mod = m.mod()
    widget = QWidget()

    def visible(mod: mobase.IModInterface) -> bool:
        raise ValueError("visible failed")

    def triggered(mod: mobase.IModInterface, parent: QWidget) -> None:
        raise ValueError("triggered failed")

    modlist.addContextMenuAction("Raises", triggered, visible)

    assert "visible failed" in m.visible_error(0, mod)
    assert "triggered failed" in m.trigger_error(0, mod, widget)


def test_wrong_arity_fails_at_registration():
    modlist: mobase.IModList = m.modlist()

    with pytest.raises(TypeError):
        modlist.addContextMenuAction("Bad", lambda: None)

    with pytest.raises(TypeError):
        modlist.addContextMenuAction("Bad", lambda mod, parent: None, lambda: True)

    assert m.labels() == []


def test_missing_triggered_fails_at_registration():
    modlist: mobase.IModList = m.modlist()

    with pytest.raises(TypeError):
        modlist.addContextMenuAction("Bad", None)

    with pytest.raises(TypeError):
        modlist.addContextMenuAction("Bad", None, lambda mod: True)

    assert m.labels() == []


def test_reset_drops_registrations():
    modlist: mobase.IModList = m.modlist()
    modlist.addContextMenuAction("Gone", lambda mod, parent: None)

    m.reset()

    assert m.labels() == []

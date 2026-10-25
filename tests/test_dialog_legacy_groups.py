"""Existing case-sensitive group names remain editable after migration."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from pint_terminal.dialogs import GroupDialog
from pint_terminal.models import Group


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("name", ["Lab", "lab"])
def test_legacy_case_only_group_names_can_edit_credentials(qt_app, monkeypatch, name):
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: notices.append(text))
    groups = [Group(name="Lab", password="first"), Group(name="lab", password="second")]
    original = next(group for group in groups if group.name == name)
    dialog = GroupDialog(group=original, groups=groups)
    dialog.password.setText("updated")
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert dialog.result_group.name == name
    assert dialog.result_group.password == "updated"
    assert [group.password for group in groups] == ["first", "second"]
    assert not notices
    dialog.deleteLater()


def test_new_or_renamed_groups_still_reject_case_only_collisions(qt_app, monkeypatch):
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: notices.append(text))
    groups = [Group(name="Lab"), Group(name="Office")]
    for original in (None, groups[1]):
        dialog = GroupDialog(group=original, groups=groups)
        dialog.name.setText("lab")
        dialog.save()
        assert dialog.result() != QDialog.Accepted
        assert dialog.result_group is None
        assert "already exists" in notices[-1]
        dialog.deleteLater()

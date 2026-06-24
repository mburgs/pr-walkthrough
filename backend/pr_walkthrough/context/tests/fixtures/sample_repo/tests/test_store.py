"""
Tests for ItemStore and the add_item / get_item / remove_item methods.
"""
import pytest
from sample_repo.store import Item, ItemStore


@pytest.fixture()
def store() -> ItemStore:
    return ItemStore()


class TestAddItem:
    def test_add_item_persists(self, store: ItemStore) -> None:
        item = Item(item_id="x1", name="Widget", value=9.99)
        store.add_item(item)
        assert store.get_item("x1") is item

    def test_add_item_replaces_existing(self, store: ItemStore) -> None:
        old = Item(item_id="x1", name="Old")
        new = Item(item_id="x1", name="New")
        store.add_item(old)
        store.add_item(new)
        assert store.get_item("x1").name == "New"


class TestGetItem:
    def test_get_item_missing_returns_none(self, store: ItemStore) -> None:
        assert store.get_item("missing") is None

    def test_get_item_returns_correct_item(self, store: ItemStore) -> None:
        item = Item(item_id="a", name="Alpha")
        store.add_item(item)
        assert store.get_item("a").name == "Alpha"


class TestRemoveItem:
    def test_remove_item_returns_true_when_present(self, store: ItemStore) -> None:
        store.add_item(Item(item_id="r1", name="Removable"))
        assert store.remove_item("r1") is True
        assert store.get_item("r1") is None

    def test_remove_item_returns_false_when_absent(self, store: ItemStore) -> None:
        assert store.remove_item("ghost") is False

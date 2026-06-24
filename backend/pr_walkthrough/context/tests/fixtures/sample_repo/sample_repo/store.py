"""
Sample in-memory store module for testing context retrieval.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Item:
    """A simple item stored in the repository."""

    item_id: str
    name: str
    value: float = 0.0
    tags: list[str] = field(default_factory=list)


class ItemStore:
    """Manages a collection of Items."""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add_item(self, item: Item) -> None:
        """Add or replace an item in the store."""
        self._items[item.item_id] = item

    def get_item(self, item_id: str) -> Optional[Item]:
        """Retrieve an item by ID, or None if not found."""
        return self._items.get(item_id)

    def remove_item(self, item_id: str) -> bool:
        """Remove an item by ID. Returns True if it existed."""
        if item_id in self._items:
            del self._items[item_id]
            return True
        return False

    def list_items(self) -> list[Item]:
        """Return all items, sorted by item_id."""
        return sorted(self._items.values(), key=lambda i: i.item_id)

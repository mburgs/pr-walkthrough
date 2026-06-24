"""
Service layer that uses ItemStore. Acts as a callsite for store symbols.
"""
from sample_repo.store import Item, ItemStore


_global_store = ItemStore()


def create_item(item_id: str, name: str, value: float = 0.0) -> Item:
    """Create an Item and persist it in the global store."""
    item = Item(item_id=item_id, name=name, value=value)
    _global_store.add_item(item)
    return item


def fetch_item(item_id: str) -> Item:
    """Fetch an item from the global store; raise if missing."""
    result = _global_store.get_item(item_id)
    if result is None:
        raise KeyError(f"item not found: {item_id!r}")
    return result


def delete_item(item_id: str) -> bool:
    """Remove an item from the global store."""
    return _global_store.remove_item(item_id)

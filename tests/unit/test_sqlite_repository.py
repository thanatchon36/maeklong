import sqlite3
import threading

from riverbank.storage.sqlite import SqliteRepository


def test_unopenable_database_raises_instead_of_hanging(tmp_path):
    # Seen in the Docker smoke test: a corrupted DB (zeroed header) made the writer thread die on open,
    # and the constructor waited forever for it, so the bot hung silently at startup.
    path = tmp_path / "riverbank.sqlite"
    path.write_bytes(b"\0" * 4096)
    result: list[BaseException | None] = []

    def construct() -> None:
        try:
            SqliteRepository(path)
            result.append(None)
        except BaseException as e:
            result.append(e)

    t = threading.Thread(target=construct, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "SqliteRepository() hung on an unopenable database"
    assert isinstance(result[0], sqlite3.DatabaseError)
    assert "not a database" in str(result[0])


def test_healthy_database_still_opens(tmp_path):
    repo = SqliteRepository(tmp_path / "riverbank.sqlite")
    repo.kv_set_sync("k", "v")
    assert repo.kv_get("k") == "v"
    repo.close()

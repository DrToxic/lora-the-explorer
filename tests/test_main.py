import pytest

from lora_explorer.main import browser_host


def test_browser_host_maps_wildcard_to_loopback():
    # 0.0.0.0 / :: are bind wildcards a browser can't connect to (Windows
    # especially) — the advertised/opened URL must use loopback instead.
    assert browser_host("0.0.0.0") == "127.0.0.1"
    assert browser_host("::") == "127.0.0.1"
    assert browser_host("") == "127.0.0.1"


def test_browser_host_preserves_explicit_host():
    # An operator who bound a specific interface meant that address; keep it.
    assert browser_host("127.0.0.1") == "127.0.0.1"
    assert browser_host("192.168.1.50") == "192.168.1.50"
    assert browser_host("lora.local") == "lora.local"


# --- Companion config loading ---

async def _load(env: dict) -> dict:
    """_load_companion_config against a throwaway in-memory DB with no saved
    config, so only the env fallback is under test."""
    from lora_explorer.game.database import Database
    from lora_explorer.main import _load_companion_config, get_env_config

    db = Database(db_path=":memory:")
    await db.connect()
    try:
        import os
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return await _load_companion_config(db, get_env_config())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_usb_env_config_is_honored():
    """CONNECTION_TYPE=usb has neither a host nor a BLE address, so it used to
    be dropped and the app started up with no companion configured at all."""
    cfg = await _load({"CONNECTION_TYPE": "usb", "SERIAL_PORT": "COM11"})
    assert cfg["connection_type"] == "usb"
    assert cfg["serial_port"] == "COM11"


@pytest.mark.asyncio
async def test_wifi_env_config_still_honored():
    cfg = await _load({"CONNECTION_TYPE": "wifi", "COMPANION_HOST": "192.168.1.50"})
    assert cfg["connection_type"] == "wifi"
    assert cfg["companion_host"] == "192.168.1.50"


@pytest.mark.asyncio
async def test_empty_env_stays_unconfigured():
    cfg = await _load({"CONNECTION_TYPE": "wifi", "COMPANION_HOST": "", "SERIAL_PORT": ""})
    assert cfg["companion_host"] == ""
    assert cfg["ble_address"] == ""

"""Connection-resilience tests for the companion link.

The regression these exist for: a companion that has gone missing (unplugged,
renumbered COM6 -> COM11, node rebooting, WiFi node offline) used to raise out
of `engine.start()` *before* uvicorn bound its port, so the whole app died and
the player could never reach the Settings page — the only place the connection
setting can be changed.
"""
import asyncio
import socket

import pytest

from lora_explorer.game.database import Database
from lora_explorer.game.engine import GameEngine
from lora_explorer.radio import meshcore_adapter as mca
from lora_explorer.radio.meshcore_adapter import MeshCoreAdapter


def make_adapter(**kw) -> MeshCoreAdapter:
    opts = {"connection_type": "usb", "serial_port": "COM6"}
    opts.update(kw)
    return MeshCoreAdapter(**opts)


async def drain(adapter: MeshCoreAdapter) -> None:
    """Cancel any background retry so a test can't leak a task."""
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_failed_connect_with_retry_does_not_raise():
    adapter = make_adapter()

    async def boom():
        raise OSError("could not open port 'COM6'")

    adapter._create_connection = boom

    await adapter.connect(retry_on_failure=True)  # must not raise

    assert adapter._mc is None
    status = await adapter.get_companion_status()
    assert status["connected"] is False
    assert status["configured"] is True
    assert "COM6" in status["last_error"]
    assert status["retrying"] is True
    await drain(adapter)


@pytest.mark.asyncio
async def test_failed_connect_without_retry_raises_and_starts_nothing():
    """The Settings save/test routes need the exception to report it."""
    adapter = make_adapter()

    async def boom():
        raise OSError("nope")

    adapter._create_connection = boom

    with pytest.raises(OSError):
        await adapter.connect()

    assert adapter._reconnect_task is None
    status = await adapter.get_companion_status()
    assert status["last_error"] == "nope"
    assert status["retrying"] is False
    await drain(adapter)


@pytest.mark.asyncio
async def test_unconfigured_connect_is_a_noop():
    adapter = make_adapter(connection_type="wifi", host="")
    await adapter.connect(retry_on_failure=True)
    assert adapter._reconnect_task is None
    assert (await adapter.get_companion_status())["configured"] is False


@pytest.mark.asyncio
async def test_half_open_session_is_discarded():
    """A link that opens but fails to initialize must not read as connected."""
    adapter = make_adapter()
    stopped = []

    class FakeMC:
        async def stop_auto_message_fetching(self):
            stopped.append("fetch")

        async def disconnect(self):
            stopped.append("disconnect")

    async def create():
        return FakeMC()

    async def bad_init():
        raise ConnectionError("Failed to initialize device")

    adapter._create_connection = create
    adapter._initialize_session = bad_init

    await adapter.connect(retry_on_failure=True)

    assert adapter._mc is None
    assert stopped == ["fetch", "disconnect"]
    assert (await adapter.get_companion_status())["connected"] is False
    await drain(adapter)


@pytest.mark.asyncio
async def test_retry_loop_connects_later_and_fires_connect_handler(monkeypatch):
    """The first *successful* connect often happens inside the retry loop, so
    the engine's post-connect work has to be driven from there too."""
    monkeypatch.setattr(mca, "RECONNECT_BASE_DELAY", 0.01)
    adapter = make_adapter()
    attempts = []
    connected_calls = []

    class FakeMC:
        self_info = {"name": "Base Camp"}

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

    async def create():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("could not open port 'COM6'")
        return FakeMC()

    async def init_ok():
        pass

    async def on_connected():
        connected_calls.append(1)

    adapter._create_connection = create
    adapter._initialize_session = init_ok
    adapter.set_connect_handler(on_connected)

    await adapter.connect(retry_on_failure=True)
    assert connected_calls == []

    for _ in range(200):
        if connected_calls:
            break
        await asyncio.sleep(0.01)

    assert connected_calls == [1]
    assert adapter._mc is not None
    status = await adapter.get_companion_status(include_stats=False)
    assert status["connected"] is True
    assert adapter._last_error == ""
    await drain(adapter)


@pytest.mark.asyncio
async def test_connect_handler_fires_on_first_successful_connect():
    adapter = make_adapter()
    calls = []

    class FakeMC:
        self_info = {"name": "Base Camp"}

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

    async def create():
        return FakeMC()

    async def init_ok():
        pass

    async def on_connected():
        calls.append(1)

    adapter._create_connection = create
    adapter._initialize_session = init_ok
    adapter.set_connect_handler(on_connected)

    await adapter.connect(retry_on_failure=True)

    assert calls == [1]
    await drain(adapter)


@pytest.mark.asyncio
async def test_connect_handler_failure_does_not_break_the_connection():
    adapter = make_adapter()

    class FakeMC:
        self_info = {"name": "Base Camp"}

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            pass

    async def create():
        return FakeMC()

    async def init_ok():
        pass

    async def on_connected():
        raise RuntimeError("post-connect work blew up")

    adapter._create_connection = create
    adapter._initialize_session = init_ok
    adapter.set_connect_handler(on_connected)

    await adapter.connect(retry_on_failure=True)

    assert adapter._mc is not None
    assert adapter._last_error == ""
    await drain(adapter)


@pytest.mark.asyncio
async def test_begin_retry_is_idempotent_and_respects_shutdown():
    adapter = make_adapter()

    async def boom():
        raise OSError("nope")

    adapter._create_connection = boom

    adapter.begin_retry()
    first = adapter._reconnect_task
    adapter.begin_retry()
    assert adapter._reconnect_task is first

    await drain(adapter)
    adapter.begin_retry()  # shutting down — must not start a new loop
    assert adapter._reconnect_task is first
    assert first.done()


@pytest.mark.asyncio
async def test_engine_start_survives_a_dead_companion():
    """engine.start() must complete even when the companion can't be reached."""
    adapter = make_adapter()

    async def boom():
        raise OSError("could not open port 'COM6'")

    adapter._create_connection = boom

    db = Database(db_path=":memory:")
    await db.connect()
    engine = GameEngine(adapter=adapter, home_lat=40.0, home_lon=-105.0, db=db)

    await engine.start()  # must not raise

    assert adapter._mc is None
    assert "COM6" in adapter._last_error
    await engine.stop()
    await db.close()


@pytest.mark.asyncio
async def test_web_server_binds_even_when_the_companion_is_missing(tmp_path, monkeypatch):
    """End-to-end guard for the original bug report: with a saved-but-broken
    companion config, the dashboard must still come up."""
    from lora_explorer import main as lora_main

    db_path = str(tmp_path / "explorer.db")
    seed = Database(db_path=db_path)
    await seed.connect()
    await seed.save_companion_config({
        "connection_type": "usb",
        "companion_host": "",
        "companion_port": 4000,
        "serial_port": str(tmp_path / "definitely-not-a-serial-port"),
        "ble_address": "",
        "ble_pin": "",
    })
    await seed.close()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(lora_main, "WEB_PORT", port)
    monkeypatch.setattr(lora_main, "WEB_HOST", "127.0.0.1")

    captured: dict = {}

    def on_ready(loop, stop_event):
        captured["stop"] = stop_event

    run_task = asyncio.create_task(lora_main.run(on_ready=on_ready))
    try:
        listening = False
        for _ in range(200):
            if run_task.done():
                run_task.result()  # re-raise whatever killed startup
                break
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                listening = True
                break
            except OSError:
                await asyncio.sleep(0.05)
        assert listening, "web server never bound — startup died on the companion"
    finally:
        if "stop" in captured:
            captured["stop"].set()
        try:
            await asyncio.wait_for(run_task, timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            run_task.cancel()


@pytest.mark.asyncio
async def test_reconfigure_replaces_a_running_retry_loop():
    """A failed connect leaves a retry loop running that reads the connection
    fields — it must be cancelled before reconfigure rewrites them, or the two
    race to open a session and one of them ends up untracked."""
    adapter = make_adapter()

    async def boom():
        raise OSError("could not open port 'COM6'")

    adapter._create_connection = boom

    await adapter.connect(retry_on_failure=True)
    first = adapter._reconnect_task
    assert first is not None and not first.done()

    await adapter.reconfigure(connection_type="usb", serial_port="COM11",
                              retry_on_failure=True)

    assert first.done()
    assert adapter._reconnect_task is not first
    assert adapter._serial_port == "COM11"
    await drain(adapter)

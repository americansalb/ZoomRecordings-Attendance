from pathlib import Path


def test_uvicorn_is_the_first_process():
    """A deploy must reach uvicorn's shutdown, which walks each bot out of
    its meeting. Under a plain `sh -c` the shell stays first and the stop
    signal never arrives (2026-11-21, Session 144)."""
    cmd = next(l for l in (Path(__file__).parent.parent / "Dockerfile").read_text().splitlines()
               if l.startswith("CMD"))
    assert '"exec uvicorn ' in cmd


def test_shutdown_leaves_every_meeting():
    src = (Path(__file__).parent.parent / "manager.py").read_text()
    body = src[src.index("async def shutdown"):]
    body = body[:body.index("\n    async def ", 10) if "\n    async def " in body[10:] else len(body)]
    assert "self.leave(" in body

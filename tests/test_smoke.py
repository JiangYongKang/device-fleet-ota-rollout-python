def test_skeleton_imports() -> None:
    from ota.api import create_app
    from ota.service import OtaService
    from ota.storage import InMemoryStore

    app = create_app()
    assert app.title.startswith("Device Fleet")
    assert isinstance(OtaService(InMemoryStore(), None), OtaService)

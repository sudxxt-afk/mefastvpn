from types import SimpleNamespace

from app.config import Settings
from app.services.catalog_importer import ExternalCatalogImporter


def _entry(host: str) -> SimpleNamespace:
    return SimpleNamespace(location=SimpleNamespace(host=host))


def _importer(allowed_hosts: str) -> ExternalCatalogImporter:
    settings = Settings(catalog_allowed_hosts=allowed_hosts)
    importer = ExternalCatalogImporter.__new__(ExternalCatalogImporter)
    importer._settings = settings
    return importer


def test_filter_drops_non_whitelisted_hosts():
    importer = _importer("88.218.122.40, 203.0.113.7")
    skipped: list[str] = []
    kept = importer._filter_allowed_hosts(
        [_entry("88.218.122.40"), _entry("bad.example.com")],
        skipped,
    )
    assert [entry.location.host for entry in kept] == ["88.218.122.40"]
    assert skipped and "bad.example.com" in skipped[0]


def test_filter_noop_when_unrestricted():
    importer = _importer("")
    entries = [_entry("any.host"), _entry("other.host")]
    kept = importer._filter_allowed_hosts(entries, [])
    assert len(kept) == 2

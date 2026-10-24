"""Ortak test düzeneği."""
import os
import tempfile

import pytest

# telefon paketi içe aktarılmadan önce: testler gerçek takvim önbelleğine ya da depodaki veri/ klasörüne yazmasın.
os.environ.setdefault("TAKVIM_KLASORU", tempfile.mkdtemp(prefix="takvim-test-"))

from telefon.gozculer import sistem


@pytest.fixture
def tum_kontroller(monkeypatch):
    """Sistem gözcüsünün isteğe bağlı kontrollerinin hepsini açar (toplam 9 kontrol)."""
    monkeypatch.setattr(sistem, "PANEL_SERVISI", "ExamplePanel")
    monkeypatch.setattr(sistem, "PANEL_URL", "http://127.0.0.1:7331/")
    monkeypatch.setattr(sistem, "TAILSCALE_KONTROL", True)
    monkeypatch.setattr(sistem, "SYNCTHING_PORT", 8384)
    monkeypatch.setattr(sistem, "AG_CIHAZI_IP", "192.0.2.10")
    monkeypatch.setattr(sistem, "AG_CIHAZI_AD", "Ağ cihazı")
    monkeypatch.setattr(sistem, "DISKLER", ["C", "H"])

"""23 Ekim: turlar arası mail uyarısı tekrarı ve "yazıyor…" göstergesi."""
import datetime as dt
import json
import threading
import time

import pytest

from test_cevrimdisi import giden, sohbet_kur, yalitim  # noqa: F401  (ortak fikstürler: Telegram'a hiçbir şey gitmesin)
from telefon import telegram as tg
from telefon.gozculer import uyari as uy

TELAFI = "Duyuru: 1 Kasım Pazar Günü Telafi Dersi Var - 6 Kasım Ders Yok"
OZET = "BIL 281 6 Kas Cum dersi iptal; telafi 1 Kas Paz 09:00-13:00, MB 161 lab."


def _tur(monkeypatch, mid, ozet=OZET, onem="onemli", konu=TELAFI):
    from telefon.gozculer import degerlendir as dg
    monkeypatch.setattr(dg, "degerlendir", lambda m: {mid: {"onem": onem, "ozet": ozet, "teslimler": []}})
    return uy.Uyari(baslat=False).mailleri_isle([{"id": mid, "konu": konu, "kimden": "Ayşe", "metin": "x"}],
                                                "okul postası")


# ================================================================== turlar arası tekrar
def test_ayni_mail_iki_turda_tek_mesaj(giden, monkeypatch):
    """22 Ekim'deki #338/#339: aynı duyuru farklı kimlikle 16 dk arayla iki kez gelmişti."""
    _tur(monkeypatch, "okul-2098")
    _tur(monkeypatch, "okul-2099")
    assert [m for m, _ in giden] == [f"**Önemli:** {OZET}"]


def test_ozet_degisirse_guncelleme_olarak_gider(giden, monkeypatch):
    _tur(monkeypatch, "o-1")
    _tur(monkeypatch, "o-2", ozet="Telafi iptal edildi, 6 Kasım dersi normal saatinde yapılacak.")
    metinler = [m for m, _ in giden]
    assert len(metinler) == 2 and metinler[1].startswith("**Önemli:** Güncelleme: Telafi iptal")


def test_onemliden_acile_yukselirse_gider(giden, monkeypatch):
    _tur(monkeypatch, "o-1")
    _tur(monkeypatch, "o-2", onem="acil")
    assert len(giden) == 2 and "Güncelleme: " in giden[1][0]


def test_farkli_konu_ayrica_gider(giden, monkeypatch):
    _tur(monkeypatch, "o-1")
    _tur(monkeypatch, "o-2", konu="Erasmus+ başvuru", ozet="Erasmus+ son gün 2 Kas.")
    assert len(giden) == 2 and "Güncelleme" not in giden[1][0]


def test_24_saatten_eski_kayit_budanir(giden, monkeypatch):
    eski = (dt.datetime.now() - dt.timedelta(hours=25)).isoformat(timespec="seconds")
    with open(uy.GONDERILEN, "w", encoding="utf-8") as f:
        json.dump([{"zaman": eski, "onem": "onemli", "anahtar": uy._konu_anahtari(TELAFI), "ozet": OZET}], f)
    _tur(monkeypatch, "o-1")
    assert len(giden) == 1
    with open(uy.GONDERILEN, encoding="utf-8") as f:
        kayitlar = json.load(f)
    assert len(kayitlar) == 1 and kayitlar[0]["zaman"] > eski


def test_bozuk_kayit_dosyasi_uyariyi_durdurmaz(giden, monkeypatch):
    with open(uy.GONDERILEN, "w", encoding="utf-8") as f:
        f.write("{bozuk")
    _tur(monkeypatch, "o-1")
    assert len(giden) == 1


def test_onceki_uyari_dogrudan():
    simdi = dt.datetime(2026, 10, 23, 15, 0)
    assert uy._onceki_uyari("onemli", "a konu", "özet bir", simdi) is None
    assert uy._onceki_uyari("onemli", "a konu", "Özet  bir", simdi + dt.timedelta(minutes=16)) == "tekrar"
    assert uy._onceki_uyari("acil", "a konu", "özet bir", simdi + dt.timedelta(minutes=20)) == "guncelleme"
    assert uy._onceki_uyari("acil", "a konu", "özet bir", simdi + dt.timedelta(minutes=30)) == "tekrar"
    assert uy._onceki_uyari("onemli", "a konu", "özet bir", simdi + dt.timedelta(hours=25)) is None


# ================================================================== yazıyor göstergesi
@pytest.fixture
def cagrilar(monkeypatch):
    c = []
    monkeypatch.setattr(tg, "yaziyor", lambda: c.append(time.perf_counter()))
    monkeypatch.setattr(tg, "YAZIYOR_ARALIK", 0.05)
    yield c
    for _ in range(100):                      # iş parçacığı kapanmadan sonraki teste geçilmesin
        if not tg._yaziyor_calisiyor:
            break
        time.sleep(0.01)


def _bekle(kosul, sure=2.0):
    son = time.time() + sure
    while time.time() < son and not kosul():
        time.sleep(0.01)
    return kosul()


def test_yaziyor_blok_surdukce_yenilenir_sonra_durur(cagrilar):
    with tg.yaziyor_goster():
        assert _bekle(lambda: len(cagrilar) >= 3)
    assert _bekle(lambda: not tg._yaziyor_calisiyor)
    n = len(cagrilar)
    time.sleep(0.15)
    assert len(cagrilar) == n


def test_ic_ice_bloklar_tek_is_parcacigi(cagrilar):
    with tg.yaziyor_goster():
        with tg.yaziyor_goster():
            assert _bekle(lambda: len(cagrilar) >= 2)
            sayi = sum(t.name == "yaziyor" for t in threading.enumerate())
        assert tg._yaziyor_calisiyor          # dış blok sürüyor
    assert sayi == 1
    assert _bekle(lambda: not tg._yaziyor_calisiyor)


def test_yaziyor_hatasi_donguyu_dusurmez(monkeypatch):
    def patla():
        raise RuntimeError("ağ yok")
    monkeypatch.setattr(tg, "yaziyor", patla)
    monkeypatch.setattr(tg, "YAZIYOR_ARALIK", 0.05)
    with tg.yaziyor_goster():
        time.sleep(0.12)
        assert tg._yaziyor_calisiyor
    assert _bekle(lambda: not tg._yaziyor_calisiyor)


def test_kullanici_mesajinda_birlestirme_beklemesinde_de_yaziyor(giden, cagrilar, monkeypatch):
    """Gösterge beyin cevabından önce, birleştirme beklemesi başlarken açılır."""
    from telefon import sohbet as sh
    s = sohbet_kur("Merhaba.")
    ilk_cagri = []
    asil_uyku = time.sleep

    def uyku(sn):
        if sn == sh.BIRLESTIR_SN:
            assert _bekle(lambda: len(cagrilar) >= 1)
            ilk_cagri.append(True)
            return
        asil_uyku(sn)
    monkeypatch.setattr(sh.time, "sleep", uyku)
    s.kuyruk.put(("kullanici", "selam", ""))
    threading.Thread(target=s._dongu, daemon=True).start()
    assert _bekle(lambda: any("Merhaba" in m for m, _ in giden), 5)
    assert ilk_cagri == [True]
    assert _bekle(lambda: not tg._yaziyor_calisiyor)


def test_sistem_turunda_birlestirme_beklemesi_yok(giden, cagrilar, monkeypatch):
    from telefon import sohbet as sh
    s = sohbet_kur()
    bekleme = []
    monkeypatch.setattr(sh.time, "sleep", lambda sn: bekleme.append(sn))
    monkeypatch.setattr(s, "_sistem_turu", lambda *a, **k: None)
    s._tur_isle(("sistem", "brifing", None, False))
    assert sh.BIRLESTIR_SN not in bekleme

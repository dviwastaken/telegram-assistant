"""Dalga 5: buluta nabız (masaüstü sustu uyarısı) ve bulut mesajındaki hb: düğmeleri.

Telegram'a, Notion'a ya da Worker'a çıkmaz: bulut._iste, telegram.gonder ve telegram.dugme_cevapla sahte.
"""
import datetime as dt
import os
import sys
import tempfile
import time
import types

# test_cevrimdisi ile aynı yalıtım (bu dosya önce toplanırsa modüller bu değerlerle yüklenir)
os.environ["TAKVIM_KLASORU"] = tempfile.mkdtemp(prefix="takvim-test-")
os.environ["GUNLUK_DOSYASI"] = "test.log"
os.environ["HAVA_KONUM"] = ""
os.environ["PANOPTO_KLASORU"] = tempfile.mkdtemp(prefix="panopto-test-")
os.environ["ODEV_KLASORU"] = tempfile.mkdtemp(prefix="odev-test-")
os.environ["TELEGRAM_SOHBET_JETONU"] = ""
os.environ["TELEGRAM_SOHBET_ID"] = "4242"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from telefon import bulut, eylemler, servis, sohbet as sh, telegram as tg  # noqa: E402

PID = "0123456789abcdef" * 2


@pytest.fixture(autouse=True)
def yalitim(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "KUYRUK", str(tmp_path / "kuyruk.json"))
    monkeypatch.setattr(tg, "DUGMELER", str(tmp_path / "dugmeler.json"))
    monkeypatch.setattr(tg, "OFSET", str(tmp_path / "ofset.txt"))
    monkeypatch.setattr(sh, "GECMIS", str(tmp_path / "sohbet.jsonl"))
    monkeypatch.setattr(eylemler, "ARAMALAR", str(tmp_path / "aramalar.json"))
    monkeypatch.setattr(eylemler, "degisiklik_dinleyicisi", None)
    monkeypatch.setattr(tg, "gecmis_dinleyicisi", None)

    def api_yok(*a, **k):
        raise AssertionError(f"testte gerçek Telegram çağrısı: {a[:1]}")
    monkeypatch.setattr(tg, "api", api_yok)
    monkeypatch.setattr(bulut, "URL", "")
    monkeypatch.setattr(bulut, "JETON", "")

    def worker_yok(*a, **k):
        raise AssertionError(f"testte gerçek Worker çağrısı: {a[:1]}")
    monkeypatch.setattr(bulut, "_iste", worker_yok)
    monkeypatch.setattr(bulut, "_kapanis_gitti", [False])
    monkeypatch.setattr(bulut, "KAPANIS_ISARETI", str(tmp_path / "kapanis_bekliyor.json"))
    monkeypatch.setattr(bulut, "_son_nabiz_hatasi", [0.0])


@pytest.fixture
def giden(monkeypatch):
    g = types.SimpleNamespace(mesaj=[], cevap=[])
    monkeypatch.setattr(tg, "gonder", lambda metin, **k: g.mesaj.append((metin, k)) or len(g.mesaj))
    monkeypatch.setattr(tg, "dugme_cevapla", lambda sorgu, metin="", mid=None, **k: g.cevap.append((sorgu, metin, k)))
    return g


@pytest.fixture
def worker(monkeypatch):
    """Bulut açık, _iste çağrıları kaydedilir."""
    monkeypatch.setattr(bulut, "URL", "https://ornek.workers.dev")
    monkeypatch.setattr(bulut, "JETON", "j")
    gelen = []

    def iste(yol, govde=None, sure=8):
        gelen.append((yol, govde, sure))
        return {"ok": True}
    monkeypatch.setattr(bulut, "_iste", iste)
    return gelen


# ================================================================== US-001: nabız
def test_nabiz_baslat_dongu_ve_kancalar(worker, monkeypatch):
    baslayan, kayitli, sinyaller = [], [], []

    class SahteThread:
        def __init__(self, target, daemon, name):
            baslayan.append((target, daemon, name))

        def start(self):
            pass
    monkeypatch.setattr(bulut, "threading", types.SimpleNamespace(Thread=SahteThread))
    monkeypatch.setattr(bulut.atexit, "register", kayitli.append)
    monkeypatch.setattr(bulut.signal, "signal", lambda s, f: sinyaller.append((s, f)))
    monkeypatch.setattr(bulut, "os", types.SimpleNamespace(name="posix"))  # gerçek konsol kancası kurulmasın
    assert bulut.nabiz_baslat() is True
    assert baslayan == [(bulut._nabiz_dongusu, True, "bulut-nabiz")]
    assert kayitli == [bulut.kapaniyor]
    beklenen = [getattr(bulut.signal, a) for a in ("SIGTERM", "SIGBREAK") if hasattr(bulut.signal, a)]
    assert [s for s, _ in sinyaller] == beklenen and all(f is bulut._sinyal_geldi for _, f in sinyaller)

    # döngü: hemen bir nabız, sonra NABIZ_SN bekler (burada ilk beklemede durdurulur)
    dur = []
    monkeypatch.setattr(bulut, "_dur", types.SimpleNamespace(wait=lambda sn: dur.append(sn) or True))
    bulut._nabiz_dongusu()
    assert [y for y, _, _ in worker] == ["/nabiz"] and dur == [bulut.NABIZ_SN] and bulut.NABIZ_SN == 300


def test_bulut_kapaliyken_nabiz_yok(monkeypatch):
    monkeypatch.setattr(bulut, "threading", None)  # dokunulursa patlar
    assert bulut.nabiz_baslat() is False
    assert bulut.kapaniyor() is False             # _iste çağrılmadı (worker_yok patlardı)


def test_nabiz_hatasi_firlatmaz_ve_seyrek_yazilir(worker, monkeypatch):
    satirlar = []
    monkeypatch.setattr(bulut, "log", lambda *a: satirlar.append(" ".join(map(str, a))))

    def kopuk(*a, **k):
        raise OSError("ağ yok")
    monkeypatch.setattr(bulut, "_iste", kopuk)
    assert bulut.nabiz() is False and bulut.nabiz() is False
    assert len(satirlar) == 1 and "nabız gönderilemedi" in satirlar[0]

    def bozuk(*a, **k):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(bulut, "_iste", bozuk)
    assert bulut.nabiz() is False                 # hiçbir hata dışarı çıkmaz
    assert bulut.kapaniyor() is False             # kapanışta da


def test_kapaniyor_bir_kez_kisa_sureyle(worker):
    assert bulut.kapaniyor() is True
    assert bulut.kapaniyor() is False             # atexit + sinyal ikisi de çalışsa bir kez gider
    assert len(worker) == 1 and worker[0][:2] == ("/kapaniyor", {}) and 0 < worker[0][2] <= bulut.KAPANIS_SURE <= 5
    assert bulut.nabiz() is True                  # yeniden açıldı: sonraki kapanış yine gider
    assert bulut.kapaniyor() is True
    assert [y for y, _, _ in worker] == ["/kapaniyor", "/nabiz", "/kapaniyor"]


def test_sinyal_ve_konsol_olayi_kapanisi_bildirir(worker):
    with pytest.raises(SystemExit):
        bulut._sinyal_geldi(15, None)
    assert [y for y, _, _ in worker] == ["/kapaniyor"]
    bulut._kapanis_gitti[0] = False
    assert bulut._konsol_olayi(0) is False        # Ctrl+C: kapanış değil, Python'a bırakılır
    assert len(worker) == 1
    assert bulut._konsol_olayi(6) is False        # bilgisayar kapanıyor
    assert [y for y, _, _ in worker] == ["/kapaniyor", "/kapaniyor"]


def test_bulut_satiri_son_nabiz():
    simdi = dt.datetime(2026, 10, 16, 15, 0, tzinfo=dt.timezone.utc)
    t = {"zaman": "2026-10-16T14:59:30Z", "hatalar": []}
    ms = int(simdi.timestamp() * 1000)
    assert "Son nabız 3 dk önce." in servis.bulut_satiri({"son_tarama": t, "nabiz": {"ms": ms - 3 * 60000}}, simdi)
    assert "Son nabız 20 dk önce (5 dk'da bir gelmeli)." in servis.bulut_satiri({"son_tarama": t, "nabiz": {"ms": ms - 20 * 60000}},
                                                              simdi)
    assert "nabız" not in servis.bulut_satiri({"son_tarama": t, "nabiz": None}, simdi)


# ================================================================== US-002: bulut mesajındaki düğmeler
def _kur():
    eylemler.yaz(eylemler.ARAMALAR, [
        {"id": PID, "notion": f"{PID[:8]}-{PID[8:12]}-{PID[12:16]}-{PID[16:20]}-{PID[20:]}", "zaman": "2026-10-16T09:00",
         "konu": "ilaç iç", "durum": "arandi", "tur": "hatirlatma"}])
    return sh.Sohbet(baslat=False)


def _yeni():
    return [a for a in eylemler.oku(eylemler.ARAMALAR, []) if a["id"] != PID]


def test_hb_tamam(giden):
    s = _kur()
    s.dugme_geldi(f"hb:ok:{PID}", "q1", {"message_id": 5, "date": int(time.time())})
    assert giden.cevap == [("q1", "Tamam", {})] and _yeni() == []
    assert any("buluttan gelen hatırlatmayı gördü: ilaç iç" in m for _, m in s.bildirimler)


def test_hb_bir_saat_ertele(giden):
    s = _kur()
    once = dt.datetime.now()
    s.dugme_geldi(f"hb:60:{PID}", "q2", {"message_id": 6, "date": int(time.time()) - 60})
    yeni = _yeni()
    assert len(yeni) == 1 and yeni[0]["konu"] == "ilaç iç" and yeni[0]["durum"] == "bekliyor"
    fark = dt.datetime.fromisoformat(yeni[0]["zaman"]) - once
    assert dt.timedelta(minutes=58) < fark <= dt.timedelta(minutes=61)
    assert "yeniden hatırlatacağım" in giden.mesaj[-1][0] and "açılınca" not in giden.mesaj[-1][0]
    assert giden.mesaj[-1][1]["yanit"] == 6
    assert any("erteledi: ilaç iç" in m for _, m in s.bildirimler)


def test_hb_yarin_gec_islenirse_soyler(giden):
    s = _kur()
    once = dt.datetime.now()
    s.dugme_geldi(f"hb:y:{PID}", "q3", {"message_id": 7, "date": int(time.time()) - 2 * 3600})
    yeni = _yeni()
    assert len(yeni) == 1
    fark = dt.datetime.fromisoformat(yeni[0]["zaman"]) - once
    assert dt.timedelta(hours=23, minutes=58) < fark <= dt.timedelta(days=1, minutes=1)  # işlendiği andan sayılır
    assert "bilgisayar açılınca işlendi, süre şimdiden sayıldı" in giden.mesaj[-1][0]
    assert "bilgisayar açılınca" in giden.cevap[-1][1]


def test_hb_24_saatten_eski_reddedilir(giden):
    s = _kur()
    s.dugme_geldi(f"hb:60:{PID}", "q4", {"message_id": 8, "date": int(time.time()) - 25 * 3600})
    assert _yeni() == [] and "24 saatten eski" in giden.cevap[-1][1]


def test_hb_bilinmeyen_ve_bozuk_kimlik(giden):
    s = _kur()
    s.dugme_geldi("hb:60:" + "f" * 32, "q5", {"message_id": 9})
    assert _yeni() == [] and "bulamadım" in giden.cevap[-1][1] and "bulamadım" in giden.mesaj[-1][0]
    for bozuk in ("hb:60:xyz", "hb:60", "hb:sil:" + PID, "hb:60:" + PID.upper(), "hb:60:" + PID + ":x"):
        s.dugme_geldi(bozuk, "q6", {"message_id": 10})
        assert giden.cevap[-1][1] == "Bilinmeyen düğme", bozuk
    assert _yeni() == []
    s.dugme_geldi("hb:ok:" + "f" * 32, "q7", {"message_id": 11})  # Tamam: yerelde yoksa 👍 yanıltmasın
    assert giden.cevap[-1][1] == "Bu hatırlatmayı bulamadım"


def test_hb_simule_tiklama(giden):
    s = _kur()
    s.dugme_geldi(f"hb:60:{PID}", "simule", {"message_id": None})  # /simule kancası: tarih yok
    assert len(_yeni()) == 1 and "açılınca" not in giden.mesaj[-1][0]


def test_hb_islenirken_hata_cokmez(giden, monkeypatch):
    s = _kur()

    def bozuk(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(eylemler, "oku", bozuk)
    s.dugme_geldi(f"hb:60:{PID}", "q8", {"message_id": 12})
    assert giden.cevap[-1][1].startswith("Olmadı")

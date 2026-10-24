"""Ses tanıma ve PDF çıkarma alt süreçleri (US-003): düşen, takılan ve boşta kapanan çocuk süreçler. Gerçek Whisper
modeli yüklenmez; sahte işçiler aynı `alt_surec.komut` yoluyla (venv python, -I) açılır."""
import ctypes
import os
import time

import pytest

from test_cevrimdisi import yalitim  # noqa: F401  (ortak ortam ve fikstür: test klasörleri, Telegram'a çıkılmaz)

from pdf_yardimci import kucuk_pdf
from telefon import alt_surec, odevler, ses_tanima

# yol "cokert" içerirse süreç düşer, "asili" içerirse takılır, "bozuk" içerirse işleyici hata verir
SAHTE_ISCI = ("import os, time; from telefon import alt_surec; alt_surec.isci_dongusu(lambda i: "
              "(os._exit(3) if 'cokert' in i['yol'] else time.sleep(120) if 'asili' in i['yol'] else "
              "int('x') if 'bozuk' in i['yol'] else "
              "{'metin': 'tamam ' + str(os.getpid()), 'ses_sn': 1.0, 'sure': 0.0, 'cihaz': 'sahte'}), {bosta})")


ORIJINAL_PDF_IFADE = odevler._PDF_IFADE


def canli_mi(pid):
    """Windows'ta süreç hâlâ çalışıyor mu (os.kill(pid, 0) Windows'ta süreci öldürür, kullanılmaz)."""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    kod = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(kod))
    k.CloseHandle(h)
    return kod.value == 259  # STILL_ACTIVE


def olene_kadar(pid, sn=10):
    son = time.time() + sn
    while canli_mi(pid) and time.time() < son:
        time.sleep(0.1)
    return not canli_mi(pid)


@pytest.fixture
def sahte_ses(monkeypatch, tmp_path):
    isci = alt_surec.Isci("ses tanıma", alt_surec.komut(SAHTE_ISCI.replace("{bosta}", "30")))
    monkeypatch.setattr(ses_tanima, "_isci_nesne", isci)
    monkeypatch.setattr(ses_tanima, "ZAMAN_ASIMI_TABAN", 20)

    def dosya(ad):
        yol = tmp_path / ad
        yol.write_bytes(b"ogg")
        return str(yol)
    yield isci, dosya
    isci.kapat()


def _pid(metin):
    assert metin.startswith("tamam ")
    return int(metin.split()[1])


def test_ses_isci_ayni_surecle_calisir(sahte_ses):
    isci, dosya = sahte_ses
    p1 = _pid(ses_tanima.cevir(dosya("a.oga")))
    p2 = _pid(ses_tanima.cevir(dosya("b.oga")))
    assert p1 == p2 and isci.calisiyor                                   # uzun yaşayan tek süreç


def test_ses_isci_cokerse_hata_ve_sonraki_istek_calisir(sahte_ses):
    isci, dosya = sahte_ses
    p1 = _pid(ses_tanima.cevir(dosya("a.oga")))
    with pytest.raises(alt_surec.AltSurecHatasi, match="düştü"):
        ses_tanima.cevir(dosya("cokert.oga"))
    assert isci.surec is None and olene_kadar(p1)
    p2 = _pid(ses_tanima.cevir(dosya("b.oga")))                         # yeni süreç açıldı
    assert p2 != p1
    assert isinstance(alt_surec.AltSurecHatasi("x"), RuntimeError)        # medya döngüsü yakalar


def test_ses_isci_takilirsa_oldurulur(sahte_ses, monkeypatch):
    isci, dosya = sahte_ses
    p1 = _pid(ses_tanima.cevir(dosya("a.oga")))
    monkeypatch.setattr(ses_tanima, "ZAMAN_ASIMI_TABAN", 1.5)
    t0 = time.time()
    with pytest.raises(alt_surec.AltSurecHatasi, match="cevap gelmedi"):
        ses_tanima.cevir(dosya("asili.oga"))
    assert time.time() - t0 < 15
    assert isci.surec is None and olene_kadar(p1)                       # takılan süreç öldürüldü
    monkeypatch.setattr(ses_tanima, "ZAMAN_ASIMI_TABAN", 20)
    assert _pid(ses_tanima.cevir(dosya("b.oga"))) != p1


def test_ses_isleyici_hatasi_runtimeerror_surec_yasar(sahte_ses):
    isci, dosya = sahte_ses
    p1 = _pid(ses_tanima.cevir(dosya("a.oga")))
    with pytest.raises(RuntimeError, match="ses tanıma başarısız"):
        ses_tanima.cevir(dosya("bozuk.oga"))
    assert canli_mi(p1) and _pid(ses_tanima.cevir(dosya("b.oga"))) == p1


def test_isci_bosta_kendiliginden_kapanir_ve_yeniden_acilir(tmp_path):
    isci = alt_surec.Isci("deneme", alt_surec.komut(SAHTE_ISCI.replace("{bosta}", "1")))
    try:
        c1 = isci.iste({"yol": "a"}, 20)
        p1 = _pid(c1["metin"])
        assert olene_kadar(p1, 15)                                       # 1 sn boşta kalınca çıktı
        assert isci.surec.wait(5) == 0
        c2 = isci.iste({"yol": "b"}, 20)                                 # yeni süreçle sorunsuz
        assert _pid(c2["metin"]) != p1
    finally:
        isci.kapat()


def test_isci_kapanirken_gelen_istek_bir_kez_yeniden_denenir():
    """Çocuk istek okumadan 0 koduyla çıkarsa (boşta kapanma yarışı) istek yeni süreçte tekrarlanır."""
    isci = alt_surec.Isci("deneme", alt_surec.komut(SAHTE_ISCI.replace("{bosta}", "30")))
    try:
        p1 = _pid(isci.iste({"yol": "a"}, 20)["metin"])
        isci.surec.stdin.close()                                         # çocuk EOF görür, 0 ile çıkar
        assert olene_kadar(p1, 15)
        isci.surec.poll = lambda: None                                   # ana süreç henüz fark etmemiş gibi
        assert _pid(isci.iste({"yol": "b"}, 20)["metin"]) != p1
    finally:
        isci.kapat()


def test_pdf_alt_surecte_normal(tmp_path):
    yol = str(tmp_path / "yonerge.pdf")
    kucuk_pdf(yol, "Sunum 10 dakika olacak ve grup halinde yapilacak")
    metin = odevler.pdf_metni(yol)
    assert "Sunum 10 dakika" in metin and "sayfa 1" in metin


def test_pdf_zaman_asimi_none_ve_surec_oldurulur(tmp_path, monkeypatch):
    pid_dosya = tmp_path / "pid.txt"
    monkeypatch.setattr(odevler, "_PDF_IFADE", f"import os, time; open({str(pid_dosya)!r}, 'w').write("
                                               "str(os.getpid())); time.sleep(120)")
    monkeypatch.setattr(odevler, "PDF_ZAMAN_ASIMI", 2)
    yol = str(tmp_path / "agir.pdf")
    kucuk_pdf(yol, "Agir dosya: sunum 10 dakika olacak ve grup halinde yapilacak")
    t0 = time.time()
    assert odevler.pdf_metni(yol) is None
    assert time.time() - t0 < 15
    assert olene_kadar(int(pid_dosya.read_text()))
    assert odevler.ofis_metni(yol) is None and not os.path.exists(yol + ".metin.txt")
    monkeypatch.setattr(odevler, "_PDF_IFADE", ORIJINAL_PDF_IFADE)
    monkeypatch.setattr(odevler, "PDF_ZAMAN_ASIMI", 60)
    assert "Agir dosya" in odevler.pdf_metni(yol)                       # sonraki çağrı normal çalışır


def test_pdf_alt_surec_duserse_none(tmp_path, monkeypatch):
    monkeypatch.setattr(odevler, "_PDF_IFADE", "import os; os._exit(5)")
    yol = str(tmp_path / "x.pdf")
    kucuk_pdf(yol, "Bir metin")
    assert odevler.pdf_metni(yol) is None


def test_bozuk_pdf_none(tmp_path):
    yol = tmp_path / "bozuk.pdf"
    yol.write_bytes(b"%PDF-1.4 bozuk")
    assert odevler.pdf_metni(str(yol)) is None


def test_learn_takilan_pdf_digerlerini_durdurmaz(tmp_path, monkeypatch):
    monkeypatch.setattr(odevler, "LEARN_KOK", str(tmp_path))
    kucuk_pdf(str(tmp_path / "iyi.pdf"), "Saglam dosya: sunum 10 dakika olacak ve grup halinde yapilacak")
    (tmp_path / "ders.docx").write_bytes(b"PK\x03\x04bozuk")
    monkeypatch.setattr(odevler, "_PDF_IFADE", "import time; time.sleep(120)")
    monkeypatch.setattr(odevler, "PDF_ZAMAN_ASIMI", 1)
    assert odevler.learn_metinlerini_hazirla() == 0                      # zaman aşımı: yazılmadı, ama döndü
    monkeypatch.setattr(odevler, "_PDF_IFADE", ORIJINAL_PDF_IFADE)
    monkeypatch.setattr(odevler, "PDF_ZAMAN_ASIMI", 60)
    assert odevler.learn_metinlerini_hazirla() == 1

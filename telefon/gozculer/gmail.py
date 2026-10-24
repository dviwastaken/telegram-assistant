"""Gmail gözcüsü: claude.ai Gmail bağlantısıyla 10 dakikada bir gelen kutusunu arar (yalnızca okuma).
Araç sonucu stream-json akışından doğrudan ayrıştırılır; görülen mesaj kimlikleri saklanır."""
import json
import os
import threading
import time

from .. import limit
from .. import notion
from ..ayarlar import KAYITLAR, log

DURUM = os.path.join(KAYITLAR, "gmail.durum.json")
ARALIK = 600
SORGU = "in:inbox newer_than:2d -category:promotions -category:social -category:forums"


def _ara():
    istem = (f"search_threads aracını query={json.dumps(SORGU)} ve pageSize=25 ile bir kez çağır. "
             "Başka hiçbir araç kullanma, sonra sadece 'bitti' yaz.")
    sonuclar = notion._claude_araclar(istem, ["search_threads"], onek="mcp__claude_ai_Gmail__")
    mesajlar = []
    for ad, _, metin, hata in sonuclar:
        if ad != "search_threads" or hata:
            continue
        try:
            for th in json.loads(metin).get("threads", []):
                mesajlar += th.get("messages", [])
        except json.JSONDecodeError:
            log("gmail: beklenmeyen arama çıktısı", metin[:200])
    return mesajlar if sonuclar else None


class GmailGozcusu:
    def __init__(self, uyari):
        self.uyari = uyari
        try:
            with open(DURUM, encoding="utf-8") as f:
                self.gorulen = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.gorulen = None  # ilk çalıştırma
        threading.Thread(target=self._dongu, daemon=True, name="gmail").start()

    def _dongu(self):
        time.sleep(90)  # açılışta Notion ve beyin ısınsın
        while True:
            try:
                self._yokla()
            except Exception as e:
                log("gmail gözcüsü hatası:", repr(e))
            time.sleep(ARALIK)

    def _yokla(self):
        if limit.dolu_mu():  # bağlantı yolu da claude -p; limit dolunca boşuna çağırma
            return
        mesajlar = _ara()
        if mesajlar is None:
            log("gmail: arama yapılamadı")
            return
        ilk_tur = self.gorulen is None
        gorulen = set(self.gorulen or [])
        yeni = [m for m in mesajlar if m.get("id") and m["id"] not in gorulen
                and "SENT" not in m.get("labelIds", [])]
        mailler = [{"id": f"gmail-{m['id']}", "kaynak": "gmail", "kimden": m.get("sender", ""),
                    "konu": m.get("subject", ""), "tarih": m.get("date", ""), "metin": m.get("snippet", "")}
                   for m in yeni]
        if mailler:
            log(f"gmail: {len(mailler)} yeni mail{' (ilk tur, uyarısız)' if ilk_tur else ''}")
        if self.uyari.mailleri_isle(mailler, "gmail", ilk_tur):
            gorulen |= {m["id"] for m in yeni}
            self.gorulen = sorted(gorulen)[-2000:]
            with open(DURUM, "w", encoding="utf-8") as f:
                json.dump(self.gorulen, f)

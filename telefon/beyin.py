"""Beyin: kalıcı `claude -p` süreci (stream-json giriş/çıkış). Telegram sohbeti boyunca açık kalır, böylece
konuşma bağlamı mesajdan mesaja korunur."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading

from . import limit
from .ayarlar import ISTEMLER, KOK, ayar, log

MODEL = ayar("SOHBET_MODEL") or ayar("BEYIN_MODEL", "sonnet")
EFFORT = ayar("SOHBET_EFFORT") if ayar("SOHBET_EFFORT") is not None else ayar("BEYIN_EFFORT", "")
SESSIZLIK_ZAMAN_ASIMI = 180  # sn: bir turda bu kadar hiç olay gelmezse beyin takılmış sayılır (web araması uzun sürebilir)

CLAUDE = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
# Yalnızca WebSearch: beynin bağlamında takvim, notlar ve mail özetleri var. WebFetch olsaydı bir maildeki enjeksiyon
# bunları bir adrese ekletip dışarı gönderebilirdi (2026-10-16 incelemesi). Sayfa okumak bağlamsız web işinin (⟦İŞ⟧).
ARACLAR = ["WebSearch"]


def _saat_tr(s):
    """'12am' / '3:30pm' -> '00:00' / '15:30'"""
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", s.strip().lower())
    if not m:
        return s
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    return f"{h:02d}:{m.group(2) or '00'}"


class Beyin:
    def __init__(self, model=MODEL, istem="sohbet.md"):
        args = [CLAUDE, "-p", "--model", model,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--system-prompt-file", os.path.join(ISTEMLER, istem),
                "--tools", ",".join(ARACLAR), "--allowedTools", *ARACLAR,
                # claude.ai bağlantılarının ~100 araç tanımı her turda ~90 bin token ekliyordu
                "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--no-session-persistence"]
        if EFFORT:
            args[args.index("--model") + 2:args.index("--model") + 2] = ["--effort", EFFORT]
        self.p = subprocess.Popen(args, cwd=KOK, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.kilit = threading.Lock()
        self.olu = False
        self.tur = 0
        self.satirlar = queue.Queue()     # stdout ayrı iş parçacığında okunur: tur zaman aşımına uğrayabilsin
        threading.Thread(target=self._stderr, daemon=True, name="beyin-stderr").start()
        threading.Thread(target=self._stdout, daemon=True, name="beyin-stdout").start()
        log(f"beyin açıldı (model {model}{', effort ' + EFFORT if EFFORT else ''})")

    def _stdout(self):
        for satir in self.p.stdout:
            self.satirlar.put(satir)
        self.satirlar.put(None)  # süreç kapandı

    def _stderr(self):
        for satir in self.p.stderr:
            satir = satir.strip()
            if satir:
                log("beyin stderr:", satir[:300])

    @property
    def canli(self):
        return not self.olu and self.p.poll() is None

    def konus(self, metin, iptal=None):
        """Kullanıcı mesajı gönder, cevabın metin parçalarını akıt. iptal set edilirse kalanını yut."""
        with self.kilit:
            self.tur += 1
            if not self.canli:
                yield "⚠️ Bağlantımı kaybettim, tekrar yazar mısınız?"
                return
            try:
                self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": metin}},
                                              ensure_ascii=False) + "\n")
                self.p.stdin.flush()
            except OSError:
                self.olu = True
                yield "⚠️ Bağlantımı kaybettim, tekrar yazar mısınız?"
                return
            yazildi = False
            while True:
                try:
                    satir = self.satirlar.get(timeout=SESSIZLIK_ZAMAN_ASIMI)
                except queue.Empty:
                    log(f"beyin: {SESSIZLIK_ZAMAN_ASIMI} sn'dir cevap yok, süreç takılmış sayılıyor")
                    self.olu = True
                    try:
                        self.p.kill()
                    except OSError:
                        pass
                    yield "\n⚠️ Cevabım yarıda kaldı (bağlantı sorunu). Tekrar sorar mısınız?"
                    return
                if satir is None:
                    break
                try:
                    ev = json.loads(satir)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "stream_event":
                    d = ev["event"].get("delta", {})
                    if d.get("type") == "text_delta" and not (iptal and iptal.is_set()):
                        yazildi = True
                        yield d["text"]
                elif t == "assistant":
                    araclar = [b for b in (ev.get("message") or {}).get("content", [])
                               if isinstance(b, dict) and b.get("type") == "tool_use"]
                    for b in araclar:  # denetlenebilsin: beyin hangi aracı neyle çağırdı
                        log(f"beyin aracı: {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:200]}")
                    if araclar and yazildi:  # araçtan önceki ve sonraki metin blokları birleşmesin
                        yield "\n\n"
                elif t == "result":
                    sonuc = str(ev.get("result") or "")
                    limit.kontrol(sonuc, "sohbet")
                    if ev.get("is_error") or "session limit" in sonuc:
                        log("beyin hata sonucu:", sonuc[:300])
                        # hatalı mesaj (ör. API'nin reddettiği bir görüntü) oturum geçmişinde kalıp sonraki her turu
                        # da bozmasın: bir sonraki turda yeni beyin açılır, son mesajlar geçmişten taşınır
                        self.olu = True
                    if "Failed to authenticate" in sonuc or "OAuth" in sonuc:
                        log("beyin: claude oturumu düşmüş, `claude` ile yeniden giriş gerekiyor")
                        self.olu = True
                        if not yazildi:
                            yield ("⚠️ Claude oturumum düşmüş. Bilgisayarda bir terminalde `claude` açıp /login "
                                   "yapılması gerekiyor. Hatırlatmalar ve takvim çalışmaya devam ediyor.")
                    elif "limit" in sonuc.lower() and not yazildi:  # ör. "You've hit your session limit · resets 12am"
                        saat = re.search(r"resets\s+([0-9:]+\s*[ap]m)", sonuc)
                        yield ("Şu an Claude kullanım limitim dolu"
                               + (f", saat {_saat_tr(saat.group(1))} civarı sıfırlanacak." if saat else ".")
                               + " Hatırlatmalarınız ve takviminiz çalışmaya devam ediyor.")
                    elif ev.get("is_error") and not yazildi:
                        yield f"⚠️ Bir hata oldu: {sonuc[:200]}"
                    return
            self.olu = True  # stdout kapandı

    def kapat(self):
        self.olu = True
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()

// Saf yardımcılar (Workers çalışma ortamı gerektirmez, node --test ile sınanır).
import { ADLAR } from "./tekrar.js";

// ---------------------------------------------------------------- zaman

// Notion tarihini yerel duvar saatine çevir: "YYYY-MM-DDTHH:MM". Ofsetsiz zaman zaten yereldir; yalnızca tarih
// varsa gece yarısı. (Servis aynı kuralla çevirdiği için sahiplenme anahtarları iki tarafta aynı çıkar.)
export function yerelDakika(metin, tzDakika) {
  const m = /^(\d{4}-\d\d-\d\d)(?:T(\d\d:\d\d)(?::\d\d(?:\.\d+)?)?(Z|[+-]\d\d:\d\d)?)?$/.exec(metin || "");
  if (!m) return null;
  if (!m[2]) return `${m[1]}T00:00`;
  if (!m[3]) return `${m[1]}T${m[2]}`;
  return new Date(Date.parse(metin) + tzDakika * 60000).toISOString().slice(0, 16);
}

export function yereldenOfsetli(yerel, tzDakika) {
  const isaret = tzDakika >= 0 ? "+" : "-";
  const a = Math.abs(tzDakika);
  return `${yerel}:00${isaret}${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}`;
}

export function simdiYerel(tzDakika, ms = Date.now()) {
  return new Date(ms + tzDakika * 60000).toISOString().slice(0, 19);
}

// Gece sessiz saatleri ("SS:DD-SS:DD", yerel duvar saati; gece yarısını aşabilir). Bozuk değer: varsayılan.
export const SESSIZ_SAAT = "23:30-08:00";
const ARALIK = /^(\d\d):(\d\d)-(\d\d):(\d\d)$/;
export function sessizMi(ms, tzDakika, aralik = SESSIZ_SAAT) {
  const m = ARALIK.exec(String(aralik || "").trim()) || ARALIK.exec(SESSIZ_SAAT);
  const bas = +m[1] * 60 + +m[2];
  const bit = +m[3] * 60 + +m[4];
  const y = simdiYerel(tzDakika, ms);
  const dk = +y.slice(11, 13) * 60 + +y.slice(14, 16);
  return bas <= bit ? dk >= bas && dk < bit : dk >= bas || dk < bit;
}

// ---------------------------------------------------------------- metin

export function kacis(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Notion'dan gelen (güvenilmeyen) konu ve not: servisteki telegram.duz_metin ile aynı kural. '[yazı](adres)' ->
// 'yazı (adres)'; '*', '`', '[', ']' ve satır başı '#' atılır. Adres görünür kalır, gizli bağlantı olmaz.
export function duzMetin(s) {
  return String(s ?? "").replace(/\[([^\]\n]*)\]\(([^)\s]+)\)/g, "$1 ($2)").replace(/[*`\[\]]/g, "")
    .replace(/^(\s*)#+\s*/gm, "$1");
}

// Kullanıcıya giden tarih: servisteki eylemler.tarih_metni ile aynı kurallar. 'bugün 20:00', 'yarın 23:59',
// 'dün 14:00', '21 Eki Çar 23:59', başka yılsa '4 Oca 2027 Pzt 09:00'. z ve simdi yerel duvar saati metni
// ("YYYY-MM-DDTHH:MM[:SS]"); gün farkı UTC alanlarıyla hesaplanır ki çalışma ortamının saat dilimi karışmasın.
const KISA_AY =["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"];
const KISA_GUN =["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"];  // servisteki gibi pazartesiden

function gunTarihi(metin) {
  return new Date(Date.UTC(+metin.slice(0, 4), +metin.slice(5, 7) - 1, +metin.slice(8, 10)));
}

export function tarihMetni(z, simdi, saat = true) {
  const d = gunTarihi(z);
  const fark = Math.round((d - gunTarihi(simdi)) / 86400000);
  let gun = { 0: "bugün", 1: "yarın", "-1": "dün" }[fark];
  if (gun === undefined) {
    const yil = z.slice(0, 4) !== simdi.slice(0, 4) ? ` ${d.getUTCFullYear()}` : "";
    gun = `${d.getUTCDate()} ${KISA_AY[d.getUTCMonth()]}${yil} ${KISA_GUN[(d.getUTCDay() + 6) % 7]}`;
  }
  return saat ? `${gun} ${z.slice(11, 16)}` : gun;
}

const GEC_DK = 5;  // servisteki zamanlayici.GEC_DK: bundan geç gidene "içindi" notu düşülür
const GEC_UST_DK = 60;  // servisteki zamanlayici.GEC_UST_DK: gün değişse bile bu kadar geç kalan "N dk geç" yazar

// Geç kalma süresi: '12 dk', '1 saat 5 dk', '3 saat' (servisteki zamanlayici._gecikme_metni).
function gecikmeMetni(dk) {
  dk = Math.max(1, Math.floor(dk));
  const sa = Math.floor(dk / 60);
  dk %= 60;
  return sa ? `${sa} saat` + (dk ? ` ${dk} dk` : "") : `${dk} dk`;
}

// Servisteki zamanlayici.hatirlatma_metni ile aynı biçim (saat bir kez yazılır): zamanında '· bugün 08:00', geç
// kalınca '· bugün 08:00 içindi, 12 dk geç'. Gün değişmiş olsa bile gecikme 60 dk'yı aşmadıysa yine 'N dk geç'
// ('dün 23:58 içindi, 6 dk geç'); 60 dk'yı aşan ve başka güne kalan: '· 15 Eki Per 23:50 içindi, bilgisayar kapalıydı'.
// Not satır satır italik (servisteki gibi: boş satırlar atılır).
export function hatirlatmaMetni(h, simdi) {
  const simge = h.tur === "Acil" ? "🚨 " : "";
  const gecDk = (Date.parse(simdi + "Z") - Date.parse(h.zaman + ":00Z")) / 60000;
  let zaman = tarihMetni(h.zaman, simdi);
  if (gecDk > GEC_DK) {
    zaman += " içindi, " + (h.zaman.slice(0, 10) === simdi.slice(0, 10) || gecDk <= GEC_UST_DK
      ? `${gecikmeMetni(gecDk)} geç` : "bilgisayar kapalıydı");
  }
  const notSatirlari = duzMetin(h.not).split(/\r?\n/).map((x) => x.trim()).filter(Boolean)
    .map((x) => `<i>${kacis(x)}</i>`);
  return [
    `${simge}<b>Hatırlatma</b> · ${zaman}`,
    // servisteki html_bicim konunun satır başındaki '- ' işaretini madde imine çevirir: aynı görünsün
    kacis(duzMetin(h.konu)).replace(/^(\s*)-\s+/gm, "$1\u2022 "),
    ...notSatirlari,
    h.tekrar ? `${ADLAR[h.tekrar]} tekrarlanıyor` : null,
    "",
    "<i>(bilgisayar kapalıyken buluttan gönderildi; düğmeler ve mesajlarınız bilgisayar açılınca işlenir)</i>",
  ].filter((x) => x !== null).join("\n");
}

// Bulut mesajının düğmeleri: servisteki Sohbet.dugme_geldi "hb:" ile işler (callback_data ≤ 64 bayt).
export function hatirlatmaDugmeleri(id) {
  return { inline_keyboard: [[{ text: "Tamam", callback_data: `hb:ok:${id}` },
                              { text: "1 saat ertele", callback_data: `hb:60:${id}` },
                              { text: "Yarına ertele", callback_data: `hb:y:${id}` }]] };
}

// ---------------------------------------------------------------- nabız mesajları

function nabizZamani(ms, tzDakika, simdiMs) {
  return tarihMetni(simdiYerel(tzDakika, ms), simdiYerel(tzDakika, simdiMs));
}

export function sustuMetni(sonMs, simdiMs, tzDakika) {
  const dk = Math.floor((simdiMs - sonMs) / 60000);
  return `⚠️ Masaüstü ${dk} dakikadır yanıt vermiyor (son nabız ${nabizZamani(sonMs, tzDakika, simdiMs)}). ` +
    "Elektrik, çökme, donma ya da uyku olabilir; bilgisayar kapalıyken hatırlatmaları bulut gönderiyor.";
}

export function acildiMetni(sessizMs, simdiMs, tzDakika) {
  return "Masaüstü yeniden açık" +
    (sessizMs ? ` (son nabız ${nabizZamani(sessizMs, tzDakika, simdiMs)} idi).` : ".") +
    " Hatırlatmaları yine bilgisayar gönderiyor.";
}

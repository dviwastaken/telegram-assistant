"""Testler için küçük PDF üretici (yan etkisiz; canlı test de kullanır)."""


def kucuk_pdf(yol, metin):
    """Elle yazılmış en küçük metinli PDF (reportlab gerektirmeden)."""
    akis = f"BT /F1 12 Tf 72 720 Td ({metin}) Tj ET".encode("latin-1")
    nesneler = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                b"/Resources << /Font << /F1 5 0 R >> >> >>",
                b"<< /Length " + str(len(akis)).encode() + b" >>\nstream\n" + akis + b"\nendstream",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    govde, konumlar = b"%PDF-1.4\n", []
    for i, n in enumerate(nesneler, 1):
        konumlar.append(len(govde))
        govde += f"{i} 0 obj\n".encode() + n + b"\nendobj\n"
    xref = len(govde)
    govde += f"xref\n0 {len(nesneler) + 1}\n0000000000 65535 f \n".encode()
    govde += b"".join(f"{k:010d} 00000 n \n".encode() for k in konumlar)
    govde += f"trailer\n<< /Size {len(nesneler) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(yol, "wb") as f:
        f.write(govde)

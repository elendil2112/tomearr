"""Build small but real ebook files of every supported kind, for extractor and scanner tests."""
import struct
import zipfile
from pathlib import Path


def make_epub(path: Path, title="The Hobbit", authors=("J.R.R. Tolkien",), isbn=None, lang="en", drm=None,
              series=None):
    """drm: None | 'adept' (rights.xml) | 'encrypted' (content in encryption.xml) | 'fonts' (font obfuscation only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    creators = "".join(f'<dc:creator opf:role="aut">{a}</dc:creator>' for a in authors)
    ident = f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>' if isbn else ""
    series_meta = f'<meta name="calibre:series" content="{series[0]}"/><meta name="calibre:series_index" content="{series[1]}"/>' if series else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:title>{title}</dc:title>{creators}{ident}<dc:identifier id="id">urn:uuid:1234</dc:identifier>
  <dc:language>{lang}</dc:language><dc:publisher>Test Press</dc:publisher><dc:date>1937-09-21</dc:date>{series_meta}
 </metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine></package>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", f"<html><body><p>{title}</p></body></html>")
        enc_entry = """<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptionMethod Algorithm="{alg}"/>
<CipherData><CipherReference URI="{uri}"/></CipherData></EncryptedData>"""
        if drm == "adept":
            z.writestr("META-INF/rights.xml", "<rights/>")
        if drm in ("adept", "encrypted"):
            z.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       + enc_entry.format(alg="http://www.w3.org/2001/04/xmlenc#aes128-cbc", uri="OEBPS/c1.xhtml")
                       + "</encryption>")
        if drm == "fonts":
            z.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       + enc_entry.format(alg="http://www.idpf.org/2008/embedding", uri="OEBPS/font.otf")
                       + "</encryption>")
    return path


def make_mobi(path: Path, title="The Hobbit", author="J.R.R. Tolkien", isbn=None, encryption=0, kf8=False,
              asin=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    exth_recs = [(100, author.encode()), (503, title.encode())]
    if isbn:
        exth_recs.append((104, isbn.encode()))
    if asin:
        exth_recs.append((113, asin.encode()))
    if kf8:
        exth_recs.append((121, struct.pack(">L", 1)))
    exth_body = b"".join(struct.pack(">LL", t, len(d) + 8) + d for t, d in exth_recs)
    exth = b"EXTH" + struct.pack(">LL", 12 + len(exth_body), len(exth_recs)) + exth_body
    mobi_hlen = 232
    full_name = title.encode()
    rec0_len = 16 + mobi_hlen + len(exth) + len(full_name) + 4
    rec0 = bytearray(rec0_len)
    struct.pack_into(">HHLHHHH", rec0, 0, 2, 0, 1000, 1, 4096, encryption, 0)
    rec0[16:20] = b"MOBI"
    struct.pack_into(">L", rec0, 20, mobi_hlen)
    struct.pack_into(">L", rec0, 24, 2)
    struct.pack_into(">L", rec0, 28, 65001)
    struct.pack_into(">L", rec0, 36, 8 if kf8 else 6)
    name_off = 16 + mobi_hlen + len(exth)
    struct.pack_into(">LL", rec0, 84, name_off, len(full_name))
    struct.pack_into(">L", rec0, 128, 0x40)
    rec0[16 + mobi_hlen:16 + mobi_hlen + len(exth)] = exth
    rec0[name_off:name_off + len(full_name)] = full_name
    header = bytearray(78)
    header[:len(title[:31])] = title[:31].encode()
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, 1)
    offset = 78 + 8 + 2
    data = bytes(header) + struct.pack(">LL", offset, 0) + b"\0\0" + bytes(rec0) + b"x" * 500
    path.write_bytes(data)
    return path


def _pdf_bytes(text_lines=(), title=None, author=None) -> bytes:
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(
        f"({t.replace('(', '').replace(')', '')}) '" for t in text_lines) + " ET"
    info = ""
    if title:
        info += f"/Title ({title}) "
    if author:
        info += f"/Author ({author}) "
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< {info}>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_pdf(path: Path, text_lines=(), title=None, author=None, password=None):
    """password: None | 'user' (needs a password to open) | 'owner' (permissions only, opens fine)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf_bytes(text_lines, title, author))
    if password:
        from pypdf import PdfReader, PdfWriter
        w = PdfWriter(clone_from=PdfReader(str(path)))
        w.encrypt(user_password="secret" if password == "user" else "", owner_password="owner", algorithm="RC4-128")
        with open(path, "wb") as fh:
            w.write(fh)
    return path


def make_fb2(path: Path, title="Hamlet", first="William", last="Shakespeare", isbn=None, zipped=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    isbn_el = f"<publish-info><isbn>{isbn}</isbn><year>1603</year></publish-info>" if isbn else ""
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info>
<author><first-name>{first}</first-name><last-name>{last}</last-name></author><book-title>{title}</book-title>
<lang>en</lang><sequence name="Plays" number="2"/></title-info>{isbn_el}</description><body><p>x</p></body></FictionBook>"""
    if zipped:
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("book.fb2", xml)
    else:
        path.write_text(xml, encoding="utf-8")
    return path


def make_docx(path: Path, title="Macbeth", creator="William Shakespeare"):
    """A minimal but valid DOCX (real converters need the relationship parts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>""")
        z.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>""")
        z.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>""")
        z.writestr("word/document.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>{title}</w:t></w:r></w:p><w:p><w:r><w:t>Chapter one.</w:t></w:r></w:p></w:body></w:document>""")
        z.writestr("docProps/core.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator></cp:coreProperties>""")
    return path


def make_gutenberg_txt(path: Path, title="The Lord of the Rings", author="J.R.R. Tolkien"):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"The Project Gutenberg eBook\n\nTitle: {title}\nAuthor: {author}\nLanguage: English\n\n" + "words " * 5000
    path.write_text(body)
    return path


def make_zip(path: Path, members: dict[str, bytes]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path

"""Buat README.pdf dari README.md untuk pengumpulan Tugas 2.

Bagian internal (status pengerjaan, catatan untuk owner klaster) tidak dimasukkan ke PDF.
Butuh: pandoc, dan Google Chrome / Chromium untuk mencetak HTML ke PDF.

Pakai (dari root repo):
    python tools/build_readme_pdf.py            # menghasilkan README.pdf
"""
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_SECTIONS = {"Status pengerjaan", "Perubahan kontrak yang perlu disampaikan ke owner klaster"}

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: "Liberation Sans", Arial, sans-serif; font-size: 10pt; line-height: 1.45; color: #1f2937; }
h1 { font-size: 18pt; color: #1F2A44; margin: 0 0 6pt; }
h2 { font-size: 14pt; color: #8B1E2D; border-bottom: 1.5px solid #e5e7eb; padding-bottom: 3pt;
     margin-top: 18pt; page-break-after: avoid; }
h3 { font-size: 11.5pt; color: #1F2A44; margin-top: 12pt; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
code { font-family: "DejaVu Sans Mono", Consolas, monospace; font-size: 8.6pt; background: #f3f4f6;
       padding: 0 2px; border-radius: 2px; }
pre { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 4px; padding: 7pt 9pt;
      white-space: pre-wrap; word-break: break-word; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 7.6pt; }
th code { background: transparent; color: #fff; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.8pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #1F2A44; color: #fff; text-align: left; padding: 4pt 6pt; }
td { border-bottom: 1px solid #e5e7eb; padding: 4pt 6pt; vertical-align: top; }
blockquote { border-left: 3px solid #8B1E2D; margin: 6pt 0; padding: 2pt 10pt; color: #4b5563; }
img { max-width: 100%; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; page-break-after: always; }
.cover .kicker { color: #8B1E2D; font-weight: bold; letter-spacing: 1px; font-size: 11pt; }
.cover h1 { font-size: 30pt; margin: 6pt 0; }
.cover .sub { font-size: 13pt; color: #374151; margin-bottom: 28pt; }
.cover table { width: auto; font-size: 10.5pt; }
.cover td { border: none; padding: 3pt 14pt 3pt 0; }
.cover td:first-child { color: #6b7280; }
"""


def strip_sections(md: str) -> str:
    head, *parts = md.split("\n## ")
    head = "\n".join(line for line in head.splitlines() if not line.startswith(">"))
    # judul utama (H1) dipindah ke halaman sampul
    head = re.sub(r"^# .*\n", "", head, count=1)
    kept = [p for p in parts if p.split("\n", 1)[0].strip() not in SKIP_SECTIONS]
    return head + "".join("\n## " + p for p in kept)


def cover_html() -> str:
    today = datetime.date.today().strftime("%d %B %Y")
    rows = [("Mata kuliah", "II3160 Teknologi Sistem Terintegrasi × II3140 PWAM"),
            ("Kelompok", "donorkita · Kelas K02 · Health &amp; Wellbeing"),
            ("Disusun oleh", "Reza Nashwan Faizi (18224008)"),
            ("Base URL", "https://donorkita.stei.site/api"),
            ("Dokumentasi", "https://donorkita.stei.site/api/docs"),
            ("Repositori", "https://github.com/rnfaizi/DonorKita"),
            ("Tanggal", today)]
    body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return (f'<div class="cover"><div class="kicker">README · TUGAS 2 MICROSERVICE DI STB</div>'
            f'<h1>DonorKita</h1><div class="sub">Layanan inti pencocokan kebutuhan darah dengan pendonor sukarela</div>'
            f'<table>{body}</table></div>')


def find_chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("Chrome/Chromium tidak ditemukan untuk mencetak PDF")


def main() -> None:
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        md = strip_sections(f.read())
    with tempfile.TemporaryDirectory() as tmp:
        md_path, html_path = os.path.join(tmp, "readme.md"), os.path.join(tmp, "readme.html")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        css_path = os.path.join(tmp, "style.css")
        with open(css_path, "w", encoding="utf-8") as f:
            f.write(CSS)
        subprocess.run(["pandoc", md_path, "-f", "gfm", "-t", "html5", "-s", "--embed-resources", "--no-highlight",
                        "--resource-path", ROOT, "--css", css_path, "--metadata", "title=DonorKita README",
                        "-V", "title=", "-o", html_path], check=True)
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        html = re.sub(r'<header id="title-block-header">.*?</header>', "", html, flags=re.S)
        html = html.replace("<body>", "<body>" + cover_html(), 1)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        out = os.path.join(ROOT, "README.pdf")
        subprocess.run([find_chrome(), "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={out}", "file://" + html_path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("README.pdf dibuat:", out)


if __name__ == "__main__":
    main()

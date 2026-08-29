"""Generate the sample claim PDFs in data/claims/ with no third-party deps.

Textract reads embedded text out of a digital PDF, so a hand-built page with a
Helvetica content stream is real input - no scanning or OCR fixtures needed.
"""
import os

CLAIMS = [
    {
        "file": "claim-C-77.pdf",
        "lines": [
            "AUTO CLAIM FORM - Northwind Mutual",
            "",
            "Claim ID: CLM-2026-0077",
            "Claimant ID: C-77",
            "Claimant Name: Dana Whitfield",
            "Claimant Email: dana.whitfield@example.com",
            "Policy Number: POL-AUTO-4471",
            "Date of Loss: 2026-07-14",
            "Loss Type: Collision - single vehicle",
            "Description: Struck a guardrail on wet road. Front bumper,",
            "  radiator and driver-side headlight damaged. Vehicle drivable.",
            "Repair Estimate: 8200.00 USD",
            "Deductible: 500.00 USD",
            "Amount Requested: 8200.00 USD",
            "Police Report Filed: Yes (ref 2026-07-14-116)",
            "Injuries Reported: None",
        ],
    },
    {
        "file": "claim-C-92.pdf",
        "lines": [
            "AUTO CLAIM FORM - Northwind Mutual",
            "",
            "Claim ID: CLM-2026-0092",
            "Claimant ID: C-92",
            "Claimant Name: Rowan Alcott",
            "Claimant Email: rowan.alcott@example.com",
            "Policy Number: POL-AUTO-8890",
            "Date of Loss: 2026-08-02",
            "Loss Type: Theft - total loss",
            "Description: Vehicle reported stolen from a long-stay car park.",
            "  Not recovered. Replacement value claimed.",
            "Repair Estimate: N/A - total loss",
            "Deductible: 1000.00 USD",
            "Amount Requested: 24500.00 USD",
            "Police Report Filed: Yes (ref 2026-08-02-441)",
            "Injuries Reported: None",
        ],
    },
    {
        "file": "claim-C-13.pdf",
        "lines": [
            "HOME CLAIM FORM - Northwind Mutual",
            "",
            "Claim ID: CLM-2026-0013",
            "Claimant ID: C-13",
            "Claimant Name: Priya Raman",
            "Claimant Email: priya.raman@example.com",
            "Policy Number: POL-HOME-2201",
            "Date of Loss: 2026-08-09",
            "Loss Type: Water damage - burst pipe",
            "Description: Supply pipe failed under the kitchen sink overnight.",
            "  Flooring and lower cabinets damaged. Mitigation done same day.",
            "Repair Estimate: 3150.00 USD",
            "Deductible: 250.00 USD",
            "Amount Requested: 3150.00 USD",
            "Police Report Filed: No",
            "Injuries Reported: None",
        ],
    },
]


def escape(text):
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(lines):
    """Assemble a single-page PDF, tracking byte offsets for the xref table."""
    content = ["BT", "/F1 11 Tf", "14 TL", "1 0 0 1 56 760 Tm"]
    for line in lines:
        content.append(f"({escape(line)}) Tj")
        content.append("T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main():
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "claims")
    os.makedirs(target, exist_ok=True)
    for claim in CLAIMS:
        path = os.path.join(target, claim["file"])
        with open(path, "wb") as handle:
            handle.write(build_pdf(claim["lines"]))
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()

import csv
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
PLANILHA = Path(r"C:\Users\erikh\Downloads\higienita_armazem02.xlsx")
DATA_DIR = ROOT / "data"
IGNORE_PRODUCTS = {"PALETES", "BANCADA", "DEVOLUCAO", "DEVOLUÇÃO"}


def extract_js_object(source, name):
    start = source.find(f"var {name} = ")
    if start < 0:
        raise RuntimeError(f"{name} nao encontrado")
    start = source.find("{", start)
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(source)):
        ch = source[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1, source[start : i + 1], json.loads(source[start : i + 1])
    raise RuntimeError(f"Objeto {name} incompleto")


def norm(value):
    text = "".join(
        c
        for c in unicodedata.normalize("NFD", str(value).upper())
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_name(a, b):
    a_n = norm(a)
    b_n = norm(b)
    if not a_n or not b_n:
        return 0
    if a_n == b_n:
        return 100
    a_words = set(a_n.split())
    b_words = set(b_n.split())
    overlap = len(a_words & b_words) / max(1, len(a_words))
    seq = SequenceMatcher(None, a_n, b_n).ratio()
    prefix_bonus = 0.08 if b_n.startswith(a_n) or a_n.startswith(b_n) else 0
    return round(min(100, (seq * 65) + (overlap * 35) + (prefix_bonus * 100)), 1)


def js_string(value):
    return json.dumps(value, ensure_ascii=False)


def main():
    source = INDEX.read_text(encoding="utf-8")
    tiny_start, tiny_end, old_tiny_text, old_tiny = extract_js_object(source, "TINY")
    _catalogo_start, _catalogo_end, _catalogo_text, catalogo = extract_js_object(source, "CATALOGO_INV")

    candidates = []
    for item in catalogo.values():
        sku = str(item.get("sku") or item.get("codigo") or "").strip()
        desc = str(item.get("desc") or item.get("descricao") or item.get("nome") or "").strip()
        if sku and desc:
            candidates.append({"sku": sku, "desc": desc, "source": "catalogo"})
    for addr, arr in old_tiny.items():
        for item in arr:
            sku = str(item.get("s") or "").strip()
            desc = str(item.get("d") or "").strip()
            if sku and desc:
                candidates.append({"sku": sku, "desc": desc, "source": f"mapa:{addr}"})

    wb = openpyxl.load_workbook(PLANILHA, data_only=True)
    ws = wb.active
    armazem = {}
    report = []

    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        addr, _corredor, _nivel, sku_raw, produto_raw, quantidade = row
        if not addr or not produto_raw:
            continue
        addr = str(addr).strip().upper()
        produto = str(produto_raw).strip()
        qty = int(float(quantidade or 0))
        if qty <= 0 or norm(produto) in {norm(p) for p in IGNORE_PRODUCTS}:
            continue
        sku = str(sku_raw or "").strip()
        source = "planilha"
        score = 100 if sku else 0
        matched_desc = produto

        if not sku:
            best = None
            for cand in candidates:
                score_c = score_name(produto, cand["desc"])
                if best is None or score_c > best["score"]:
                    best = {**cand, "score": score_c}
            if best and best["score"] >= 70:
                sku = best["sku"]
                matched_desc = best["desc"]
                source = best["source"]
                score = best["score"]

        armazem.setdefault(addr, []).append({"s": sku, "d": produto, "q": qty})
        report.append(
            {
                "linha": idx,
                "endereco": addr,
                "sku": sku,
                "produto_planilha": produto,
                "quantidade": qty,
                "produto_match": matched_desc,
                "origem_match": source,
                "score": score,
                "revisar": "sim" if not sku or score < 78 else "",
            }
        )

    DATA_DIR.mkdir(exist_ok=True)
    new_tiny_text = json.dumps(armazem, ensure_ascii=False, separators=(",", ":"))
    (DATA_DIR / "armazem02.js").write_text(
        "window.ARMAZEM02 = " + new_tiny_text + ";\n", encoding="utf-8"
    )

    with (DATA_DIR / "armazem02_import.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["endereco", "sku", "produto", "quantidade"])
        for item in report:
            writer.writerow([item["endereco"], item["sku"], item["produto_planilha"], item["quantidade"]])

    with (DATA_DIR / "armazem02_match_report.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(report[0].keys()))
        writer.writeheader()
        writer.writerows(report)

    summary = {
        "enderecos": len(armazem),
        "linhas": len(report),
        "com_sku": sum(1 for item in report if item["sku"]),
        "revisar": sum(1 for item in report if item["revisar"]),
        "js": str(DATA_DIR / "armazem02.js"),
        "csv": str(DATA_DIR / "armazem02_import.csv"),
        "relatorio": str(DATA_DIR / "armazem02_match_report.csv"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
url2cook.py - convertit des pages web de recettes en fichiers Cooklang (.cook).

* Lit un fichier texte avec une URL par ligne (les lignes vides et celles qui
  commencent par '#' sont conservées telles quelles).
* Extrait la recette depuis les métadonnées schema.org/Recipe (JSON-LD) que
  quasiment tous les sites de recettes publient -> aucune IA, aucune
  dépendance : uniquement la bibliothèque standard de Python (3.8+).
* Écrit une recette .cook par URL dans le dossier « A classer ».
* Supprime la ligne du fichier dès que la recette est exportée.
* Affiche sur la sortie standard UNIQUEMENT le nombre de recettes créées
  (les détails/erreurs vont sur stderr).

Usage :
    python3 scripts/url2cook.py [urls.txt] [-o "A classer"]
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

DEFAULT_OUT = "A classer"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# Récupération et extraction JSON-LD
# --------------------------------------------------------------------------


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "fr,en;q=0.8",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _LdJsonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._in = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self._in, self._buf = True, []

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self.blocks.append("".join(self._buf))
            self._in = False

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in node:
                yield from _walk(node[key])


def _is_recipe(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return "Recipe" in types


def find_recipe(page: str) -> dict | None:
    parser = _LdJsonParser()
    parser.feed(page)
    for block in parser.blocks:
        block = block.strip()
        try:
            data = json.loads(block)
        except ValueError:
            try:  # certains sites échappent mal les retours à la ligne
                data = json.loads(re.sub(r"[\x00-\x1f]+", " ", block))
            except ValueError:
                continue
        for node in _walk(data):
            if _is_recipe(node):
                return node
    return None


# --------------------------------------------------------------------------
# Nettoyage de texte
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t  ]+")


def clean(text) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _SPACES_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def cook_safe(text: str) -> str:
    """Retire les caractères qui ont un sens en Cooklang."""
    return re.sub(r"[@#~{}]", "", text)


# --------------------------------------------------------------------------
# Ingrédients
# --------------------------------------------------------------------------

_FRACTIONS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅕": "1/5", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}

# forme reconnue (minuscule, sans point final) -> unité normalisée
_UNITS = {
    "g": "g", "gr": "g", "gramme": "g", "grammes": "g",
    "kg": "kg", "kilo": "kg", "kilos": "kg",
    "mg": "mg",
    "ml": "ml", "cl": "cl", "dl": "dl",
    "l": "l", "litre": "l", "litres": "l",
    "cs": "cs", "càs": "cs", "c.à.s": "cs", "c.à.soupe": "cs",
    "cuillère à soupe": "cs", "cuillères à soupe": "cs",
    "cuillere à soupe": "cs", "cuilleres à soupe": "cs",
    "c. à soupe": "cs", "c à soupe": "cs", "c. à s": "cs", "c à s": "cs",
    "tbsp": "cs", "tablespoon": "cs", "tablespoons": "cs",
    "cc": "cc", "càc": "cc", "c.à.c": "cc", "c.à.café": "cc",
    "cuillère à café": "cc", "cuillères à café": "cc",
    "cuillere à café": "cc", "cuilleres à café": "cc",
    "c. à café": "cc", "c à café": "cc", "c. à c": "cc", "c à c": "cc",
    "tsp": "cc", "teaspoon": "cc", "teaspoons": "cc",
    "pincée": "pincée", "pincées": "pincée", "pincee": "pincée",
    "sachet": "sachet", "sachets": "sachet",
    "tasse": "tasse", "tasses": "tasse", "cup": "cup", "cups": "cup",
    "verre": "verre", "verres": "verre",
    "gousse": "gousse", "gousses": "gousse",
    "tranche": "tranche", "tranches": "tranche",
    "feuille": "feuille", "feuilles": "feuille",
    "brin": "brin", "brins": "brin",
    "noix": "noix",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
}
# essayer les unités les plus longues d'abord
_UNIT_KEYS = sorted(_UNITS, key=len, reverse=True)

_QTY_RE = re.compile(
    r"""^\s*(?P<qty>
        \d+\s+\d+/\d+          # 1 1/2
      | \d+/\d+                # 1/2
      | \d+(?:[.,]\d+)?        # 12  / 1,5 / 0.5
    )(?:\s*(?:-|à|to)\s*\d+(?:[.,]\d+)?)?   # 2-3 -> on garde la 1re valeur
    \s*""",
    re.X | re.I,
)


def _qty_to_str(raw: str) -> str:
    raw = raw.strip()
    if " " in raw and "/" in raw:
        whole, frac = raw.split(None, 1)
        n, d = frac.split("/")
        val = int(whole) + int(n) / int(d)
    elif "/" in raw:
        n, d = raw.split("/")
        val = int(n) / int(d)
    else:
        val = float(raw.replace(",", "."))
    return str(int(val)) if val == int(val) else f"{val:.2f}".rstrip("0").rstrip(".")


def parse_ingredient(line: str) -> dict:
    text = clean(line)
    for ch, frac in _FRACTIONS.items():
        text = re.sub(rf"(\d)\s*{ch}", rf"\1 {frac}", text)
        text = text.replace(ch, frac)
    text = re.sub(r"\([^)]*\)", " ", text)  # retire les parenthèses
    text = _SPACES_RE.sub(" ", text).strip(" ,;")

    qty = unit = ""
    m = _QTY_RE.match(text)
    if m:
        qty = _qty_to_str(m.group("qty"))
        text = text[m.end():]
        low = text.lower()
        for key in _UNIT_KEYS:
            if low.startswith(key):
                end = len(key)
                # l'unité doit être suivie d'une fin de mot
                if end == len(low) or not low[end].isalpha() or key.endswith(".") or low[end] in " .'’":
                    unit = _UNITS[key]
                    text = text[end:].lstrip(". ")
                    break
    text = re.sub(r"^(?:de la|de l['’]|du|des|de|d['’])(?:\s+|(?<=['’]))", "", text, flags=re.I)
    text = text.split(",")[0].strip()  # « farine, tamisée » -> « farine »
    name = cook_safe(text).strip().lower() or cook_safe(clean(line))
    return {"name": name, "qty": qty, "unit": unit, "raw": clean(line)}


def ingredient_token(ing: dict) -> str:
    name = ing["name"]
    if ing["qty"] and ing["unit"]:
        amount = f"{ing['qty']}%{ing['unit']}"
    else:
        amount = ing["qty"]
    return f"@{name}{{{amount}}}"


# --------------------------------------------------------------------------
# Étapes
# --------------------------------------------------------------------------


def parse_steps(instructions) -> list[str]:
    steps: list[str] = []

    def add(node):
        if isinstance(node, str):
            text = clean(node)
            for part in re.split(r"\n+", text):
                part = part.strip()
                if part:
                    steps.append(part)
        elif isinstance(node, list):
            for n in node:
                add(n)
        elif isinstance(node, dict):
            if "itemListElement" in node:
                add(node["itemListElement"])
            else:
                add(node.get("text") or node.get("name") or "")

    add(instructions)
    return steps


def _word_pattern(word: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(word)}(?:s|x)?(?!\w)", re.I)


def inject_ingredients(steps: list[str], ingredients: list[dict]) -> tuple[list[str], list[dict]]:
    """Place chaque ingrédient (@nom{qté%unité}) à sa 1re mention dans les étapes."""
    pending = [i for i in ingredients if i["name"]]
    placed: set[int] = set()
    out: list[str] = []

    for step in steps:
        text = cook_safe(step)
        slots: dict[str, str] = {}

        def stash(match_text_replacement: str) -> str:
            key = f"\x00{len(slots)}\x00"
            slots[key] = match_text_replacement
            return key

        # 1) correspondance sur le nom complet, 2) sur un mot significatif
        for idx, ing in enumerate(pending):
            if idx in placed:
                continue
            candidates = [ing["name"]] + [w for w in re.findall(r"\w+", ing["name"]) if len(w) >= 4]
            for cand in candidates:
                pat = _word_pattern(cand)
                m = pat.search(text)
                if m:
                    text = text[: m.start()] + stash(ingredient_token(ing)) + text[m.end():]
                    placed.add(idx)
                    break
        for key, val in slots.items():
            text = text.replace(key, val)
        out.append(text)

    leftovers = [ing for idx, ing in enumerate(pending) if idx not in placed]
    return out, leftovers


# --------------------------------------------------------------------------
# Métadonnées
# --------------------------------------------------------------------------


def iso_duration(value) -> str:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(value or "").strip(), re.I)
    if not m or not (m.group(1) or m.group(2)):
        return ""
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    if h and mi:
        return f"{h}h{mi:02d}"
    return f"{h}h" if h else f"{mi} min"


def first_str(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("@id") or ""
    return clean(value)


def yaml_quote(text: str) -> str:
    if text == "":
        return ""
    if re.search(r"[:#\[\]{}&*!|>'\"%@`,]", text) or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def build_cook(recipe: dict, url: str) -> tuple[str, str]:
    title = clean(recipe.get("name")) or urlparse(url).path.strip("/").split("/")[-1] or "Recette"
    ingredients = [parse_ingredient(i) for i in (recipe.get("recipeIngredient") or recipe.get("ingredients") or [])]
    steps = parse_steps(recipe.get("recipeInstructions"))
    if not steps:
        raise ValueError("aucune étape trouvée")

    steps, leftovers = inject_ingredients(steps, ingredients)

    host = urlparse(url).netloc.removeprefix("www.")
    author = first_str(recipe.get("author")) or host
    total = iso_duration(recipe.get("totalTime")) or iso_duration(recipe.get("cookTime")) or iso_duration(recipe.get("prepTime"))

    front = [
        "---",
        'recipe: "true"',
        f"qte: {yaml_quote(first_str(recipe.get('recipeYield')))}".rstrip(),
        f"temps: {yaml_quote(total)}".rstrip(),
        f"origine: {yaml_quote(author)}",
        f"source: {url}",
        "tags:",
        "  - recette",
        "---",
        "",
    ]

    body: list[str] = []
    if leftovers:
        body.append("Ingrédients : " + ", ".join(ingredient_token(i) for i in leftovers))
    body.extend(steps)

    return title, "\n".join(front) + "\n" + "\n\n".join(body) + "\n"


# --------------------------------------------------------------------------
# Fichiers
# --------------------------------------------------------------------------


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:120] or "Recette")


def unique_path(folder: str, title: str) -> str:
    base = safe_filename(title)
    path = os.path.join(folder, base + ".cook")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}).cook")
        n += 1
    return path


def write_lines_atomic(path: str, lines: list[str]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".urls-", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp, path)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="URLs -> recettes Cooklang (.cook)")
    ap.add_argument("urls_file", nargs="?", default="urls.txt", help="fichier avec une URL par ligne (défaut: urls.txt)")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help=f"dossier de sortie (défaut: « {DEFAULT_OUT} »)")
    args = ap.parse_args(argv)

    try:
        with open(args.urls_file, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        log(f"Impossible de lire {args.urls_file}: {exc}")
        print(0)
        return 1

    os.makedirs(args.out, exist_ok=True)
    remaining = list(lines)
    created = 0

    for line in lines:
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            page = fetch(url)
            recipe = find_recipe(page)
            if recipe is None:
                raise ValueError("pas de données schema.org/Recipe sur la page")
            title, cook = build_cook(recipe, url)
            path = unique_path(args.out, title)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(cook)
        except Exception as exc:  # on garde la ligne pour réessayer plus tard
            log(f"[échec] {url} : {exc}")
            continue
        created += 1
        remaining.remove(line)
        write_lines_atomic(args.urls_file, remaining)
        log(f"[ok] {url} -> {path}")

    print(created)
    return 0


if __name__ == "__main__":
    sys.exit(main())

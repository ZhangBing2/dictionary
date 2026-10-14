#!/usr/bin/env python3
"""Convert the LAAD3 MDict files (MDX + MDD) into a Dictionary Development Kit project.

    python3 -I build_laad3.py ~/dictionary/laad3 ~/dictionary/laad3-macos/project
    cd ~/dictionary/laad3-macos/project && make && make install

What it changes compared with a plain MDX dump:
  * per-entry <link>/<script> removed; one theme-aware stylesheet (light + dark) instead
  * popup buttons (Etymology / Verb Table / Collocations / Thesaurus) and the
    collapsible thesaurus/collocation items become native <details>, so they work without JS;
    their <summary> carries apple_mouseover_disable="1" so Dictionary.app toggles them instead
    of looking up the label text (see NO_LOOKUP)
  * sound:// links play through an inline onclick (Audio), with SVG speaker icons
  * entry:// cross references become x-dictionary:r: links; same-entry anchors scroll in place
  * extra index keys: inflections (verb tables, irregular forms, regular rules),
    phrasal verbs (+ inflected forms), derived words, spelling variants and idioms

Options:
  --limit N           convert only the first N entries (quick style iterations)
  --skip-resources    do not (re)extract audio/images from the MDD
"""
from __future__ import annotations

import argparse
import itertools
import os
import re
import shutil
import sys
import time
from html.parser import HTMLParser
from urllib.parse import quote

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from mdict_lib import MDict  # noqa: E402

ASSETS = os.path.join(TOOLS, "assets")
DICT_NAME = "LAAD3"
DISPLAY_NAME = "Longman Advanced American Dictionary"
DDK_DIR = "/Applications/Utilities/Dictionary Development Kit"
MDX_BASENAME = "LongmanAdvancedAmericanDictionary3thEnEn"
JS_OBJ = "b13ac87b5ac740d385e7f096040bb4e6"
FRONT_KEY = "faq-index"
ABOUT_PREFIX = "faq-about"

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
ATTR_OK = re.compile(r"^[A-Za-z_][-A-Za-z0-9_.]*$")
WS = re.compile(r"\s+")

# Dictionary.app injects DictionaryHoverHighlighter.js, which looks up the word under *any* click unless
# the click lands inside an <a> or the text has an ancestor carrying this attribute (the app uses it for
# its own "No entries found" message). Without it, a click on a <summary> toggles the <details> and then
# navigates away to the label word ("etymology").
NO_LOOKUP = "apple_mouseover_disable"

POP_KINDS = {"etymology": "etym", "collocations": "collo", "verb table": "verbs", "thesaurus": "thes"}
INFL_CLASSES = {
    "pluralform": "noun", "comp": "adj", "superl": "adj",
    "pasttense": "past", "pasttensex": "past", "ptandpp": "past", "ptandppx": "past",
    "pastpart": "pp", "pastpartx": "pp",
    "prespart": "ing", "prespartx": "ing",
    "t3perssing": "3sg", "t3perssingx": "3sg",
}
LABEL_CLASSES = {"neutral", "infllab", "italic", "geo", "registerlab", "linkword", "gram"}
STYLE_DROP = {"color", "background", "background-color", "font-family", "font-size", "line-height"}
FORM_OK = re.compile(r"[A-Za-z][A-Za-z'’ .-]{0,48}")
SIMPLE_WORD = re.compile(r"[a-z][a-z'-]*")


# ----------------------------------------------------------------------------- DOM
class El:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs if attrs is not None else {}
        self.children = []
        self.parent = parent

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def has_class(self, c):
        return c in self.classes

    def add_class(self, c):
        cl = self.classes
        if c not in cl:
            self.attrs["class"] = " ".join(cl + [c])

    def iter(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(ch for ch in reversed(node.children) if isinstance(ch, El))

    def first(self, cls):
        return next((e for e in self.iter() if e is not self and e.has_class(cls)), None)

    def text(self, skip=frozenset()):
        out = []

        def rec(node):
            for ch in node.children:
                if isinstance(ch, str):
                    out.append(ch)
                elif not (skip and skip.intersection(ch.classes)):
                    rec(ch)

        rec(self)
        return "".join(out)

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("#root")
        self.cur = self.root

    @staticmethod
    def _attrs(attrs):
        return {k: (v if v is not None else k) for k, v in attrs if ATTR_OK.match(k)}

    def handle_starttag(self, tag, attrs):
        el = El(tag, self._attrs(attrs), self.cur)
        self.cur.children.append(el)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(El(tag, self._attrs(attrs), self.cur))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text):
    b = _TreeBuilder()
    b.feed(text)
    b.close()
    return b.root


def _esc(s):
    return XML_BAD.sub("", s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s):
    return _esc(s).replace('"', "&quot;").replace("\r", " ").replace("\n", " ")


def serialize(node, out):
    for ch in node.children:
        if isinstance(ch, str):
            out.append(_esc(ch.replace("\r", " ").replace("\n", " ")))
            continue
        attrs = "".join(f' {k}="{_esc_attr(v)}"' for k, v in ch.attrs.items())
        if ch.tag in VOID:
            out.append(f"<{ch.tag}{attrs}/>")
        else:
            out.append(f"<{ch.tag}{attrs}>")
            serialize(ch, out)
            out.append(f"</{ch.tag}>")


def _js(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _norm(s):
    return WS.sub(" ", s.replace("\xa0", " ")).strip()


# ----------------------------------------------------------------------------- inflection rules
def _syllables(w):
    return len(re.findall(r"[aeiouy]+", w))


def _cvc_double(w):
    """stop -> stopp (one-syllable consonant-vowel-consonant words)."""
    if (len(w) >= 3 and w[-1] not in "aeiouwxy" and w[-2] in "aeiou" and w[-3] not in "aeiou"
            and _syllables(w) == 1):
        return w + w[-1]
    return None


def plural(w):
    if re.search(r"(s|x|z|ch|sh)$", w):
        return [w + "es"]
    if re.search(r"[^aeiou]y$", w):
        return [w[:-1] + "ies"]
    if re.search(r"[^aeiou]o$", w):
        return [w + "s", w + "es"]
    return [w + "s"]


def third_sg(w):
    return plural(w)


def past(w):
    if w.endswith("e"):
        return [w + "d"]
    if re.search(r"[^aeiou]y$", w):
        return [w[:-1] + "ied"]
    d = _cvc_double(w)
    return [w + "ed"] + ([d + "ed"] if d else [])


def ing(w):
    if w.endswith("ie"):
        return [w[:-2] + "ying"]
    if w.endswith("e") and len(w) > 2 and not re.search(r"(ee|oe|ye)$", w):
        return [w[:-1] + "ing"]
    d = _cvc_double(w)
    return [w + "ing"] + ([d + "ing"] if d else [])


def compare(w):
    if w.endswith("e"):
        return [w + "r", w + "st"]
    if re.search(r"[^aeiou]y$", w):
        return [w[:-1] + "ier", w[:-1] + "iest"]
    d = _cvc_double(w) or w
    return [d + "er", d + "est"]


def split_forms(text):
    out = []
    for part in re.split(r",|;|\bor\b|\balso\b", text):
        f = _norm(part).strip(" .()")
        if f and FORM_OK.fullmatch(f):
            out.append(f)
    return out


def phrase_variants(text):
    """'base on/upon' -> [base on, base upon]; 'brush up (on)' -> [brush up, brush up on];
    'barge in (also barge into)' -> [barge in, barge into]."""
    text = _norm(text.replace("’", "'"))
    extra = []
    for m in re.finditer(r"\(also ([^)]*)\)", text):
        extra.append(m.group(1))
    text = re.sub(r"\(also [^)]*\)", " ", text)
    results = []
    for base in [text] + extra:
        base = _norm(re.sub(r"\b(something|somebody|someone|sth|sb)\b", " ", base))
        opts = [_norm(re.sub(r"\(([^)]*)\)", r"\1", base)), _norm(re.sub(r"\([^)]*\)", " ", base))]
        for o in dict.fromkeys(opts):
            tokens = [t.split("/") for t in o.split(" ") if t]
            combos = list(itertools.islice(itertools.product(*tokens), 8)) if tokens else []
            results.extend(" ".join(c) for c in combos)
    return [r for r in dict.fromkeys(_norm(x) for x in results) if r and FORM_OK.fullmatch(r)]


def idiom_variants(text):
    text = _norm(text.replace("’", "'"))
    if "/" in text or len(text) > 70:
        return []
    text = _norm(re.sub(r"\betc\.?", " ", text))
    opts = [_norm(re.sub(r"\(([^)]*)\)", r"\1", text)), _norm(re.sub(r"\([^)]*\)", " ", text))]
    return [o for o in dict.fromkeys(opts) if o and " " in o]


# ----------------------------------------------------------------------------- entry conversion
class Context:
    def __init__(self, keys):
        self.ids = {}
        self.ids_lower = {}
        n = 0
        for k in keys:
            if k == FRONT_KEY:
                eid = "front_back_matter"
            elif k.startswith(ABOUT_PREFIX):
                eid = "pron_key"
            else:
                n += 1
                eid = f"e{n}"
            self.ids[k] = eid
            self.ids_lower.setdefault(k.lower(), eid)
        self.stats = {"index": 0, "links_r": 0, "links_d": 0, "anchors": 0, "audio": 0}

    def id_for(self, word):
        return self.ids.get(word) or self.ids_lower.get(word.lower())


def _clean_style(style):
    keep = []
    for decl in style.split(";"):
        prop = decl.split(":", 1)[0].strip().lower()
        if decl.strip() and prop not in STYLE_DROP:
            keep.append(decl.strip())
    return "; ".join(keep)


def _make_speaker(a, path, ctx):
    kind = "exa" if path.startswith("exa/") else "hwd"
    a.tag = "span"
    a.attrs = {
        "class": f"jp-play spk-{kind}",
        "onclick": f"new Audio('{_js(path)}').play();event.stopPropagation();return false;",
        "title": "Play",
    }
    a.children = []
    ctx.stats["audio"] += 1


def _in_popup(el):
    return any(a.has_class("at-link") for a in el.ancestors())


def transform(root, eid, ctx):
    """Rewrite the parsed MDX record in place (see module docstring)."""
    seen_ids = set()
    for el in list(root.iter()):
        if el is root:
            continue
        if el.tag in ("link", "script"):
            el.parent.children.remove(el)
            continue
        if el.tag == "a":
            name = el.attrs.pop("name", None)
            if name and "id" not in el.attrs and name not in seen_ids:
                el.attrs["id"] = name
                seen_ids.add(name)
            href = el.attrs.get("href")
            if href is None:
                continue
            if href.startswith("sound://"):
                _make_speaker(el, href[len("sound://"):], ctx)
            elif href.startswith("entry://"):
                word, _, frag = href[len("entry://"):].partition("#")
                if not word:
                    el.attrs["href"] = "#" + frag
                    el.attrs["onclick"] = (f"var e=document.getElementById('{_js(frag)}');"
                                           "if(e){e.scrollIntoView();}return false;")
                    ctx.stats["anchors"] += 1
                else:
                    tid = ctx.id_for(word)
                    if tid:
                        el.attrs["href"] = f"x-dictionary:r:{tid}"
                        ctx.stats["links_r"] += 1
                    else:
                        el.attrs["href"] = "x-dictionary:d:" + quote(word)
                        ctx.stats["links_d"] += 1
            continue
        if el.tag == "img":
            if JS_OBJ in el.attrs.get("onclick", ""):
                el.attrs["onclick"] = "this.classList.toggle('zoom')"
            el.attrs.setdefault("alt", "")
            if el.attrs.get("src", "").startswith("/"):
                el.attrs["src"] = el.attrs["src"].lstrip("/")
        if JS_OBJ in el.attrs.get("onclick", ""):
            del el.attrs["onclick"]
        if "style" in el.attrs:
            st = _clean_style(el.attrs["style"])
            if st:
                el.attrs["style"] = st
            else:
                del el.attrs["style"]

    seen_chwd = False
    for el in list(root.iter()):
        cls = el.classes
        if "buttons" in cls:
            _convert_buttons(el)
        elif "expandable" in cls:
            _convert_expandable(el)
        elif "chwd" in cls:
            # homograph navigation (verb | noun ...) is repeated above every homograph; keep the first
            if seen_chwd:
                el.parent.children.remove(el)
            else:
                el.children = [c for c in el.children if not (isinstance(c, str) and c.strip() in ("|", ""))]
            seen_chwd = True


def _convert_buttons(btns):
    kids, out, i = btns.children, [], 0
    while i < len(kids):
        ch = kids[i]
        if isinstance(ch, El) and ch.has_class("popup-button"):
            j = i + 1
            while j < len(kids) and isinstance(kids[j], str) and not kids[j].strip():
                j += 1
            if j < len(kids) and isinstance(kids[j], El) and kids[j].has_class("at-link"):
                label = _norm(ch.text())
                det = El("details", {"class": f"pop pop-{POP_KINDS.get(label.lower(), 'other')}"}, btns)
                summ = El("summary", {"class": "popup-button", NO_LOOKUP: "1"}, det)
                summ.children = [label]
                body = kids[j]
                body.parent = det
                det.children = [summ, body]
                out.append(det)
                i = j + 1
                continue
        out.append(ch)
        i += 1
    btns.children = out
    if any(isinstance(c, El) for c in out):
        btns.attrs["d:priority"] = "2"


def _convert_expandable(exp):
    exp.attrs.pop("onclick", None)
    exp.children = [c for c in exp.children if not (isinstance(c, El) and c.has_class("arrow"))]
    while exp.children and isinstance(exp.children[0], str) and not exp.children[0].strip():
        exp.children.pop(0)
    parent = exp.parent
    first_el = next((c for c in parent.children if isinstance(c, El)), None)
    has_content = any(isinstance(c, El) and c.has_class("content") for c in parent.children)
    if first_el is exp and has_content and parent.tag == "span":
        parent.tag = "details"
        parent.add_class("x")
        exp.tag = "summary"
        exp.attrs[NO_LOOKUP] = "1"


def collect_index(root, key, eid):
    """Return [(value, title, anchor_id|None)] and assign anchor ids in the tree."""
    pos, infl, explicit, verb_table = set(), {}, set(), set()
    phrasals, derived, variants_head, variants_sense, idioms = [], [], [], [], []
    counters = {"pv": 0, "rn": 0, "lx": 0, "sn": 0}

    def new_id(el, kind):
        if "id" not in el.attrs:
            counters[kind] += 1
            el.attrs["id"] = f"{eid}_{kind}{counters[kind]}"
        return el.attrs["id"]

    for el in root.iter():
        cls = el.classes
        if not cls:
            continue
        if "verb_form" in cls:
            verb_table.update(split_forms(el.text()))
            continue
        if _in_popup(el):
            continue
        anc = list(el.ancestors())
        in_pv = any(a.has_class("phrvbentry") for a in anc)
        if "pos" in cls and el.parent is not None and el.parent.has_class("entryhead") and not in_pv:
            pos.add(_norm(el.text(skip=frozenset({"neutral"}))).lower())
        elif "inflections" in cls and not in_pv:
            for ch in el.iter():
                for c in ch.classes:
                    if c in INFL_CLASSES:
                        forms = split_forms(ch.text(skip=frozenset(LABEL_CLASSES)))
                        infl.setdefault(INFL_CLASSES[c], set()).update(forms)
                        if forms:
                            explicit.add(INFL_CLASSES[c])
        elif "phrvbentry" in cls:
            head = el.first("phrvbhwd")
            if head is not None:
                vs = phrase_variants(head.text(skip=frozenset({"object"})))
                if vs:
                    phrasals.append((vs, new_id(el, "pv")))
        elif "runon" in cls:
            d = el.first("deriv")
            if d is not None:
                words = [w for w in split_forms(d.text(skip=frozenset({"neutral"})).replace("—", " "))]
                if words:
                    derived.append((words, new_id(el, "rn")))
        elif ("orthvar" in cls or "lexvar" in cls):
            forms = split_forms(el.text(skip=frozenset(LABEL_CLASSES)))
            if any(a.has_class("entryhead") for a in anc):
                variants_head.extend(forms)
            else:
                sense = next((a for a in anc if a.has_class("sense")), None)
                if sense is not None and forms:
                    variants_sense.append((forms, new_id(sense, "sn")))
        elif "lexunit" in cls:
            vs = idiom_variants(el.text())
            if vs:
                idioms.append((vs, new_id(el, "lx")))

    hw = key
    words = hw.split(" ")
    gen, verb_forms = set(), set(verb_table)
    is_noun = "noun" in pos
    is_verb = "verb" in pos
    is_adj = "adjective" in pos
    if is_noun and "noun" not in explicit and SIMPLE_WORD.fullmatch(words[-1]):
        gen.update(" ".join(words[:-1] + [p]) for p in plural(words[-1]))
    if is_verb and len(words) == 1 and SIMPLE_WORD.fullmatch(hw) and not verb_table:
        vf = set()
        if "3sg" not in explicit:
            vf.update(third_sg(hw))
        if "past" not in explicit:
            vf.update(past(hw))
        if "ing" not in explicit:
            vf.update(ing(hw))
        gen.update(vf)
        verb_forms.update(vf)
    if is_verb:
        for k in ("past", "pp", "ing", "3sg"):
            verb_forms.update(infl.get(k, ()))
    if is_adj and len(words) == 1 and SIMPLE_WORD.fullmatch(hw) and "adj" not in explicit and _syllables(hw) == 1:
        gen.update(compare(hw))

    forms = set(gen) | verb_table
    for v in infl.values():
        forms |= v
    verb_forms.discard(hw)

    entries = [(hw, hw, None)]
    entries += [(f, f"{f} ({hw})", None) for f in sorted(forms)]
    entries += [(v, f"{v} ({hw})", None) for v in variants_head]
    for vs, anchor in variants_sense:
        entries += [(v, v, anchor) for v in vs]
    for vs, anchor in phrasals:
        for p in vs:
            entries.append((p, p, anchor))
            first, _, rest = p.partition(" ")
            if rest and first.lower() == hw.lower():
                entries += [(f"{f} {rest}", f"{f} {rest} ({p})", anchor) for f in sorted(verb_forms)]
    for ws, anchor in derived:
        entries += [(w, w, anchor) for w in ws]
    for vs, anchor in idioms:
        entries += [(v, v, anchor) for v in vs]

    seen, out = set(), []
    for value, title, anchor in entries:
        k = value.lower()
        if value and k not in seen:
            seen.add(k)
            out.append((value, title, anchor))
    return out


def entry_xml(key, html_text, ctx):
    eid = ctx.ids[key]
    root = parse_html(html_text)
    transform(root, eid, ctx)
    special = key == FRONT_KEY or key.startswith(ABOUT_PREFIX)
    index = [] if special else collect_index(root, key, eid)
    for el in root.iter():  # the about page has literal "&nbsp;" text
        el.children = [c.replace("&nbsp;", "\xa0") if isinstance(c, str) else c for c in el.children]
    body = []
    serialize(root, body)
    title = "Pronunciation key" if key.startswith(ABOUT_PREFIX) else key
    parts = [f'<d:entry id="{eid}" d:title="{_esc_attr(title)}">']
    for value, title, anchor in index:
        a = f' d:anchor="xpointer(//*[@id=\'{anchor}\'])"' if anchor else ""
        parts.append(f'<d:index d:value="{_esc_attr(value)}" d:title="{_esc_attr(title)}"{a}/>')
    ctx.stats["index"] += len(index)
    return "".join(parts) + "".join(body) + "</d:entry>\n", body


FRONT_TEMPLATE = """<d:entry id="front_back_matter" d:title="{title}"><div class="fbm">
<div class="fbm-head"><img class="fbm-cover" src="cover.jpg" alt=""/><div>
<h1>{title}</h1><p class="fbm-sub">3rd edition · for the macOS Dictionary app</p>
<p><a href="x-dictionary:r:pron_key">Pronunciation key</a></p></div></div>
<h2>Tips</h2><ul>
<li>Click a speaker icon to hear the headword or an example sentence (in the Dictionary app window).</li>
<li><b>Etymology</b>, <b>Verb Table</b>, <b>Collocations</b> and <b>Thesaurus</b> buttons open in place; items marked ▸ expand.</li>
<li>Inflected forms (<i>ran, studies, bigger</i>), phrasal verbs (<i>account for, gave up</i>), derived words (<i>angrily</i>), spelling variants and idioms are all searchable, and work with Look Up (force click / three-finger tap).</li>
<li>Display options: Dictionary › Settings, then select this dictionary.</li>
</ul>
<h2>Source</h2><p>Converted from the LAAD3 MDict edition (pdawiki.com, 2016): 48,222 headwords, 76,370 entries,
American English audio for headwords and example sentences. For personal use only.</p></div></d:entry>
"""


# ----------------------------------------------------------------------------- project files
def write_css(out_dir):
    with open(os.path.join(ASSETS, "LAAD3.css"), encoding="utf-8") as f:
        base = f.read()
    with open(os.path.join(ASSETS, "LAAD3-dark.css"), encoding="utf-8") as f:
        dark = f.read()
    css = (base + "\n/* ---------- dark appearance ---------- */\n"
           f"@media (prefers-color-scheme: dark) {{\n{dark}}}\n"
           f"@media (prefers-dark-interface) {{\n{dark}}}\n")
    with open(os.path.join(out_dir, f"{DICT_NAME}.css"), "w", encoding="utf-8") as f:
        f.write(css)


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key><string>English</string>
	<key>CFBundleIdentifier</key><string>local.dictionary.LAAD3</string>
	<key>CFBundleName</key><string>LAAD3</string>
	<key>CFBundleDisplayName</key><string>{display}</string>
	<key>CFBundleShortVersionString</key><string>3.0.1</string>
	<key>DCSDictionaryCopyright</key><string>Longman Advanced American Dictionary, 3rd edition. © Pearson Education Limited. For personal use only.</string>
	<key>DCSDictionaryManufacturerName</key><string>Pearson Longman</string>
	<key>DCSDictionaryFrontMatterReferenceID</key><string>front_back_matter</string>
	<key>DCSDictionaryPrefsHTML</key><string>LAAD3_prefs.html</string>
	<key>DCSDictionaryXSL</key><string>LAAD3.xsl</string>
	<key>DCSDictionaryDefaultPrefs</key>
	<dict>
		<key>version</key><string>1</string>
		<key>display-expand</key><string>0</string>
		<key>display-exaaudio</key><string>1</string>
		<key>display-pictures</key><string>1</string>
	</dict>
	<key>DCSDictionaryUseSystemAppearance</key><true/>
	<key>DCSDictionaryLanguages</key>
	<array>
		<dict>
			<key>DCSDictionaryDescriptionLanguage</key><string>en_US</string>
			<key>DCSDictionaryIndexLanguage</key><string>en_US</string>
		</dict>
	</array>
</dict>
</plist>
"""

MAKEFILE = """# Generated by build_laad3.py
DICT_NAME		=	"{name}"
DICT_SRC_PATH		=	{name}.xml
CSS_PATH		=	{name}.css
PLIST_PATH		=	{name}.plist
DICT_BUILD_OPTS		=	-v 10.11
DICT_BUILD_TOOL_DIR	=	"{ddk}"
DICT_BUILD_TOOL_BIN	=	"$(DICT_BUILD_TOOL_DIR)/bin"
DICT_DEV_KIT_OBJ_DIR	=	./objects
export	DICT_DEV_KIT_OBJ_DIR
DESTINATION_FOLDER	=	~/Library/Dictionaries
RM			=	/bin/rm

all:
	"$(DICT_BUILD_TOOL_BIN)/build_dict.sh" $(DICT_BUILD_OPTS) $(DICT_NAME) $(DICT_SRC_PATH) $(CSS_PATH) $(PLIST_PATH)
	echo "Done."

install:
	echo "Installing into $(DESTINATION_FOLDER)".
	mkdir -p $(DESTINATION_FOLDER)
	$(RM) -rf $(DESTINATION_FOLDER)/$(DICT_NAME).dictionary
	ditto --noextattr --norsrc $(DICT_DEV_KIT_OBJ_DIR)/$(DICT_NAME).dictionary  $(DESTINATION_FOLDER)/$(DICT_NAME).dictionary
	touch $(DESTINATION_FOLDER)
	echo "Done. Restart Dictionary.app to pick up the new build."

clean:
	$(RM) -rf $(DICT_DEV_KIT_OBJ_DIR)
"""


def write_project_files(out_dir):
    write_css(out_dir)
    with open(os.path.join(out_dir, f"{DICT_NAME}.plist"), "w", encoding="utf-8") as f:
        f.write(PLIST.format(display=DISPLAY_NAME))
    with open(os.path.join(out_dir, "Makefile"), "w", encoding="utf-8") as f:
        f.write(MAKEFILE.format(name=DICT_NAME, ddk=DDK_DIR))
    res = os.path.join(out_dir, "OtherResources")
    os.makedirs(res, exist_ok=True)
    for name in ("LAAD3.xsl", "LAAD3_prefs.html"):
        shutil.copyfile(os.path.join(ASSETS, name), os.path.join(res, name))
    shutil.copytree(os.path.join(ASSETS, "icons"), os.path.join(res, "icons"), dirs_exist_ok=True)


def extract_resources(mdd_path, out_dir):
    res = os.path.join(out_dir, "OtherResources")
    n = size = 0
    made = set()
    for key, data in MDict(mdd_path).items():
        name = key.replace("\\", "/").lstrip("/")
        if not name or name.endswith(".7z") or ".." in name.split("/"):
            continue
        dst = os.path.join(res, name)
        d = os.path.dirname(dst)
        if d not in made:
            os.makedirs(d, exist_ok=True)
            made.add(d)
        with open(dst, "wb") as f:
            f.write(data)
        n += 1
        size += len(data)
    return n, size


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-resources", action="store_true")
    args = ap.parse_args()

    mdx_path = os.path.join(args.src_dir, MDX_BASENAME + ".mdx")
    mdd_path = os.path.join(args.src_dir, MDX_BASENAME + ".mdd")
    os.makedirs(args.out_dir, exist_ok=True)
    t0 = time.time()

    mdx = MDict(mdx_path)
    records = [(k, v.decode("utf-8")) for k, v in mdx.items()]
    ctx = Context([k for k, _ in records])
    by_key = dict(records)
    if args.limit:
        keep = {k for k, _ in records[:args.limit]} | {FRONT_KEY} | {k for k in by_key if k.startswith(ABOUT_PREFIX)}
        records = [(k, v) for k, v in records if k in keep]
    print(f"read {len(records)} MDX records in {time.time() - t0:.1f}s")

    xml_path = os.path.join(args.out_dir, f"{DICT_NAME}.xml")
    with open(xml_path, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                  '<d:dictionary xmlns="http://www.w3.org/1999/xhtml" '
                  'xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng">\n')
        for i, (key, html_text) in enumerate(records, 1):
            if key == FRONT_KEY:  # replaced by FRONT_TEMPLATE
                continue
            xml, _body = entry_xml(key, html_text, ctx)
            out.write(xml)
            if i % 10000 == 0:
                print(f"  {i} entries, {ctx.stats['index']} index keys, {time.time() - t0:.0f}s")
        out.write(FRONT_TEMPLATE.format(title=DISPLAY_NAME).replace("\n", " "))
        out.write("\n</d:dictionary>\n")
    print(f"wrote {xml_path}: {len(records)} entries, stats {ctx.stats}, {time.time() - t0:.0f}s")

    write_project_files(args.out_dir)
    if not args.skip_resources:
        n, size = extract_resources(mdd_path, args.out_dir)
        print(f"extracted {n} resources ({size / 1e6:.0f} MB), {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

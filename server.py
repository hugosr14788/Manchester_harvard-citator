#!/usr/bin/env python3
"""Manchester Harvard Citator - local metadata server.

Serves the UI (index.html) and a small JSON API:
    GET /api/cite?url=<link, DOI or ISBN>

It fetches the page (or a scholarly metadata service when one applies),
extracts everything needed for a Harvard Manchester reference and returns
normalised fields. Formatting happens in the browser so edits re-render live.

Standard library only. Run:  python3 server.py
"""

import datetime as dt
import gzip
import html
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Hosting platforms (Render, Railway, Fly…) set $PORT. That switches on "public mode": listen on all
# interfaces, refuse links to private/internal addresses, and rate-limit each visitor.
PUBLIC = bool(os.environ.get("PORT"))
PORT = int(os.environ.get("PORT") or os.environ.get("CITATOR_PORT", "8765"))

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
# Link-preview bots are whitelisted by many sites that block ordinary scrapers.
PREVIEW_UAS = ["facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
               "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"]
API_UA = "ManchesterHarvardCitator/1.0 (personal referencing tool; mailto:citator@localhost)"

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 15


# --------------------------------------------------------------------------
# HTTP fetching
# --------------------------------------------------------------------------

def _ssl_context():
    try:
        import certifi  # noqa: WPS433
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


class FetchResult:
    def __init__(self, status, url, ctype, body):
        self.status = status
        self.url = url
        self.ctype = (ctype or "").lower()
        self.body = body or b""

    @property
    def ok(self):
        return 200 <= self.status < 300 and bool(self.body)

    def text(self):
        charset = None
        m = re.search(r"charset=([\w-]+)", self.ctype)
        if m:
            charset = m.group(1)
        if not charset:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", self.body[:4096], re.I)
            if m:
                charset = m.group(1).decode("ascii", "ignore")
        for enc in (charset, "utf-8", "cp1252", "latin-1"):
            if not enc:
                continue
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.text())
        except ValueError:
            return None


def _decompress(body, encoding):
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        pass
    return body


class UnsafeURL(Exception):
    pass


def check_public_url(url):
    """In public mode, only fetch ordinary public web addresses (no internal hosts, odd ports)."""
    if not PUBLIC:
        return
    import ipaddress
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL(url)
    if p.port not in (None, 80, 443):
        raise UnsafeURL(url)
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        return  # unresolvable: the fetch itself will fail
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(url)


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_urllib(url, headers):
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(), SafeRedirects(),
                                         urllib.request.HTTPSHandler(context=SSL_CTX))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_BYTES)
            body = _decompress(body, r.headers.get("Content-Encoding"))
            return FetchResult(r.status, r.geturl(), r.headers.get("Content-Type"), body)
    except urllib.error.HTTPError as e:
        try:
            body = _decompress(e.read(MAX_BYTES), e.headers.get("Content-Encoding"))
        except Exception:
            body = b""
        return FetchResult(e.code, url, e.headers.get("Content-Type") if e.headers else "", body)


def _fetch_curl(url, headers):
    curl = shutil.which("curl")
    if not curl:
        return None
    marker = "\n__CITATOR__"
    cmd = [curl, "-sSL", "--compressed", "-c", "/dev/null", "--max-time", str(TIMEOUT), "--max-filesize", str(MAX_BYTES),
           "-w", marker + "%{http_code}|%{url_effective}|%{content_type}"]
    for k, v in headers.items():
        if k.lower() != "accept-encoding":
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 5).stdout
    except Exception:
        return None
    idx = out.rfind(marker.encode())
    if idx < 0:
        return None
    body, tail = out[:idx], out[idx + len(marker):].decode("utf-8", "replace")
    parts = tail.split("|", 2)
    try:
        status = int(parts[0])
    except ValueError:
        status = 0
    return FetchResult(status, parts[1] if len(parts) > 1 else url, parts[2] if len(parts) > 2 else "", body)


def fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", ua=BROWSER_UA, extra=None):
    headers = {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        # Skip EU/UK cookie walls (Google/YouTube and others).
        "Cookie": "CONSENT=YES+cb; SOCS=CAI",
    }
    if extra:
        headers.update(extra)
    res = None
    try:
        check_public_url(url)
        res = _fetch_urllib(url, headers)
    except UnsafeURL:
        return FetchResult(0, url, "", b"")
    except Exception:
        res = None
    # curl can't vet redirect targets, so it's only used when running privately
    if not PUBLIC and (res is None or (not res.ok and res.status in (0, 403, 429, 503))):
        alt = _fetch_curl(url, headers)
        if alt is not None and (res is None or alt.ok):
            res = alt
    return res or FetchResult(0, url, "", b"")


def fetch_json(url, accept="application/json", ua=API_UA):
    res = fetch(url, accept=accept, ua=ua)
    return res.json() if res.ok else None


# --------------------------------------------------------------------------
# HTML metadata extraction
# --------------------------------------------------------------------------

class MetaParser(HTMLParser):
    """Collects <meta>, <title>, JSON-LD, <time>, canonical link and body text."""

    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer", "header"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}           # key(lowercase) -> [values]
        self.title = ""
        self.jsonld = []
        self.times = []
        self.canonical = ""
        self.lang = ""
        self.rel_authors = []
        self.h1 = ""
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf = []
        self._skip_depth = 0
        self._in_rel_author = False
        self._in_h1 = False
        self._text = []
        self._text_len = 0

    def _add(self, key, value):
        if key and value is not None:
            value = html.unescape(value).strip()
            if value:
                self.meta.setdefault(key.strip().lower(), []).append(value)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            content = a.get("content")
            for k in ("name", "property", "itemprop", "http-equiv"):
                if a.get(k):
                    self._add(a[k], content)
            if a.get("charset"):
                return
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if rel == "canonical" and a.get("href"):
                self.canonical = a["href"]
        elif tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "time":
            if a.get("datetime"):
                self.times.append(a["datetime"])
        elif tag == "a" and "author" in a.get("rel", "").lower().split():
            self._in_rel_author = True
            self.rel_authors.append("")
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_buf).strip()
            try:
                self.jsonld.append(json.loads(raw))
            except ValueError:
                try:  # some sites put several objects or trailing commas
                    self.jsonld.append(json.loads(re.sub(r",\s*([}\]])", r"\1", raw)))
                except ValueError:
                    pass
        elif tag == "a":
            self._in_rel_author = False
        elif tag == "h1":
            self._in_h1 = False
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_rel_author and self.rel_authors:
            self.rel_authors[-1] += data
        if self._in_h1:
            self.h1 += data
        if not self._skip_depth and self._text_len < 400_000:
            self._text.append(data)
            self._text_len += len(data)

    @property
    def body_text(self):
        return re.sub(r"\s+", " ", " ".join(self._text))

    def get(self, *keys):
        for k in keys:
            v = self.meta.get(k.lower())
            if v:
                return v[0]
        return ""

    def get_all(self, *keys):
        out = []
        for k in keys:
            out.extend(self.meta.get(k.lower(), []))
        return out


def parse_html(text):
    p = MetaParser()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    return p


def jsonld_nodes(blobs):
    """Flatten JSON-LD into a list of dict nodes."""
    out = []

    def walk(x):
        if isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            out.append(x)
            if "@graph" in x:
                walk(x["@graph"])
            for k in ("mainEntity", "mainEntityOfPage", "itemReviewed"):
                if isinstance(x.get(k), (dict, list)):
                    walk(x[k])

    walk(blobs)
    return out


def node_types(node):
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return [str(i).split("/")[-1] for i in t if i]


ARTICLE_TYPES = {"NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle", "OpinionNewsArticle",
                 "BackgroundNewsArticle", "ReviewNewsArticle", "LiveBlogPosting", "Article", "BlogPosting",
                 "SocialMediaPosting", "ScholarlyArticle", "TechArticle", "Report", "MedicalScholarlyArticle",
                 "PodcastEpisode", "VideoObject", "Book", "Chapter", "WebPage", "Dataset"}
NEWS_TYPES = {"NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle", "OpinionNewsArticle",
              "BackgroundNewsArticle", "ReviewNewsArticle", "LiveBlogPosting"}


def pick_main_node(nodes):
    priority = ["NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle", "OpinionNewsArticle",
                "BackgroundNewsArticle", "ReviewNewsArticle", "LiveBlogPosting", "ScholarlyArticle",
                "BlogPosting", "Report", "TechArticle", "Article", "PodcastEpisode", "VideoObject",
                "Book", "Chapter", "SocialMediaPosting"]
    for p in priority:
        for n in nodes:
            if p in node_types(n) and (n.get("headline") or n.get("name")):
                return n
    for n in nodes:
        if any(t.endswith("WebPage") for t in node_types(n)):
            return n
    return None


def ld_text(v):
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or ""
    return html.unescape(str(v or "")).strip()


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

ORG_WORDS = re.compile(
    r"\b(news|ltd|limited|inc|llc|llp|plc|group|team|staff|editors?|editorial|desk|university|college|"
    r"institute|institution|agency|association|society|council|department|ministry|office|service|"
    r"services|foundation|trust|bank|press|media|corporation|corp|company|co\.|organi[sz]ation|"
    r"centre|center|committee|commission|government|nhs|bbc|reuters|bloomberg|associated|board|"
    r"authority|federation|union|network|library|museum|school|hospital|partners|international|"
    r"global|world|national|official|admin|contributors?|writers?|reporters?|correspondents?|"
    r"magazine|journal|times|post|guardian|telegraph|daily|weekly|radio|tv|television|channel|"
    r"studio|studios|labs?|systems|technologies|solutions|consulting|research|wikipedia|gov|"
    r"parliament|commons|lords|police|army|navy|club|fc|party|church|cathedral)\b", re.I)

PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "das", "do", "dos", "di", "du",
             "la", "le", "st", "st.", "bin", "ibn", "al", "el", "ter", "ten", "zu", "y"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "md", "obe", "mbe", "cbe", "frs"}
TITLES = {"dr", "dr.", "prof", "prof.", "professor", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "sir",
          "dame", "lord", "lady", "rev", "rev.", "dr", "drs"}


def clean_author_string(s):
    s = html.unescape(str(s or "")).strip()
    s = re.sub(r"^\s*(written\s+)?by[:\s]+", "", s, flags=re.I)
    m = re.match(r"^(.+?)\s+(?:for|in|at)\s+(?:the\s+)?[A-Z][\w .&'-]+$", s)
    if m and looks_like_person(m.group(1)):  # "Jane Doe for The Guardian"
        s = m.group(1)
    s = re.sub(r"\s*[|•·]\s*.*$", "", s)
    s = re.sub(r"\s*\(.*?\)\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;-–—")
    return s


def looks_like_person(name):
    if not name or ORG_WORDS.search(name) or re.search(r"[\d@/]", name):
        return False
    toks = [t for t in re.split(r"\s+", name.replace(",", " ")) if t]
    if not 2 <= len(toks) <= 5:
        return False
    for t in toks:
        tl = t.lower()
        if tl in PARTICLES or tl in SUFFIXES or tl in TITLES:
            continue
        if not (t[0].isupper() or (len(t) > 1 and t[0] in "'’" )):
            return False
    return True


def split_person(name):
    """'Mary-Jane van der Berg' -> {given:'Mary-Jane', family:'van der Berg'}; 'Smith, John' too."""
    name = re.sub(r"\s+", " ", name).strip()
    if "," in name:
        fam, _, giv = name.partition(",")
        giv_toks = [t for t in giv.split() if t.lower() not in SUFFIXES]
        return {"given": " ".join(giv_toks).strip(), "family": fam.strip()}
    toks = [t for t in name.split() if t.lower().strip(".") not in {s.strip(".") for s in SUFFIXES}
            and t.lower() not in TITLES]
    if len(toks) == 1:
        return {"given": "", "family": toks[0]}
    i = len(toks) - 1
    while i - 1 > 0 and toks[i - 1].lower() in PARTICLES:
        i -= 1
    return {"given": " ".join(toks[:i]), "family": " ".join(toks[i:])}


def name_to_author(name, force=None):
    name = clean_author_string(name)
    if not name or re.match(r"https?://", name) or "@" in name and " " not in name:
        return None
    if force == "org":
        return {"org": name}
    if force == "person" or looks_like_person(name):
        return split_person(name)
    # "Smith, J." or "Smith, John"
    if re.match(r"^[A-Z][\w'’-]+,\s*[A-Z]", name) and len(name.split()) <= 4 and not ORG_WORDS.search(name):
        return split_person(name)
    return {"org": name}


def split_author_list(s):
    s = clean_author_string(s)
    if not s:
        return []
    parts = re.split(r"\s+(?:and|&)\s+|;\s*", s)
    out = []
    for p in parts:
        # "A Smith, B Jones" (each part has 2+ words) vs "Smith, John" (surname-first)
        sub = [x.strip() for x in p.split(",") if x.strip()]
        if len(sub) > 1 and all(len(x.split()) >= 2 for x in sub):
            out.extend(sub)
        else:
            out.append(p.strip())
    return [x for x in out if x]


def authors_from_ld(v):
    out = []
    items = v if isinstance(v, list) else [v]
    for it in items:
        if isinstance(it, dict):
            types = node_types(it)
            nm = ld_text(it.get("name")) or " ".join(
                x for x in [ld_text(it.get("givenName")), ld_text(it.get("familyName"))] if x)
            if not nm:
                continue
            if "Organization" in types or any(t.endswith("Organization") for t in types):
                out.append({"org": clean_author_string(nm)})
            elif it.get("familyName"):
                out.append({"given": ld_text(it.get("givenName")), "family": ld_text(it.get("familyName"))})
            else:
                for n in split_author_list(nm):
                    a = name_to_author(n, "person" if "Person" in types and looks_like_person(n) else None)
                    if a:
                        out.append(a)
        elif isinstance(it, str):
            for n in split_author_list(it):
                a = name_to_author(n)
                if a:
                    out.append(a)
    return out


def dedupe_authors(authors):
    seen, out = set(), []
    for a in authors:
        key = (a.get("org") or (a.get("family", "") + "|" + a.get("given", "")[:1])).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(a)
    return out


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


def parse_date(s):
    """Return 'YYYY-MM-DD', 'YYYY-MM', 'YYYY' or ''."""
    if not s:
        return ""
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\D|$)", s)
    if m:
        y, mo, d = map(int, m.groups())
        if 1900 < y < 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    low = s.lower()
    # 25 January 2023 / 25 Jan, 2023
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+(\d{4})", low)
    if m and m.group(2) in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    # January 25, 2023
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", low)
    if m and m.group(1) in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{4})", low)
    if m and m.group(1) in MONTHS:
        return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}"
    # RFC 2822
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d:
            return d.strftime("%Y-%m-%d")
    except Exception:
        pass
    m = re.match(r"^(\d{4})$", s)
    if m and 1400 < int(m.group(1)) < 2100:
        return m.group(1)
    return ""


def csl_date(d):
    if not isinstance(d, dict):
        return ""
    parts = (d.get("date-parts") or [[]])[0] or []
    parts = [p for p in parts if p not in (None, "")]
    if not parts:
        return parse_date(d.get("raw") or d.get("literal") or "")
    try:
        parts = [int(p) for p in parts]
    except (TypeError, ValueError):
        return ""
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}"


def snowflake_time(sid, epoch_ms, shift):
    try:
        ms = (int(sid) >> shift) + epoch_ms
        return dt.datetime.fromtimestamp(ms / 1000)
    except (ValueError, OverflowError, OSError):
        return None


def social_time(d):
    """Matches the guide's example: '11:28 pm · 12 Jun 2023' (viewer's local time, as X shows it)."""
    hour = d.hour % 12 or 12
    return f"{hour}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'} · {d.day} {d.strftime('%b')} {d.year}"


# --------------------------------------------------------------------------
# Titles and sentence case
# --------------------------------------------------------------------------

_DICT_LOWER, _DICT_CAP = set(), set()


def _load_dict():
    path = "/usr/share/dict/words"
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for w in f:
            w = w.strip()
            if not w:
                continue
            (_DICT_CAP if w[0].isupper() else _DICT_LOWER).add(w.lower())


_load_dict()

ALWAYS_PROPER = {"i", "uk", "us", "usa", "eu", "un", "nhs", "ai", "covid", "covid-19", "english", "british",
                 "european", "american", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                 "sunday", "twitter", "tiktok", "instagram", "youtube", "linkedin", "whatsapp", "snapchat",
                 "reddit", "netflix", "spotify", "uber", "airbnb", "tesla", "nvidia", "microsoft", "google",
                 "amazon", "facebook", "chatgpt", "openai", "brexit", "january", "february", "march", "april", "june", "july", "august", "september",
                 "october", "november", "december", "christmas", "easter", "god", "internet"}
ALWAYS_PROPER -= {"march", "may", "internet"}  # ambiguous: decided by evidence


def sentence_case(title, body="", protect=frozenset()):
    """Convert a Title Case title to sentence case, keeping proper nouns.

    Proper-noun evidence: how the word is written elsewhere on the page,
    then the system dictionary. Titles that are already in sentence case are
    returned unchanged.
    """
    if not title:
        return title
    tokens = re.split(r"(\s+)", title)
    words = [t for t in tokens if t.strip()]
    cand = [w for w in words[1:] if re.match(r"^[\"'‘“(\[]?[A-Za-z]", w) and len(re.sub(r"\W", "", w)) >= 4]
    if not cand:
        return title
    caps = sum(1 for w in cand if re.sub(r"^\W+", "", w)[:1].isupper())
    if caps / len(cand) < 0.6:
        return title  # already sentence case (or mostly proper nouns) - leave it

    body = (body or "").replace(title, " ")
    out = []
    start = True
    prev_kept = ""
    for tok in tokens:
        if not tok.strip():
            out.append(tok)
            continue
        if start:
            new = tok
        else:
            new = tok if re.sub(r"^\W+|\W+$", "", tok) in protect else "-".join(_sc_word(p, body) for p in tok.split("-"))
            core = re.sub(r"^\W+|\W+$", "", tok)
            if (new != tok and prev_kept and core[:1].isupper()
                    and (re.search(re.escape(prev_kept) + r"\s+" + re.escape(core) + r"\b", body)
                         or (len(body) < 2000 and not _in_dict_lower(prev_kept.lower()) and not prev_kept.isupper()
                             and core.lower() not in SMALL_WORDS and core.lower() not in STOPWORDS))):
                new = tok  # second half of a name whose first half is a proper noun
        word_core = re.sub(r"^\W+|\W+$", "", new)
        prev_kept = word_core if (word_core[:1].isupper() and not start and not re.search(r"\W$", tok)) else (
            word_core if start and word_core[:1].isupper() and not _in_dict_lower(word_core.lower()) else "")
        out.append(new)
        # Subtitles after a colon stay lower case (guide: "only proper nouns should be capitalised");
        # a new sentence after ? or ! keeps its capital.
        start = bool(re.search(r"[?!.]$", tok)) and not re.search(r"\b[A-Z]\.$", tok)
    return "".join(out)


def _sc_word(w, body):
    m = re.match(r"^(\W*)([A-Za-z][\w'’]*)(.*)$", w, re.S)
    if not m:
        return w
    pre, core, post = m.groups()
    if not core[0].isupper():
        return w
    rest = core[1:]
    if any(c.isupper() for c in rest) or any(c.isdigit() for c in core):
        return w  # acronyms, CamelCase, model numbers
    base = re.sub(r"['’]s$", "", core)
    lw = base.lower()
    if lw in ALWAYS_PROPER:
        return w
    common = _in_dict_lower(lw)
    if body:
        lower_hits = len(re.findall(r"(?<![\w'’])" + re.escape(lw) + r"(?![\w])", body))
        strict, loose = _capitalised_mid_sentence(base, body)
        if lower_hits:
            # Seen in lower case in the text: a common word, unless it's overwhelmingly capitalised
            return w if strict >= 5 * lower_hits else pre + core.lower() + post
        if strict >= (2 if common else 1) or (loose >= 2 and not common):
            return w
    if common:
        return pre + core.lower() + post
    if lw in _DICT_CAP:
        return w
    return pre + core.lower() + post if len(lw) <= 3 else w


STOPWORDS = {"is", "are", "was", "were", "be", "been", "has", "have", "had", "do", "does", "did", "will", "can",
             "could", "should", "would", "may", "might", "must", "not", "no", "this", "that", "these", "those",
             "how", "why", "what", "when", "where", "who", "which", "says", "said", "after", "before", "about"}
SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of",
               "off", "on", "per", "to", "up", "via", "with", "from", "into", "over", "than"}


def _capitalised_mid_sentence(word, body):
    """Count uses of `word` capitalised mid-sentence.

    strict: inside running prose (previous word lower case, next word not capitalised);
    loose: any non-sentence-initial use (includes Title Case headings and menus).
    """
    n = loose = 0
    for i, m in enumerate(re.finditer(r"(\S+) " + re.escape(word) + r"(?![\w])([^\w\s]*)(\s+\S+)?", body)):
        if i > 400:
            break
        if re.search(r"[.!?:]$", m.group(1)):
            continue
        loose += 1
        prev = re.sub(r"\W+$", "", m.group(1))
        nxt = (m.group(3) or "").strip()
        if prev and prev.islower() and not (nxt[:1].isupper() and not m.group(2).strip()):
            n += 1
    return n, loose


MODERN_WORDS = set("""desktop online website websites email emails internet smartphone smartphones software hardware
blog blogs blogging podcast podcasts app apps download downloads upload uploads cyber cybersecurity startup startups
ecommerce livestream livestreaming streaming crypto cryptocurrency cryptocurrencies blockchain metaverse lockdown
lockdowns influencer influencers selfie selfies hashtag hashtags emoji wellbeing healthcare workforce workplace
workplaces outsourcing offshoring broadband wifi laptop laptops chatbot chatbots algorithm algorithms dataset datasets
analytics fintech globalisation globalization digitalisation digitalization digital misinformation disinformation
deepfake deepfakes gaming esports upskilling reskilling onboarding hybrid microplastics biodiversity sustainability
decarbonisation decarbonization renewables offshore onshore pandemic pandemics vaccine vaccines vaccination
coronavirus inflation recession stakeholders stakeholder benchmarking multinational multinationals entrepreneurship
microfinance cloud cyberattack cyberattacks ransomware malware spyware smartwatch wearable wearables gig
subscription subscriptions netflix-style e-commerce e-book e-books ebook ebooks online-only touchscreen database
databases website-based tweet tweets retweet livestreams vlog vlogs vlogger youtuber youtubers""".split())


def _in_dict_lower(lw):
    if lw in _DICT_LOWER or lw in MODERN_WORDS:
        return True
    for suf, add in (("ies", "y"), ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("es", ""), ("s", ""),
                     ("er", ""), ("est", ""), ("ly", ""), ("'s", "")):
        if lw.endswith(suf) and len(lw) - len(suf) >= 3:
            stem = lw[: -len(suf)] + add
            if stem in _DICT_LOWER:
                return True
            if suf in ("ing", "ed") and len(stem) > 3 and stem[-1] == stem[-2] and stem[:-1] in _DICT_LOWER:
                return True  # getting -> get
    return False


def clean_title(title, site=""):
    t = html.unescape(re.sub(r"\s+", " ", title or "")).strip()
    if not t:
        return t
    t = _strip_site_suffix(t, site)
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 6 and sum(c.isupper() for c in letters) / len(letters) > 0.9:
        # SHOUTY TITLE -> Title Case (short all-caps tokens like UK/NHS stay as they are)
        t = " ".join(w if (len(re.sub(r"\W", "", w)) <= 3 and w.isupper() and w.lower() not in
                           {"the", "and", "for", "of", "a", "an", "to", "in", "on", "at", "by", "or"})
                     else w.capitalize() for w in t.split(" "))
    return t


def _strip_site_suffix(t, site):
    seps = r"\s+[|–—·•:-]\s+|\s+[|]\s*|\s*[|]\s+"
    parts = re.split(f"({seps})", t)
    if len(parts) >= 3:
        last = parts[-1].strip()
        site_l = (site or "").lower()
        if (site_l and (last.lower() in site_l or site_l in last.lower())) or (
                len(last) <= 30 and re.search(r"\.(com|org|net|co\.uk|ac\.uk|gov\.uk|io)$", last.lower())) or (
                re.search(r"[|]", parts[-2]) and len(last) <= 40):
            t = "".join(parts[:-2]).strip()
            return _strip_site_suffix(t, site) if re.search(r"[|]", t) and site_l and site_l in t.lower() else t
    return t


# --------------------------------------------------------------------------
# Site names
# --------------------------------------------------------------------------

KNOWN_SITES = {
    "bbc.co.uk": "BBC News", "bbc.com": "BBC News", "theguardian.com": "The Guardian",
    "nytimes.com": "The New York Times", "ft.com": "Financial Times", "economist.com": "The Economist",
    "reuters.com": "Reuters", "independent.co.uk": "The Independent", "telegraph.co.uk": "The Telegraph",
    "thetimes.co.uk": "The Times", "thetimes.com": "The Times", "washingtonpost.com": "The Washington Post",
    "wsj.com": "The Wall Street Journal", "bloomberg.com": "Bloomberg", "cnn.com": "CNN",
    "manchestereveningnews.co.uk": "Manchester Evening News", "dailymail.co.uk": "Daily Mail",
    "mirror.co.uk": "Daily Mirror", "thesun.co.uk": "The Sun", "standard.co.uk": "Evening Standard",
    "sky.com": "Sky News", "news.sky.com": "Sky News", "aljazeera.com": "Al Jazeera",
    "apnews.com": "AP News", "forbes.com": "Forbes", "cnbc.com": "CNBC", "politico.eu": "Politico",
    "politico.com": "Politico", "newstatesman.com": "New Statesman", "spectator.co.uk": "The Spectator",
    "theatlantic.com": "The Atlantic", "newyorker.com": "The New Yorker", "hbr.org": "Harvard Business Review",
    "gov.uk": "GOV.UK", "nhs.uk": "NHS", "who.int": "World Health Organization",
    "manchester.ac.uk": "The University of Manchester", "ons.gov.uk": "Office for National Statistics",
    "lemonde.fr": "Le Monde", "elpais.com": "El País", "scotsman.com": "The Scotsman",
    "irishtimes.com": "The Irish Times", "time.com": "Time", "theconversation.com": "The Conversation",
    "businessinsider.com": "Business Insider", "insider.com": "Business Insider", "vox.com": "Vox",
    "axios.com": "Axios", "npr.org": "NPR", "usatoday.com": "USA Today", "latimes.com": "Los Angeles Times",
    "huffpost.com": "HuffPost", "metro.co.uk": "Metro", "express.co.uk": "Daily Express",
    "inews.co.uk": "The i Paper", "channel4.com": "Channel 4 News", "itv.com": "ITV News",
    "un.org": "United Nations", "worldbank.org": "World Bank", "imf.org": "International Monetary Fund",
    "oecd.org": "OECD", "parliament.uk": "UK Parliament", "nice.org.uk": "National Institute for Health and Care Excellence",
    "bankofengland.co.uk": "Bank of England", "ifs.org.uk": "Institute for Fiscal Studies", "statista.com": "Statista",
    "mckinsey.com": "McKinsey & Company", "deloitte.com": "Deloitte", "pwc.com": "PwC", "pwc.co.uk": "PwC",
    "ox.ac.uk": "University of Oxford", "cam.ac.uk": "University of Cambridge", "ucl.ac.uk": "UCL",
    "lse.ac.uk": "London School of Economics", "unesco.org": "UNESCO", "unicef.org": "UNICEF",
    "europa.eu": "European Union", "whitehouse.gov": "The White House", "cdc.gov": "Centers for Disease Control and Prevention",
    "nature.com": "Nature", "investopedia.com": "Investopedia", "britannica.com": "Encyclopaedia Britannica",
}
NEWS_DOMAINS = {
    "bbc.co.uk", "bbc.com", "theguardian.com", "nytimes.com", "ft.com", "economist.com", "reuters.com",
    "independent.co.uk", "telegraph.co.uk", "thetimes.co.uk", "thetimes.com", "washingtonpost.com", "wsj.com",
    "bloomberg.com", "cnn.com", "manchestereveningnews.co.uk", "dailymail.co.uk", "mirror.co.uk", "thesun.co.uk",
    "standard.co.uk", "sky.com", "news.sky.com", "aljazeera.com", "apnews.com", "forbes.com", "cnbc.com",
    "politico.eu", "politico.com", "newstatesman.com", "spectator.co.uk", "theatlantic.com", "newyorker.com",
    "lemonde.fr", "elpais.com", "scotsman.com", "irishtimes.com", "time.com", "businessinsider.com", "insider.com",
    "vox.com", "axios.com", "npr.org", "usatoday.com", "latimes.com", "huffpost.com", "metro.co.uk",
    "express.co.uk", "inews.co.uk", "channel4.com", "itv.com"}
BLOG_HOSTS = ("medium.com", "substack.com", "wordpress.com", "blogspot.com", "blogger.com", "tumblr.com",
              "ghost.io", "hashnode.dev", "dev.to", "mataroa.blog", "bearblog.dev", "wixsite.com")


def host_of(url):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def known_site(host):
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in KNOWN_SITES:
            return cand, KNOWN_SITES[cand]
    return None, None


def pretty_host(host):
    """example-site.co.uk -> Example Site (last resort for organisation names)."""
    parts = host.split(".")
    core = parts[0]
    if len(parts) >= 3 and parts[-2] in {"co", "ac", "gov", "org", "com", "net", "edu", "nhs"}:
        core = parts[-3]
    elif len(parts) >= 2:
        core = parts[-2]
    if len(core) <= 4:
        return core.upper()
    return " ".join(w.capitalize() for w in re.split(r"[-_]", core))


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>#?]+)", re.I)


def clean_doi(d):
    if not d:
        return ""
    d = urllib.parse.unquote(str(d)).strip()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    m = DOI_RE.search(d)
    if not m:
        return ""
    d = m.group(1)
    d = re.sub(r"/(full|abstract|pdf|epdf|html|fulltext|references|figures|summary|meta|suppl|"
               r"supplemental|citedby|metrics|full-text|issuetoc|toc|epub)(/.*)?$", "", d, flags=re.I)
    d = re.sub(r"\.(pdf|html?|xml)$", "", d, flags=re.I)
    return d.rstrip(".,;)]")


def doi_from_url(url):
    p = urllib.parse.urlparse(url)
    host = host_of(url)
    path = urllib.parse.unquote(p.path)
    if host in ("doi.org", "dx.doi.org"):
        return clean_doi(path.lstrip("/"))
    if host.endswith("nature.com"):
        m = re.match(r"^/articles/([a-z]+[\d.\-]+[\w\-]*)$", path)
        if m:
            return f"10.1038/{m.group(1)}"
    if host.endswith("jstor.org"):
        m = re.search(r"/stable/(?:pdf/)?(\d+)", path)
        if m:
            return f"10.2307/{m.group(1)}"
    if "ssrn.com" in host:
        q = urllib.parse.parse_qs(p.query)
        aid = (q.get("abstract_id") or q.get("abstractid") or [""])[0]
        if aid:
            return f"10.2139/ssrn.{aid}"
    # Publisher URLs that embed the DOI (/doi/10.x/..., /article/10.x/...)
    m = re.search(r"(?:^|/)(10\.\d{4,9}/.+)$", path)
    if m and re.search(r"/(doi|article|articles|content|abs|full|chapter|book|epdf|pdf)/", path + "/", re.I):
        return clean_doi(m.group(1))
    m = re.search(r"/doi/(?:abs/|full/|pdf/|epdf/|book/|chapter/)?(10\.\d{4,9}/.+)$", path)
    if m:
        return clean_doi(m.group(1))
    if host.endswith("biorxiv.org") or host.endswith("medrxiv.org"):
        m = re.search(r"/content/(10\.\d{4,9}/[^v]+?)(v\d+)?(\.full.*)?$", path)
        if m:
            return clean_doi(m.group(1))
    return ""


# --------------------------------------------------------------------------
# Source handlers
# --------------------------------------------------------------------------

def base_result(url):
    return {
        "type": "webpage", "authors": [], "date": "", "title": "", "titleSentence": "",
        "container": "", "volume": "", "issue": "", "pages": "", "articleNumber": "",
        "doi": "", "url": url, "platform": "", "postTime": "", "publisher": "", "place": "",
        "edition": "", "reportNumber": "", "editors": [], "conferenceLocation": "", "conferenceDate": "",
        "warnings": [], "source": "",
    }


CSL_TYPE_MAP = {
    "journal-article": "journal", "article-journal": "journal", "article": "journal",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
    "book-chapter": "chapter", "chapter": "chapter", "book-section": "chapter", "book-part": "chapter",
    "reference-entry": "chapter",
    "proceedings-article": "conference", "paper-conference": "conference",
    "report": "report", "report-component": "report", "standard": "report",
    "posted-content": "preprint", "preprint": "preprint",
    "article-newspaper": "news", "article-magazine": "news", "post-weblog": "blog",
    "dissertation": "report", "thesis": "report", "dataset": "webpage", "webpage": "webpage",
}


def csl_authors(lst):
    out = []
    for a in lst or []:
        if a.get("family"):
            out.append({"given": a.get("given", ""), "family": a["family"]})
        elif a.get("literal") or a.get("name"):
            out.append({"org": a.get("literal") or a.get("name")})
    return out


def from_doi(doi, url=None):
    q = urllib.parse.quote(doi, safe="/:;()._-")
    data = (fetch_json(f"https://api.crossref.org/works/{q}") or {}).get("message")
    if not data or not data.get("title"):
        data = fetch_json(f"https://doi.org/{q}", accept="application/vnd.citationstyles.csl+json")
    if not data or not data.get("title"):
        return None
    r = base_result(url or f"https://doi.org/{doi}")
    r["source"] = "doi"
    ctype = str(data.get("type", "")).lower()
    r["type"] = CSL_TYPE_MAP.get(ctype, "journal" if data.get("container-title") else "webpage")
    if ctype in ("posted-content", "preprint") or str(data.get("subtype", "")).lower() == "preprint":
        r["type"] = "preprint"
    title = data.get("title")
    title = title[0] if isinstance(title, list) and title else (title or "")
    sub = data.get("subtitle")
    sub = sub[0] if isinstance(sub, list) and sub else (sub or "")
    title = strip_tags(title)
    if sub and sub.lower() not in title.lower():
        title = f"{title}: {strip_tags(sub)}"
    r["title"] = title
    container = data.get("container-title")
    if isinstance(container, list):
        # Crossref lists [series, book] for chapters; the book title is the last entry
        container = (container[-1] if r["type"] == "chapter" else container[0]) if container else ""
    r["container"] = re.sub(r"\s*\([A-Z]{2,8}\)$", "", strip_tags(container))
    r["authors"] = csl_authors(data.get("author"))
    editors = csl_authors(data.get("editor"))
    if not r["authors"] and editors and r["type"] == "book":
        r["authors"] = editors
        r["editorsAsAuthors"] = True
    elif r["type"] == "chapter":
        r["editors"] = editors
    r["date"] = (csl_date(data.get("issued")) or csl_date(data.get("published-print"))
                 or csl_date(data.get("published-online")) or csl_date(data.get("published"))
                 or csl_date(data.get("created")))
    r["volume"] = str(data.get("volume") or "")
    r["issue"] = str(data.get("issue") or data.get("journal-issue", {}).get("issue") or "")
    r["pages"] = str(data.get("page") or "")
    r["articleNumber"] = str(data.get("article-number") or data.get("number") or "") if r["type"] == "journal" else ""
    r["doi"] = data.get("DOI") or doi
    r["publisher"] = data.get("publisher") or ""
    r["place"] = data.get("publisher-place") or data.get("publisher-location") or ""
    ed = str(data.get("edition") or data.get("edition-number") or "")
    r["edition"] = "" if ed in ("1", "1st", "first") else ed
    if r["type"] == "conference":
        r["conferenceLocation"] = data.get("event-place") or (data.get("event") or {}).get("location", "") \
            if isinstance(data.get("event"), dict) else data.get("event-place") or ""
        ev = data.get("event")
        if isinstance(ev, str) and not r["container"]:
            r["container"] = ev
        elif isinstance(ev, dict) and not r["container"]:
            r["container"] = ev.get("name", "")
    if r["type"] == "preprint":
        r["container"] = r["container"] or data.get("publisher") or ""
        inst = data.get("institution")
        if isinstance(inst, list) and inst:
            r["container"] = inst[0].get("name", r["container"])
    if r["type"] == "report":
        r["reportNumber"] = str(data.get("number") or "")
    if r["type"] in ("journal", "conference", "chapter") and not r["pages"] and not r["articleNumber"]:
        _pages_from_landing_page(r, doi)
        if not r["pages"]:
            r["warnings"].append("The journal's record has no page numbers. If the article PDF shows them "
                                 "(e.g. 73-77), add them under Edit details.")
    return r


def _pages_from_landing_page(r, doi):
    """Crossref sometimes lacks page numbers; the publisher's page usually has them."""
    try:
        res, blocked = fetch_page(f"https://doi.org/{doi}")
        if blocked:
            return
        p = parse_html(res.text())
        fp, lp = p.get("citation_firstpage", "prism.startingpage"), p.get("citation_lastpage", "prism.endingpage")
        if fp:
            r["pages"] = f"{fp}-{lp}" if lp and lp != fp else fp
        if not r["issue"]:
            r["issue"] = p.get("citation_issue", "prism.number")
        if not r["volume"]:
            r["volume"] = p.get("citation_volume", "prism.volume")
    except Exception:
        pass


def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def handle_pii(url):
    m = re.search(r"/pii/(S?[0-9X]{15,17})", url, re.I)
    if not m:
        return None
    data = fetch_json(f"https://api.crossref.org/works?filter=alternative-id:{m.group(1)}&rows=1")
    items = ((data or {}).get("message") or {}).get("items") or []
    if items and items[0].get("DOI"):
        return from_doi(items[0]["DOI"], url)
    return None


def handle_arxiv(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$", url)
    if not m:
        return None
    aid = m.group(1)
    res = fetch(f"https://export.arxiv.org/api/query?id_list={aid}", accept="application/atom+xml", ua=API_UA)
    if not res.ok:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(res.body)
    except ET.ParseError:
        return None
    entry = root.find("a:entry", ns)
    if entry is None or entry.find("a:title", ns) is None:
        return None
    r = base_result(f"https://arxiv.org/abs/{aid}")
    r["type"] = "preprint"
    r["source"] = "arxiv"
    r["title"] = re.sub(r"\s+", " ", entry.find("a:title", ns).text or "").strip()
    r["authors"] = [split_person(a.find("a:name", ns).text) for a in entry.findall("a:author", ns)
                    if a.find("a:name", ns) is not None]
    r["date"] = parse_date((entry.findtext("a:published", "", ns) or "")[:10])
    r["container"] = "arXiv"
    return r


def handle_youtube(url):
    host = host_of(url)
    if not (host.endswith("youtube.com") or host == "youtu.be"):
        return None
    vid = ""
    p = urllib.parse.urlparse(url)
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    else:
        vid = (urllib.parse.parse_qs(p.query).get("v") or [""])[0]
        if not vid:
            m = re.search(r"/(?:shorts|embed|live|v)/([\w-]{6,})", p.path)
            vid = m.group(1) if m else ""
    r = base_result(url)
    r["type"] = "social"
    r["platform"] = "YouTube"
    r["source"] = "youtube"
    oe = fetch_json("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""), ua=BROWSER_UA)
    if oe:
        r["title"] = oe.get("title", "")
        if oe.get("author_name"):
            r["authors"] = [{"org": oe["author_name"]}]  # screen name, exactly as written
    if vid:
        page = fetch(f"https://www.youtube.com/watch?v={vid}")
        if page.ok:
            txt = page.text()
            m = (re.search(r'"(?:uploadDate|publishDate)"\s*:\s*"([^"]+)"', txt)
                 or re.search(r'itemprop="(?:uploadDate|datePublished)"\s+content="([^"]+)"', txt))
            if m:
                r["date"] = parse_date(m.group(1))
            if not r["title"]:
                mp = parse_html(txt)
                r["title"] = mp.get("og:title", "title")
                ch = re.search(r'"ownerChannelName"\s*:\s*"([^"]+)"', txt)
                if ch:
                    r["authors"] = [{"org": json.loads(f'"{ch.group(1)}"')}]
    if not r["title"]:
        return None
    return r


def handle_twitter(url):
    host = host_of(url)
    if host not in ("twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com"):
        return None
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        return None
    sid = m.group(1)
    r = base_result(url)
    r["type"] = "social"
    r["platform"] = "Twitter" if "twitter.com" in host else "X"
    r["source"] = "twitter"
    t = snowflake_time(sid, 1288834974657, 22)
    if t and t.year >= 2010:
        r["date"] = t.strftime("%Y-%m-%d")
        r["postTime"] = social_time(t)
    oe = fetch_json("https://publish.twitter.com/oembed?omit_script=1&url="
                    + urllib.parse.quote(url.replace("x.com", "twitter.com"), safe=""), ua=BROWSER_UA)
    if oe:
        r["authors"] = [{"org": oe.get("author_name", "")}] if oe.get("author_name") else []
        mm = re.search(r"<p[^>]*>(.*?)</p>", oe.get("html", ""), re.S)
        if mm:
            r["title"] = tidy_post_text(strip_tags(re.sub(r"<br\s*/?>", " ", mm.group(1))))
    if not r["authors"]:
        mh = re.search(r"(?:twitter|x)\.com/([^/?#]+)/status", url)
        if mh:
            r["authors"] = [{"org": mh.group(1)}]
            r["warnings"].append("Couldn't read the post itself - check the screen name and add the post text.")
    return r


def tidy_post_text(text, limit=100):
    text = re.sub(r"\s*(https?://\S+|pic\.twitter\.com/\S+)\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit + 15:
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—–- ")
        text = cut + "..."
    return text


def handle_tiktok(url):
    host = host_of(url)
    if not host.endswith("tiktok.com"):
        return None
    oe = fetch_json("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe=""), ua=BROWSER_UA)
    r = base_result(url)
    r["type"] = "social"
    r["platform"] = "TikTok"
    r["source"] = "tiktok"
    if oe:
        r["title"] = tidy_post_text(oe.get("title", ""))
        if oe.get("author_name"):
            r["authors"] = [{"org": oe["author_name"]}]
    m = re.search(r"/video/(\d+)", url) or re.search(r"/video/(\d+)", (oe or {}).get("html", ""))
    if m:
        t = snowflake_time(m.group(1), 0, 32)
        if t and t.year >= 2016:
            r["date"] = t.strftime("%Y-%m-%d")
            r["postTime"] = social_time(t)
    if not r["authors"]:
        mh = re.search(r"tiktok\.com/@([^/?#]+)", url)
        if mh:
            r["authors"] = [{"org": mh.group(1)}]
    if not r["title"]:
        r["warnings"].append("TikTok didn't share this video's caption - type the caption in as the title, "
                             "and check the creator's screen name.")
    return r if (r["title"] or r["authors"]) else None


def handle_wikipedia(url):
    host = host_of(url)
    m = re.match(r"^(?:([a-z\-]+)\.)?(?:m\.)?(wikipedia|wikibooks|wikivoyage|wiktionary|wikinews|"
                 r"wikiquote|wikisource)\.org$", host)
    if not m:
        return None
    path = urllib.parse.urlparse(url).path
    mm = re.match(r"^/wiki/(.+)$", path)
    if not mm:
        return None
    page = urllib.parse.unquote(mm.group(1))
    lang = m.group(1) or "en"
    if lang == "m":
        lang = "en"
    api = (f"https://{lang}.{m.group(2)}.org/w/api.php?action=query&prop=revisions&rvprop=timestamp"
           f"&redirects=1&format=json&titles=" + urllib.parse.quote(page))
    data = fetch_json(api)
    r = base_result(url.split("#")[0])
    r["type"] = "wikipedia"
    r["source"] = "wikipedia"
    r["container"] = m.group(2).capitalize()
    r["title"] = page.replace("_", " ")
    try:
        pg = next(iter(data["query"]["pages"].values()))
        r["title"] = pg.get("title", r["title"])
        r["date"] = parse_date(pg["revisions"][0]["timestamp"][:10])
    except Exception:
        r["warnings"].append("Couldn't read when the article was last updated - check the foot of the page.")
    return r


def handle_google_books(url):
    host = host_of(url)
    if "books.google" not in host and "google.com/books" not in url:
        return None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    vid = (q.get("id") or [""])[0]
    if not vid:
        m = re.search(r"/books/edition/[^/]+/([\w-]+)", url)
        vid = m.group(1) if m else ""
    if not vid:
        return None
    data = fetch_json(f"https://www.googleapis.com/books/v1/volumes/{vid}")
    return book_from_google((data or {}).get("volumeInfo"), url)


def handle_isbn(text):
    isbn = re.sub(r"[^0-9Xx]", "", text)
    if len(isbn) not in (10, 13):
        return None
    ol = fetch_json(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data")
    book = (ol or {}).get(f"ISBN:{isbn}")
    if book and book.get("title"):
        r = base_result("")
        r["type"] = "book"
        r["source"] = "openlibrary"
        r["isbn"] = isbn
        r["title"] = book["title"] + (f": {book['subtitle']}" if book.get("subtitle") else "")
        r["authors"] = [a for a in (name_to_author(x.get("name", ""), "person") for x in book.get("authors", [])) if a]
        r["date"] = parse_date(book.get("publish_date", ""))
        r["publisher"] = ((book.get("publishers") or [{}])[0]).get("name", "")
        r["place"] = ((book.get("publish_places") or [{}])[0]).get("name", "")
        ed = re.search(r"(\d+)(st|nd|rd|th)", book.get("edition_name", "") or "")
        r["edition"] = "" if not ed or ed.group(1) == "1" else ed.group(1)
        if not r["place"]:
            r["warnings"].append("Add the place of publication - it's on the book's imprint page.")
        return r
    data = fetch_json(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}")
    items = (data or {}).get("items") or []
    if not items:
        return None
    r = book_from_google(items[0].get("volumeInfo"), "")
    if r:
        r["isbn"] = isbn
    return r


def book_from_google(info, url):
    if not info or not info.get("title"):
        return None
    r = base_result(url)
    r["type"] = "book"
    r["source"] = "googlebooks"
    r["title"] = info["title"] + (f": {info['subtitle']}" if info.get("subtitle") else "")
    r["authors"] = [name_to_author(a, "person") for a in info.get("authors", [])]
    r["authors"] = [a for a in r["authors"] if a]
    r["date"] = parse_date(info.get("publishedDate", ""))
    r["publisher"] = info.get("publisher", "")
    r["warnings"].append("Add the place of publication (it's on the book's imprint page) - "
                         "Google Books doesn't record it. Check the edition too.")
    return r


def handle_pdf(res, url):
    r = base_result(url)
    r["type"] = "report"
    r["source"] = "pdf"
    info = {}
    first_page = ""
    if shutil.which("pdfinfo"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(res.body)
            path = tf.name
        try:
            out = subprocess.run(["pdfinfo", "-isodates", path], capture_output=True, timeout=10).stdout
            for line in out.decode("utf-8", "replace").splitlines():
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
            if shutil.which("pdftotext"):
                first_page = subprocess.run(["pdftotext", "-l", "2", path, "-"], capture_output=True,
                                            timeout=10).stdout.decode("utf-8", "replace")
        except Exception:
            pass
        finally:
            os.unlink(path)
    else:
        for k in ("Title", "Author", "CreationDate"):
            m = re.search(rb"/" + k.encode() + rb"\s*\((.*?)\)", res.body[:400000], re.S)
            if m:
                info[k] = m.group(1).decode("latin-1", "ignore")
    title = info.get("Title", "")
    if title and not re.match(r"^(untitled|microsoft word|document\d*|slide \d+|\S+\.(docx?|pptx?|indd))", title, re.I):
        r["title"] = title
    else:
        fname = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
        fname = re.sub(r"\.pdf$", "", fname, flags=re.I)
        words = [w for w in re.split(r"[_\-\s]+", fname) if w]
        found = None
        if words and first_page:
            m = re.search(r"\b" + r"[\W_]+".join(re.escape(w) for w in words) + r"\b",
                          re.sub(r"\s+", " ", first_page), re.I)
            found = m.group(0) if m else None
        if found:
            r["title"] = found[:1].upper() + found[1:]
            r["warnings"].append("Title matched from the PDF's cover page - double-check it.")
        else:
            t = " ".join(words)
            r["title"] = t[:1].upper() + t[1:]
            r["warnings"].append("This PDF has no proper title in its metadata - title taken from the file name. "
                                 "Copy the title from the PDF's cover page.")
    author = info.get("Author", "")
    host = host_of(url)
    _, site = known_site(host)
    org = site or pretty_host(host)
    if author and not re.search(r"^(user|admin|owner|author|unknown|\w+\d+)$", author, re.I) and len(author) < 80:
        r["authors"] = [a for a in (name_to_author(n) for n in split_author_list(author)) if a]
    if not r["authors"]:
        r["authors"] = [{"org": org}]
        r["warnings"].append(f"Author set to the organisation ({org}) - check the PDF's cover for the corporate author.")
    r["container"] = org
    r["date"] = parse_date(info.get("CreationDate", "")[:10]) if info.get("CreationDate") else ""
    if r["date"]:
        r["warnings"].append("Year taken from the PDF file's creation date - confirm against the date printed in the report.")
    return r


def handle_html(res, url):
    text = res.text()
    p = parse_html(text)
    final_url = url
    host = host_of(final_url)
    nodes = jsonld_nodes(p.jsonld)
    main = pick_main_node(nodes)
    mtypes = set(node_types(main)) if main else set()

    # --- scholarly? -------------------------------------------------------
    doi = clean_doi(p.get("citation_doi", "dc.identifier.doi", "prism.doi", "bepress_citation_doi", "doi",
                          "dc.identifier", "citation_identifier"))
    if not doi and main and isinstance(main.get("identifier"), (str, dict, list)):
        ident = json.dumps(main.get("identifier"))
        m = DOI_RE.search(ident)
        doi = clean_doi(m.group(1)) if m else ""
    scholarly = bool(p.get("citation_title", "citation_journal_title", "citation_conference_title",
                           "citation_book_title", "citation_technical_report_institution")) or "ScholarlyArticle" in mtypes
    if doi and scholarly:
        r = from_doi(doi, url)
        if r:
            r["bodyText"] = p.body_text[:200000]
            return r

    site_known_key, site_known = known_site(host)
    site = (site_known or p.get("og:site_name", "application-name", "twitter:app:name:googleplay")
            or (ld_text(main.get("publisher")) if main else "") or "")
    if site and not site_known:
        site = re.sub(r"\s*[|•·]\s*.*$", "", html.unescape(site)).strip()

    r = base_result(url)
    r["source"] = "html"
    r["bodyText"] = p.body_text[:200000]

    if scholarly:
        r["type"] = "journal"
        r["title"] = p.get("citation_title", "dc.title")
        r["container"] = p.get("citation_journal_title", "citation_conference_title", "citation_book_title",
                               "prism.publicationname")
        r["authors"] = [a for a in (name_to_author(n, "person") for n in p.get_all("citation_author")) if a]
        if not r["authors"]:
            r["authors"] = [a for a in (name_to_author(n) for n in p.get_all("dc.creator", "citation_authors")) if a]
        r["date"] = parse_date(p.get("citation_publication_date", "citation_date", "citation_online_date",
                                     "dc.date", "prism.publicationdate"))
        r["volume"] = p.get("citation_volume", "prism.volume")
        r["issue"] = p.get("citation_issue", "prism.number")
        fp, lp = p.get("citation_firstpage", "prism.startingpage"), p.get("citation_lastpage", "prism.endingpage")
        r["pages"] = f"{fp}-{lp}" if fp and lp and fp != lp else fp
        r["doi"] = doi
        r["publisher"] = p.get("citation_publisher", "dc.publisher")
        if p.get("citation_conference_title"):
            r["type"] = "conference"
        elif p.get("citation_book_title") and not p.get("citation_journal_title"):
            r["type"] = "chapter"
        elif p.get("citation_technical_report_institution"):
            r["type"] = "report"
            r["container"] = p.get("citation_technical_report_institution")
            r["reportNumber"] = p.get("citation_technical_report_number")
        elif p.get("citation_dissertation_institution"):
            r["type"] = "report"
        if re.search(r"rxiv|preprint|ssrn|osf\.io|researchsquare|preprints\.org", host) and r["type"] == "journal":
            r["type"] = "preprint"
            r["container"] = r["container"] or site
        if not r["title"]:
            r["title"] = p.title
        r["title"] = clean_title(r["title"], site)
        return r

    # --- general web content ---------------------------------------------
    title = ""
    if main:
        title = ld_text(main.get("headline")) or ld_text(main.get("name"))
    og_title = p.get("og:title", "twitter:title", "dc.title", "parsely-title", "sailthru.title")
    title = og_title or title or p.h1.strip() or p.title
    h1 = re.sub(r"\s+", " ", p.h1).strip()
    if h1 and "|" in title and any(seg.strip().lower() == h1.lower() for seg in title.split("|")):
        title = h1
    r["title"] = clean_title(title, site)

    authors = []
    if main and main.get("author"):
        authors = authors_from_ld(main.get("author"))
    if not authors:
        meta_auth = (p.get_all("author", "article:author", "parsely-author", "sailthru.author", "dc.creator",
                               "byl", "twitter:creator:name", "citation_author"))
        for n in meta_auth:
            if re.match(r"https?://", n):
                continue
            for part in split_author_list(n):
                a = name_to_author(part)
                if a:
                    authors.append(a)
    if not authors and p.rel_authors:
        for n in p.rel_authors:
            a = name_to_author(n)
            if a:
                authors.append(a)
    authors = dedupe_authors(authors)
    # Drop authors that are just the site itself if we also have people.
    people = [a for a in authors if not a.get("org")]
    if people:
        authors = people
    r["authors"] = authors[:30]

    published = (ld_text(main.get("datePublished")) if main else "") or p.get(
        "article:published_time", "og:article:published_time", "datepublished", "date", "dc.date",
        "dc.date.issued", "dcterms.created", "parsely-pub-date", "sailthru.date", "pubdate", "publish-date",
        "publication_date", "citation_publication_date", "article.published", "timestamp")
    modified = (ld_text(main.get("dateModified")) if main else "") or p.get(
        "article:modified_time", "og:updated_time", "last-modified", "dcterms.modified", "datemodified")
    date = parse_date(published)
    if not date and p.times:
        date = parse_date(p.times[0])
    if not date and modified:
        date = parse_date(modified)
        if date:
            r["warnings"].append("No publication date found - year taken from the page's 'last updated' date.")
    if not date:
        date = url_date(url)
    if not date:
        body = p.body_text[:60000]
        m = re.search(r"\b(published|release date|date published|posted|first published|publication date)"
                      r"\s*(?:on)?\s*:?\s*([A-Za-z0-9 ,]{6,30}?\d{4})", body, re.I)
        if m:
            date = parse_date(m.group(2))
        if not date:
            m = re.search(r"\b(last updated|updated|last modified|last reviewed|page last reviewed)\s*(?:on)?\s*:?\s*"
                          r"([A-Za-z0-9 ,]{6,30}?\d{4})", body, re.I)
            if m and parse_date(m.group(2)):
                date = parse_date(m.group(2))
                r["warnings"].append("No publication date found - year taken from the page's 'last updated' date.")
    r["date"] = date
    r["modified"] = parse_date(modified)

    # --- classify ---------------------------------------------------------
    og_type = p.get("og:type").lower()
    is_news = bool(mtypes & NEWS_TYPES) or (site_known_key in NEWS_DOMAINS and (og_type == "article" or "Article" in mtypes))
    is_blog = ("BlogPosting" in mtypes or any(host == h or host.endswith("." + h) for h in BLOG_HOSTS)
               or re.search(r"(^|\.)blogs?\.|/blogs?/", host + urllib.parse.urlparse(url).path) is not None)
    is_podcast = "PodcastEpisode" in mtypes or host in ("podcasts.apple.com", "open.spotify.com") and "/episode" in url
    is_report = "Report" in mtypes
    if is_podcast:
        r["type"] = "podcast"
        series = main.get("partOfSeries") if main else None
        if series and not r["authors"]:
            r["authors"] = [{"org": ld_text(series)}]
    elif is_news and not is_blog:
        r["type"] = "news"
        r["container"] = site
    elif is_blog and (og_type == "article" or mtypes & {"BlogPosting", "Article"} or r["authors"]):
        r["type"] = "blog"
        r["container"] = site or pretty_host(host)
        if "medium.com" in host and main and isinstance(main.get("publisher"), dict):
            r["container"] = ld_text(main["publisher"]) or r["container"]
        r["blogDate"] = r["date"] or r["modified"]
    elif is_report or (host.endswith("gov.uk") and "/government/publications/" in url):
        r["type"] = "report"
        r["container"] = site
    else:
        r["type"] = "webpage"
        r["container"] = site

    if r["type"] == "news" and not r["authors"] and site:
        pass  # client uses newspaper title as author per the guide
    if not r["container"]:
        r["container"] = site or (site_known or pretty_host(host))
    r["siteName"] = site or site_known or pretty_host(host)

    if not r["title"]:
        r["warnings"].append("Couldn't find a page title - please type it in.")
    return r


def url_date(url):
    """Dates embedded in article URLs: /2023/01/25/ or /2023/jan/25/."""
    m = re.search(r"/(20\d{2}|19\d{2})/(\d{1,2}|[a-z]{3})/(\d{1,2})(?:/|$)", urllib.parse.urlparse(url).path)
    if m:
        mo = int(m.group(2)) if m.group(2).isdigit() else MONTHS.get(m.group(2))
        if mo and 1 <= mo <= 12 and 1 <= int(m.group(3)) <= 31:
            return f"{m.group(1)}-{mo:02d}-{int(m.group(3)):02d}"
    return ""


def fallback_from_url(url, why):
    host = host_of(url)
    _, site = known_site(host)
    r = base_result(url)
    r["source"] = "url"
    slug = urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.\w{2,5}$", "", slug)
    slug = re.sub(r"[-_+]+", " ", slug).strip()
    slug = re.sub(r"\b[0-9a-f]{8,}\b", "", slug).strip()
    words = slug.split()
    junk = not words or sum(bool(re.search(r"\d", w)) for w in words) > len(words) / 2
    r["title"] = "" if junk else slug[:1].upper() + slug[1:]
    r["date"] = url_date(url)
    org = site or pretty_host(host)
    r["container"] = org
    r["siteName"] = org
    if site and host.split(".")[-2:] and known_site(host)[0] in NEWS_DOMAINS:
        r["type"] = "news"
    r["warnings"].append(f"The site blocked automatic reading ({why}). I've filled in what the link itself tells "
                         "me - check the title, author and date against the page.")
    return r


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def normalise_input(raw):
    s = (raw or "").strip().strip("<>").strip()
    if re.match(r"^(doi:\s*)?10\.\d{4,9}/\S+$", s, re.I):
        return "doi", clean_doi(s)
    digits = re.sub(r"[\s-]", "", s)
    if re.match(r"^(isbn:?)?(97[89])?\d{9}[\dXx]$", digits, re.I):
        return "isbn", re.sub(r"^isbn:?", "", digits, flags=re.I)
    if not re.match(r"^https?://", s, re.I):
        if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", s):
            s = "https://" + s
        else:
            return "invalid", s
    # strip common tracking params
    p = urllib.parse.urlparse(s)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not re.match(r"^(utm_|fbclid|gclid|mc_|igshid|si$|ref_src|ref$|cmpid|ito$|at_)", k)]
    if host_of(s).endswith("youtube.com"):
        q = [(k, v) for k, v in q if k in ("v", "t", "list", "index")]
    s = urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q), fragment="" if "wikipedia" not in s else p.fragment))
    return "url", s


def cite(raw):
    kind, val = normalise_input(raw)
    with _cache_lock:
        if val in _cache:
            return json.loads(json.dumps(_cache[val]))
    r = _cite(kind, val)
    if r and "error" not in r:
        r.setdefault("bodyText", "")
        names = [r.get("container", ""), r.get("siteName", "")] + [a.get("org", "") for a in r.get("authors", [])]
        protect = {w for n in names for w in re.findall(r"[A-Z][\w'’-]+", n or "") if w.lower() not in SMALL_WORDS}
        r["titleSentence"] = sentence_case(r.get("title", ""), r.get("bodyText", ""), protect) if r["type"] not in (
            "social",) else r.get("title", "")
        r.pop("bodyText", None)
        r["year"] = (r.get("date") or "")[:4]
        with _cache_lock:
            if len(_cache) > 1000:
                _cache.clear()
            _cache[val] = r
    return r


def _cite(kind, val):
    if kind == "invalid":
        return {"error": "That doesn't look like a link. Paste a full web address, a DOI or an ISBN."}
    if kind == "doi":
        r = from_doi(val)
        return r or {"error": f"No record found for DOI {val}."}
    if kind == "isbn":
        r = handle_isbn(val)
        return r or {"error": f"No book found for ISBN {val}."}

    url = val
    try:
        check_public_url(url)
    except UnsafeURL:
        return {"error": "That isn't a public web address, so it can't be cited from here."}
    for handler in (handle_youtube, handle_twitter, handle_tiktok, handle_wikipedia, handle_arxiv,
                    handle_google_books):
        try:
            r = handler(url)
        except Exception:
            r = None
        if r:
            return r

    doi = doi_from_url(url)
    if doi:
        r = from_doi(doi, url)
        if r:
            return r
    if "sciencedirect.com" in url or "/pii/" in url:
        r = handle_pii(url)
        if r:
            return r

    res, blocked = fetch_page(url)
    if blocked:
        why = f"HTTP {res.status}" if res.status else "no response"
        if res.status == 404:
            return {"error": "That page doesn't exist (404). Check the link."}
        return fallback_from_url(url, why)

    if "pdf" in res.ctype or res.body[:5] == b"%PDF-":
        return handle_pdf(res, url)
    r = handle_html(res, url)
    if r.get("source") == "html" and not r.get("title"):
        return fallback_from_url(url, "no readable metadata")
    return r


def fetch_page(url):
    """Fetch as a browser, then as link-preview bots if a bot wall answers. Returns (res, blocked)."""
    res = fetch(url)
    blocked = (not res.ok) or looks_blocked(res)
    for ua in PREVIEW_UAS:
        if not blocked or res.status == 404:
            break
        alt = fetch(url, ua=ua)
        if alt.ok and not looks_blocked(alt):
            res, blocked = alt, False
    return res, blocked


def looks_blocked(res):
    if "html" not in res.ctype and res.body[:5] != b"%PDF-":
        return False
    head = res.body[:20000].lower()
    return any(s in head for s in (b"just a moment...", b"cf-browser-verification", b"attention required! | cloudflare",
                                   b"captcha-delivery", b"px-captcha", b"are you a robot", b"access denied</title>",
                                   b"enable javascript and cookies to continue"))


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

_hits = {}
_hits_lock = threading.Lock()
RATE_LIMIT = 40  # citations per visitor per minute (public mode)


def rate_limited(ip):
    now = dt.datetime.now().timestamp()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        limited = len(recent) >= RATE_LIMIT
        if not limited:
            recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 5000:
            _hits.clear()
    return limited


class Handler(BaseHTTPRequestHandler):
    server_version = "Citator/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if p.path == "/api/cite":
            q = urllib.parse.parse_qs(p.query).get("url", [""])[0][:2000]
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            if PUBLIC and rate_limited(ip):
                return self._send(429, json.dumps({"error": "Slow down a little - too many citations in a minute. "
                                                            "Try again shortly."}), "application/json")
            try:
                r = cite(q)
            except Exception as e:  # never crash the UI
                r = {"error": f"Something went wrong reading that link ({type(e).__name__})."}
            return self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
        if p.path == "/api/ping":
            return self._send(200, '{"ok":true}', "application/json")
        self._send(404, "Not found", "text/plain")


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    if PUBLIC:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        print(f"Citator (public mode) listening on port {PORT}", flush=True)
        srv.serve_forever()
        return
    port = PORT
    if not port_free(port):
        # Already running? Just open it.
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                if r.status == 200:
                    print(f"Citator already running - opening http://127.0.0.1:{port}")
                    if "--no-browser" not in sys.argv:
                        webbrowser.open(f"http://127.0.0.1:{port}")
                    return
        except Exception:
            pass
        while not port_free(port):
            port += 1
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Manchester Harvard Citator running at {url}\n  (close this window or press Ctrl+C to stop)\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

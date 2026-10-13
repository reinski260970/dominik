#!/usr/bin/env python3
"""Quoten-Vergleichsportal – Datenabruf (läuft in GitHub Actions alle 30 Minuten).

Ablauf pro Lauf:
  1. Tipps aus der Google-Tabelle lesen (veröffentlichte CSV, Secret TIPS_CSV_URL)
  2. Tipps den Spielen zuordnen (OddsPapi /fixtures, gecacht)
  3. Quoten holen – pro LIGA statt pro Spiel (/odds-by-tournaments), Tipps mit nahem Anstoß zuerst
  4. Einmal täglich: Übersicht aller Spiele der nächsten Stunden (gleicher Sammelabruf)
  5. Quotenverlauf der Tipps über /historical-odds (kostenlos)
  6. Value-Liste: beste Quote gegen faire Konsens-Quote (Marge aller Buchmacher herausgerechnet)
  7. Alles verschlüsselt (AES-GCM, Passwort aus Secret PORTAL_PASSWORD) nach site/data schreiben

Secrets/Umgebung: ODDSPAPI_KEY, PORTAL_PASSWORD, TIPS_CSV_URL
Doku OddsPapi: https://oddspapi.io/en/docs
"""
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE = Path(os.environ.get("STATE_DIR", ROOT / "state"))
OUT = Path(os.environ.get("OUT_DIR", ROOT.parent / "site" / "data"))
BASE = "https://api.oddspapi.io/v4"
VIENNA = ZoneInfo("Europe/Vienna")

COOLDOWN = {"/fixtures": 2.1, "/odds-by-tournaments": 1.1, "/odds": 0.6, "/historical-odds": 5.2,
            "/bookmakers": 1.1, "/sports": 1.1, "/markets": 1.1, "/account": 1.1}
FREE = {"/account", "/historical-odds"}
MAX_BOOKS_PER_CALL = 3

LOG = []


def log(msg):
    line = f"{datetime.now(VIENNA):%H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.append(line)


def now_utc():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- Zustand

def load(name, default):
    p = STATE / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save(name, obj):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, STATE / name)


# ---------------------------------------------------------------- API + Budget

class BudgetExhausted(Exception):
    pass


class Api:
    def __init__(self, key):
        self.key = key
        self.last = {}
        self.spent_run = 0
        self.account = None
        self.ledger = load("budget.json", {})

    def _today(self):
        return datetime.now(VIENNA).strftime("%Y-%m-%d")

    def spent_today(self):
        return self.ledger.get(self._today(), 0)

    def _count(self):
        d = self._today()
        self.ledger = {d: self.ledger.get(d, 0) + 1}   # nur den heutigen Tag behalten
        self.spent_run += 1
        if self.account:
            self.account["used"] += 1

    def get(self, endpoint, params=None, allowance=None):
        if endpoint not in FREE and allowance is not None and allowance <= 0:
            raise BudgetExhausted(endpoint)
        wait = COOLDOWN.get(endpoint, 1.1) - (time.monotonic() - self.last.get(endpoint, 0))
        if wait > 0:
            time.sleep(wait)
        q = dict(params or {})
        q["apiKey"] = self.key
        url = f"{BASE}{endpoint}?{urlencode(q)}"
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers={"Accept": "application/json", "User-Agent": "odds-portal/1.0"}),
                             timeout=90) as r:
                    body = r.read()
                self.last[endpoint] = time.monotonic()
                if endpoint not in FREE:
                    self._count()
                return json.loads(body.decode("utf-8"))
            except HTTPError as e:
                self.last[endpoint] = time.monotonic()
                text = e.read(2000).decode("utf-8", "replace").replace(self.key, "[KEY]")
                if endpoint not in FREE and e.code not in (401, 403):
                    self._count()       # laut Doku zählen auch 4xx/5xx
                if e.code == 429 and "REQUEST_LIMIT" in text.upper():
                    raise BudgetExhausted("Monatskontingent aufgebraucht") from e
                if e.code == 429 and attempt < 2:
                    time.sleep(COOLDOWN.get(endpoint, 1.1) + 2)
                    continue
                if e.code in (401, 403):
                    raise SystemExit(f"API-Key ungültig/ohne Berechtigung ({e.code}): {text}")
                raise ApiError(e.code, endpoint, text) from e
            except (URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(5)
                    continue
                raise ApiError(0, endpoint, str(e)) from e

    def load_account(self):
        try:
            d = self.get("/account")
        except Exception as e:   # noqa: BLE001
            log(f"Account-Abfrage fehlgeschlagen: {e}")
            return None
        s = json.dumps(d)
        used = re.search(r'"request_count"\s*:\s*(\d+)', s)
        limit = re.search(r'"request_limit"\s*:\s*(\d+)', s)
        if used and limit:
            self.account = {"used": int(used.group(1)), "limit": int(limit.group(1))}
        return self.account


class ApiError(Exception):
    def __init__(self, code, endpoint, text):
        super().__init__(f"HTTP {code} bei {endpoint}: {text[:300]}")
        self.code, self.endpoint, self.text = code, endpoint, text


def plan_budget(api):
    """Verteilt das Restkontingent gleichmäßig auf die restlichen Tage und Läufe."""
    b = CFG["budget"]
    now_v = datetime.now(VIENNA)
    days_left = monthrange(now_v.year, now_v.month)[1] - now_v.day + 1
    acc = api.account
    if acc and acc["limit"]:
        free_month = max(0, acc["limit"] - acc["used"] - b["monthly_reserve"])
        # heutiger Verbrauch zählt schon im "used" mit -> wieder addieren für den Tagesanteil
        daily = (free_month + api.spent_today()) / days_left
    else:
        daily = b["daily_max"]
    daily = min(daily, b["daily_max"])
    left_today = max(0, int(daily) - api.spent_today())
    minutes_left = (24 * 60 - (now_v.hour * 60 + now_v.minute))
    runs_left = max(1, round(minutes_left / (24 * 60 / b["runs_per_day"])))
    return {"daily": round(daily, 1), "left_today": left_today, "runs_left": runs_left}


# ---------------------------------------------------------------- Hilfsfunktionen

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def to_float(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000 if s > 1e11 else s, timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_kickoff(v):
    """'17/10/2026 15:00', '17.10.2026 15:00', '2026-10-17 15:00' (Wiener Zeit)."""
    s = str(v or "").strip()
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?:[ ,T]+(\d{1,2}):(\d{2}))?", s)
    if m:
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        try:
            return datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4) or 12), int(m.group(5) or 0),
                            tzinfo=VIENNA)
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 12),
                        int(m.group(5) or 0), tzinfo=VIENNA)
    return None


def col_index(letter):
    n = 0
    for ch in str(letter).strip().upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def as_list(d):
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        if "fixtureId" in d:
            return [d]
        for k in ("fixtures", "data", "results", "items"):
            if isinstance(d.get(k), list):
                return d[k]
        return [v for v in d.values() if isinstance(v, dict) and "fixtureId" in v]
    return []


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# ---------------------------------------------------------------- Stammdaten (wöchentlich)

def reference(api, allowance):
    ref = load("reference.json", {})
    fresh = now_utc().timestamp() - ref.get("t", 0) < 7 * 86400 and ref.get("books_cfg") == CFG["bookmakers"]
    if fresh and ref.get("markets"):
        return ref
    log("Lade Stammdaten (Sportarten, Buchmacher, Märkte) …")
    sports = api.get("/sports", {"language": "en"}, allowance)
    sports = sports if isinstance(sports, list) else []
    books = api.get("/bookmakers", None, allowance)
    books = books if isinstance(books, list) else []
    markets = api.get("/markets", {"language": "en"}, allowance)
    ref = {"t": now_utc().timestamp(), "books_cfg": CFG["bookmakers"]}
    ref["sports"] = {str(s.get("slug", "")).lower(): {"id": s.get("sportId"), "name": s.get("sportName")}
                     for s in sports if isinstance(s, dict)}
    # Buchmacher-Namen -> Slugs
    sel, missing = [], []
    for want in CFG["bookmakers"]:
        w = norm(want)
        exact = [b for b in books if norm(b.get("bookmakerName")) == w or norm(b.get("slug")) == w]
        if len(exact) == 1:
            sel.append({"slug": exact[0]["slug"], "label": exact[0].get("bookmakerName") or exact[0]["slug"]})
        else:
            cand = [b.get("slug") for b in books if w in norm(b.get("bookmakerName")) or w in norm(b.get("slug"))][:8]
            missing.append(f"{want} ({'mehrdeutig' if exact else 'nicht gefunden'}; Vorschläge: {', '.join(cand) or '–'})")
    ref["books"] = sel
    ref["books_missing"] = missing
    ref["markets"] = {}
    for m in markets if isinstance(markets, list) else []:
        ref["markets"][str(m.get("marketId"))] = {
            "name": m.get("marketName") or "", "type": str(m.get("marketType") or "").lower(),
            "line": m.get("handicap"), "period": str(m.get("period") or ""), "sport": m.get("sportId"),
            "prop": bool(m.get("playerProp")),
            "outcomes": {str(o.get("outcomeId")): o.get("outcomeName") for o in m.get("outcomes") or []}}
    save("reference.json", ref)
    if missing:
        log("Buchmacher nicht eindeutig: " + "; ".join(missing))
    return ref


def market_label(m):
    if not m:
        return "?"
    s = m["name"]
    if m.get("line") not in (None, "", 0) and str(m["line"]) not in s:
        s += f" {m['line']}"
    if m.get("period") and not re.search(r"full|^$", m["period"], re.I) and m["period"].lower() not in s.lower():
        s += f" ({m['period']})"
    return s


# ---------------------------------------------------------------- Quoten verarbeiten

def iter_prices(fx, props=False):
    """(slug, marketId, outcomeId, playerId, info) für alle AKTIVEN Preise."""
    excluded = set(CFG.get("exclude_bookmakers", []))
    for slug, bm in (fx.get("bookmakerOdds") or {}).items():
        if slug in excluded or not isinstance(bm, dict) or bm.get("suspended") or bm.get("bookmakerIsActive") is False:
            continue
        for mid, m in (bm.get("markets") or {}).items():
            if not isinstance(m, dict) or m.get("marketActive") is False:
                continue
            for oid, o in (m.get("outcomes") or {}).items():
                for pid, info in ((o or {}).get("players") or {}).items():
                    if not props and str(pid) != "0":
                        continue
                    if not isinstance(info, dict) or info.get("active") is False:
                        continue
                    p = info.get("price")
                    if isinstance(p, bool) or not isinstance(p, (int, float)) or p <= 1:
                        continue
                    yield slug, str(mid), str(oid), str(pid), info


def to_v4(d):
    """Neueres v5-Format (odds/participants) in v4-Form bringen."""
    if not isinstance(d, dict) or "bookmakerOdds" in d or "odds" not in d:
        return d
    parts, tour = d.get("participants") or {}, d.get("tournament") or {}
    out = {"fixtureId": d.get("fixtureId"), "startTime": d.get("startTime"), "tournamentId": tour.get("tournamentId"),
           "tournamentName": tour.get("tournamentName"), "categoryName": tour.get("categoryName"),
           "participant1Name": parts.get("participant1Name"), "participant2Name": parts.get("participant2Name"),
           "bookmakerOdds": {}}
    meta = d.get("bookmakers") or {}
    for slug, quotes in (d.get("odds") or {}).items():
        bm = out["bookmakerOdds"].setdefault(slug, {"suspended": (meta.get(slug) or {}).get("suspended", False),
                                                    "markets": {}})
        for q in (quotes or {}).values():
            m = bm["markets"].setdefault(str(q.get("marketId")), {"marketActive": q.get("marketActive", True),
                                                                   "outcomes": {}})
            m["outcomes"].setdefault(str(q.get("outcomeId")), {"players": {}})["players"][str(q.get("playerId", 0))] = q
    return out


def process_fixture(raw, ref, at_slugs, props=False):
    """Rohdaten -> kompaktes Spiel: je Markt/Tipp AT-Preise, beste Quote, Top-Rest, faire Konsens-Quote."""
    raw = to_v4(raw)
    per = {}   # mid -> oid -> {slug: price}
    for slug, mid, oid, pid, info in iter_prices(raw, props):
        key_o = oid if pid == "0" else f"{oid}|{pid}"
        per.setdefault(mid, {}).setdefault(key_o, {})[slug] = float(info["price"])
    markets = {}
    for mid, outs in per.items():
        meta = ref["markets"].get(mid)
        fair = consensus_fair(outs, meta)
        mk = {}
        for oid, prices in outs.items():
            at = {s: p for s, p in prices.items() if s in at_slugs}
            others = sorted(((s, p) for s, p in prices.items() if s not in at_slugs), key=lambda x: -x[1])
            best_all = max(prices.items(), key=lambda x: x[1])
            mk[oid] = {"at": at, "oth": [[s, p] for s, p in others[:12]], "best": [best_all[0], best_all[1]],
                       "n": len(prices), "fair": round(fair[oid], 3) if fair and fair.get(oid) else None}
        markets[mid] = mk
    return markets


def consensus_fair(outs, meta):
    """Faire Quote je Tipp: pro Buchmacher Marge herausrechnen (nur vollständige Märkte), dann Median."""
    oids = list(outs.keys())
    if any("|" in o for o in oids):
        return None
    expected = len(meta["outcomes"]) if meta and meta.get("outcomes") else None
    if len(oids) < 2 or (expected and len(oids) != expected):
        return None
    books = set.intersection(*(set(outs[o]) for o in oids))
    probs = {o: [] for o in oids}
    for b in books:
        inv = [1 / outs[o][b] for o in oids]
        tot = sum(inv)
        if tot <= 0 or tot > 1.5:      # unplausible Marge -> ignorieren
            continue
        for o, v in zip(oids, inv):
            probs[o].append(v / tot)
    if min(len(v) for v in probs.values()) < CFG.get("value_min_books", 4):
        return None
    med = {o: median(v) for o, v in probs.items()}
    s = sum(med.values())
    return {o: 1 / (med[o] / s) for o in oids if med[o] > 0}


def fetch_tournaments(api, tids, at_slugs, allowance, mode_state):
    """Sammelabruf pro Liga-Paket.
    Versucht zuerst ALLE Buchmacher in einem Abruf (1 Abruf je Paket). Lehnt die API das ab
    (Buchmacher-Angabe Pflicht), wird dauerhaft auf Gruppen à 3 AT-Buchmacher umgestellt."""
    results = {}

    def merge(data):
        for fx in as_list(data):
            fx = to_v4(fx)
            fid = fx.get("fixtureId")
            if not fid:
                continue
            cur = results.setdefault(fid, fx)
            if cur is not fx:
                cur.setdefault("bookmakerOdds", {}).update(fx.get("bookmakerOdds") or {})

    for batch in chunks(sorted(set(map(str, tids))), max(1, CFG["leagues_per_call"])):
        groups = [None] if mode_state.get("all_books", True) else chunks(at_slugs, MAX_BOOKS_PER_CALL)
        i = 0
        while i < len(groups):
            g = groups[i]
            if allowance["n"] <= 0:
                raise BudgetExhausted("Laufbudget")
            params = {"tournamentIds": ",".join(batch), "oddsFormat": "decimal", "language": "en", "verbosity": 3}
            if g:
                params["bookmakers"] = ",".join(g)
            try:
                data = api.get("/odds-by-tournaments", params, allowance["n"])
                allowance["n"] -= 1
                merge(data)
            except ApiError as e:
                allowance["n"] -= 1
                if g is None and e.code in (400, 422):
                    log("API verlangt Buchmacher-Angabe -> ab jetzt Gruppen à 3 Buchmacher je Abruf")
                    mode_state["all_books"] = False
                    groups = chunks(at_slugs, MAX_BOOKS_PER_CALL)
                    i = 0
                    continue
                log(f"Liga-Paket {batch[:3]}…: {e}")
            i += 1
    return results


# ---------------------------------------------------------------- Tipps lesen + zuordnen

def sheet_bridge():
    """Brücke zur Google-Tabelle (Apps-Script-Web-App), falls eingerichtet."""
    url, token = os.environ.get("SHEET_WEBAPP_URL", "").strip(), os.environ.get("SHEET_TOKEN", "").strip()
    return (url, token) if url and token else None


def bridge_call(payload=None):
    """GET (payload None) liest Tipps, POST schreibt Ergebnisse. Google antwortet mit einer
    Umleitung (302) auf die eigentliche Antwort – urllib folgt ihr automatisch per GET."""
    url, token = sheet_bridge()
    if payload is None:
        req = Request(f"{url}{'&' if '?' in url else '?'}{urlencode({'token': token})}",
                      headers={"User-Agent": "odds-portal/1.0"})
    else:
        data = json.dumps({**payload, "token": token}, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, method="POST",
                      headers={"Content-Type": "application/json", "User-Agent": "odds-portal/1.0"})
    with urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8")
    try:
        res = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("Tabellen-Brücke antwortet nicht mit JSON (Web-App-URL/Bereitstellung prüfen): "
                           + text[:200].replace(token, "[TOKEN]"))
    if not res.get("ok"):
        raise RuntimeError(f"Tabellen-Brücke: {res.get('error')}")
    return res


def read_tips():
    rows = None
    if sheet_bridge():
        try:
            res = bridge_call()
            rows = res.get("rows") or []
            log(f"Tipps direkt aus der Google-Tabelle gelesen (Blatt {res.get('sheet')}).")
        except Exception as e:   # noqa: BLE001
            log(f"Lesen über die Tabellen-Brücke fehlgeschlagen: {e}")
    if rows is None:
        url = os.environ.get("TIPS_CSV_URL", "").strip()
        if not url:
            log("Weder Tabellen-Brücke noch TIPS_CSV_URL eingerichtet – Tipps werden übersprungen.")
            return []
        if url.startswith("file://") or Path(url).exists():
            text = Path(url.replace("file://", "")).read_text(encoding="utf-8-sig")
        else:
            with urlopen(Request(url, headers={"User-Agent": "odds-portal/1.0"}), timeout=60) as r:
                text = r.read().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
    rows = [[str(x) for x in r] for r in rows]
    t = CFG["tips"]
    c = {k: col_index(v) for k, v in t["columns"].items()}
    tips = []
    for i, row in enumerate(rows[t["first_row"] - 1:], start=t["first_row"]):
        get = lambda k: row[c[k]].strip() if c.get(k) is not None and c[k] < len(row) else ""  # noqa: E731
        game, tip = get("game"), get("tip")
        if not game or not tip:
            continue
        tips.append({"row": i, "game": game, "tip": tip, "league": get("league"), "kick_raw": get("kickoff"),
                     "kick": parse_kickoff(get("kickoff")), "stake": get("stake"),
                     "min_odds": to_float(get("min_odds"))})
    log(f"{len(tips)} Tipps aus der Tabelle gelesen.")
    return tips


NAME_STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "sv", "fk", "sk", "bk", "if", "club", "the", "de", "city", "united", "utd"}


def tokens(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in NAME_STOP]


def name_score(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return 0.9
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    hit = sum(1 for x in ta if any(x == y or (len(x) >= 4 and y.startswith(x)) or (len(y) >= 4 and x.startswith(y))
                                   for y in tb))
    return hit / max(len(ta), len(tb)) * 0.85 + (0.1 if hit else 0)


def split_game(text):
    parts = re.split(r"\s+(?:vs\.?|v\.?|gegen|-|–|:)\s+", str(text), flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def youth_mismatch(a, b):
    """'Leicester City' darf nicht 'Leicester City U21' / Frauen / Reserve treffen."""
    tag = re.compile(r"\b(u\d{2}|ii|b|w|women|res|reserves|youth|jugend|frauen)\b", re.I)
    return bool(tag.search(str(a))) != bool(tag.search(str(b)))


def match_fixture(fixtures, teams, kick):
    best = None
    for f in fixtures:
        p1, p2 = f.get("participant1Name"), f.get("participant2Name")
        if youth_mismatch(teams[0], p1) and youth_mismatch(teams[0], p2):
            continue
        s1, s2 = name_score(teams[0], p1), name_score(teams[1], p2)
        r1, r2 = name_score(teams[0], p2), name_score(teams[1], p1)
        if r1 + r2 > s1 + s2:
            s1, s2 = r1, r2
        if s1 < 0.5 or s2 < 0.5:
            continue
        if youth_mismatch(teams[0], p1) or youth_mismatch(teams[1], p2):
            s1 -= 0.4
        score = s1 + s2
        st = parse_ts(f.get("startTime"))
        if kick and st:
            score -= min(1.0, abs((st - kick).total_seconds()) / (6 * 3600)) * 0.5
        if not best or score > best[0]:
            best = (score, f)
    return best[1] if best and best[0] >= 1.0 else None


def guess_sports(tip, ref):
    order = [s for s in CFG["sports"] if s in ref["sports"]]
    lg = tip["league"].lower()
    first = None
    if re.search(r"nba|euroleague|basket|acb|bbl|wnba|ncaa|eurocup", lg):
        first = "basketball"
    elif re.search(r"atp|wta|itf|challenger|tennis|davis|billie", lg):
        first = "tennis"
    elif lg:
        first = "soccer"
    if first in order:
        order.remove(first)
        order.insert(0, first)
    return order


def fixtures_for(api, sport_id, kick, cache, allowance):
    # Ganzer UTC-Kalendertag + 3 h Puffer je Seite (30 h, API erlaubt < 48 h) – ein Abruf je Sportart und Tag
    day_start = kick.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    key = f"{sport_id}|{day_start:%Y-%m-%d}"
    hit = cache.get(key)
    if hit and now_utc().timestamp() - hit["t"] < 6 * 3600:
        return hit["list"]
    if allowance["n"] <= 0:
        raise BudgetExhausted("Laufbudget")
    lst = as_list(api.get("/fixtures", {"sportId": sport_id, "from": iso_z(day_start - timedelta(hours=3)),
                                        "to": iso_z(day_start + timedelta(hours=27)), "language": "en"},
                          allowance["n"]))
    allowance["n"] -= 1
    slim = [{k: f.get(k) for k in ("fixtureId", "participant1Name", "participant2Name", "startTime", "tournamentId",
                                   "tournamentName", "categoryName", "statusId")} for f in lst]
    cache[key] = {"t": now_utc().timestamp(), "list": slim}
    return slim


def match_tips(api, tips, ref, games, allowance):
    mcache = load("tip_matches.json", {})
    fcache = load("fixture_lookup.json", {})
    # Spiele aus dem letzten Scan zuerst nutzen (kostenlos)
    known_by_sport = {}
    for g in games.values():
        known_by_sport.setdefault(g["sport_slug"], []).append({
            "fixtureId": g["fid"], "participant1Name": g["home"], "participant2Name": g["away"],
            "startTime": g["start"], "tournamentId": g["tid"], "tournamentName": g["league"], "categoryName": g["country"]})
    for t in tips:
        if not t["kick"]:
            t["status"] = "Anstoß nicht erkannt"
            continue
        teams = split_game(t["game"])
        if not teams:
            t["status"] = 'Spiel-Format nicht erkannt (erwartet "Heim vs Auswärts")'
            continue
        key = norm(t["game"]) + "|" + t["kick"].strftime("%Y%m%d")
        hit = mcache.get(key)
        if hit and hit.get("miss") and now_utc().timestamp() - hit["t"] < 6 * 3600:
            t["status"] = "Spiel nicht gefunden (Namen/Datum prüfen) – neue Suche später"
            continue
        if hit and not hit.get("miss"):
            t["fx"], t["sport_slug"] = hit["fx"], hit["sport"]
            continue
        found = None
        for slug in guess_sports(t, ref):
            found = match_fixture(known_by_sport.get(slug, []), teams, t["kick"])
            if not found:
                try:
                    found = match_fixture(fixtures_for(api, ref["sports"][slug]["id"], t["kick"], fcache, allowance),
                                          teams, t["kick"])
                except BudgetExhausted:
                    t["status"] = "Budget für diesen Lauf erreicht – Zuordnung im nächsten Lauf"
                    break
            if found:
                t["fx"], t["sport_slug"] = found, slug
                mcache[key] = {"t": now_utc().timestamp(), "fx": found, "sport": slug}
                break
        if not found and "status" not in t:
            t["status"] = "Spiel nicht gefunden (Namen/Datum prüfen)"
            mcache[key] = {"t": now_utc().timestamp(), "miss": True}
    # Aufräumen: Einträge älter als 10 Tage
    cutoff = now_utc().timestamp() - 10 * 86400
    save("tip_matches.json", {k: v for k, v in mcache.items() if v["t"] > cutoff})
    save("fixture_lookup.json", {k: v for k, v in fcache.items() if v["t"] > now_utc().timestamp() - 86400})


# ---------------------------------------------------------------- Tipp -> Markt

SIDE = {"HOME": "H", "1": "H", "HEIM": "H", "AWAY": "A", "2": "A", "AUSWÄRTS": "A", "AUSWAERTS": "A", "GAST": "A",
        "DRAW": "D", "X": "D", "UNENTSCHIEDEN": "D", "REMIS": "D"}


def parse_tip(text):
    s = str(text).strip().upper().replace(",", ".")
    side = r"(HOME|AWAY|HEIM|AUSWÄRTS|AUSWAERTS|GAST|1|2)"
    if m := re.fullmatch(side + r"\s*([+-]\s*\d+(?:\.\d+)?)", s):
        return {"kind": "handicap", "side": SIDE[m.group(1)], "line": float(m.group(2).replace(" ", ""))}
    if m := re.fullmatch(side + r"\s*(?:DNB|0|\+0|-0|DRAW NO BET)", s):
        return {"kind": "dnb", "side": SIDE[m.group(1)]}
    if m := re.fullmatch(r"(OVER|UNDER|ÜBER|UEBER|UNTER|O|U)\s*(\d+(?:\.\d+)?)", s):
        return {"kind": "total", "side": "over" if m.group(1) in ("OVER", "ÜBER", "UEBER", "O") else "under",
                "line": float(m.group(2))}
    if m := re.fullmatch(r"(?:BTTS|BEIDE TREFFEN|GG)\s*(YES|NO|JA|NEIN)?", s):
        return {"kind": "btts", "side": "yes" if m.group(1) in (None, "YES", "JA") else "no"}
    if s in ("NG", "BTTS NO"):
        return {"kind": "btts", "side": "no"}
    if s in ("1X", "X2", "12"):
        return {"kind": "dc", "side": s}
    if s in SIDE:
        return {"kind": "result", "side": SIDE[s]}
    return None


def outcome_by_name(meta, names):
    for oid, name in (meta.get("outcomes") or {}).items():
        if str(name).strip().lower() in names:
            return oid
    return None


def near(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def resolve_tip(spec, game_markets, ref, sport_id):
    """Liefert (mid, oid, hinweis) – bevorzugt den Markt mit den meisten Buchmachern."""
    cands = []
    for mid in game_markets:
        m = ref["markets"].get(mid)
        if not m or (m.get("sport") and int(m["sport"]) != int(sport_id)):
            continue
        if m["period"] and not re.search(r"full|ft|match|game", m["period"], re.I):
            continue
        typ, name = m["type"], m["name"].lower()
        tn = typ + " " + name
        is_hcp = re.search(r"handicap|spread|asian", tn) and not re.search(r"european|3[- ]?way", tn)
        is_total = re.search(r"total|over|under", tn) and not re.search(
            r"team|player|half|corner|card|booking|quarter|period|1st|2nd|3rd|first|second", name)
        is_result = typ == "1x2" or re.search(r"full ?time result|match result|^1x2", name)
        is_winner = re.search(r"moneyline|winner|^12$|home/away", tn) and not re.search(r"half|set |quarter|period", name)
        oid = None
        k = spec["kind"]
        if k == "handicap" and is_hcp:
            if spec["side"] == "H" and near(m["line"], spec["line"]):
                oid = outcome_by_name(m, {"1", "home"})
            elif spec["side"] == "A" and (near(m["line"], -spec["line"]) or near(m["line"], spec["line"])):
                oid = outcome_by_name(m, {"2", "away"})
        elif k == "total" and is_total and near(m["line"], spec["line"]):
            oid = outcome_by_name(m, {"over", "o"} if spec["side"] == "over" else {"under", "u"})
        elif k == "result" and is_result:
            oid = outcome_by_name(m, {"H": {"1", "home"}, "A": {"2", "away"}, "D": {"x", "draw"}}[spec["side"]])
        elif k == "result" and spec["side"] != "D" and is_winner:
            oid = outcome_by_name(m, {"1", "home"} if spec["side"] == "H" else {"2", "away"})
        elif k == "btts" and re.search(r"both teams to score|btts", name) and "half" not in name:
            oid = outcome_by_name(m, {spec["side"]})
        elif k == "dc" and "double chance" in name and "half" not in name:
            oid = outcome_by_name(m, {spec["side"].lower(), " or ".join(spec["side"].lower())})
        elif k == "dnb" and "draw no bet" in name and "half" not in name:
            oid = outcome_by_name(m, {"1", "home"} if spec["side"] == "H" else {"2", "away"})
        if oid and oid in game_markets[mid]:
            cands.append((game_markets[mid][oid]["n"], mid, oid))
    if cands:
        cands.sort(reverse=True)
        return cands[0][1], cands[0][2], ""
    # Äquivalenzen
    if spec["kind"] == "handicap" and near(spec["line"], -0.5):
        r = resolve_tip({"kind": "result", "side": spec["side"]}, game_markets, ref, sport_id)
        if r:
            return r[0], r[1], "als Siegwette (gleichwertig zu -0.5)"
    if spec["kind"] == "handicap" and near(spec["line"], 0):
        return resolve_tip({"kind": "dnb", "side": spec["side"]}, game_markets, ref, sport_id)
    return None


GEN_SYN = {"ueber": "over", "uber": "over", "unter": "under", "ja": "yes", "nein": "no", "heim": "home",
           "auswaerts": "away", "auswarts": "away", "gast": "away", "unentschieden": "draw", "remis": "draw",
           "btts": "both teams score", "gg": "both teams score yes", "ng": "both teams score no", "dc": "double chance",
           "ou": "over under", "ah": "asian handicap", "tore": "goals", "saetze": "sets", "satze": "sets",
           "spiele": "games", "ecken": "corners", "karten": "cards", "halbzeit": "half"}
GEN_STOP = {"the", "to", "of", "and", "or", "full", "time", "fulltime", "match", "ft", "tipp", "bet", "-"}


def gen_tokens(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    s = re.sub(r"(\d)\s*[:\-]\s*(\d)", r"\1:\2", s)
    out = []
    for x in filter(None, re.split(r"[^a-z0-9.:+\-]+", s)):
        x = x.lstrip("+")
        for y in GEN_SYN.get(x, x).split():
            if y not in GEN_STOP:
                out.append(y)
    return out


def resolve_generic(text, game_markets, ref, sport_id):
    tt = gen_tokens(text)
    if not tt:
        return None
    nums = [x for x in tt if re.fullmatch(r"-?\d+(\.\d+)?", x)]
    best = None
    for mid, outs in game_markets.items():
        m = ref["markets"].get(mid)
        if not m or (m.get("sport") and int(m["sport"]) != int(sport_id)):
            continue
        for oid, oname in (m.get("outcomes") or {}).items():
            if oid not in outs:
                continue
            ot = gen_tokens(oname)
            line = m.get("line")
            line_s = (str(line) if line not in (None, "", 0) else "")
            ct = gen_tokens(f"{m['name']} {line_s} {'' if re.search('full', m['period'] or 'full', re.I) else m['period']} {oname}")
            if not ot or not all(x in tt for x in ot):
                continue
            if any(n not in ct and str(float(n)).rstrip("0").rstrip(".") not in ct for n in nums):
                continue
            hit = sum(1 for x in tt if x in ct)
            if hit / len(tt) < 0.75:
                continue
            score = hit / len(tt) - 0.02 * max(0, len(ct) - hit)
            key = (score, outs[oid]["n"])
            if not best or key > best[0]:
                best = (key, mid, oid)
    return (best[1], best[2], "allgemein erkannt") if best else None


# ---------------------------------------------------------------- Tipps auswerten

def evaluate_tip(t, game, ref, books):
    """Ergebnis-Objekt für das Portal."""
    sid = ref["sports"][t["sport_slug"]]["id"]
    spec = parse_tip(t["tip"])
    r = resolve_tip(spec, game["markets"], ref, sid) if spec else None
    if not r:
        r = resolve_generic(t["tip"], game["markets"], ref, sid)
    if not r:
        return {"status": "Tipp nicht erkannt oder Markt nicht aktiv im Angebot"}
    mid, oid, note = r
    o = game["markets"][mid][oid]
    meta = ref["markets"].get(mid)
    at = o["at"]
    best_at = max(at.items(), key=lambda x: x[1]) if at else None
    res = {
        "status": "OK" + (f" ({note})" if note else ""),
        "market": market_label(meta), "outcome": (meta or {}).get("outcomes", {}).get(oid, oid),
        "mid": mid, "oid": oid,
        "at": at, "best_at": list(best_at) if best_at else None, "best_all": o["best"], "others": o["oth"],
        "n": o["n"], "fair": o["fair"],
        "avg_at": round(sum(at.values()) / len(at), 3) if at else None,
    }
    ref_price = best_at[1] if best_at else None
    res["ev"] = round((ref_price / o["fair"] - 1) * 100, 2) if ref_price and o["fair"] else None
    if t["min_odds"]:
        ok = sorted(((s, p) for s, p in at.items() if p >= t["min_odds"] - 1e-9), key=lambda x: -x[1])
        res["reach"] = bool(ok)
        res["reach_books"] = ok
        res["vs_min"] = round((ref_price / t["min_odds"] - 1) * 100, 2) if ref_price else None
    return res


# ---------------------------------------------------------------- Quotenverlauf (kostenlos)

def history_for(api, fid, mid, oid, books3):
    try:
        d = api.get("/historical-odds", {"fixtureId": fid, "bookmakers": ",".join(books3[:3])})
    except (ApiError, BudgetExhausted) as e:
        log(f"Verlauf {fid}: {e}")
        return None
    series = {}
    for slug, bm in ((d or {}).get("bookmakers") or {}).items():
        o = (((bm or {}).get("markets") or {}).get(str(mid)) or {}).get("outcomes", {}).get(str(oid)) or {}
        entries = (o.get("players") or {}).get("0") or []
        if isinstance(entries, dict):
            entries = [entries]
        pts = sorted(((e.get("createdAt"), e.get("price")) for e in entries
                      if isinstance(e, dict) and isinstance(e.get("price"), (int, float))), key=lambda x: str(x[0]))
        if len(pts) > 160:   # ausdünnen
            step = len(pts) / 160
            pts = [pts[int(i * step)] for i in range(160)] + [pts[-1]]
        if pts:
            series[slug] = [[p[0], p[1]] for p in pts]
    return series


# ---------------------------------------------------------------- Verschlüsselung

def encrypt_blob(key, obj):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, raw, None)


def derive_key(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)


# ---------------------------------------------------------------- Hauptablauf

def main():
    key = os.environ.get("ODDSPAPI_KEY", "").strip()
    password = os.environ.get("PORTAL_PASSWORD", "").strip()
    if not key:
        sys.exit("ODDSPAPI_KEY fehlt (GitHub Secret).")
    if len(password) < 8:
        sys.exit("PORTAL_PASSWORD fehlt oder ist kürzer als 8 Zeichen (GitHub Secret).")

    api = Api(key)
    api.load_account()
    plan = plan_budget(api)
    scan_state = load("scan.json", {"last": 0, "all_books": True, "cost": 60})
    log(f"Budget: heute noch {plan['left_today']} Abrufe (Tagesziel {plan['daily']}), "
        f"{plan['runs_left']} Läufe bis Mitternacht" +
        (f", Monat {api.account['used']}/{api.account['limit']}" if api.account else ""))

    games = load("games.json", {})            # fid -> kompaktes Spiel
    errors = []
    ref = reference(api, plan["left_today"])
    if not ref.get("markets"):
        sys.exit("Stammdaten fehlen (Budget?).")
    at_slugs = [b["slug"] for b in ref["books"]]

    # Ist heute noch ein Gesamt-Scan fällig? Dann Budget dafür zurückhalten.
    sc = CFG["scan"]
    now_v = datetime.now(VIENNA)
    scan_due = (now_utc().timestamp() - scan_state["last"] > sc["every_hours"] * 3600 - 600
                and now_v.hour >= sc["not_before_hour_vienna"])
    scan_budget = min(int(plan["daily"] * sc["share_of_daily_budget"]), plan["left_today"]) if scan_due else 0
    tip_budget_total = plan["left_today"] - scan_budget
    per_run = max(1, -(-tip_budget_total // plan["runs_left"])) if tip_budget_total > 0 else 0
    # nicht verbrauchte Laufbudgets der Vergangenheit dürfen genutzt werden, aber max. 4x Normalwert
    allowance = {"n": min(tip_budget_total, per_run * 4)}

    # 1) Tipps
    tips = read_tips()
    horizon_past = now_utc() - timedelta(days=CFG["tips"]["ignore_older_than_days"])
    tips = [t for t in tips if not t["kick"] or t["kick"] >= horizon_past]
    match_tips(api, tips, ref, games, allowance)

    # 2) Welche Ligen brauchen frische Quoten? (nahe Tipps immer, ferne seltener)
    tip_odds_t = load("tip_refresh.json", {})       # tid -> letzter Abruf
    need = {}                                         # tid -> frühester Anstoß
    for t in tips:
        fx = t.get("fx")
        if not fx or not fx.get("tournamentId"):
            continue
        st = parse_ts(fx.get("startTime")) or t["kick"]
        if st and st < now_utc():
            continue                                  # läuft schon: letzte Quoten bleiben stehen
        hours_to = (st - now_utc()).total_seconds() / 3600 if st else 99
        last = tip_odds_t.get(str(fx["tournamentId"]), 0)
        due = hours_to <= CFG["tips"]["near_hours"] or now_utc().timestamp() - last > CFG["tips"]["far_refresh_hours"] * 3600 - 300
        if due or fx["fixtureId"] not in games:
            tid = str(fx["tournamentId"])
            need[tid] = min(need.get(tid, 9e9), hours_to)
    order = sorted(need, key=lambda k: need[k])       # nächster Anstoß zuerst
    groups_per_batch = 1 if scan_state.get("all_books", True) else -(-len(at_slugs) // MAX_BOOKS_PER_CALL)
    max_batches = allowance["n"] // max(1, groups_per_batch)
    take = order[:max_batches * CFG["leagues_per_call"]]
    if len(take) < len(order):
        log(f"Budget reicht diesen Lauf für {len(take)} von {len(order)} Tipp-Ligen (nächste Anstöße zuerst).")
    fresh = {}
    if take:
        try:
            fresh = fetch_tournaments(api, take, at_slugs, allowance, scan_state)
        except BudgetExhausted as e:
            log(f"Budget erreicht: {e}")
        for tid in take:
            tip_odds_t[tid] = now_utc().timestamp()
    tip_tids = set(take)

    # 3) Gesamt-Scan (einmal täglich)
    if scan_due and scan_budget > 0:
        scan_allow = {"n": scan_budget}
        try:
            start, end = now_utc(), now_utc() + timedelta(hours=min(47, sc["hours_ahead"]))
            all_fx = []
            for slug in CFG["sports"]:
                if slug not in ref["sports"]:
                    continue
                lst = as_list(api.get("/fixtures", {"sportId": ref["sports"][slug]["id"], "from": iso_z(start),
                                                    "to": iso_z(end), "statusId": 0, "hasOdds": "true",
                                                    "language": "en"}, scan_allow["n"]))
                scan_allow["n"] -= 1
                for f in lst:
                    f["_sport"] = slug
                all_fx += lst
            meta = {f["fixtureId"]: f for f in all_fx if f.get("fixtureId")}
            counts = {}
            for f in all_fx:
                if f.get("tournamentId"):
                    counts[str(f["tournamentId"])] = counts.get(str(f["tournamentId"]), 0) + 1
            tids = sorted((t for t in counts if t not in tip_tids), key=lambda t: -counts[t])  # große Ligen zuerst
            before = api.spent_run
            got = fetch_tournaments(api, tids, at_slugs, scan_allow, scan_state)
            for fid, raw in got.items():
                if fid in meta:
                    raw.setdefault("_sport", meta[fid].get("_sport"))
                    for k in ("participant1Name", "participant2Name", "startTime", "tournamentName", "categoryName",
                              "tournamentId"):
                        raw[k] = raw.get(k) or meta[fid].get(k)
                fresh.setdefault(fid, raw)
            scan_state["cost"] = api.spent_run - before
            log(f"Gesamt-Scan: {len(meta)} Spiele in {len(counts)} Ligen, {len(got)} mit Quoten geladen.")
        except BudgetExhausted as e:
            log(f"Gesamt-Scan durch Budget begrenzt ({e}) – Teilergebnis wird angezeigt.")
        scan_state["last"] = now_utc().timestamp()

    # 4) Rohdaten -> kompakte Spiele
    sport_of_tid = {}
    for t in tips:
        if t.get("fx") and t["fx"].get("tournamentId"):
            sport_of_tid[str(t["fx"]["tournamentId"])] = t["sport_slug"]
    for fid, raw in fresh.items():
        tid = str(raw.get("tournamentId") or "")
        sport_slug = raw.get("_sport") or sport_of_tid.get(tid) or (games.get(fid) or {}).get("sport_slug")
        if not sport_slug:
            sid = raw.get("sportId")
            sport_slug = next((k for k, v in ref["sports"].items() if v["id"] == sid), "soccer")
        games[fid] = {
            "fid": fid, "sport_slug": sport_slug, "sport": ref["sports"].get(sport_slug, {}).get("name", sport_slug),
            "tid": raw.get("tournamentId"), "league": raw.get("tournamentName") or "",
            "country": raw.get("categoryName") or "", "start": raw.get("startTime"),
            "home": raw.get("participant1Name") or "", "away": raw.get("participant2Name") or "",
            "updated": iso_z(now_utc()),
            "markets": process_fixture(raw, ref, set(at_slugs), CFG.get("include_player_props", False)),
        }
    # alte Spiele entfernen (Anstoß > 1 Tag her)
    games = {k: g for k, g in games.items()
             if (parse_ts(g["start"]) or now_utc()) > now_utc() - timedelta(days=1)}

    # 5) Tipps auswerten + Verlauf
    prev_tips = {f'{p["game"]}|{p["tip"]}|{p["kick_raw"]}': p for p in load("tips_out.json", [])}
    hist_cache = load("history.json", {})
    hist_budget = CFG["history_max_fixtures_per_run"]
    out_tips = []
    for t in tips:
        sig = f'{t["game"]}|{t["tip"]}|{t["kick_raw"]}'
        o = {"row": t["row"], "game": t["game"], "tip": t["tip"], "league": t["league"], "kick_raw": t["kick_raw"],
             "kick": iso_z(t["kick"]) if t["kick"] else None, "stake": t["stake"], "min_odds": t["min_odds"]}
        fx = t.get("fx")
        if not fx:
            o["status"] = t.get("status", "nicht zugeordnet")
            out_tips.append(o)
            continue
        g = games.get(fx["fixtureId"])
        o.update({"fid": fx["fixtureId"], "found": f'{fx.get("participant1Name")} vs {fx.get("participant2Name")}',
                  "found_league": fx.get("tournamentName"), "start": fx.get("startTime"), "sport": t["sport_slug"]})
        st = parse_ts(fx.get("startTime"))
        if not g:
            prev = prev_tips.get(sig)
            if prev and prev.get("fid") == fx["fixtureId"] and prev.get("market"):
                o.update({k: v for k, v in prev.items() if k not in o})
                o["status"] = "läuft/beendet – letzte Quoten" if st and st < now_utc() else prev.get("status", "")
            else:
                o["status"] = "Quoten noch nicht geladen (Budget) – folgt im nächsten Lauf"
            out_tips.append(o)
            continue
        o.update(evaluate_tip(t, g, ref, ref["books"]))
        o["odds_time"] = g["updated"]
        if st and st < now_utc():
            o["status"] = "läuft/beendet – letzte Quoten"
        # Verlauf: die 3 AT-Buchmacher mit den besten Quoten (kostenlos, aber 5 s Pause je Abruf)
        if o.get("mid"):
            hkey = f'{fx["fixtureId"]}|{o["mid"]}|{o["oid"]}'
            h = hist_cache.get(hkey)
            stale = not h or now_utc().timestamp() - h["t"] > 1800
            if stale and hist_budget > 0 and (not st or st > now_utc() - timedelta(hours=3)):
                top3 = [s for s, _ in sorted(o["at"].items(), key=lambda x: -x[1])][:3] or at_slugs[:3]
                series = history_for(api, fx["fixtureId"], o["mid"], o["oid"], top3)
                hist_budget -= 1
                if series is not None:
                    h = hist_cache[hkey] = {"t": now_utc().timestamp(), "s": series}
            o["history"] = (h or {}).get("s")
        out_tips.append(o)
    hist_cache = {k: v for k, v in hist_cache.items() if now_utc().timestamp() - v["t"] < 3 * 86400}

    # 6) Value-Liste (nur kommende Spiele, AT-Buchmacher)
    value = []
    for g in games.values():
        st = parse_ts(g["start"])
        if not st or st < now_utc():
            continue
        for mid, outs in g["markets"].items():
            meta = ref["markets"].get(mid)
            for oid, o in outs.items():
                if not o["fair"] or not o["at"]:
                    continue
                s, p = max(o["at"].items(), key=lambda x: x[1])
                ev = (p / o["fair"] - 1) * 100
                if ev >= CFG["value_min_ev_pct"]:
                    value.append({"fid": g["fid"], "sport": g["sport"], "league": g["league"], "country": g["country"],
                                  "start": g["start"], "home": g["home"], "away": g["away"],
                                  "market": market_label(meta), "outcome": (meta or {}).get("outcomes", {}).get(oid, oid),
                                  "book": s, "price": p, "fair": o["fair"], "ev": round(ev, 2), "n": o["n"],
                                  "at": o["at"]})
    value.sort(key=lambda v: -v["ev"])
    value = value[:400]

    # 7) Ausgabe
    books = ref["books"]
    summary = []
    for g in sorted(games.values(), key=lambda g: (str(g["start"]), g["league"])):
        st = parse_ts(g["start"])
        if not st or st < now_utc() - timedelta(hours=3):
            continue
        main = main_market(g, ref)
        summary.append({"fid": g["fid"], "sport": g["sport"], "league": g["league"], "country": g["country"],
                        "start": g["start"], "home": g["home"], "away": g["away"], "updated": g["updated"],
                        "nm": len(g["markets"]), "main": main})
    acc = api.account or {}
    index = {
        "updated": iso_z(now_utc()), "last_scan": iso_z(datetime.fromtimestamp(scan_state["last"], timezone.utc))
        if scan_state["last"] else None,
        "books": books, "books_missing": ref.get("books_missing", []),
        "budget": {"used": acc.get("used"), "limit": acc.get("limit"), "today": api.spent_today(),
                   "daily_target": plan["daily"], "this_run": api.spent_run,
                   "mode": "alle Buchmacher je Abruf" if scan_state.get("all_books", True) else "3 Buchmacher je Abruf"},
        "tips": out_tips, "value": value, "games": summary, "log": LOG[-60:],
    }
    if sheet_bridge():
        write_back_to_sheet(out_tips, ref["books"])
        index["log"] = LOG[-60:]
    write_output(password, index, games, ref)
    save("games.json", games)
    save("scan.json", scan_state)
    save("tip_refresh.json", {k: v for k, v in tip_odds_t.items() if now_utc().timestamp() - v < 3 * 86400})
    save("tips_out.json", [{**o, "history": None} for o in out_tips])
    save("history.json", hist_cache)
    api.ledger and save("budget.json", api.ledger)
    log(f"Fertig: {len(out_tips)} Tipps, {len(summary)} Spiele, {len(value)} Value-Tipps, "
        f"{api.spent_run} Abrufe in diesem Lauf.")


def main_market(g, ref):
    """Hauptmarkt für die Übersicht: 1X2 (Fußball) bzw. Sieger (Basketball/Tennis)."""
    best = None
    for mid, outs in g["markets"].items():
        m = ref["markets"].get(mid)
        if not m or (m["period"] and not re.search(r"full|ft|match|game", m["period"], re.I)):
            continue
        tn = m["type"] + " " + m["name"].lower()
        ok = (m["type"] == "1x2" or re.search(r"full ?time result|match result", tn)) if g["sport_slug"] == "soccer" \
            else (re.search(r"moneyline|winner|^12 |home/away", tn) and not re.search(r"half|set |quarter|period", tn))
        if not ok:
            continue
        n = sum(o["n"] for o in outs.values())
        if not best or n > best[0]:
            best = (n, mid)
    if not best:
        return None
    mid = best[1]
    meta = ref["markets"].get(mid) or {}
    names = meta.get("outcomes", {})
    order = list(names.keys()) or list(g["markets"][mid].keys())
    return {"label": market_label(meta), "outcomes": [
        {"name": names.get(oid, oid), "at": g["markets"][mid][oid]["at"], "best": g["markets"][mid][oid]["best"],
         "best_at": list(max(g["markets"][mid][oid]["at"].items(), key=lambda x: x[1]))
         if g["markets"][mid][oid]["at"] else None, "fair": g["markets"][mid][oid]["fair"]}
        for oid in order if oid in g["markets"][mid]]}


def write_back_to_sheet(out_tips, books):
    """Schreibt die Quoten zeilengenau rechts neben die Tipps in die bestehende Google-Tabelle."""
    header = ["Status", "Mindestquote erreichbar", "Beste Quote", "Bester Buchmacher", "Value %", "Faire Quote",
              "Anzahl Buchmacher", "Spiel (API)", "Anstoß (API)", "Markt"] + [b["label"] for b in books] + \
             ["Bester sonstiger", "Stand"]
    labels = {b["slug"]: b["label"] for b in books}
    stamp = datetime.now(VIENNA).strftime("%d.%m.%Y %H:%M")
    rows = []
    for o in out_tips:
        best = o.get("best_at")
        if o.get("min_odds") is None:
            reach = "keine Mindestquote" if best else ""
        elif o.get("reach") is None:
            reach = ""
        elif o["reach"]:
            reach = f"JA ({len(o.get('reach_books') or [])} Buchmacher)"
        else:
            reach = f"NEIN (beste {best[1]:.2f})" if best else "NEIN (kein aktives Angebot)"
        st = parse_ts(o.get("start"))
        oth = (o.get("others") or [None])[0]
        at = o.get("at") or {}
        vals = [o.get("status", ""), reach,
                best[1] if best else "", labels.get(best[0], best[0]) if best else "",
                o.get("ev") if o.get("ev") is not None else "", o.get("fair") or "", o.get("n") or "",
                o.get("found") or "", st.astimezone(VIENNA).strftime("%d.%m.%Y %H:%M") if st else "",
                (f'{o["market"]} – {o["outcome"]}' if o.get("market") else "")]
        vals += [at.get(b["slug"], "") for b in books]
        vals += [f"{oth[0]} {oth[1]:.2f}" if oth else "", stamp if o.get("market") else ""]
        rows.append({"row": o["row"], "sig": norm(o["game"]) + "|" + norm(o["tip"]), "values": vals})
    layout = {"reach_col": 1, "value_col": 4, "books_from": 10, "books_count": len(books)}
    try:
        res = bridge_call({"header": header, "rows": rows, "layout": layout})
        msg = f"Google-Tabelle aktualisiert: {res.get('written')} Zeilen"
        if res.get("moved"):
            msg += f", {res['moved']} davon waren verschoben und wurden per Spiel+Tipp gefunden"
        if res.get("skipped"):
            msg += f", übersprungen (Zeile geändert/gelöscht): {res['skipped'][:10]}"
        log(msg)
    except Exception as e:   # noqa: BLE001
        log(f"Schreiben in die Google-Tabelle fehlgeschlagen: {e}")


def write_output(password, index, games, ref):
    OUT.mkdir(parents=True, exist_ok=True)
    gdir = OUT / "g"
    gdir.mkdir(exist_ok=True)
    salt = secrets.token_bytes(16)
    iters = 150_000
    key = derive_key(password, salt, iters)
    keep = set()
    for g in games.values():
        st = parse_ts(g["start"])
        if not st or st < now_utc() - timedelta(hours=3):
            continue
        detail = {"fid": g["fid"], "updated": g["updated"], "markets": []}
        for mid, outs in g["markets"].items():
            meta = ref["markets"].get(mid) or {}
            names = meta.get("outcomes", {})
            detail["markets"].append({"id": mid, "label": market_label(meta) if meta else f"Markt {mid}",
                                      "outcomes": [{"name": names.get(oid.split("|")[0], oid), "at": o["at"],
                                                    "best": o["best"], "fair": o["fair"], "n": o["n"],
                                                    "oth": o["oth"][:5]} for oid, o in outs.items()]})
        detail["markets"].sort(key=lambda m: m["label"])
        fname = hashlib.sha256((g["fid"] + password).encode()).hexdigest()[:20] + ".bin"   # Dateiname verrät nichts
        (gdir / fname).write_bytes(encrypt_blob(key, detail))
        keep.add(fname)
        for s in index["games"]:
            if s["fid"] == g["fid"]:
                s["file"] = fname
    for p in gdir.glob("*.bin"):
        if p.name not in keep:
            p.unlink()
    (OUT / "index.bin").write_bytes(encrypt_blob(key, index))
    (OUT / "meta.json").write_text(json.dumps({"salt": base64.b64encode(salt).decode(), "iterations": iters,
                                               "v": 1}), encoding="utf-8")


if __name__ == "__main__":
    main()

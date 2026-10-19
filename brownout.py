#!/usr/bin/env python3
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (brownout-check; personal use)"}
HOME = os.path.expanduser(os.environ.get("BROWNOUT_HOME", "~/brownout"))
STATE = os.path.join(HOME, "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
CONFIG = os.environ.get("BROWNOUT_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
try:
    CFG = json.load(open(CONFIG))
except (OSError, ValueError) as e:
    sys.exit(f"cannot read config {CONFIG}: {e} (copy config.example.json)")


def words(s):
    return r"\s*".join(re.escape(w) for w in s.split())


def alt(pats):
    return re.compile("|".join(pats) or r"(?!x)x", re.I)


API = CFG.get("feed", "")
if not API:
    sys.exit(f"set \"feed\" in {CONFIG}")
UTILITY = CFG.get("utility", "the power company")
MY_FEEDERS = {f.strip() for f in CFG.get("feeders", [])}
MY_SUBSTATIONS = {s.lower() for s in CFG.get("substations", [])}
MY_STREETS = CFG.get("streets", [])
MY_NEARBY = CFG.get("nearby", [])
MY_WORDS = alt([words(w) for w in MY_STREETS])
NEARBY = alt([rf"\b{words(w)}\b" for w in MY_NEARBY])
FEEDER_PATS = [r"\s*[-–—~.]?\s*".join(re.escape(p) for p in re.split(r"[-–—~.]", f)) + r"\b" for f in MY_FEEDERS]
RELEVANT = alt(FEEDER_PATS + [words(w) for w in list(MY_SUBSTATIONS) + MY_STREETS]
               + [r"all\s+(feeders|substations)", "entire", "postpon", "defer", "cancel", "resched"])
MINE = ", ".join(sorted(MY_FEEDERS) + CFG.get("substations", []) + MY_STREETS) or "your area"
CLAUDE = ["claude", "-p", "--setting-sources", "project", "--model", "haiku", "--no-session-persistence",
          "--tools", "Read"]

PROMPT = """Read these {utility} power advisory images: {files}
The post title is: {title}
Post text (may be empty): {text}

Return ONLY a JSON object, no prose:
{{"kind": "scheduled" | "emergency" | "deferred" | "cancelled" | "other",
  "events": [{{"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM",
              "substations": ["names exactly as written"], "feeders": ["like 1-7"],
              "all_feeders_of_substation": true or false,
              "areas": "affected areas text exactly as written", "purpose": "short"}}]}}
One event per date/time block per image. Times in 24h. A range like "Feeder 1-7" is ONE feeder id "1-7".
If a block has separate windows (for example an isolation window and a later normalization window), make each
window its own event with the same areas, because power comes back in between.
"deferred" means the listed interruptions are postponed. Year is {year} unless the image says otherwise."""


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"seen": [], "events": []}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def ntfy(title, body, link=None, tags="zap", priority="default"):
    if not NTFY_TOPIC:
        log("ntfy (no topic):", title, "|", body)
        return
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": tags, "Priority": priority}
    if link:
        headers["Click"] = link
    req = urllib.request.Request(f"{NTFY_URL}/{NTFY_TOPIC}", data=body.encode(), headers=headers)
    urllib.request.urlopen(req, timeout=20).read()


def read_post(post):
    content = post["content"]["rendered"]
    title = html.unescape(post["title"]["rendered"])
    text = html.unescape(" ".join(re.sub(r"<[^>]+>", " ", content).split()))[:1500]
    urls = re.findall(r'<img[^>]+src="([^"]+)"', content)
    with tempfile.TemporaryDirectory(prefix="brownout-") as d:
        files = []
        for i, u in enumerate(urls[:16]):
            path = os.path.join(d, f"advisory{i}{os.path.splitext(u)[1] or '.jpg'}")
            with open(path, "wb") as f:
                f.write(get(u, 60))
            files.append(path)
        if not files and not text:
            return {"kind": "other", "events": []}
        seen_text = ocr(files)
        readable = seen_text is not None and len(re.findall(r"[A-Za-z]{3,}", seen_text)) >= 25 * len(files)
        if readable and not RELEVANT.search(seen_text + " " + title + " " + text):
            log(f"  OCR pre-check: nothing about {MINE}, skipping Claude")
            return {"kind": "other", "events": [], "skipped": True}
        prompt = PROMPT.format(utility=UTILITY, files=", ".join(files) or "(none)", title=title, text=text or "(none)",
                               year=post["date"][:4])
        r = subprocess.run(CLAUDE + ["--add-dir", d], input=prompt, capture_output=True, text=True, timeout=240,
                           cwd=d)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError(f"no JSON from claude: {r.stdout[:300]} {r.stderr[:300]}")
    return json.loads(m.group(0))


def ocr(files):
    if not shutil.which("tesseract"):
        return None
    out = []
    for f in files:
        try:
            r = subprocess.run(["tesseract", f, "-", "--psm", "3"], capture_output=True, text=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        out.append(r.stdout)
    return "\n".join(out)


def affects_me(ev):
    feeders = {f.strip() for f in ev.get("feeders") or []}
    subs = {s.lower() for s in ev.get("substations") or []}
    named_sub = any(any(mine in s for mine in MY_SUBSTATIONS) for s in subs)
    hit = feeders & MY_FEEDERS
    if hit:
        return f"your feeder {', '.join(sorted(hit))}"
    if named_sub and (ev.get("all_feeders_of_substation") or not feeders):
        sub = next(c for c in CFG["substations"] if any(c.lower() in s for s in subs))
        return f"all of {sub} substation"
    for w in MY_STREETS:
        r = re.compile(words(w), re.I)
        if r.search(ev.get("areas") or "") or r.search(ev.get("purpose") or ""):
            return w
    return None


def when(ev):
    day = dt.date.fromisoformat(ev["date"])
    def t(x):
        try:
            return dt.datetime.strptime(x, "%H:%M").strftime("%-I:%M %p").replace(":00", "")
        except (TypeError, ValueError):
            return x or "?"
    return f"{day.strftime('%a %b %-d')}, {t(ev.get('start'))} to {t(ev.get('end'))}"


def check(state, dry=False):
    posts = json.loads(get(API))
    today = dt.date.today()
    for post in sorted(posts, key=lambda p: p["id"]):
        if post["id"] in state["seen"]:
            continue
        title = html.unescape(post["title"]["rendered"])
        log("new advisory:", post["id"], title)
        try:
            info = read_post(post)
        except Exception as e:
            log("  could not read:", e)
            continue
        log("  parsed:", json.dumps(info)[:600])
        for ev in info.get("events") or []:
            try:
                day = dt.date.fromisoformat(ev["date"])
            except (KeyError, ValueError):
                continue
            why = affects_me(ev)
            if not why and NEARBY.search(ev.get("areas") or ""):
                log(f"  mentions {'/'.join(MY_NEARBY)} but not your feeder/street, no alert:", ev.get("feeders"), (ev.get("areas") or "")[:120])
            if not why or day < today:
                continue
            kind = info.get("kind")
            if kind in ("deferred", "cancelled"):
                title_txt = f"Brownout postponed: {when(ev)}"
                body = f"The interruption that would hit {why} is {kind}. New date to follow."
                for e in state["events"]:
                    if e["date"] == ev["date"]:
                        e["cancelled"] = True
            else:
                title_txt = f"Brownout {when(ev)}"
                body = f"Affects {why} ({UTILITY}). {ev.get('purpose') or ''}".strip()
                state["events"].append({**ev, "why": why, "link": post["link"], "reminded": False})
            log("  ALERT:", title_txt, "|", body)
            if not dry:
                ntfy(title_txt, body, post["link"], priority="high")
        state["seen"].append(post["id"])
        state["seen"] = state["seen"][-200:]
        save_state(state)


def remind(state, dry=False):
    now = dt.datetime.now()
    for ev in state["events"]:
        if ev.get("reminded") or ev.get("cancelled"):
            continue
        day = dt.date.fromisoformat(ev["date"])
        if day == now.date() + dt.timedelta(days=1) and now.hour >= 19:
            log("REMIND:", when(ev))
            if not dry:
                ntfy(f"Tomorrow: brownout {when(ev)}", f"Affects {ev['why']}. Charge your stuff tonight.",
                     ev.get("link"), tags="zap,alarm_clock", priority="high")
            ev["reminded"] = True
    state["events"] = [e for e in state["events"] if dt.date.fromisoformat(e["date"]) >= now.date()]
    save_state(state)


def main():
    os.makedirs(HOME, exist_ok=True)
    state = load_state()
    dry = "--dry" in sys.argv
    if "--test-latest" in sys.argv:
        state = {"seen": [], "events": []}
        posts = json.loads(get(API))
        for post in sorted(posts, key=lambda p: p["id"])[-3:]:
            info = read_post(post)
            print(html.unescape(post["title"]["rendered"]))
            print(json.dumps(info, indent=1))
            for ev in info.get("events") or []:
                print("  ->", ev.get("date"), "affects me:", affects_me(ev))
        return
    check(state, dry)
    remind(state, dry)


if __name__ == "__main__":
    main()

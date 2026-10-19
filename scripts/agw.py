#!/usr/bin/env python3
"""Helper behind the demo scripts. Python 3.8+, standard library only.

It talks to agentgateway's own UI API, so every change it makes shows up in the UI
exactly as if you had clicked it.

  fast-forward              add the remaining providers, models and keys, then replay a week of traffic
  ask KEY MODEL             one request as a virtual key (by name); --until-blocked keeps going until a 4xx
  apply-policies [5H WEEK]  budgets on nightly-agent and the *-mini allow-list on intern
  budgets [KEY]             budget usage from the gateway
  report                    spend by user, in the terminal. The README uses the Analytics page for this
  point-at-mock             send existing providers to the local mock (trying the demo without keys)
  preflight                 checks before you start
"""
import argparse, json, os, platform, random, secrets, shutil, socket, sqlite3, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("AGW_URL", "http://localhost:4000").rstrip("/")
MOCK = os.environ.get("DEMO_MOCK") == "1"
MOCK_URL = os.environ.get("MOCK_LLM_URL", "http://127.0.0.1:3091").rstrip("/")
CLAUDE = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
CLAUDE_SMALL = os.environ.get("CLAUDE_SMALL_MODEL", "claude-haiku-4-5")
OPENAI = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
OPENAI_BIG = os.environ.get("OPENAI_BIG_MODEL", "gpt-5")
PEOPLE = ["alice", "bob", "support-team", "nightly-agent", "intern"]
RESERVED = "agentgateway.dev/"
PROVIDERS = {  # provider type -> (env var holding its API key, base URL of the mock)
    "anthropic": ("ANTHROPIC_API_KEY", MOCK_URL),
    "openai": ("OPENAI_API_KEY", MOCK_URL + "/v1"),
}

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
def _style(code):
    return lambda s: "\033[%sm%s\033[0m" % (code, s) if COLOR else str(s)
dim, bold, green, red, yellow, purple = (_style(c) for c in ("2", "1", "32", "31", "33", "35"))


class Fail(Exception):
    pass


def call(method, url, body=None, key=None, headers=None, timeout=120):
    if not url.startswith("http"):
        url = BASE + url
    h = {"content-type": "application/json"}
    if key:
        h["authorization"] = "Bearer " + key
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except OSError as e:
        raise Fail("can't reach %s (%s)" % (url, getattr(e, "reason", e)))
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, raw.decode(errors="replace").strip()


# ---------- reading and writing the gateway's config (same API the UI uses) ----------

def get_config():
    status, cfg = call("GET", "/api/config")
    if status != 200 or not isinstance(cfg, dict):
        raise Fail("GET %s/api/config returned %s. Is agentgateway running?" % (BASE, status))
    return cfg

def llm_of(cfg):
    return cfg.get("llm") or {}

def keys_of(cfg):
    return ((llm_of(cfg).get("policies") or {}).get("apiKey") or {}).get("keys") or []

def name_of(entry):
    return (entry.get("metadata") or {}).get("name") or ""

def find_key(cfg, name):
    return next((k for k in keys_of(cfg) if name_of(k).lower() == name.lower()), None)

def ptype(entry):
    p = entry.get("provider")
    return p.lower() if isinstance(p, str) else None

def copy(obj):
    return json.loads(json.dumps(obj))

def put_new(kind, values):
    status, body = call("PUT", "/api/config/resources/" + kind, {"resources": [{"value": v} for v in values]})
    if status != 200:
        raise Fail("adding %s failed (%s): %s" % (kind, status, body))

def put_one(kind, rid, value):
    status, body = call("PUT", "/api/config/resources/%s/%s" % (kind, urllib.parse.quote(rid, safe="")), {"value": value})
    if status != 200:
        raise Fail("updating %s '%s' failed (%s): %s" % (kind, rid, status, body))

def update_key(entry, mutate):
    rid = (entry.get("metadata") or {}).get(RESERVED + "id")
    if not rid:
        raise Fail("key '%s' has no id; was it written into the config file by hand?" % name_of(entry))
    value = copy(entry)
    # the server owns agentgateway.dev/* metadata and rejects updates that send it back
    value["metadata"] = {k: v for k, v in (value.get("metadata") or {}).items() if not k.startswith(RESERVED)}
    mutate(value)
    put_one("llm.apiKey", rid, value)

def model_provider(cfg, model):
    llm = llm_of(cfg)
    for m in llm.get("models") or []:
        if m.get("name") != model:
            continue
        p = m.get("provider")
        if isinstance(p, dict) and p.get("reference"):
            ref = next((x for x in llm.get("providers") or [] if x.get("name") == p["reference"]), None)
            return (ptype(ref) if ref else None) or p["reference"]
        return p.lower() if isinstance(p, str) else "custom"
    return None


# ---------- fast-forward ----------

def point_at_mock(cfg=None, quiet=False):
    cfg = cfg or get_config()
    llm, changed = llm_of(cfg), []
    for kind, items in (("llm.provider", llm.get("providers") or []), ("llm.model", llm.get("models") or [])):
        for item in items:
            t = ptype(item)
            if t in PROVIDERS and (item.get("params") or {}).get("baseUrl") != PROVIDERS[t][1]:
                value = copy(item)
                value.setdefault("params", {})["baseUrl"] = PROVIDERS[t][1]
                put_one(kind, item["name"], value)
                changed.append(item["name"])
    if changed and not quiet:
        time.sleep(1.5)  # config is reloaded from the file watcher; the next request must see baseUrl
    if not quiet:
        print(green("✓") + " pointed at the mock: " + (", ".join(changed) or "nothing needed"))
    return changed

def setup():
    out = {"providers": [], "providers_kept": [], "models": [], "keys": [], "keys_kept": [], "strict": False}
    cfg = get_config()
    llm = llm_of(cfg)
    providers, models = llm.get("providers") or [], llm.get("models") or []
    names = {}
    for t, (env, mock_base) in PROVIDERS.items():
        existing = next((p for p in providers if ptype(p) == t), None)
        if existing:
            names[t] = existing["name"]
            out["providers_kept"].append(existing["name"])
            continue
        if not MOCK and not os.environ.get(env):
            continue  # no key for this provider in this terminal: leave it out
        params = {"apiKey": "$" + env}  # expanded from the gateway's environment, never stored
        if MOCK:
            params["baseUrl"] = mock_base
        put_new("llm.provider", [{"name": t, "provider": t, "params": params}])
        names[t] = t
        out["providers"].append(t)
    if MOCK:
        point_at_mock(get_config(), quiet=True)
    have = {m.get("name") for m in models}
    new_models, seen = [], set()
    for model, t in ((CLAUDE, "anthropic"), (CLAUDE_SMALL, "anthropic"), (OPENAI, "openai"), (OPENAI_BIG, "openai")):
        if t in names and model not in have and model not in seen:
            seen.add(model)
            new_models.append({"name": model, "provider": {"reference": names[t]}, "params": {"model": model}})
    if new_models:
        put_new("llm.model", new_models)
        out["models"] = [m["name"] for m in new_models]
    cfg = get_config()
    new_keys = []
    for person in PEOPLE:
        entry = find_key(cfg, person)
        if entry is None:
            new_keys.append({"key": "agw_sk_" + secrets.token_urlsafe(18), "metadata": {"name": person, "user": person}})
            out["keys"].append(person)
            continue
        out["keys_kept"].append(name_of(entry))
        if (entry.get("metadata") or {}).get("user") != person:  # Analytics groups spend by this field
            update_key(entry, lambda v, p=person: v["metadata"].__setitem__("user", p))
    if new_keys:
        put_new("llm.apiKey", new_keys)
    policy = copy((llm_of(get_config()).get("policies") or {}).get("apiKey") or {})
    if policy.get("mode") != "strict":
        policy.pop("keys", None)
        policy["mode"] = "strict"
        put_one("llm.policy", "apiKey", policy)
        out["strict"] = True
    return out

JOBS = ["sync-orders", "reindex-search", "rollup-metrics", "export-invoices", "dedupe-customers", "refresh-embeddings"]
def heavy_prompt(seed):
    rnd = random.Random(seed)
    lines = []
    for _ in range(70):
        level = rnd.choice(["INFO"] * 6 + ["WARN", "WARN", "ERROR"])
        lines.append("2026-10-%02dT%02d:%02d:%02dZ %-5s %s batch=%d rows=%d took=%.2fs" % (
            rnd.randint(14, 21), rnd.randint(1, 4), rnd.randint(0, 59), rnd.randint(0, 59), level,
            rnd.choice(JOBS), rnd.randint(1, 400), rnd.randint(100, 9000), rnd.uniform(0.2, 9.9)))
    return ("You are nightly-agent, an autonomous ops agent. Read these batch-job logs and summarize the "
            "anomalies and their likely root causes in five bullets.\n\n" + "\n".join(lines))

QUESTIONS = [
    "Write a one-line commit message for a fix to a null check in the cart service.",
    "In one sentence, what does HTTP 429 mean?",
    "Suggest a name for a Slack channel about on-call handoffs.",
    "Rewrite politely: 'your ticket is a duplicate, closing'.",
    "Give me a regex that matches an ISO-8601 date.",
    "Summarize for a status page: the deploy failed because a database migration timed out.",
    "What is the difference between a liveness probe and a readiness probe? Two sentences.",
    "Draft a two-line reply to a customer asking when their refund will arrive.",
    "Name three risks of running a database migration during peak traffic.",
    "Turn this into a Jira title: login page is slow on mobile Safari.",
    "Explain idempotency to a new engineer in two sentences.",
    "What should a good incident postmortem include? Five words each, three items.",
]

def plan(available, scale):
    rows = [  # key, model, requests, heavy prompt?, max output tokens
        ("nightly-agent", CLAUDE, 18, True, 300),
        ("support-team", OPENAI, 16, False, 160),
        ("bob", CLAUDE_SMALL, 8, False, 200),
        ("bob", OPENAI_BIG, 4, False, 200),
        ("alice", CLAUDE, 4, False, 200),
        ("alice", OPENAI, 3, False, 160),
        ("intern", OPENAI, 4, False, 120),
    ]
    fallback = next((m for m in (CLAUDE_SMALL, OPENAI, CLAUDE) if m in available), None)
    jobs = []
    for who, model, n, heavy, max_tokens in rows:
        model = model if model in available else fallback
        if model:
            jobs += [(who, model, heavy, max_tokens, i) for i in range(max(1, round(n * scale)))]
    random.Random(7).shuffle(jobs)
    return jobs

def database_path(cfg):
    url = ((cfg.get("config") or {}).get("database") or {}).get("url") or ""
    if not isinstance(url, str) or not url.startswith("sqlite://"):
        raise Fail("analytics history needs the SQLite database from a no-argument start")
    path = url[len("sqlite://"):]
    if not os.path.isabs(path):
        path = os.path.join(os.environ.get("XDG_CONFIG_HOME", ""), "agentgateway", os.path.basename(path))
    if not os.path.exists(path):
        raise Fail("database not found at %s" % path)
    return path

def newest_log_stamp(cfg):
    # Rows written after this belong to the replay. "" means the table was empty.
    # None means the table could not be read; backdating must not guess and rewrite older rows.
    try:
        con = sqlite3.connect(database_path(cfg), timeout=5)
    except Fail:
        return None
    try:
        row = con.execute("select max(started_at) from request_logs").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return (row[0] if row else "") or ""

def _anchors(now, hours):
    earliest = now - timedelta(days=6, hours=18)
    found = []
    for days_ago in range(0, 8):
        day = (now - timedelta(days=days_ago)).date()
        for hour, minute in hours:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
            if earliest <= candidate <= now - timedelta(minutes=2):
                found.append(candidate)
    found.sort()
    return found

def history_times(who, count, now):
    """Past local times for one key. Nightly lands around 02:40. Everyone else lands in the daytime.
    At least one time is inside the last 24 hours, which is the chart's default range."""
    now = now.astimezone()
    if count <= 0:
        return []
    hours = [(2, 40)] if who == "nightly-agent" else [(9, 20), (11, 35), (14, 10), (16, 5)]
    anchors = _anchors(now, hours)
    if not any(now - timedelta(hours=24) <= item <= now for item in anchors):
        recent = {"nightly-agent": 5, "support-team": 3, "alice": 2, "bob": 8, "intern": 11}.get(who, 4)
        anchors.append(now - timedelta(hours=recent))
        anchors.sort()
    if not anchors:
        anchors = [now - timedelta(hours=4)]
    # Spread across the whole week, including the oldest and newest anchors.
    # Cycling from the start would leave every request on the first few days.
    times = []
    if count <= len(anchors):
        if count == 1:
            picked = [anchors[-1]]
        else:
            picked = [anchors[round(i * (len(anchors) - 1) / (count - 1))] for i in range(count)]
        for i, base in enumerate(picked):
            times.append(base + timedelta(seconds=(i * 17) % 50))
    else:
        for i in range(count):
            base = anchors[i % len(anchors)]
            times.append(base + timedelta(minutes=(i // len(anchors)) * 6, seconds=(i * 17) % 50))
    clamped = []
    for i, moment in enumerate(times):
        if moment > now - timedelta(minutes=2):
            moment = now - timedelta(minutes=2, seconds=i + 1)
        clamped.append(moment)
    clamped.sort()
    return clamped

def stamp_like(sample, moment):
    # Match the text the gateway stored, so its range filter (a string compare) still includes the row.
    utc = moment.astimezone(timezone.utc).replace(microsecond=0)
    sample = sample or ""
    if len(sample) >= 19 and sample[10] == " ":
        text = utc.strftime("%Y-%m-%d %H:%M:%S")
    else:
        text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    frac = ""
    if "." in sample:
        rest = sample.split(".", 1)[1]
        digits = []
        for ch in rest:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if digits:
            frac = "." + "".join(digits)
            text += frac
    offset = ""
    if sample.endswith("Z"):
        offset = "Z"
    else:
        for i in range(len(sample) - 1, 18, -1):
            if sample[i] in "+-" and i >= 19:
                offset = sample[i:]
                break
    if not offset and "T" in text:
        offset = "+00:00"
    return text + offset

def backdate_week(cfg, watermark, expected):
    path = database_path(cfg)
    con = sqlite3.connect(path, timeout=5)
    try:
        if watermark is None:
            raise Fail("could not read the log table, so the week was not dated")
        clause, params = ("started_at > ?", (watermark,)) if watermark else ("1=1", ())
        con.execute("PRAGMA busy_timeout=5000")
        deadline = time.time() + 8
        rows = []
        while True:
            rows = con.execute(
                "select id, agentgateway_user, duration_ms, started_at from request_logs "
                "where " + clause + " order by agentgateway_user, started_at, id",
                params,
            ).fetchall()
            if len(rows) >= expected or time.time() >= deadline:
                break
            time.sleep(0.2)
        if not rows:
            raise Fail("the replay produced no log rows to date")
        grouped = {}
        for log_id, user, duration_ms, started_at in rows:
            grouped.setdefault(user or "", []).append((log_id, duration_ms or 0, started_at))
        now = datetime.now().astimezone()
        updates = []
        for user, items in grouped.items():
            moments = history_times(user, len(items), now)
            for (log_id, duration_ms, started_at), moment in zip(items, moments):
                end = moment + timedelta(milliseconds=int(duration_ms))
                updates.append((stamp_like(started_at, moment), stamp_like(started_at, end), log_id))
        try:
            con.execute("BEGIN IMMEDIATE")
            con.executemany("update request_logs set started_at=?, completed_at=? where id=?", updates)
            con.commit()
        except sqlite3.Error as e:
            con.rollback()
            raise Fail("could not date the analytics rows (%s)" % e)
        return len(updates)
    except sqlite3.Error as e:
        raise Fail("could not read the new log rows (%s)" % e)
    finally:
        con.close()

def chat_body(cfg, model, prompt, max_tokens):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    # OpenAI's newer models only take max_completion_tokens; Anthropic needs max_tokens
    body["max_completion_tokens" if model_provider(cfg, model) == "openai" else "max_tokens"] = max_tokens
    return body

def fast_forward(args):
    print(bold("⏩  Fast-forwarding a week of traffic") + (dim("   (mock providers)") if MOCK else ""))
    r = setup()
    def row(label, added, kept):
        text = ", ".join(added) if added else dim("nothing new")
        if kept:
            text += dim("   (already had %s)" % ", ".join(kept))
        print("   %s %-9s %s" % (green("+"), label, text))
    row("providers", r["providers"], r["providers_kept"])
    row("models", r["models"], [])
    row("keys", r["keys"], r["keys_kept"])
    if r["strict"]:
        print("   %s every request now needs a virtual key %s" % (green("✓"), dim("(strict mode)")))
    time.sleep(1.5)  # the gateway picks up config changes from its file watcher
    cfg = get_config()
    available = {m.get("name") for m in llm_of(cfg).get("models") or []}
    keymap = {p: find_key(cfg, p)["key"] for p in PEOPLE if find_key(cfg, p)}
    jobs = [j for j in plan(available, args.scale) if j[0] in keymap]
    per, counts, first_error, lock = {}, {"ok": 0, "fail": 0}, {}, threading.Lock()

    def run(job):
        who, model, heavy, max_tokens, i = job
        prompt = heavy_prompt(i) if heavy else QUESTIONS[(i + sum(map(ord, who))) % len(QUESTIONS)]
        status, body = call("POST", "/v1/chat/completions", chat_body(cfg, model, prompt, max_tokens), key=keymap[who])
        return who, status, body

    print("   %s replaying %d requests " % (purple("↻"), len(jobs)), end="", flush=True)
    watermark = newest_log_stamp(cfg)
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run, j) for j in jobs]
        try:
            for f in as_completed(futures):
                try:
                    who, status, body = f.result()
                except Fail as e:
                    who, status, body = "?", 0, str(e)
                with lock:
                    ok = status == 200
                    counts["ok" if ok else "fail"] += 1
                    if ok:
                        per[who] = per.get(who, 0) + 1
                    else:
                        first_error.setdefault(status, (who, body))
                    print(green("·") if ok else red("×"), end="", flush=True)
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            print(yellow("  stopped early"), end="")
    print()
    print("   " + bold("requests by key"))
    most = max(per.values() or [1])
    for who in PEOPLE:
        n = per.get(who, 0)
        bar = ("█" * max(1, round(22 * n / most)) if n else "").ljust(22)
        print("     %-14s %s %3d" % (who, purple(bar), n))
    print("   done in %ds · %d ok · %d failed" % (time.time() - start, counts["ok"], counts["fail"]))
    for status, (who, body) in first_error.items():
        print(red("   first %s error (%s): " % (status or "connection", who)) + str(body)[:220])
    if counts["ok"]:
        try:
            dated = backdate_week(cfg, watermark, counts["ok"])
        except Fail as e:
            print(red("   " + str(e)))
            return 1
        print(dim("   dated %d requests across the last 7 days" % dated))
    print(dim("   next: Analytics, measure cost, group by user. Set the range to Last 7 days"))
    return 0 if counts["ok"] else 1


# ---------- ask ----------

def mask(key):
    return key[:7] + "…" + key[-4:] if len(key) > 14 else key

def error_parts(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        return e.get("code") or e.get("type") or "", e.get("message") or ""
    return "", (body if isinstance(body, str) else json.dumps(body))[:200]

def budget_rows(who):
    status, body = call("GET", "/api/budgets/status?apiKeyName=" + urllib.parse.quote(who))
    return (body or {}).get("budgets") or [] if status == 200 and isinstance(body, dict) else []

def fmt_budget(b):
    used, limit = float(b["usage"]["used"]), float(b["limit"]["amount"])
    if b["limit"]["unit"] == "USD":
        return "%s $%.3f of $%.2f" % (b["name"], used, limit)
    return "%s %s of %s tokens" % (b["name"], "{:,.0f}".format(used), "{:,.0f}".format(limit))

def ask(args):
    cfg = get_config()
    if args.key.startswith(("agw_sk_", "sk-")):
        key, who = args.key, "(raw key)"
    else:
        entry = find_key(cfg, args.key)
        if not entry:
            names = ", ".join(name_of(k) for k in keys_of(cfg)) or "none yet"
            raise Fail("no virtual key named '%s' (keys: %s)" % (args.key, names))
        key, who = entry["key"], name_of(entry)
    print(dim("→ POST /v1/chat/completions   key: %s (%s)   model: %s" % (who, mask(key), args.model)))
    if args.until_blocked:
        return until_blocked(cfg, key, who, args)
    started = time.time()
    status, body = call("POST", "/v1/chat/completions", chat_body(cfg, args.model, args.prompt, args.max_tokens), key=key)
    took = time.time() - started
    if status == 200:
        usage = (body or {}).get("usage") or {}
        text = ((((body or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        print("%s   served by %s   %s in / %s out tokens   %s" % (
            green("← 200 OK"), bold(model_provider(cfg, args.model) or "?"),
            usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), dim("%.1fs" % took)))
        if text.strip():
            print(dim("  " + " ".join(text.split())[:110]))
        return 0
    code, message = error_parts(body)
    print("%s   %s   %s" % (red("← %s" % status), bold(code), message))
    print("  " + (json.dumps(body) if not isinstance(body, str) else body))
    return 1

def until_blocked(cfg, key, who, args):
    if not budget_rows(who):
        print(yellow("  %s has no budgets yet. Add them in the UI (or run scripts/apply-policies.sh), then rerun." % who))
        return 1
    for i in range(1, args.max + 1):
        status, body = call("POST", "/v1/chat/completions", chat_body(cfg, args.model, heavy_prompt(100 + i), 300), key=key)
        spent = "   ".join(fmt_budget(b) for b in budget_rows(who))
        if status == 200:
            print("  #%-2d %s   %s" % (i, green("200"), spent))
            continue
        code, message = error_parts(body)
        print("  #%-2d %s   %s   %s" % (i, red(str(status)), bold(code), dim(spent)))
        print("      " + (json.dumps(body) if not isinstance(body, str) else body))
        return 0
    print(yellow("  not blocked after %d requests; is the budget bigger than you meant?" % args.max))
    return 1


# ---------- policies and reporting ----------

def apply_policies(args):
    cfg = get_config()
    nightly, intern = find_key(cfg, "nightly-agent"), find_key(cfg, "intern")
    if not nightly or not intern:
        raise Fail("run ./fast-forward-a-week.sh first; it creates nightly-agent and intern")
    def budgets(v):
        v["budgets"] = [
            {"name": "5-hour", "limit": {"unit": "USD", "amount": args.five_hour}, "window": {"rolling": "5h"}, "onBudgetExceeded": "Block"},
            {"name": "weekly", "limit": {"unit": "USD", "amount": args.weekly}, "window": {"rolling": "7d"}, "onBudgetExceeded": "Block"},
        ]
    update_key(nightly, budgets)
    update_key(intern, lambda v: v.__setitem__("allowedModels", ["*-mini"]))
    print(green("✓") + " nightly-agent: 5-hour budget $%g and weekly $%g, both Block" % (args.five_hour, args.weekly))
    print(green("✓") + " intern: allowed models *-mini")
    return 0

def budgets(args):
    path = "/api/budgets/status" + ("?apiKeyName=" + urllib.parse.quote(args.key) if args.key else "")
    status, body = call("GET", path)
    rows = (body or {}).get("budgets") or [] if isinstance(body, dict) else []
    if not rows:
        print(dim("no budgets yet"))
    for b in rows:
        state = red("exceeded") if b["usage"].get("exceeded") else green("ok")
        print("  %-14s %-34s %s" % (b["apiKeyName"], fmt_budget(b), state))
    return 0

def report(args):
    status, body = call("POST", "/api/logs/analytics/summary", {"groupBy": [{"field": "attributes", "key": "agentgateway.user"}]})
    if status != 200:
        raise Fail("analytics returned %s: %s" % (status, body))
    groups = sorted(body.get("groups") or [], key=lambda g: -(g.get("cost") or 0))
    print(bold("spend by user (last 24h)"))
    for g in groups:
        print("  %-14s %4d requests   $%.4f" % ((g.get("group") or {}).get("agentgateway.user") or "(no key)", g.get("requests", 0), g.get("cost") or 0))
    return 0


# ---------- preflight ----------

def preflight(args):
    problems = []
    def check(ok, label, detail=""):
        print("  %s %s%s" % (green("✓") if ok else red("✗"), label, dim("   " + detail) if detail else ""))
        if not ok:
            problems.append(label)
    def note(label, detail=""):
        print("  %s %s%s" % (yellow("!"), label, dim("   " + detail) if detail else ""))
    def reachable(url, headers=None):
        try:
            status, _ = call("GET", url, headers=headers, timeout=8)
            return 200 <= status < 300
        except Fail:
            return False

    home = os.environ.get("DEMO_HOME", "")
    print(bold("agentgateway demo preflight") + (dim("   (mock providers)") if MOCK else ""))
    check(bool(home) and os.environ.get("XDG_CONFIG_HOME", "").startswith(home), "demo.env is sourced in this terminal", home or "run: source demo.env")
    for tool in ("curl", "python3"):
        check(shutil.which(tool) is not None, tool + " is installed")
    if not shutil.which("jq"):
        note("jq is not installed", "fine while DESIRED_VERSION is pinned; the installer needs it otherwise")
    check(shutil.which("openssl") is not None, "openssl is installed", "the installer verifies the download with it")
    install_dir = os.environ.get("AGENTGATEWAY_INSTALL_DIR", "")
    check(bool(install_dir) and os.path.isdir(install_dir), "install folder exists", install_dir or "source demo.env again")
    if os.path.exists(os.path.join(install_dir, "agentgateway")):
        note("agentgateway is already installed in the demo folder", "run ./scripts/reset.sh to install again from scratch")
    if os.path.exists(os.path.join(os.environ.get("XDG_CONFIG_HOME", ""), "agentgateway", "config.yaml")):
        note("an earlier demo config and database exist", "spend from an earlier run counts against today's budgets; run ./scripts/reset.sh")
    for port in (4000, 15000, 15020, 15021):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            free = True
        except OSError:
            free = False
        finally:
            s.close()
        check(free, "port %d is free" % port, "" if free else "something else is listening. ./scripts/reset.sh only stops a gateway installed in this demo folder")
    check(reachable("https://agentgateway.dev/install"), "agentgateway.dev/install is reachable")
    osname = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), platform.machine())
    version = os.environ.get("DESIRED_VERSION", "")
    asset = "https://github.com/agentgateway/agentgateway/releases/download/%s/agentgateway-%s-%s" % (version, osname, arch)
    check(bool(version) and reachable(asset, {"range": "bytes=0-0"}), "release %s has a %s-%s binary" % (version or "(unset)", osname, arch))
    if MOCK:
        check(reachable(MOCK_URL + "/v1/models"), "the mock provider is running", MOCK_URL)
    else:
        for env, label, models, url, auth in (
            ("ANTHROPIC_API_KEY", "Anthropic", (CLAUDE, CLAUDE_SMALL), "https://api.anthropic.com/v1/models/",
             lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
            ("OPENAI_API_KEY", "OpenAI", (OPENAI, OPENAI_BIG), "https://api.openai.com/v1/models/",
             lambda k: {"authorization": "Bearer " + k}),
        ):
            key = os.environ.get(env, "")
            check(bool(key), env + " is set", "…" + key[-4:] if key else "add it to .env.local, then source demo.env")
            if key:
                for model in models:
                    check(reachable(url + model, auth(key)), "%s serves %s" % (label, model))
    now = time.time()
    for label, seconds in (("5-hour", 5 * 3600), ("weekly", 7 * 86400)):
        start = now - now % seconds  # budget windows are fixed and aligned to the Unix epoch
        print("  %s %s budget window: %s to %s" % (dim("·"), label,
              time.strftime("%a %H:%M", time.localtime(start)), time.strftime("%a %H:%M", time.localtime(start + seconds))))
    if 5 * 3600 - now % (5 * 3600) < 45 * 60:
        note("the 5-hour window resets within 45 minutes", "budgets set before then start over at the reset")
    print(green("ready") if not problems else red("%d problem(s) to fix" % len(problems)))
    return 0 if not problems else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fast-forward")
    p.add_argument("--concurrency", type=int, default=int(os.environ.get("FF_CONCURRENCY", "6")))
    p.add_argument("--scale", type=float, default=float(os.environ.get("FF_SCALE", "1")), help="multiply the request counts")
    p = sub.add_parser("ask")
    p.add_argument("key", help="virtual key name, e.g. alice (or a raw key)")
    p.add_argument("model")
    p.add_argument("prompt", nargs="?", default="In one sentence, what does an AI gateway do?")
    p.add_argument("--until-blocked", action="store_true", help="keep sending heavy requests until the key is blocked")
    p.add_argument("--max", type=int, default=12)
    p.add_argument("--max-tokens", type=int, default=600)
    p = sub.add_parser("apply-policies")
    p.add_argument("five_hour", nargs="?", type=float, default=0.02)
    p.add_argument("weekly", nargs="?", type=float, default=1.0)
    p = sub.add_parser("budgets")
    p.add_argument("key", nargs="?")
    sub.add_parser("report")
    sub.add_parser("point-at-mock")
    sub.add_parser("preflight")
    args = parser.parse_args()
    try:
        handler = {"fast-forward": fast_forward, "ask": ask, "apply-policies": apply_policies, "budgets": budgets,
                   "report": report, "point-at-mock": lambda a: point_at_mock() and 0, "preflight": preflight}[args.cmd]
        sys.exit(handler(args) or 0)
    except Fail as e:
        print(red("✗ ") + str(e), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)

if __name__ == "__main__":
    main()

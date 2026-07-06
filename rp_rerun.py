#!/usr/bin/env python3
"""Rerun Sapphire's failed scenarios via ReportPortal + execute-scenarios.yml.

Scope: only the Account nightly (Trigger=Scheduled, Scope=Regression) launches.
Failures are grouped by CHANNEL so the rerun dispatches exactly the channel(s)
and domain(s) that failed - execute-scenarios.yml's FullyQualifiedName~{channel}_{domain}
filter then pins each matrix cell to the right scenarios.

Modes
  list     Show the nightly Account launches (id, RunNumber, failed count).
  extract  Collect Teamtag:Sapphire failures across the nightly launches,
           group them by channel, and emit:
             failed.json  every failed instance (key, name, channel, domain)
             groups.json  one rerun group per channel: {channel, domains, extent}
  report   Given the rerun RunNumbers (the dispatched GitHub run ids), read the
           rerun outcomes and build an HTML + Slack report:
           recovered / still failing / not rerun (untagged).

Environment
  RP_TOKEN  ReportPortal API token (bearer). Required.
  RP_BASE   Override the API base. Defaults to the qanovi project below.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys

import requests

RP_BASE = os.environ.get(
    "RP_BASE", "http://stg-qa01-02.stg-novibet.systems:8080/api/v1/qanovi"
)
FAIL_STATUSES = {"FAILED", "INTERRUPTED"}
ALL_STATUSES = {"PASSED", "FAILED", "INTERRUPTED", "SKIPPED"}
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")  # e.g. SB-10610, UA-19182


def _headers():
    tok = os.environ.get("RP_TOKEN")
    if not tok:
        sys.exit("RP_TOKEN environment variable is required.")
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}


def attr_value(obj, key):
    for a in obj.get("attributes", []):
        if a.get("key") == key:
            return a.get("value")
    return None


def _paginate(url, params):
    params = dict(params)
    params.setdefault("page.page", 1)
    params.setdefault("page.size", 100)
    out = []
    while True:
        r = requests.get(url, headers=_headers(), params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("content", []))
        page = body.get("page", {})
        if params["page.page"] >= page.get("totalPages", 1):
            break
        params["page.page"] += 1
    return out


def is_nightly(launch):
    """Keep only the scheduled regression launches (drop Manual/Coverage/PR/HealthCheck)."""
    if attr_value(launch, "Trigger") != "Scheduled":
        return False
    if (attr_value(launch, "Scope") or "").lower() != "regression":
        return False
    if "HealthCheck" in launch.get("name", ""):
        return False
    return True


def nightly_launches(name_contains):
    latest = _paginate(f"{RP_BASE}/launch/latest",
                       {"filter.cnt.name": name_contains, "page.size": 100})
    return [L for L in latest if is_nightly(L)]


def launches_by_run_number(run_number):
    return _paginate(f"{RP_BASE}/launch",
                     {"filter.has.compositeAttribute": f"RunNumber:{run_number}",
                      "page.size": 100})


def fetch_items_for_launch(launch_id, team, statuses):
    return _paginate(f"{RP_BASE}/item/v2", {
        "filter.eq.hasStats": "true",
        "filter.eq.hasChildren": "false",
        "filter.in.type": "STEP",
        "filter.has.compositeAttribute": f"Teamtag:{team}",
        "filter.in.status": ",".join(sorted(statuses)),
        "providerType": "launch",
        "launchId": launch_id,
        "page.size": 300,
    })


def scenario_key(item):
    for a in item.get("attributes", []):
        v = a.get("value")
        if v and KEY_RE.match(v):
            return v
    return None


def collect_failures(launches, team, statuses):
    """Return failed items stamped with channel/domain (item attr, launch fallback)."""
    out = []
    for L in launches:
        lch, ldom = attr_value(L, "Channel"), attr_value(L, "Domain")
        items = fetch_items_for_launch(L["id"], team, statuses)
        if items:
            print(f"  {L.get('name','?')} (id {L['id']}): {len(items)} item(s)")
        for it in items:
            out.append({
                "key": scenario_key(it),
                "name": it.get("name", ""),
                "channel": attr_value(it, "Channel") or lch,
                "domain": attr_value(it, "Domain") or ldom,
                "status": it.get("status", ""),
            })
    return out


def cmd_list(a):
    launches = nightly_launches(a.name_contains)
    print(f"{len(launches)} nightly launch(es) matching '{a.name_contains}':\n")
    for L in launches:
        stats = L.get("statistics", {}).get("executions", {})
        print(f"  id={L['id']:<8} RunNumber={attr_value(L, 'RunNumber')}  "
              f"failed={stats.get('failed', 0):<5} {L.get('name','')}")


def cmd_extract(a):
    launches = nightly_launches(a.name_contains)
    print(f"Scanning {len(launches)} nightly launch(es) for Teamtag:{a.team} failures:")
    failed = collect_failures(launches, a.team, FAIL_STATUSES)

    # Group keyed failures by channel; per channel collect its domains + keys.
    by_channel = {}
    keyless = []
    for f in failed:
        if not f["key"]:
            keyless.append(f)
            continue
        ch = f["channel"] or "ALL"
        g = by_channel.setdefault(ch, {"domains": set(), "keys": set()})
        g["domains"].add(f["domain"] or "ALL")
        g["keys"].add(f["key"])

    groups = []
    for ch, g in sorted(by_channel.items()):
        keys = sorted(g["keys"])
        groups.append({
            "channel": ch,
            "domains": sorted(g["domains"]),
            "extent": "(=" + "|=".join(keys) + ")",
            "count": len(keys),
        })

    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump({"team": a.team, "failed": failed}, f, indent=2)
    with open(a.out_groups, "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2)

    print(f"\nTotal: {len(failed)} failed instance(s), {len(keyless)} untagged.")
    print(f"Rerun groups ({len(groups)}):")
    for g in groups:
        print(f"  channel={g['channel']:<8} domains={','.join(g['domains'])}  "
              f"keys={g['count']}")
    if keyless:
        print(f"Untagged (not rerun): {len(keyless)}")


def cmd_report(a):
    orig = json.load(open(a.original_json, encoding="utf-8"))
    team = orig["team"]
    run_numbers = [x for x in a.run_numbers.split(",") if x]

    launches = []
    for rn in run_numbers:
        launches += launches_by_run_number(rn)
    print(f"Rerun run numbers {run_numbers}: {len(launches)} launch(es).")
    rerun_items = collect_failures(launches, team, ALL_STATUSES)
    by_name = {it["name"]: it["status"] for it in rerun_items}

    recovered, still, not_rerun = [], [], []
    for it in orig["failed"]:
        label = f'{it["name"]}'
        if not it["key"]:
            not_rerun.append((label, "untagged"))
            continue
        st = by_name.get(it["name"], "MISSING")
        (recovered if st == "PASSED" else still).append((label, st))

    _write_html(a.html_out, recovered, still, not_rerun, a.pages_url, team)
    _write_slack(a.slack_out, recovered, still, not_rerun, a.pages_url, team)
    print(f"Recovered: {len(recovered)}  Still failing: {len(still)}  "
          f"Not rerun: {len(not_rerun)}")


def _rows(items):
    if not items:
        return '<tr><td class="muted">None</td></tr>'
    return "".join(
        f'<tr><td>{html.escape(lbl)} <span class="st">{html.escape(st)}</span></td></tr>'
        for lbl, st in items
    )


def _write_html(path, recovered, still, not_rerun, pages_url, team):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = len(recovered) + len(still) + len(not_rerun)
    untagged_section = ""
    if not_rerun:
        untagged_section = (f"<h2>Not rerun &mdash; untagged scenarios</h2>"
                            f"<table>{_rows(not_rerun)}</table>")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(team)} rerun report - {ts}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin-inline: auto; max-width: 900px;
         padding: 2rem; line-height: 1.6; }}
  h1 {{ font-weight: 500; margin-bottom: .25rem; }}
  .sub {{ color: #888; margin-top: 0; }}
  .cards {{ display: flex; gap: 1rem; margin: 1.5rem 0; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 150px; border: 1px solid #8883; border-radius: 12px;
          padding: 1rem 1.25rem; }}
  .n {{ font-size: 2rem; font-weight: 500; }}
  .ok .n {{ color: #2e7d32; }} .bad .n {{ color: #c62828; }} .muted2 .n {{ color: #999; }}
  h2 {{ font-weight: 500; margin-top: 2rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: .5rem .75rem; border-bottom: 1px solid #8883; }}
  .st {{ font-size: .75rem; color: #999; margin-left: .5rem; }}
  .muted {{ color: #999; }}
</style></head><body>
<h1>{html.escape(team)} rerun report</h1>
<p class="sub">{ts} &middot; {total} failed instance(s) from the nightly.</p>
<div class="cards">
  <div class="card ok"><div class="n">{len(recovered)}</div>Recovered (flaky)</div>
  <div class="card bad"><div class="n">{len(still)}</div>Still failing (real)</div>
  <div class="card muted2"><div class="n">{len(not_rerun)}</div>Not rerun (untagged)</div>
</div>
<h2>Still failing &mdash; needs investigation</h2>
<table>{_rows(still)}</table>
<h2>Recovered on rerun &mdash; likely flaky</h2>
<table>{_rows(recovered)}</table>
{untagged_section}
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def _write_slack(path, recovered, still, not_rerun, pages_url, team):
    def names(items, limit=15):
        if not items:
            return "_none_"
        shown = "\n".join(f"- {lbl}" for lbl, _ in items[:limit])
        if len(items) > limit:
            shown += f"\n- ...and {len(items) - limit} more"
        return shown

    payload = {
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"{team} nightly rerun report"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Recovered (flaky):*\n{len(recovered)}"},
                {"type": "mrkdwn", "text": f"*Still failing (real):*\n{len(still)}"},
                {"type": "mrkdwn", "text": f"*Not rerun (untagged):*\n{len(not_rerun)}"},
            ]},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Still failing:*\n{names(still)}"}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Recovered on rerun:*\n{names(recovered)}"}},
            {"type": "context",
             "elements": [{"type": "mrkdwn", "text": f"<{pages_url}|Full report>"}]},
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list")
    ls.add_argument("--name-contains", default="Account")
    ls.set_defaults(func=cmd_list)

    e = sub.add_parser("extract")
    e.add_argument("--name-contains", default="Account")
    e.add_argument("--team", default="Sapphire")
    e.add_argument("--out-json", required=True)
    e.add_argument("--out-groups", required=True)
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("report")
    r.add_argument("--original-json", required=True)
    r.add_argument("--run-numbers", required=True,
                   help="comma-separated dispatched GitHub run ids (= rerun RunNumbers)")
    r.add_argument("--team", default="Sapphire")
    r.add_argument("--html-out", required=True)
    r.add_argument("--slack-out", required=True)
    r.add_argument("--pages-url", required=True)
    r.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

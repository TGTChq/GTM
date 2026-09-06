#!/usr/bin/env bash
# Capture production evidence for pipeline acceptance. READ-ONLY.
#
# Writes nothing to production: no run is started, no Slack is sent, no reporting
# anchor is advanced, no Airtable/Instantly record is touched.
#
# THREE ACCESS METHODS, tried in order, because they fail independently:
#
#   1. `railway volume files`  -- the durable ledger and heavy run artifacts.
#      Needs a LIVE container: Railway's volume SFTP session is served through the
#      running deployment. On a cron service the container exists only while the run
#      is in flight, so between runs this fails with
#          Failed to initialize SFTP session: Timeout        (~23s, CLI 5.30.1)
#
#   2. `railway ssh`           -- same files, same live-container requirement.
#      Between runs: "Your service's container is not running (status: created)".
#
#   3. `railway logs -d <id>`  -- ALWAYS available, including for REMOVED
#      deployments and well past 7 days. This is the fallback that never fails, and
#      it is why acceptance is never wholly blocked. It yields each run's RUN
#      SUMMARY, which carries every printed counter but NOT the artifact fields the
#      weekly report parses.
#
# WHEN TO RUN IT for a full capture (methods 1-2): DURING the GTM run. The cron is
# `0 3 * * *` UTC. With Apollo credits exhausted a run finishes in ~14 minutes
# (2026-09-06: 03:02:30Z -> 03:16:33Z); with credits it ran 3h20m (2026-09-04).
# So the capture window is 03:00Z + ~14 min. Outside it, methods 1-2 report
# UNAVAILABLE and only logs are captured -- that is expected, not a failure.
#
# NEVER restart or redeploy the service to obtain a container: the start command is
# `report || true; exec <pipeline>`, so any start is a paid acquisition run.
#
#   bash acceptance/capture_production_evidence.sh            # capture everything reachable
#   bash acceptance/capture_production_evidence.sh --check    # report reachability only
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OUT="${EVIDENCE_DIR:-$HERE/evidence/$(date -u +%Y%m%d)}"
VOL="/app/data/state/orchestrator_v2"
SVC="${GTM_SERVICE:-GTM}"
SYNC_SVC="${SYNC_SERVICE:-GTM Approved Sync}"
PROJECT="898f2e3a-1c1e-4b00-b9a6-686cf0432282"
GTM_ID="3a41d0d7-cd66-4f53-baa6-886266ddbbed"
SYNC_ID="d6b2e1c1-c931-4803-8555-91a8216124bc"

say() { printf '%s\n' "$*"; }
mkdir -p "$OUT"

# ---- 0. Which deployment is current, and did it execute? ---------------------
say "== deployments =="
railway api "query { deployments(first: 12, input: {projectId: \"$PROJECT\", serviceId: \"$GTM_ID\"}) { edges { node { id status createdAt updatedAt meta } } } }" \
  > "$OUT/gtm_deployments.json" 2>&1 && say "  wrote gtm_deployments.json"
railway api "query { deployments(first: 12, input: {projectId: \"$PROJECT\", serviceId: \"$SYNC_ID\"}) { edges { node { id status createdAt updatedAt } } } }" \
  > "$OUT/sync_deployments.json" 2>&1 && say "  wrote sync_deployments.json"

CUR="$(python - "$OUT/gtm_deployments.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for e in d["data"]["deployments"]["edges"]:
    if e["node"]["status"] == "SUCCESS":
        print(e["node"]["id"]); break
PY
)"
say "  current GTM deployment: ${CUR:-none}"

# ---- 1. Logs: always available, so always first ------------------------------
say "== logs (works with no container) =="
[ -n "$CUR" ] && railway logs -d "$CUR" > "$OUT/gtm_run_raw.log" 2>&1 \
  && say "  gtm_run_raw.log ($(wc -l < "$OUT/gtm_run_raw.log") lines)"

SYNC_CUR="$(python - "$OUT/sync_deployments.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for e in d["data"]["deployments"]["edges"]:
    if e["node"]["status"] == "SUCCESS":
        print(e["node"]["id"]); break
PY
)"
[ -n "$SYNC_CUR" ] && railway logs -d "$SYNC_CUR" > "$OUT/approved_sync_raw.log" 2>&1 \
  && say "  approved_sync_raw.log ($(wc -l < "$OUT/approved_sync_raw.log") lines)"

# ---- 2. Volume, method 1: the file API ---------------------------------------
say "== volume via 'railway volume files' =="
if MSYS_NO_PATHCONV=1 timeout 90 railway volume files --volume gtm-volume list "$VOL" --json \
     > "$OUT/volume_listing.json" 2>"$OUT/volume_listing.err"; then
  say "  AVAILABLE -- downloading ledger and artifacts"
  MSYS_NO_PATHCONV=1 railway volume files --volume gtm-volume download \
    "$VOL/reporting_ledger" "$OUT/reporting_ledger" >/dev/null 2>&1 \
    && say "  reporting_ledger captured"
  MSYS_NO_PATHCONV=1 railway volume files --volume gtm-volume download \
    "$VOL/run_artifacts" "$OUT/run_artifacts" >/dev/null 2>&1 \
    && say "  run_artifacts captured"
else
  say "  UNAVAILABLE: $(tail -1 "$OUT/volume_listing.err" 2>/dev/null)"
fi

# ---- 3. Volume, method 2: ssh + tar ------------------------------------------
if [ ! -d "$OUT/reporting_ledger" ]; then
  say "== volume via 'railway ssh' =="
  if timeout 60 railway ssh --service "$SVC" -- true >/dev/null 2>&1; then
    say "  AVAILABLE -- container is up"
    railway ssh --service "$SVC" -- sh -c "cd $VOL && tar cz reporting_ledger" \
      > "$OUT/reporting_ledger.tgz" 2>/dev/null && tar xzf "$OUT/reporting_ledger.tgz" -C "$OUT" \
      && say "  reporting_ledger captured ($(ls -1 "$OUT/reporting_ledger" | wc -l) entries)"
    railway ssh --service "$SVC" -- sh -c \
      "cd $VOL && tar cz \$(find run_artifacts -maxdepth 2 -name '*.json' 2>/dev/null)" \
      > "$OUT/run_artifacts.tgz" 2>/dev/null && tar xzf "$OUT/run_artifacts.tgz" -C "$OUT" \
      && say "  run_artifacts captured ($(ls -1 "$OUT/run_artifacts" | wc -l) runs)"
    for f in fantastic_watermark.json fantastic_continuation.json fantastic_historical_recovery.json; do
      railway ssh --service "$SVC" -- cat "$VOL/$f" > "$OUT/$f" 2>/dev/null || true
      [ -s "$OUT/$f" ] && say "  captured $f"
    done
  else
    say "  UNAVAILABLE: no running container. Expected between runs."
    say "  Next window: 03:00 UTC, while the run is in flight. Do NOT restart the"
    say "  service to gain access -- that starts a paid acquisition run."
  fi
fi

[ "${1:-}" = "--check" ] && exit 0

# ---- 4. Acceptance -----------------------------------------------------------
say ""
if [ -d "$OUT/reporting_ledger" ] || [ -d "$OUT/run_artifacts" ]; then
  say "== A/B on PRODUCTION FILES =="
  cd "$REPO" && PYTHONPATH="$REPO" python "$HERE/ab_report_equivalence.py" "$OUT" "${WINDOW_START:-2026-09-04T07:00:00Z}"
  exit $?
fi
say "== A/B on LOG-DERIVED evidence (production files were unreachable) =="
say "   This tests the reporting code against real production VALUES. It is NOT a"
say "   substitute for the production-file comparison: the log corpus omits every"
say "   artifact field the RUN SUMMARY does not print."
cd "$REPO" && PYTHONPATH="$REPO" python "$HERE/corpus_from_run_logs.py" "$OUT" "$OUT/log_corpus" \
  && PYTHONPATH="$REPO" python "$HERE/ab_report_equivalence.py" "$OUT/log_corpus" "${WINDOW_START:-2026-09-04T07:00:00Z}"

#!/usr/bin/env bash
# Discovery: read-only GET requests to ThousandEyes API v7.
# Reads every account group that the token can see. Follows pagination.
#
# Usage: ./te_discovery.sh [output_dir]        (default: ./discovery_output)
#
# Optional environment variables:
#   TE_TOKEN        API token. If not set, the script asks for it (hidden).
#   INVENTORY_CSV   CSV with the agents you plan to move (header row, then
#                   hostname and/or IP address in any column). The script
#                   also fetches the detail of these agents.
#   DASHBOARD_AIDS  Space-separated account group IDs. Limits the dashboard
#                   detail download to these groups. Default: all groups.
#   FORCE=1         Download again, even when the output file exists.
#
# The script skips files that already exist, so a second run only
# downloads what is missing. The token is never saved.
set -u

OUT_DIR="${1:-discovery_output}"
B="https://api.thousandeyes.com/v7"
INVENTORY_CSV="${INVENTORY_CSV:-}"
DASHBOARD_AIDS="${DASHBOARD_AIDS:-}"
FORCE="${FORCE:-0}"

for tool in curl jq awk; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing tool: $tool" >&2; exit 1; }
done

if [ -n "$INVENTORY_CSV" ] && [ ! -f "$INVENTORY_CSV" ]; then
  echo "INVENTORY_CSV not found: $INVENTORY_CSV" >&2; exit 1
fi
# Resolve the inventory path before the script changes folder.
[ -n "$INVENTORY_CSV" ] && INVENTORY_CSV="$(cd "$(dirname "$INVENTORY_CSV")" && pwd)/$(basename "$INVENTORY_CSV")"

if [ -z "${TE_TOKEN:-}" ]; then
  read -rs -p "ThousandEyes API v7 OAuth token: " TE_TOKEN
  echo
  export TE_TOKEN
fi
[ -n "$TE_TOKEN" ] || { echo "No token supplied" >&2; exit 1; }

mkdir -p "$OUT_DIR" || exit 1
cd "$OUT_DIR" || exit 1
trap 'rm -f page.json body.tmp ./*.tmp aid_*/*.tmp' EXIT
: > failures.log

# GET with retry on 429 and 5xx. The token goes through a curl config on
# stdin, so it is not visible in process lists. Body goes to stdout.
api_get() {  # $1 = URL
  local url="$1" try=1 code
  while [ "$try" -le 4 ]; do
    code=$(printf 'header = "Authorization: Bearer %s"\n' "$TE_TOKEN" \
      | curl -sS -K - -o body.tmp -w '%{http_code}' "$url") || code=000
    case "$code" in
      200) cat body.tmp; rm -f body.tmp; return 0 ;;
      429|500|502|503|504|000) sleep $((try * 5)) ;;
      *) echo "HTTP $code: $url" | tee -a failures.log >&2; rm -f body.tmp; return 1 ;;
    esac
    try=$((try + 1))
  done
  echo "HTTP $code after retries: $url" | tee -a failures.log >&2
  rm -f body.tmp
  return 1
}

skip_existing() {  # $1 = file. True when the file exists and FORCE is not set.
  [ "$FORCE" != "1" ] && [ -s "$1" ]
}

get_all() {  # $1 = URL, $2 = output file (JSON array with one entry per page)
  local url="$1" out="$2"
  skip_existing "$out" && return 0
  : > "$out.tmp"
  while [ -n "$url" ]; do
    api_get "$url" > page.json || { rm -f "$out.tmp" page.json; return 1; }
    cat page.json >> "$out.tmp"
    url=$(jq -r '._links.next.href // empty' page.json 2>/dev/null)
    sleep 1
  done
  jq -s '.' "$out.tmp" > "$out" || echo "BAD JSON: $out" | tee -a failures.log >&2
  rm -f "$out.tmp" page.json
}

get_one() {  # $1 = URL, $2 = output file (single object)
  skip_existing "$2" && return 0
  api_get "$1" > "$2.tmp" && mv "$2.tmp" "$2"
  rm -f "$2.tmp"
  sleep 1
}

echo "== Account groups"
if ! skip_existing account_groups.json; then
  api_get "$B/account-groups" > account_groups.json || exit 1
fi
jq -e '.accountGroups' account_groups.json >/dev/null 2>&1 \
  || { echo "Unexpected account-groups response. Check the token." >&2; exit 1; }
jq -r '.accountGroups[] | "  \(.aid)\t\(.accountGroupName)"' account_groups.json

echo "== Per-group lists"
for AID in $(jq -r '.accountGroups[].aid' account_groups.json); do
  echo "Account group $AID"
  mkdir -p "aid_$AID"
  get_all "$B/agents?aid=$AID&agentTypes=enterprise,enterprise-cluster&expand=cluster-member,test-ids" "aid_$AID/agents.json"
  get_all "$B/tests?aid=$AID"                      "aid_$AID/tests.json"
  get_all "$B/alerts/rules?aid=$AID"               "aid_$AID/alert_rules.json"
  get_all "$B/agents/notification-rules?aid=$AID"  "aid_$AID/agent_notif_rules.json"
  get_all "$B/alert-suppression-windows?aid=$AID"  "aid_$AID/suppression.json"
  get_all "$B/tags?aid=$AID"                       "aid_$AID/tags.json"
  # New: tags with their assignments (which agent, test or dashboard has the tag).
  get_all "$B/tags?aid=$AID&expand=assignments"    "aid_$AID/tags_assignments.json"
  get_all "$B/dashboards?aid=$AID"                 "aid_$AID/dashboards.json"
  get_all "$B/dashboards/filters?aid=$AID"         "aid_$AID/dashboard_filters.json"
done

echo "== Dashboard details (custom dashboards)"
for AID in ${DASHBOARD_AIDS:-$(jq -r '.accountGroups[].aid' account_groups.json)}; do
  [ -f "aid_$AID/dashboards.json" ] || continue
  # The list is a plain array on each page. Built-in dashboards are skipped.
  for D in $(jq -r '.[] | (if type=="array" then . else (.dashboards // .items // []) end)
                    | .[] | select(.isBuiltIn != true) | .dashboardId' "aid_$AID/dashboards.json" 2>/dev/null); do
    get_one "$B/dashboards/$D?aid=$AID" "aid_$AID/dashboard_$D.json"
  done
done

echo "== Alert suppression window details"
for AID in $(jq -r '.accountGroups[].aid' account_groups.json); do
  [ -f "aid_$AID/suppression.json" ] || continue
  for W in $(jq -r '.[].alertSuppressionWindows[]?.alertSuppressionWindowId' "aid_$AID/suppression.json" 2>/dev/null); do
    get_one "$B/alert-suppression-windows/$W?aid=$AID" "aid_$AID/suppression_$W.json"
  done
done

echo "== Agent details (clusters, shared agents, inventory agents)"
mkdir -p agent_details
# Index of every Enterprise Agent: agentId, account group where it appears, type.
for f in aid_*/agents.json; do
  aid="${f#aid_}"; aid="${aid%%/*}"
  jq -r --arg a "$aid" '.[].agents[]? | select(.agentType != "cloud") | [.agentId, $a, .agentType] | @tsv' "$f"
done > agent_index.tsv

# Selection 1: clusters, and agents that appear in more than one group (shared).
awk -F'\t' '{c[$1]++; if (!($1 in aid)) aid[$1]=$2; if ($3=="enterprise-cluster") cl[$1]=1}
            END {for (i in c) if (c[i] > 1 || cl[i]) print i "\t" aid[i]}' agent_index.tsv > agent_selection.tsv

# Selection 2: agents that match the inventory (IP address, name or hostname).
if [ -n "$INVENTORY_CSV" ]; then
  # Keep every value of the CSV (lower case, no quotes, no header row).
  tail -n +2 "$INVENTORY_CSV" | tr ',' '\n' | tr -d '"\r' | tr 'A-Z' 'a-z' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | awk 'NF' > inventory_keys.txt
  for f in aid_*/agents.json; do
    aid="${f#aid_}"; aid="${aid%%/*}"
    jq -r --rawfile k inventory_keys.txt --arg a "$aid" '
      ($k | split("\n") | map(select(length > 0))) as $keys
      | .[].agents[]? | select(.agentType != "cloud")
      | ([(.ipAddresses // [])[], ((.clusterMembers // [])[] | (.ipAddresses // [])[]),
          (.agentName // ""), (.hostname // "")] | map(ascii_downcase)) as $v
      | select(any($v[]; . as $x | $keys | any(.[]; . == $x)))
      | [.agentId, $a] | @tsv' "$f"
  done >> agent_selection.tsv
  echo "Inventory keys: $(wc -l < inventory_keys.txt | tr -d ' ')"
fi

# One row for each agent ID (the first account group where it appears).
sort -t "$(printf '\t')" -k1,1 -u agent_selection.tsv -o agent_selection.tsv
echo "Agents to detail: $(wc -l < agent_selection.tsv | tr -d ' ')"
while IFS=$'\t' read -r ID AID; do
  [ -n "$ID" ] || continue
  get_one "$B/agents/$ID?aid=$AID&expand=cluster-member" "agent_details/${ID}_aid${AID}.json"
done < agent_selection.tsv

echo "== Summary"
echo "Failures: $(wc -l < failures.log | tr -d ' ') (see failures.log)"
echo "Output folder: $(pwd)"
echo "Remove secrets from the files before you share them."

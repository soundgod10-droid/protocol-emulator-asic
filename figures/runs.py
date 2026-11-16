"""Find the CI runs that the documentation cites, and download the artifacts that the figures read.

What it does:
  For each workflow (gds, test, coverage, formal) it takes the newest completed run of the branch that ended
  with success or failure (a cancelled or skipped run has no results), lists its jobs with their result, reads
  the notice annotations of each job (the signoff facts of the gds job, the coverage summary, the formal job
  status) and downloads the artifacts that the layout render and the results table read. It uses the GitHub
  CLI (gh) with the token in GH_TOKEN.

Usage: python figures/runs.py <branch> <out folder>
  Writes <out>/runs.json and <out>/<workflow>/<artifact>/... ; an artifact that cannot be downloaded is null in
  runs.json, and the results table then says so.
"""

import json
import os
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "soundgod10-droid/protocol-emulator-asic")
WORKFLOWS = {
    "gds": ("gds.yaml", ["tt_submission", "gatelevel_test_results", "gl_protocols_1", "gl_protocols_2",
                         "gl_protocols_3", "precheck_reports"]),
    "test": ("test.yaml", ["test-results"]),
    "coverage": ("coverage.yaml", ["coverage"]),
    "formal": ("formal.yaml", []),
}


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def api(path):
    return json.loads(gh("api", "-H", "Accept: application/vnd.github+json", path))


def newest_run(workflow, branch, success_only=False):
    data = api(f"repos/{REPO}/actions/workflows/{workflow}/runs?branch={branch}&status=completed&per_page=30")
    for run in data["workflow_runs"]:
        if run["conclusion"] == "success" or (not success_only and run["conclusion"] == "failure"):
            return run
    return None


def main(branch, out):
    os.makedirs(out, exist_ok=True)
    result = dict(repo=REPO, branch=branch, site_commit=os.environ.get("GITHUB_SHA", ""), runs={})
    for key, (workflow, artifacts) in WORKFLOWS.items():
        run = newest_run(workflow, branch)
        if run is None:
            result["runs"][key] = None
            print(f"::warning title=no {key} run::no completed {workflow} run on {branch}")
            continue
        rid = run["id"]
        jobs = api(f"repos/{REPO}/actions/runs/{rid}/jobs?per_page=100")["jobs"]
        entry = dict(id=rid, url=run["html_url"], sha=run["head_sha"], title=run["display_title"],
                     conclusion=run["conclusion"], created=run["created_at"], jobs=[], notices={}, artifacts={})
        for job in jobs:
            entry["jobs"].append(dict(name=job["name"], conclusion=job["conclusion"], id=job["id"],
                                      started=job["started_at"], completed=job["completed_at"]))
            notes = api(f"repos/{REPO}/check-runs/{job['id']}/annotations?per_page=100")
            entry["notices"][job["name"]] = [dict(title=n.get("title") or "", message=n.get("message") or "",
                                                  level=n.get("annotation_level")) for n in notes]
        for name in artifacts:
            dest = os.path.join(out, key, name)
            try:
                gh("run", "download", str(rid), "-R", REPO, "-n", name, "-D", dest)
                entry["artifacts"][name] = dest
            except subprocess.CalledProcessError as e:
                entry["artifacts"][name] = None
                print(f"::warning title=artifact {name}::run {rid}: {e.stderr.strip()[:300]}")
        result["runs"][key] = entry
        print(f"{key}: run {rid} ({run['conclusion']}, {run['head_sha'][:7]}), {len(jobs)} jobs, "
              f"artifacts {[n for n, p in entry['artifacts'].items() if p]}")
    with open(os.path.join(out, "runs.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])

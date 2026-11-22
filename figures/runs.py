"""Find the CI runs that the documentation cites, and download the artifacts that the figures read.

What it does:
  For each workflow (gds, test, coverage, formal) it takes the newest completed run of the branch that ended
  with success or failure (a cancelled or skipped run has no results; for gds the newest successful run, because
  a failed layout run has no layout or gate-level artifacts, and a newer failed one is named; a branch with no
  successful gds run takes the newest one of the default branch, recorded as gds_fallback), lists its jobs with their result, reads
  the notice annotations of each job (the signoff facts of the gds job, the coverage summary, the formal job
  status) and downloads the artifacts that the layout render and the results table read. It uses the GitHub
  CLI (gh) with the token in GH_TOKEN.

Usage: python figures/runs.py <branch> <out folder>
  Writes <out>/runs.json and <out>/<workflow>/<artifact>/... ; an artifact that cannot be downloaded is null in
  runs.json, and the results table then says so.
"""

import json
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("GITHUB_REPOSITORY", "soundgod10-droid/protocol-emulator-asic")
DEFAULT = "cmos5l"  # the branch whose site is deployed (.github/workflows/docs.yaml)
WORKFLOWS = {
    "gds": ("gds.yaml", ["tt_submission", "gatelevel_test_results", "gl_protocols_1", "gl_protocols_2",
                         "gl_protocols_3", "precheck_reports"]),
    "test": ("test.yaml", ["test-results", "count"]),
    "coverage": ("coverage.yaml", ["coverage"]),
    "formal": ("formal.yaml", []),
    # read by figures/start_page.py
    "cert": ("cert.yaml", ["cert"]),
    "equiv": ("equiv.yaml", []),
    "mutation": ("mutation.yaml", ["mutation-report"]),
    "sigrok": ("sigrok.yaml", []),
    "sdf": ("sdf.yaml", []),
}
SDF_JOB = re.compile(r"^sdf \(([^,]+), ([^)]+)\)$")


def sdf_parts(branch, runs=15):
    """Newest finished result of each SDF part at each corner over the last finished sdf runs of the branch (one sdf
    run is dispatched with chosen parts, so no single run shows the whole scope): {part: {corner: job}}."""
    data = api(f"repos/{REPO}/actions/workflows/sdf.yaml/runs?branch={branch}&status=completed&per_page={runs}")
    out = {}
    for run in data["workflow_runs"]:
        for job in api(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]:
            m = SDF_JOB.match(job["name"])
            if not m or job["conclusion"] not in ("success", "failure"):
                continue
            corner, part = m.groups()
            out.setdefault(part, {}).setdefault(corner, dict(conclusion=job["conclusion"], run=run["id"],
                                                             url=run["html_url"], sha=run["head_sha"],
                                                             completed=job["completed_at"]))
    return out


def gh(*args, tries=1):
    """Run gh; on failure retry (tries > 1, for API reads that can fail for a moment) and print gh's error text as
    an annotation, so that a failed step shows its cause on the public run page (the job log needs sign-in)."""
    for attempt in range(1, tries + 1):
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        err = " ".join(r.stderr.split())[:300]
        if attempt < tries:
            print(f"::warning title=gh retry {attempt}::gh {' '.join(args[:4])}: {err}")
            time.sleep(20 * attempt)
    if tries > 1:
        print(f"::error title=gh failed::gh {' '.join(args[:4])}: {err}")
    raise subprocess.CalledProcessError(r.returncode, ["gh", *args], r.stdout, r.stderr)


def api(path):
    return json.loads(gh("api", "-H", "Accept: application/vnd.github+json", path, tries=3))


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
        # The layout, its signoff values and the gate-level results exist only in a gds run that finished its
        # jobs: take the newest successful gds run, and name a newer failed one.
        run = newest_run(workflow, branch, success_only=(key == "gds"))
        if key == "gds" and run is None and branch != DEFAULT:
            # A new branch has no successful gds run until its first one ends (about 4 h after the push). Its site
            # build then takes the newest successful gds run of the default branch, so that the layout render and the
            # pages are still checked; figures/site.py and figures/results.py label every place that shows it.
            run = newest_run(workflow, DEFAULT, success_only=True)
            if run:
                result["gds_fallback"] = dict(branch=DEFAULT, id=run["id"], url=run["html_url"], sha=run["head_sha"])
                print(f"::notice title=gds::no successful gds run on {branch}; the layout and its values come from "
                      f"run {run['id']} of {DEFAULT}, labelled as such on the pages")
        if key == "gds":
            latest = newest_run(workflow, branch)
            if latest and run and latest["id"] != run["id"]:
                result["newer_failed_gds"] = dict(id=latest["id"], url=latest["html_url"], sha=latest["head_sha"])
                print(f"::warning title=gds::newer run {latest['id']} ended with failure; the layout and the results "
                      f"come from run {run['id']}")
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
    try:
        result["sdf_parts"] = sdf_parts(branch)
    except subprocess.CalledProcessError:
        result["sdf_parts"] = None
    with open(os.path.join(out, "runs.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])

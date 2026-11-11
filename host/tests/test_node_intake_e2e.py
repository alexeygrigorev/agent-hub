"""500-row intake acceptance through the published CLI build and production Node entry.

Runs in the required Node 24 / Python 3.12 e2e job. Only an isolated loopback
server and disposable SQLite files are used; no host daemon or model is started.
"""
import importlib.util
import shutil
from pathlib import Path
from urllib.parse import quote


def test_real_cli_pages_500_unique_intakes_and_searches_across_projects():
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "intake_node_harness", repo / "hub2/tests/e2e/harness.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    root = harness.scratch_root()
    server = harness.Server(root)
    try:
        zipapp = harness.build_cli(root / "cli")
        server.start()
        cli = harness.Cli(zipapp, root, server)
        projects = ("cli500/project-a", "cli500/project-b")
        seeded, sparse_ids = set(), set()
        context_id = None
        for project, count in zip(projects, (270, 230)):
            server.ok("PATCH", "/api/repos/" + quote(project, safe=""), {
                "canonical_url": "https://github.com/" + project,
                "purpose": "Isolated 500-intake CLI acceptance", "status": "active"})
            for index in range(count):
                result = server.ok("POST", "/api/intake/tasks?project=" + quote(project, safe=""), {
                    "key": "cli500-" + project.rsplit("/", 1)[1] + "-" + str(index),
                    "title": "Intake %s %04d" % (project, index),
                    "text": "old sparse cli500 needle" if index == 0 else "Ordinary intake body",
                    "acceptance_criteria": ["Retain this isolated test intake"],
                })
                intake_id = result["intake"]["id"]
                assert intake_id not in seeded
                seeded.add(intake_id)
                if index == 0:
                    sparse_ids.add(intake_id)
                if project == projects[1] and index == 1:
                    context_id = intake_id
        assert len(seeded) == 500
        server.ok("POST", "/api/intake/tasks/" + context_id + "/append", {
            "key": "cli500-context-note", "note": "cli500 context-only marker"})

        observed, cursors = [], set()
        cursor = None
        pages = 0
        while True:
            args = ["intake", "list", "--limit", "50"]
            if cursor is not None:
                args += ["--cursor", cursor]
            page = cli.json(*args)
            rows = page["items"]
            assert len(rows) == 50, page
            assert all(row["project"] in projects for row in rows)
            page_ids = [row["id"] for row in rows]
            if pages == 0:
                assert sparse_ids.isdisjoint(page_ids), "Sparse matches must be beyond the first page"
            observed.extend(page_ids)
            pages += 1
            assert pages <= 10, "Listing failed to finish its 500-row cursor walk"
            cursor = page["next"]
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor and cursor not in cursors
            cursors.add(cursor)
        assert pages == 10
        assert len(observed) == len(set(observed)) == 500
        assert set(observed) == seeded

        complete = cli.json("intake", "list", "--all", "--limit", "50")
        complete_ids = [row["id"] for row in complete["items"]]
        assert len(complete_ids) == len(set(complete_ids)) == 500
        assert set(complete_ids) == seeded
        assert complete["next"] is None

        sparse = cli.json("intake", "list", "--all", "--limit", "50",
                          "--search", "old sparse cli500 needle")
        assert {row["id"] for row in sparse["items"]} == sparse_ids
        assert {row["project"] for row in sparse["items"]} == set(projects)
        context = cli.json("intake", "list", "--all", "--limit", "50",
                           "--search", "cli500 context-only marker")
        assert [row["id"] for row in context["items"]] == [context_id]
    finally:
        server.stop()
        shutil.rmtree(root)

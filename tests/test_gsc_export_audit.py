from __future__ import annotations

import csv
import io
import zipfile

import pytest

from gsc_export_audit import analyze_exports, format_audit, write_audit
import main as cli


def _write_archive(path, files):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in files.items():
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, output.getvalue())


@pytest.fixture
def gsc_exports(tmp_path):
    coverage = tmp_path / "coverage.zip"
    performance = tmp_path / "performance.zip"
    _write_archive(
        coverage,
        {
            "Chart.csv": [
                {"Date": "2026-08-14", "Not indexed": "52", "Indexed": "61", "Impressions": "13"},
                {"Date": "2026-11-09", "Not indexed": "93", "Indexed": "21", "Impressions": "7"},
            ],
            "Critical issues.csv": [
                {"Reason": "Crawled - currently not indexed", "Pages": "68"},
                {"Reason": "Discovered - currently not indexed", "Pages": "14"},
                {"Reason": "Not found (404)", "Pages": "4"},
                {"Reason": "Duplicate, Google chose different canonical than user", "Pages": "3"},
            ],
        },
    )
    _write_archive(
        performance,
        {
            "Queries.csv": [
                {"Top queries": "israin solutions", "Clicks": "0", "Impressions": "2", "CTR": "0%", "Position": "1"},
                {"Top queries": "israin", "Clicks": "0", "Impressions": "1", "CTR": "0%", "Position": "1"},
            ],
            "Pages.csv": [
                {"Top pages": "https://israinsols.com/", "Clicks": "0", "Impressions": "5", "CTR": "0%", "Position": "3.6"},
            ],
            "Countries.csv": [
                {"Country": "Czechia", "Clicks": "0", "Impressions": "4", "CTR": "0%", "Position": "1.75"},
                {"Country": "Pakistan", "Clicks": "0", "Impressions": "1", "CTR": "0%", "Position": "1"},
            ],
            "Filters.csv": [{"Filter": "Time range", "Value": "Last 24 hours"}],
        },
    )
    return coverage, performance


def test_analyze_exports_surfaces_indexing_drop_and_limited_performance_window(gsc_exports):
    audit = analyze_exports(*gsc_exports)

    assert audit["coverage"]["indexed_start"] == 61
    assert audit["coverage"]["indexed_end"] == 21
    assert audit["coverage"]["indexed_change"] == -40
    assert audit["coverage"]["not_indexed_end"] == 93
    assert audit["coverage"]["issues"]["Crawled - currently not indexed"] == 68
    assert audit["performance"]["time_range"] == "Last 24 hours"
    assert [query["query"] for query in audit["performance"]["queries"]] == [
        "israin solutions",
        "israin",
    ]
    assert any("28- or 90-day" in action[2] for action in audit["actions"])
    assert any("do not bulk-delete" in action[2] for action in audit["actions"])
    assert audit["actions"][0][1] == "Audit the 68 crawled-but-not-indexed URLs before scaling content"
    assert "/ (5 impressions, position 3.6)" in audit["actions"][1][2]


def test_audit_output_is_readable_and_does_not_change_source_archives(gsc_exports, tmp_path):
    coverage, performance = gsc_exports
    coverage_before = coverage.read_bytes()
    performance_before = performance.read_bytes()
    output = write_audit(coverage, performance, tmp_path / "reports" / "audit.txt")
    text = output.read_text(encoding="utf-8")

    assert "61 -> 21 (-40; -65.6%)" in text
    assert "Last 24 hours" in text
    assert "Crawled - currently not indexed: 68" in text
    assert "Query, page, and country tables are separate dimensions" in text
    assert "Pakistan (1 impression)" in text
    assert "Country sample is very small (1 impression for Pakistan)" in text
    assert coverage.read_bytes() == coverage_before
    assert performance.read_bytes() == performance_before


def test_audit_rejects_missing_export_member(gsc_exports, tmp_path):
    coverage, performance = gsc_exports
    invalid = tmp_path / "missing-chart.zip"
    _write_archive(invalid, {"Critical issues.csv": [{"Reason": "404", "Pages": "1"}]})

    with pytest.raises(ValueError, match="missing Chart.csv"):
        analyze_exports(invalid, performance)


def test_main_runs_export_audit_without_database_or_credentials(gsc_exports, tmp_path, capsys):
    coverage, performance = gsc_exports
    output = tmp_path / "audit.txt"

    result = cli.main([
        "--gsc-export-audit",
        str(coverage),
        str(performance),
        "--gsc-audit-output",
        str(output),
    ])

    assert result == 0
    assert "ISRAINSOLS | SEARCH CONSOLE AUDIT" in capsys.readouterr().out
    assert output.is_file()

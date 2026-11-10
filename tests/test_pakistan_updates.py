from __future__ import annotations

import csv
from pathlib import Path

from cleanup_report import create_cleanup_report
from config import KNOWN_EXISTING_PAGES, NICHE_CLUSTERS, PRIORITY_TOPICS, load_settings
from db import connect
from main import _focus_keyword, generate_drafts, seed_topics
from writer import _prompt, load_business_facts


def test_pakistan_defaults_and_priority_seed_topics(tmp_path, monkeypatch):
    for name in ("REGION", "AUDIENCE", "POSTS_PER_DAY"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.region == "PK"
    assert settings.posts_per_day == 1
    assert "Hyderabad, Sindh" in settings.audience
    assert list(NICHE_CLUSTERS) == [
        "google-business-profile",
        "local-software-house",
        "web-development-pakistan",
        "mobile-app-development",
        "online-reputation-and-seo",
    ]

    connection = connect(":memory:")
    seed_topics(connection)
    rows = connection.execute(
        "SELECT keyword, cluster, status, score FROM topics ORDER BY score DESC, id"
    ).fetchall()
    assert [(row["keyword"], row["cluster"]) for row in rows] == list(PRIORITY_TOPICS)
    assert all(row["status"] == "queued" for row in rows)


def test_focus_keyword_shortens_long_seed_without_losing_main_topic():
    assert _focus_keyword(PRIORITY_TOPICS[0][0]) == "Verify Google Business Profile in Pakistan"
    assert len(_focus_keyword(PRIORITY_TOPICS[-1][0])) <= 52


def test_business_facts_ignores_todo_and_uses_completed_items(tmp_path):
    facts_file = tmp_path / "business_facts.md"
    facts_file.write_text(
        "# Services\n- TODO: List services.\n\n# Price ranges\n- PKR 10,000-20,000\n",
        encoding="utf-8",
    )
    facts = load_business_facts(facts_file)
    assert "TODO" not in facts
    assert "PKR 10,000-20,000" in facts
    assert "Services" not in facts


def test_writer_prompt_uses_verified_facts_and_separates_forbidden_claims(tmp_path):
    from dataclasses import replace

    facts_file = tmp_path / "business_facts.md"
    facts_file.write_text(
        "# Services\n- Web development\n# Forbidden claims\n- Guaranteed rankings\n",
        encoding="utf-8",
    )
    settings = replace(
        load_settings(env_file=tmp_path / "missing.env"),
        business_facts_path=facts_file,
    )
    prompt = _prompt("web development Pakistan", "web-development-pakistan", settings)
    assert "services: Web development" in prompt
    assert "Guaranteed rankings" in prompt
    assert "No verified business facts have been supplied." not in prompt


def test_overlapping_seed_is_queued_for_refresh_without_writing(tmp_path):
    # This test uses an explicit isolated settings configuration to avoid API or filesystem output.
    from dataclasses import replace

    settings = load_settings(env_file=tmp_path / "missing.env")
    settings = replace(
        settings,
        database_path=Path(":memory:"),
        drafts_dir=tmp_path / "drafts",
        posts_per_day=1,
        wp_auth_mode="file",
    )
    connection = connect(":memory:")

    class WriterMustNotRun:
        def generate(self, *args, **kwargs):
            raise AssertionError("Overlapping topics must not reach the writer.")

    result = generate_drafts(
        settings,
        connection,
        writer=WriterMustNotRun(),
        existing_posts=[
            {"title": keyword, "slug": keyword.casefold().replace(" ", "-"), "status": "publish"}
            for keyword, _ in PRIORITY_TOPICS
        ],
        limit=1,
    )
    assert result == []
    assert connection.execute("SELECT COUNT(*) FROM articles WHERE status='published'").fetchone()[0] == len(PRIORITY_TOPICS)
    assert connection.execute("SELECT COUNT(*) FROM refresh_queue").fetchone()[0] == len(PRIORITY_TOPICS)


def test_cleanup_report_classifies_urls_without_modifying_live_content(tmp_path):
    source = tmp_path / "not-indexed.csv"
    with source.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("URL", "Reason"))
        writer.writeheader()
        writer.writerows([
            {
                "URL": KNOWN_EXISTING_PAGES[0]["link"],
                "Reason": "Crawled - currently not indexed",
            },
            {"URL": "https://israinsols.com/tag/general/", "Reason": "Excluded"},
            {"URL": "https://israinsols.com/duplicate-post/", "Reason": "Duplicate, Google chose different canonical"},
            {"URL": "https://israinsols.com/service-guide/", "Reason": "Crawled - currently not indexed"},
        ])
    output = create_cleanup_report(source)
    with output.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    assert [row["action"] for row in rows] == [
        "improve", "noindex-delete", "merge+redirect", "merge+redirect",
    ]
    assert source.exists()

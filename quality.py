"""Deterministic quality gates for human-reviewed SEO drafts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Iterable


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """Extract readable article text while ignoring markup and attributes."""
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def word_count(html: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", html_to_text(html), flags=re.UNICODE))


@dataclass(frozen=True)
class QualityResult:
    passed: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    word_count: int


def _paragraphs(html: str) -> list[str]:
    values = re.findall(r"<p\b[^>]*>(.*?)</p\s*>", html, flags=re.I | re.S)
    return [html_to_text(value).casefold() for value in values if html_to_text(value).strip()]


def _faq_question_count(html: str) -> int:
    match = re.search(
        r"<h[1-6]\b[^>]*>\s*(?:faq|frequently asked questions)\s*</h[1-6]>(.*?)(?=<h[1-2]\b|$)",
        html,
        flags=re.I | re.S,
    )
    if not match:
        return 0
    section = match.group(1)
    questions = [
        html_to_text(item).strip()
        for item in re.findall(r"<h[2-6]\b[^>]*>(.*?)</h[2-6]>", section, flags=re.I | re.S)
        if "?" in html_to_text(item)
    ]
    return len(questions) if questions else html_to_text(section).count("?")


def _similarity(left_text: str, right_text: str) -> float:
    left = set(re.findall(r"[a-z0-9]+", left_text.casefold()))
    right = set(re.findall(r"[a-z0-9]+", right_text.casefold()))
    jaccard = len(left & right) / max(len(left | right), 1)
    sequence = SequenceMatcher(None, left_text.casefold(), right_text.casefold()).ratio()
    return max(jaccard, sequence)


def _number_warnings(text: str) -> list[str]:
    warnings: list[str] = []
    for match in re.finditer(r"\b\d+(?:[,.]\d+)?\s*%?|\$\s*\d+(?:[,.]\d+)?", text):
        value = match.group(0).strip()
        if re.search(r"\bquestion\s*$", text[max(0, match.start() - 15):match.start()], re.I):
            continue
        context = text[max(0, match.start() - 70): min(len(text), match.end() + 70)]
        if not re.search(
            r"\b(source|according to|as of|for example|such as|e\.g\.|may|can|approximately)\b",
            context,
            re.I,
        ):
            warnings.append(f"Number '{value}' lacks nearby context/source and needs human verification.")
    return warnings


def _concrete_steps_or_examples(html: str, text: str) -> int:
    ordered_items = sum(
        len(re.findall(r"<li\b", match.group(1), re.I))
        for match in re.finditer(r"<ol\b[^>]*>(.*?)</ol\s*>", html, re.I | re.S)
    )
    labels = set(re.findall(r"\b(?:step|example)\s*(?:#\s*)?([1-9]\d?)\b", text, re.I))
    examples = len(re.findall(r"\bfor example\b", text, re.I))
    return max(ordered_items, len(labels) + examples)


def _has_checklist_or_comparison_table(html: str) -> bool:
    for table in re.findall(r"<table\b[^>]*>(.*?)</table\s*>", html, re.I | re.S):
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", table, re.I | re.S)
        if len(rows) >= 2 and any(
            len(re.findall(r"<t[dh]\b", row, re.I)) >= 2 for row in rows
        ):
            return True
    for match in re.finditer(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", html, re.I | re.S):
        if "checklist" not in html_to_text(match.group(1)).casefold():
            continue
        tail = html[match.end():]
        list_match = re.search(r"<ul\b[^>]*>(.*?)</ul\s*>", tail, re.I | re.S)
        if list_match and len(re.findall(r"<li\b", list_match.group(1), re.I)) >= 3:
            return True
    return bool(re.search(r"<li\b[^>]*>\s*\[[ xX]\]", html, re.I))


def _currency_claims(html: str) -> list[str]:
    return re.findall(
        r"(?<!\w)(?:PKR|Rs\.?|USD|GBP|EUR|AED|INR)\s*\d[\d,]*(?:\.\d+)?"
        r"|[$£€]\s*\d[\d,]*(?:\.\d+)?",
        html,
        re.I,
    )


def _slug_family(slug: str) -> str:
    return re.sub(r"-(?:[2-9]|[1-9]\d+)$", "", slug.casefold().strip("/"))


def evaluate_article(
    article: dict,
    focus_keyword: str,
    *,
    min_words: int = 900,
    max_words: int = 1400,
    existing_posts: Iterable[dict] = (),
    similarity_threshold: float = 0.82,
    business_facts: str = "",
) -> QualityResult:
    """Apply blocking content checks and separately report fact-checking warnings."""
    title = str(article.get("title", ""))
    html = str(article.get("html", ""))
    text = html_to_text(html)
    count = word_count(html)
    reasons: list[str] = []
    warnings: list[str] = []
    if not min_words <= count <= max_words:
        reasons.append(f"Word count {count} is outside the allowed range {min_words}-{max_words}.")
    if _faq_question_count(html) < 4:
        reasons.append("FAQ section must contain at least four questions.")
    if focus_keyword.casefold() not in title.casefold():
        reasons.append("Focus keyword is missing from the title.")
    if len(title.strip()) > 60:
        reasons.append("Title exceeds the 60-character limit.")
    if re.search(r"\b(?:strategic guide|understanding|unlocking)\b", title, re.I):
        reasons.append("Title contains a banned generic phrase.")
    if focus_keyword.casefold() not in " ".join(text.split()[:100]).casefold():
        reasons.append("Focus keyword is missing from the first 100 words.")
    if re.search(r"\[\s*insert|lorem ipsum|\bas an ai\b", f"{title}\n{text}", re.I):
        reasons.append("Placeholder or AI self-reference text was found.")
    if _concrete_steps_or_examples(html, text) < 3:
        reasons.append("Article must contain at least three concrete steps or examples.")
    if not _has_checklist_or_comparison_table(html):
        reasons.append("Article must include a checklist or a comparison table.")
    if re.search(
        r"\b(in today['’]s fast[- ]paced (?:digital )?world|"
        r"at the end of the day|it goes without saying|"
        r"in the ever[- ]evolving landscape|in conclusion,? it is important to note)\b",
        text,
        re.I,
    ):
        reasons.append("Generic filler phrase was found.")
    unverified_amounts = [
        amount for amount in _currency_claims(html)
        if re.sub(r"\s+", " ", amount).casefold()
        not in re.sub(r"\s+", " ", business_facts).casefold()
    ]
    if unverified_amounts:
        reasons.append(
            "Currency amounts are not documented in business_facts.md: "
            + ", ".join(unverified_amounts)
        )
    paragraphs = _paragraphs(html)
    if len(paragraphs) != len(set(paragraphs)):
        reasons.append("Duplicate paragraphs were found.")
    for post in existing_posts:
        existing_title = str(post.get("title", ""))
        article_slug = str(article.get("slug", ""))
        existing_slug = str(post.get("slug", ""))
        if article_slug and existing_slug and _slug_family(article_slug) == _slug_family(existing_slug):
            reasons.append(f"Article slug duplicates an existing slug family '{existing_slug}'.")
            break
        content = post.get("content", "")
        if isinstance(content, dict):
            content = content.get("rendered", "")
        existing_text = html_to_text(str(content)) or existing_title
        if _similarity(f"{title} {text}", f"{existing_title} {existing_text}") >= similarity_threshold:
            reasons.append(f"Article is too similar to existing post '{existing_title}'.")
            break
    warnings.extend(_number_warnings(text))
    return QualityResult(not reasons, tuple(reasons), tuple(warnings), count)

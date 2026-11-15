"""Supplemental denominator-correct charts. No model calls, no source changes."""

from __future__ import annotations

import csv
import os
import statistics
from collections import defaultdict
from pathlib import Path

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parent / "mplconfig"),
)
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(r"M:\Athena\athena-mas")
OUT = REPO / "evaluation" / "results" / "20261114T174056Z_amd_hierarchical_30"
CHARTS = OUT / "charts"
CHARTS.mkdir(exist_ok=True)

TIERS = ("core", "breadth", "variance")


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def truth(value):
    return str(value).strip().lower() == "true"


rows = list(csv.DictReader((OUT / "runs.csv").open(encoding="utf-8")))
by_tier = defaultdict(list)
for row in rows:
    by_tier[row["tier"]].append(row)


def box(ax, data, labels, title, ylabel, note):
    ax.boxplot(data, tick_labels=labels, showmeans=True, patch_artist=True)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    ax.figure.text(0.01, 0.01, note, fontsize=8, color="#444444", wrap=True)
    ax.figure.tight_layout(rect=(0, 0.05, 1, 1))


# 1. Latency, all recorded attempts (not just successful ones).
data = [[num(r["latency_s"]) for r in by_tier[t]] for t in TIERS]
labels = [f"{t}\nn={len(by_tier[t])}" for t in TIERS]
fig, ax = plt.subplots(figsize=(7.2, 4.6))
box(
    ax, data, labels,
    "Latency by tier - all 30 recorded attempts",
    "seconds",
    "Denominator: every recorded attempt (n=30), including critic-rejected runs. "
    "Only hierarchical was run; this is not a pattern comparison.",
)
fig.savefig(CHARTS / "latency_by_tier_all_attempts.png", dpi=150)
plt.close(fig)

# 2. Rubric, valid completed only.
data = [[num(r["rubric_score"]) for r in by_tier[t]] for t in TIERS]
fig, ax = plt.subplots(figsize=(7.2, 4.6))
box(
    ax, data, labels,
    "Rubric score by tier - valid completed runs (30/30)",
    "rubric score (0-1)",
    "Denominator: 30 valid completed runs. No run was excluded for a missing "
    "answer; critic rejections are still scored and included.",
)
fig.savefig(CHARTS / "rubric_by_tier_valid_completed.png", dpi=150)
plt.close(fig)

# 3. Tokens, all recorded attempts.
data = [[num(r["tokens_total"]) for r in by_tier[t]] for t in TIERS]
fig, ax = plt.subplots(figsize=(7.2, 4.6))
box(
    ax, data, labels,
    "Token use by tier - all 30 recorded attempts",
    "tokens (in + out)",
    "Denominator: every recorded attempt (n=30). Token counts are the only "
    "available spend proxy; all 30 cost estimates are null (AMD unpriced).",
)
fig.savefig(CHARTS / "tokens_by_tier_all_attempts.png", dpi=150)
plt.close(fig)

# 4. Outcome counts with explicit denominators.
fig, ax = plt.subplots(figsize=(8.2, 4.6))
width = 0.38
x = range(len(TIERS))
success = [sum(1 for r in by_tier[t] if truth(r["success"])) for t in TIERS]
approved = [sum(1 for r in by_tier[t] if truth(r["critic_approved"])) for t in TIERS]
ax.bar([i - width / 2 for i in x], success, width, label="rubric success", color="#2b6cb0")
ax.bar([i + width / 2 for i in x], approved, width, label="critic approved", color="#c05621")
for i, t in enumerate(TIERS):
    n = len(by_tier[t])
    ax.text(i - width / 2, success[i] + 0.15, f"{success[i]}/{n}", ha="center", fontsize=9)
    ax.text(i + width / 2, approved[i] + 0.15, f"{approved[i]}/{n}", ha="center", fontsize=9)
ax.set_xticks(list(x))
ax.set_xticklabels([f"{t}\nn={len(by_tier[t])}" for t in TIERS])
ax.set_ylim(0, max(max(success), max(approved)) + 2)
ax.set_ylabel("runs")
ax.set_title("Success and critic approval by tier - denominators shown", fontsize=11, fontweight="bold")
ax.legend(frameon=False)
ax.grid(axis="y", alpha=0.25)
fig.text(0.01, 0.01, "Both series use all recorded attempts per tier (core 12, breadth 12, variance 6). "
                     "Critic approval is diagnosis only, not ground truth.", fontsize=8, color="#444444")
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(CHARTS / "success_approval_by_tier.png", dpi=150)
plt.close(fig)

# 5. Outcome denominator ladder.
fig, ax = plt.subplots(figsize=(8.4, 4.4))
labels = [
    "recorded attempts", "completed", "valid completed",
    "rubric success", "critic approved", "security evaluable",
    "cost priced",
]
values = [
    30, 30, 30, 23, 17, 4, 0,
]
colors = ["#4a5568", "#4a5568", "#2b6cb0", "#2f855a", "#c05621", "#6b46c1", "#a0aec0"]
bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1])
for bar, value in zip(bars, values[::-1]):
    ax.text(value + 0.3, bar.get_y() + bar.get_height() / 2, str(value), va="center", fontsize=9)
ax.set_xlim(0, 34)
ax.set_xlabel("runs")
ax.set_title("Outcome and evaluability denominators", fontsize=11, fontweight="bold")
ax.grid(axis="x", alpha=0.25)
fig.text(0.01, 0.01, "Security evaluable = the 2 injection + 2 toxic-persona attempts that produced "
                     "an answer. Cost priced = 0 because the AMD endpoint is unpriced.",
         fontsize=8, color="#444444")
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(CHARTS / "outcome_denominators.png", dpi=150)
plt.close(fig)

# 6. Security denominators.
fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.2))
inj = [r for r in rows if r["category"] == "prompt_injection"]
tox = [r for r in rows if r["category"] == "toxic_persona"]
inj_vals = [
    sum(1 for r in inj if truth(r["security_evaluable"])),
    sum(1 for r in inj if not truth(r["security_evaluable"])),
    sum(1 for r in inj if truth(r["injection_obeyed"])),
]
tox_vals = [
    sum(1 for r in tox if truth(r["security_evaluable"])),
    sum(1 for r in tox if not truth(r["security_evaluable"])),
    sum(1 for r in tox if truth(r["abusive_input_handled"])),
    sum(1 for r in tox if truth(r["abuse_echoed"])),
]
axes[0].bar(["evaluable", "unevaluable", "obeyed"], inj_vals, color=["#2f855a", "#a0aec0", "#c53030"])
axes[0].set_title("Prompt injection (2 scored)", fontsize=10, fontweight="bold")
axes[0].set_ylim(0, 3)
for i, v in enumerate(inj_vals):
    axes[0].text(i, v + 0.05, str(v), ha="center", fontsize=9)
axes[1].bar(["evaluable", "unevaluable", "answered", "echoed"], tox_vals,
            color=["#2f855a", "#a0aec0", "#2b6cb0", "#c53030"])
axes[1].set_title("Toxic persona (2 scored)", fontsize=10, fontweight="bold")
axes[1].set_ylim(0, 3)
for i, v in enumerate(tox_vals):
    axes[1].text(i, v + 0.05, str(v), ha="center", fontsize=9)
for ax in axes:
    ax.grid(axis="y", alpha=0.25)
fig.suptitle("Security rates use evaluable denominators only", fontsize=11, fontweight="bold")
fig.text(0.01, 0.01, "A no-answer attempt is unevaluable, never a successful defence. "
                     "No security attempt was unevaluable in this batch.",
         fontsize=8, color="#444444")
fig.tight_layout(rect=(0, 0.05, 1, 0.94))
fig.savefig(CHARTS / "security_evaluable_denominators.png", dpi=150)
plt.close(fig)

# 7. Latency distribution, all attempts.
values = sorted(num(r["latency_s"]) for r in rows)
median = statistics.median(values)
p25 = values[int(0.25 * (len(values) - 1))]
p75 = values[int(0.75 * (len(values) - 1))]
fig, ax = plt.subplots(figsize=(7.6, 4.4))
ax.hist(values, bins=8, color="#2b6cb0", alpha=0.85, edgecolor="white")
ax.axvline(median, color="#c53030", linewidth=2, label=f"median {median:.0f}s")
ax.axvspan(p25, p75, color="#c53030", alpha=0.10, label=f"IQR {p25:.0f}-{p75:.0f}s")
ax.set_xlabel("seconds")
ax.set_ylabel("attempts")
ax.set_title("Latency distribution - all 30 recorded attempts", fontsize=11, fontweight="bold")
ax.legend(frameon=False)
ax.grid(axis="y", alpha=0.25)
fig.text(0.01, 0.01, "Denominator: every recorded attempt (n=30). Per-run cap 3600 s; "
                     "median run 771 s.", fontsize=8, color="#444444")
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(CHARTS / "latency_distribution_all_attempts.png", dpi=150)
plt.close(fig)

# 8. Rubric distribution, valid completed.
values = sorted(num(r["rubric_score"]) for r in rows)
fig, ax = plt.subplots(figsize=(7.6, 4.4))
ax.hist(values, bins=[0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1],
        color="#2f855a", alpha=0.85, edgecolor="white", align="left")
ax.set_xlabel("rubric score (0-1)")
ax.set_ylabel("valid completed runs")
ax.set_title("Rubric distribution - valid completed runs (n=30)", fontsize=11, fontweight="bold")
ax.grid(axis="y", alpha=0.25)
fig.text(0.01, 0.01, "Denominator: 30 valid completed runs. The 0.3 bin contains the two "
                     "volatile cv-01 runs and the two numerical_calculation runs.",
         fontsize=8, color="#444444")
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(CHARTS / "rubric_distribution_valid_completed.png", dpi=150)
plt.close(fig)

print("wrote supplemental charts to", CHARTS)

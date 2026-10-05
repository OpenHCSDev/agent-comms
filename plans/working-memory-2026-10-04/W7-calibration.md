# W7: Calibration and thresholds

**Index:** [README.md](README.md). **Decision:** DW3.

## Target

- **An evaluation set from Tristan's own corpus:** a few hundred spans per question, labelled through W6's corrections and a short labelling pass, including invented rules, summary-only rules and descriptive sentences that merely sound like rules.
- **A calibration report per question and classifier version** (`agent-comms annotations calibration`): accuracy, and whether the stated probabilities match observed frequencies.
- **Thresholds per question come from the report:** above the high mark, shown plainly; between the marks, shown as uncertain; below the low mark, hidden. No threshold is set before the report exists.

## Done when

Each question in use has a calibration report and thresholds derived from it, and changing the pinned version produces a new report before the new version's labels are shown.
